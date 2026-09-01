"""
discover.py — Creator Discovery Pipeline, Stages 0 through 8.

Self-contained: everything this script needs lives in this one file. Run
only via GitHub Actions (the "Creator Discovery Pipeline" workflow) —
reads all inputs from environment variables, no CLI args, no .env file.

What it does, in order:
  0. Read inputs (niche, brand, location, platform, gender, limits,
     optional competitor brands + fit-score weight overrides)
  0.5 Optional: fetch + summarize the brand's website (Tavily + Sonnet 5)
  1. Expand the niche into search terms, hashtags, and creator archetypes,
     plus location variants (Sonnet 5)
  2. Discover candidate creators via Serper.dev — four passes: niche terms,
     creator archetypes, hashtags, and (if given) competitor-mention search
  3. Drop anything already known (dedup check #1, before spending API credits)
  2.6 Optional PRIMARY discovery via Gemini web search (GEMINI_WEB_DISCOVERY,
     auto-on when GEMINI_API_KEY is set). Gemini runs before Serper and acts
     as the intelligent research engine; Serper supplements with any handles
     Gemini didn't surface.  Model: configured via GEMINI_MODEL repo Variable
     (default gemini-3.6-flash) — update the Variable when Google retires a model.
  4. Enrich each candidate: licensed provider (if configured) -> official
     Business Discovery API (Instagram only, if configured) -> Serper
     snippet parsing (always available fallback)
  5. Filter by follower range and posting activity
  6. Classify: niche/audience/location match, content angle, brand
     affinity note, gender, city/country (Haiku 4.5)
  6.5 Detect partnership signals (deterministic regex on bio) and compute
      Overall Fit as a weighted sum (not an LLM guess — so per-brand
      weight overrides actually get respected)
  7. Final dedup check
  8. Write to Google Sheets: Master tab, the sector-specific tab, Run Log

Stages 9 (shortlist.py) and 10 (dm_drafting.py) are separate scripts with
their own GitHub Actions workflows — human-gated, not run automatically.
"""
import json
import os
import re
import time
from datetime import datetime, timezone

import anthropic
import gspread
import requests
from google.oauth2.service_account import Credentials

# ============================================================
# CONSTANTS
# ============================================================

SONNET_MODEL = "claude-sonnet-4-6"
HAIKU_MODEL = "claude-haiku-4-5-20251001"

# Gemini — used as PRIMARY discovery engine and/or Meta verification fallback
# when GEMINI_WEB_DISCOVERY or GEMINI_VERIFICATION_FALLBACK is enabled.
# Model is configurable via the GEMINI_MODEL repo Variable (Settings → Secrets
# and variables → Actions → Variables tab) so future model retirements don't
# require modifying this source file — just update the Variable and rerun.
# Default: gemini-3.6-flash (confirmed working; Google Search grounding supported).
# Free tier: check aistudio.google.com/rate-limit for current RPM/RPD limits.
# If Google retires the active model, the 404 handler prints the exact replacement.
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-3.6-flash").strip() or "gemini-3.6-flash"
GEMINI_API_BASE = "https://generativelanguage.googleapis.com/v1beta/models"
# Gemini is the PRIMARY intelligent discovery engine for this pipeline.
# Target 30-50 candidates per run so the downstream verification/scoring
# pipeline has a large enough pool to filter from — Gemini decides who is
# worth researching, NOT who is "best". Serper then supplements with
# additional handles Gemini didn't surface.
GEMINI_WEB_DISCOVERY_MAX_RESULTS = 40

SHEETS_SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]

MASTER_HEADERS = [
    "dedup_key", "platform", "profile_link", "username",
    # Campaign is part of a creator's identity for review/outreach purposes,
    # not just a label: the same account can legitimately be a fit for two
    # different brands, with independent scores, review decisions, and
    # outreach state for each. See load_master_index()/write_batch() —
    # existing-row lookup is keyed on (dedup_key, Campaign), not dedup_key
    # alone, specifically so this is possible without one campaign's
    # decision overwriting another's.
    "Campaign",
    "Niche 1", "Niche 2", "Niche 3",
    "city", "country", "location_verified", "gender_inferred", "gender_confidence",
    "account_type", "account_type_confidence",
    "product_fit_score", "content_opportunity_score", "creator_quality_score",
    "niche_match", "audience_match", "location_match",
    "content_angle_strength", "partnership_signal_score", "overall_fit", "fit_explanation",
    "content_angle", "brand_affinity_note", "partnership_signal_matched", "competitor_affinity",
    # Blank unless SONNET_REFINEMENT=true — populated only for the bounded pool
    # of near-finalists that got the optional second, stronger-model pass.
    # Deliberately one column, not a full discovery-score-vs-outreach-readiness
    # split system a fuller version of this idea could become.
    "outreach_readiness",
    "total_posts", "followers_count", "follower_verification", "follower_source",
    "engagement_rate", "posting_frequency", "audience_quality_score",
    "last_post_date", "activity_status",
    "contact_email", "contact_phone", "contact_source",
    "data_source", "data_confidence",
    "matched_query", "matched_hashtag", "matched_archetype", "matched_lane", "discovery_method",
    "date_added",
    # review_status replaces the old "Shortlisted" Y/blank column: blank
    # (not yet reviewed) / Approved / Rejected. The old column conflated
    # "not reviewed yet" and "reviewed, didn't want it" into the same blank
    # value, which a review-status field with three real states doesn't.
    "review_status",
    # outreach_channel answers a different question than review_status:
    # WHERE should an approved creator go, not whether they were approved.
    # blank (not yet decided) / email / dm / none (approved, but no outreach
    # planned). Only ever meaningful once review_status == "Approved".
    "outreach_channel",
    # Set by the bridge script (not by discover.py itself) once a row with
    # outreach_channel == "email" has actually been pushed into the
    # matching outreach campaign's Master Sheet as a lead — blank / pushed /
    # failed. Exists so "not pushed yet" and "tried to push, failed" are
    # distinguishable, instead of inferring "pushed" from a side effect.
    "campaign_push_status",
]
NICHE_COLS = ["Niche 1", "Niche 2", "Niche 3"]
SECTOR_HEADERS = [h for h in MASTER_HEADERS if h not in NICHE_COLS]

# Candidates that get excluded before scoring (wrong account type) or held back
# for manual review (follower count couldn't be verified against a hard minimum)
# land here instead of silently vanishing, so you can see WHY something didn't
# make the Master tab.
EXCLUDED_HEADERS = SECTOR_HEADERS + ["rejection_reason"]

RUN_LOG_HEADERS = [
    "run_date", "campaign", "brand_name", "brand_website", "niche_input",
    "expanded_terms", "expanded_hashtags", "expanded_archetypes", "search_lanes", "exclusion_signals",
    "location_input", "gender_filter", "competitor_brands_input",
    "fit_weights_used", "search_budget_used", "llm_candidate_limit_used",
    "min_followers_used", "max_followers_used", "creator_size_tier_used",
    "unknown_followers_policy_used", "min_overall_fit_used",
    "total_found", "total_already_known", "total_rejected_account_type", "total_rejected_location",
    "total_needs_follower_verification", "total_hard_follower_reject", "total_activity_rejected",
    "total_deterministically_excluded", "total_llm_budget_cutoff", "llm_candidates_classified",
    "total_llm_parse_failed", "total_gender_rejected", "total_below_min_fit",
    "total_below_min_content_opportunity", "total_after_filters",
    # Which enrichment tier actually supplied the follower count, broken down by
    # platform. deep_research columns show how many counts came from the DR
    # report vs. live sources — previously these were untallied and the Run Log
    # silently under-reported unverified counts for DR runs.
    # deep_research_report_used / total_deep_research_discovered were already
    # computed in the log dict but missing from this header list, so they were
    # never written to the sheet — fixed here.
    "instagram_verified_via_business_api", "instagram_verified_via_serper",
    "instagram_verified_via_tavily", "instagram_via_deep_research", "instagram_unverified",
    "tiktok_verified_via_serper", "tiktok_verified_via_tavily",
    "tiktok_via_deep_research", "tiktok_unverified",
    "sonnet_refinement_used", "total_sonnet_refined", "total_sonnet_parse_failed",
    "total_sonnet_downgraded_below_threshold",
    "claude_web_discovery_used", "total_claude_web_discovered",
    "gemini_web_discovery_used", "total_gemini_web_discovered",
    "gemini_verification_fallback_used", "total_gemini_verified",
    "deep_research_report_used", "total_deep_research_discovered",
]

# Weighting reflects "could this product naturally appear here" (product_fit,
# content_opportunity) ahead of raw niche-keyword match. niche_match and
# content_angle_strength are still computed and shown on every row for
# transparency, but no longer drive the score by default — a creator scoring
# high on literal niche match but low on plausible product integration (or
# vice versa, e.g. a fitness creator with no "loungewear" keyword overlap but
# an obvious post-workout/recovery moment for the product) was exactly the
# gap the old niche-dominant weighting couldn't capture. Override any of
# these with WEIGHT_* env vars, including the legacy WEIGHT_NICHE /
# WEIGHT_CONTENT_ANGLE if you want those dimensions back in the mix.
DEFAULT_WEIGHTS = {
    "product_fit_score": 0.30, "audience_match": 0.25, "content_opportunity_score": 0.20,
    "creator_quality_score": 0.10, "partnership_signal_score": 0.10, "location_match": 0.05,
}

PARTNERSHIP_PATTERNS = [
    r"\bambassador\b", r"\bpartner(ed)?\s+with\b", r"\baffiliate\b",
    r"\buse\s+code\b", r"\bdiscount\s+code\b", r"\bcode:?\s*[A-Z0-9]{3,}\b",
    r"#ad\b", r"#sponsored\b", r"\bsponsored\s+by\b", r"\bgifted\s+by\b",
    r"\bcollab(oration)?\s+with\b", r"\bteaming\s+up\s+with\b",
    r"\bltk\.\w+\b", r"shareasale\.com", r"linktr\.ee",
]
STRONG_PARTNERSHIP_PATTERNS = {
    r"\bambassador\b", r"\bpartner(ed)?\s+with\b", r"\baffiliate\b",
    r"\buse\s+code\b", r"\bdiscount\s+code\b", r"#ad\b", r"#sponsored\b",
    r"\bsponsored\s+by\b", r"\bgifted\s+by\b",
}

COMPETITOR_SEARCH_KEYWORDS = ["ambassador", "partner", "gifted", "ad"]

BIO_LINK_DOMAINS = [
    r"linktr\.ee", r"beacons\.ai", r"msha\.ke", r"campsite\.bio",
    r"linkin\.bio", r"lnk\.bio", r"milkshake\.app", r"stan\.store",
]
URL_RE = re.compile(r"https?://[^\s,]+")
EMAIL_RE = re.compile(r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}")
PHONE_RE = re.compile(r"(\+?\d{1,3}[-.\s]?)?\(?\d{3,4}\)?[-.\s]?\d{3,4}[-.\s]?\d{3,4}")
BUSINESS_CONTEXT_WORDS = [
    "business", "collab", "collaboration", "inquir", "management",
    "manager", "booking", "contact", "brand", "partnership", "pr@",
]

PLATFORM_DOMAINS = {"instagram": "instagram.com", "tiktok": "tiktok.com"}

MAX_SHEETS_RETRIES = 5

# Cost and quality guardrails.  SEARCH_BUDGET is a run-wide limit (not a
# per-platform limit); the LLM cap keeps a weak search result from turning into
# hundreds of candidate-level Claude calls.
MAX_SEARCH_BUDGET = 400
MAX_LLM_CANDIDATES = 60
# Sonnet refinement pool: hard ceiling independent of RESULT_LIMIT, same
# guardrail pattern as MAX_LLM_CANDIDATES above — bounds the extra cost to a
# fixed, predictable number of Sonnet calls per run regardless of how large
# RESULT_LIMIT or SEARCH_BUDGET are set.
MAX_SONNET_REFINEMENT_POOL = 20

# These are deliberately broad, high-confidence signals only.  They prevent
# obvious storefronts, repost repositories, and unrelated accounts from
# consuming an enrichment or Claude call.  Ambiguous candidates stay in the
# pool for the model to assess.
OBVIOUS_NON_CREATOR_PATTERNS = [
    r"\bonline (clothing )?boutique\b", r"\bshop (our|now|the)\b", r"\bretailer\b",
    r"\bwholesale\b", r"\bshipping worldwide\b", r"\b(tiktok|instagram) shop\b",
    r"\boriginal sound\b", r"\bsound (repository|archive)\b", r"\bmemorial\b",
    r"\bmissing person\b", r"\btrue crime\b", r"\bunsolved crime\b",
]


# ============================================================
# STAGE 0 — INTAKE
# ============================================================

def get_config() -> dict:
    def env(key, default=""):
        return os.environ.get(key, default) or default

    # CAMPAIGN is the routing key used by every downstream stage (shortlist
    # sync, DM drafting, the email bridge, Asana) — deliberately a separate
    # field from BRAND_NAME rather than reusing it, since brand_name is
    # free text fed to prompts (can be a display name, phrasing chosen for
    # tone) while campaign must match, character for character, an existing
    # templates/<Campaign>/ folder the moment outreach_channel is "email".
    # In practice these are usually the same string — set them identically
    # unless you have a specific reason to run two differently-named
    # campaigns against one brand (e.g. a relaunch push tracked separately
    # from the original campaign).
    campaign = env("CAMPAIGN")
    niche = env("NICHE")
    brand_name = env("BRAND_NAME")
    location = env("LOCATION")
    platform = env("PLATFORM")
    target_gender = env("TARGET_GENDER")

    missing = [k for k, v in
               {"CAMPAIGN": campaign, "NICHE": niche, "BRAND_NAME": brand_name, "LOCATION": location,
                "PLATFORM": platform, "TARGET_GENDER": target_gender}.items() if not v]
    if missing:
        raise ValueError(
            f"Missing required input(s): {', '.join(missing)}. Check they were "
            f"filled in when you triggered the 'Creator Discovery Pipeline' workflow."
        )
    if platform not in ("instagram", "tiktok", "both"):
        raise ValueError("PLATFORM must be 'instagram', 'tiktok', or 'both'")
    if target_gender not in ("male", "female", "both"):
        raise ValueError("TARGET_GENDER must be 'male', 'female', or 'both'")

    min_f_raw = env("MIN_FOLLOWERS", "0")
    max_f_raw = env("MAX_FOLLOWERS", "0")

    # CREATOR_SIZE_TIER — named alternative to typing raw follower numbers.
    # Be clear about what this does and doesn't do: it's convenience (map a
    # name to a range) plus one genuine functional addition (see
    # size_bias_terms below) — it does NOT fix follower-verification
    # reliability. A "large" tier candidate whose follower count Serper/Tavily
    # can't confirm still lands in Excluded under needs_verification, same as
    # today. The actual fix for that is a licensed provider or Business
    # Discovery API; a tier name doesn't change what data is available.
    SIZE_TIERS = {
        "emerging": (1_000, 10_000),
        "mid": (10_000, 50_000),
        "large": (50_000, 250_000),
        "mega": (250_000, None),
    }
    size_tier = env("CREATOR_SIZE_TIER").strip().lower()
    size_bias_terms = []
    if size_tier:
        if size_tier not in SIZE_TIERS:
            raise ValueError(f"CREATOR_SIZE_TIER must be one of {list(SIZE_TIERS)}, got {size_tier!r}")
        tier_min, tier_max = SIZE_TIERS[size_tier]
        if min_f_raw not in ("", "0") or max_f_raw not in ("", "0"):
            print(f"[config] CREATOR_SIZE_TIER={size_tier!r} is set — ignoring MIN_FOLLOWERS/MAX_FOLLOWERS "
                  f"input to avoid an ambiguous combination. Using the tier's range instead.")
        min_f_raw, max_f_raw = str(tier_min), str(tier_max or "0")
        # The one part of "tier" that's more than a MIN_FOLLOWERS alias:
        # roundup/"best of" style phrasing tends to surface pages about
        # creators who've already been written up elsewhere, which skews
        # larger than a raw archetype/hashtag search. Soft bias, not a
        # guarantee — added as extra search terms, not a replacement for
        # real verification.
        if size_tier in ("large", "mega"):
            size_bias_terms = [f"top {niche} creators", f"best {niche} influencers"]

    weights = dict(DEFAULT_WEIGHTS)
    weight_env_map = {
        "product_fit_score": "WEIGHT_PRODUCT_FIT", "audience_match": "WEIGHT_AUDIENCE",
        "content_opportunity_score": "WEIGHT_CONTENT_OPPORTUNITY", "creator_quality_score": "WEIGHT_CREATOR_QUALITY",
        "location_match": "WEIGHT_LOCATION", "partnership_signal_score": "WEIGHT_PARTNERSHIP",
        # Legacy dimensions — off by default (see DEFAULT_WEIGHTS comment), but
        # still settable if you want niche-keyword match or content-angle
        # strength back in the weighted sum.
        "niche_match": "WEIGHT_NICHE", "content_angle_strength": "WEIGHT_CONTENT_ANGLE",
    }
    # Individual WEIGHT_* env vars — supported for local/API runs where the
    # 25-input workflow_dispatch cap doesn't apply.
    for key, env_key in weight_env_map.items():
        v = env(env_key)
        if v:
            weights[key] = float(v)

    # FIT_WEIGHT_OVERRIDES — one free-text field ("product_fit=0.4,audience=0.2"),
    # this is what the GitHub Actions form actually uses, since 8 separate
    # weight_* inputs pushed discover.yml over GitHub's 25-input cap on
    # workflow_dispatch (that's a hard platform limit, not a preference).
    # Short aliases here so the form field stays typeable; applied last, so
    # this takes precedence over the individual WEIGHT_* vars above if both
    # happen to be set.
    weight_alias_map = {
        "product_fit": "product_fit_score", "audience": "audience_match",
        "content_opportunity": "content_opportunity_score", "creator_quality": "creator_quality_score",
        "partnership": "partnership_signal_score", "location": "location_match",
        "niche": "niche_match", "content_angle": "content_angle_strength",
    }
    overrides_raw = env("FIT_WEIGHT_OVERRIDES")
    if overrides_raw:
        for pair in overrides_raw.split(","):
            pair = pair.strip()
            if not pair or "=" not in pair:
                continue
            k, v = pair.split("=", 1)
            k = k.strip().lower()
            internal_key = weight_alias_map.get(k, k)  # allow full internal names too
            if internal_key not in weight_env_map:
                print(f"[config] Ignoring unknown FIT_WEIGHT_OVERRIDES key: {k!r}")
                continue
            try:
                weights[internal_key] = float(v.strip())
            except ValueError:
                print(f"[config] Ignoring unparseable FIT_WEIGHT_OVERRIDES entry: {pair!r}")

    competitor_raw = env("COMPETITOR_BRANDS")
    competitor_brands = [c.strip() for c in competitor_raw.split(",") if c.strip()] if competitor_raw else []

    result_limit = int(env("RESULT_LIMIT", "100"))
    if result_limit < 1 or result_limit > MAX_LLM_CANDIDATES:
        raise ValueError(f"RESULT_LIMIT must be between 1 and {MAX_LLM_CANDIDATES} in cost-controlled mode")
    # How many RAW candidates to search for and enrich before filtering/scoring/
    # ranking down to result_limit. This used to just BE result_limit, which is
    # the bug that made "give me 5" mean "stop after the first 5 URLs found" —
    # often before the archetype/hashtag passes even ran. Now it means "search
    # widely, then give me the best 5 that survive filtering." Override with
    min_followers = parse_follower_count(min_f_raw) if min_f_raw and min_f_raw != "0" else 0
    max_followers = parse_follower_count(max_f_raw) if max_f_raw and max_f_raw != "0" else 0
    if min_followers and max_followers and min_followers > max_followers:
        raise ValueError("MIN_FOLLOWERS cannot be greater than MAX_FOLLOWERS")

    # SEARCH_BUDGET if you want to search even wider; capped at 400 to bound
    # Serper/Claude spend on a single run.
    #
    # The base multiplier (result_limit * 8) was set before real production
    # data existed on what a MIN/MAX_FOLLOWERS range actually costs in
    # survivors. Two separate real runs with a ~200K-wide range both showed
    # ~90-93% of raw candidates rejected at the follower-range check alone
    # (37/40 and 49/60) — a hard, reproducible number, not a guess. When a
    # follower range is configured, the default budget scales up accordingly
    # so a narrow range doesn't also mean "and search way less than you
    # asked for" by default. An explicit SEARCH_BUDGET env var still always
    # wins over this calculation either way.
    if min_followers or max_followers:
        default_search_budget = min(max(result_limit * 20, 80), MAX_SEARCH_BUDGET)
    else:
        default_search_budget = min(max(result_limit * 8, 30), MAX_SEARCH_BUDGET)
    search_budget = int(env("SEARCH_BUDGET", str(default_search_budget)))
    if search_budget < 1:
        raise ValueError("SEARCH_BUDGET must be at least 1")
    if search_budget > MAX_SEARCH_BUDGET:
        print(f"[config] SEARCH_BUDGET={search_budget} capped at {MAX_SEARCH_BUDGET} for cost control")
        search_budget = MAX_SEARCH_BUDGET

    llm_candidate_limit = int(env("LLM_CANDIDATE_LIMIT", str(min(max(result_limit * 4, 12), MAX_LLM_CANDIDATES))))
    if llm_candidate_limit < result_limit:
        raise ValueError("LLM_CANDIDATE_LIMIT cannot be smaller than RESULT_LIMIT")
    if llm_candidate_limit > MAX_LLM_CANDIDATES:
        print(f"[config] LLM_CANDIDATE_LIMIT={llm_candidate_limit} capped at {MAX_LLM_CANDIDATES}")
        llm_candidate_limit = MAX_LLM_CANDIDATES

    min_overall_fit = float(env("MIN_OVERALL_FIT", "5.0"))
    if not 0 <= min_overall_fit <= 10:
        raise ValueError("MIN_OVERALL_FIT must be between 0 and 10")
    unknown_followers_policy = env("UNKNOWN_FOLLOWERS_POLICY", "needs_verification")
    if unknown_followers_policy not in ("needs_verification", "include"):
        raise ValueError("UNKNOWN_FOLLOWERS_POLICY must be 'needs_verification' or 'include'")

    if any(v < 0 for v in weights.values()) or not any(v > 0 for v in weights.values()):
        raise ValueError("Fit weights must be non-negative and include at least one positive value")

    # Optional but high-leverage: a typed brand/product brief. This does NOT
    # replace BRAND_WEBSITE (which gets scraped) — it's merged with it, and
    # matters most when there's no website or the website is thin. Feeds
    # search-term/archetype generation, classification, and account-type
    # judgment, not just the final fit_explanation.
    exclude_raw = env("EXCLUDE")
    exclude_terms = [t.strip() for t in exclude_raw.split(",") if t.strip()] if exclude_raw else []

    # SEARCH_VOCABULARY — your own search terms/hashtags/archetypes, typed
    # directly instead of having Claude invent them each run. GitHub Actions'
    # web form renders this as a SINGLE-LINE text box, so the format uses ";"
    # to separate categories rather than newlines (a newline-based format
    # would be unusable through the actual "Run workflow" UI). Format:
    #   category: comma, separated, values; category: more, values
    # Recognized categories: terms, hashtags, archetypes. Any category you
    # omit still falls back to Claude-generated expansion for just that
    # category — this isn't all-or-nothing. Example:
    #   terms: towel robe, hoodie robe; hashtags: #dadlife, #mensfashion; archetypes: fitness dad, top dad influencers USA
    # A line like "top dad influencers USA" under archetypes is a legitimate
    # way to bias discovery toward more established creators — Serper can't
    # filter or sort by follower count, but roundup/"top X" style phrasing
    # tends to surface pages about creators who've already been written up
    # elsewhere, which skews larger than a raw hashtag search. It's a soft
    # bias, not a guarantee — it doesn't replace real follower verification.
    search_vocab_raw = env("SEARCH_VOCABULARY")
    manual_terms, manual_hashtags, manual_archetypes = [], [], []
    if search_vocab_raw:
        for segment in search_vocab_raw.split(";"):
            segment = segment.strip()
            if not segment or ":" not in segment:
                continue
            category, values = segment.split(":", 1)
            category = category.strip().lower()
            parsed_values = [v.strip() for v in values.split(",") if v.strip()]
            if category in ("term", "terms"):
                manual_terms.extend(parsed_values)
            elif category in ("hashtag", "hashtags"):
                manual_hashtags.extend(v if v.startswith("#") else f"#{v}" for v in parsed_values)
            elif category in ("archetype", "archetypes"):
                manual_archetypes.extend(parsed_values)
            else:
                print(f"[config] SEARCH_VOCABULARY: unrecognized category {category!r}, ignoring segment")

    # Gemini auto-enable: both discovery and verification default to True when
    # GEMINI_API_KEY is set, because the architecture is designed Gemini-first.
    # Explicit GEMINI_WEB_DISCOVERY=false / GEMINI_VERIFICATION_FALLBACK=false
    # overrides even when the key is present.
    _gemini_key_present = bool(os.environ.get("GEMINI_API_KEY", "").strip())

    return {
        "campaign": campaign,
        "niche": niche, "brand_name": brand_name, "brand_website": env("BRAND_WEBSITE"),
        "brand_brief": env("BRAND_BRIEF"), "target_buyer": env("TARGET_BUYER"),
        "use_cases": env("USE_CASES"), "creator_types": env("CREATOR_TYPES"), "exclude": env("EXCLUDE"),
        "exclude_terms": exclude_terms,
        "manual_terms": manual_terms, "manual_hashtags": manual_hashtags, "manual_archetypes": manual_archetypes,
        "creator_size_tier": size_tier, "size_bias_terms": size_bias_terms,
        "location": location, "platform": platform, "target_gender": target_gender,
        "result_limit": result_limit, "search_budget": search_budget,
        "llm_candidate_limit": llm_candidate_limit,
        "min_followers": min_followers, "max_followers": max_followers,
        "min_followers_raw": env("MIN_FOLLOWERS", ""),   # raw string for Gemini prompt
        "max_followers_raw": env("MAX_FOLLOWERS", ""),   # raw string for Gemini prompt
        # If a hard MIN_FOLLOWERS is set and a candidate's follower count can't
        # be verified, default behavior is to hold it out of Master for manual
        # review rather than silently letting it through. Set to "include" to
        # restore the old (permissive) behavior.
        "unknown_followers_policy": unknown_followers_policy,
        "allow_business_accounts": env("ALLOW_BUSINESS_ACCOUNTS", "false").strip().lower() == "true",
        "activity_cutoff_days": int(env("ACTIVITY_CUTOFF_DAYS", "30")),
        "require_activity_verified": env("REQUIRE_ACTIVITY_VERIFIED", "false").strip().lower() == "true",
        # Off by default — nothing changes cost/behavior-wise unless explicitly
        # opted into. When true, the top candidates competing for the final
        # RESULT_LIMIT slots (not every classified candidate — that would
        # multiply cost by the LLM candidate limit, not just RESULT_LIMIT) get
        # a second, more critical pass from Sonnet before final ranking. This
        # targets the specific gap where Haiku's first pass calls something a
        # "plausible fit" based on category/bio, but a closer read of the same
        # evidence (recent captions, when available) would catch that the
        # actual content doesn't support the claim as strongly as the score
        # implies.
        "sonnet_refinement": env("SONNET_REFINEMENT", "false").strip().lower() == "true",
        # Off by default, same as SONNET_REFINEMENT. discover.yml is already
        # at GitHub's 25-input cap for workflow_dispatch, so this is
        # deliberately NOT a per-run form field — set it as a hardcoded value
        # in discover.yml's env: block, or as a repo/environment "Variable"
        # (${{ vars.CLAUDE_WEB_DISCOVERY }}), not a Secret, since it's not
        # sensitive. This is a "set occasionally" toggle, not a per-campaign
        # one, so it not being on the run-trigger form is the right call
        # rather than spending one of the last input slots on it.
        "claude_web_discovery": env("CLAUDE_WEB_DISCOVERY", "false").strip().lower() == "true",
        "claude_web_discovery_max_candidates": int(env("CLAUDE_WEB_DISCOVERY_MAX_CANDIDATES", "20")),
        # Gemini — PRIMARY intelligent discovery engine + verification fallback.
        # Auto-enabled when GEMINI_API_KEY is set (see _gemini_key_present above).
        # Set GEMINI_WEB_DISCOVERY=false or GEMINI_VERIFICATION_FALLBACK=false
        # explicitly to disable either feature even if the key is present.
        # GEMINI_API_KEY is a Secret; the two flags are repo Variables (not Secrets).
        "gemini_web_discovery": env("GEMINI_WEB_DISCOVERY",
                                     "true" if _gemini_key_present else "false").strip().lower() == "true",
        "gemini_web_discovery_max_candidates": int(env("GEMINI_WEB_DISCOVERY_MAX_CANDIDATES",
                                                        str(GEMINI_WEB_DISCOVERY_MAX_RESULTS))),
        "gemini_verification_fallback": env("GEMINI_VERIFICATION_FALLBACK",
                                             "true" if _gemini_key_present else "false").strip().lower() == "true",
        "min_overall_fit": min_overall_fit,
        # Off by default (0 = no gate), same "repo Variable, not a form field"
        # reasoning as CLAUDE_WEB_DISCOVERY above — the 25-input cap leaves no
        # room, and this is a tuning knob, not a per-campaign setting. Added
        # because MIN_OVERALL_FIT alone let a candidate with content_
        # opportunity_score=1 (essentially "we have no evidence this creator
        # ever shows relevant content") still reach Master at an acceptable
        # aggregate score — high audience_match/creator_quality masked a
        # near-zero score on the single dimension that matters most for
        # "will this actually produce a usable post." A separate floor on
        # that one dimension catches what an aggregate threshold can't.
        "min_content_opportunity": int(env("MIN_CONTENT_OPPORTUNITY", "0")),
        "competitor_brands": competitor_brands,
        "fit_weights": weights,
        "platforms": ["instagram", "tiktok"] if platform == "both" else [platform],
    }


def parse_follower_count(text: str) -> int:
    multipliers = {
        "crore": 10_000_000, "cr": 10_000_000,
        "lakh": 100_000, "lac": 100_000, "l": 100_000,
        "k": 1_000, "thousand": 1_000,
        "m": 1_000_000, "million": 1_000_000,
    }
    text = str(text).strip().lower().replace(",", "").replace(" ", "")
    match = re.match(r"([\d.]+)([a-z]*)", text)
    if not match:
        raise ValueError(f"Couldn't parse follower count: {text!r}")
    number_str, unit = match.groups()
    number = float(number_str)
    if unit in multipliers:
        return int(number * multipliers[unit])
    if unit == "":
        return int(number)
    raise ValueError(f"Unrecognized unit {unit!r} in follower count: {text!r}")


# ============================================================
# SHARED HELPERS: Claude text extraction, Sheets backoff
# ============================================================

def extract_claude_text(response) -> str:
    """
    Scans content blocks for the text one instead of assuming content[0] is
    always text — extended thinking returns a 'thinking' block first on
    supported models, and content[0].text crashes when that happens.
    """
    for block in response.content:
        if getattr(block, "type", None) == "text":
            return block.text
    block_types = [getattr(b, "type", "unknown") for b in response.content]
    raise RuntimeError(f"No text block in Claude response — got: {block_types}")


def with_backoff(fn, *args, **kwargs):
    last_error = None
    for attempt in range(MAX_SHEETS_RETRIES):
        try:
            return fn(*args, **kwargs)
        except gspread.exceptions.APIError as e:
            last_error = e
            wait = 2 ** attempt
            print(f"[sheets] API error ({e}), retrying in {wait}s...")
            time.sleep(wait)
    raise RuntimeError(f"Exceeded {MAX_SHEETS_RETRIES} retries writing to Google Sheets") from last_error


def strip_json_fences(text: str) -> str:
    text = text.strip()
    # Strip opening fence (```json or ```) with any trailing whitespace/newline
    text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.IGNORECASE).strip()
    # Strip closing fence (```) with any leading whitespace
    text = re.sub(r"\s*```\s*$", "", text).strip()
    return text


def extract_json_object(text: str):
    """
    Extract the first valid JSON object from `text`, tolerating trailing
    prose/commentary after it. This is the actual failure mode observed in
    production: despite every prompt saying "return ONLY valid JSON, no
    preamble, no markdown fences," Haiku sometimes still appends explanatory
    text after a perfectly valid JSON block (e.g. "**Key reasoning:** ...").
    A strict json.loads() on the full string fails with "Extra data" even
    though the JSON itself parsed fine up to that point — the classification
    wasn't actually broken, the parser just refused anything but an exact
    match.

    json.JSONDecoder().raw_decode() parses a value starting at position 0 and
    returns where it ended, so trailing text after that point is simply
    ignored rather than failing the whole parse. Leading fences are still
    stripped first (raw_decode can't skip leading non-JSON text on its own).

    Raises json.JSONDecodeError if no valid JSON object exists at the start
    of the (fence-stripped) text at all — same exception type json.loads
    would raise, so every existing except json.JSONDecodeError handler
    keeps working unchanged.

    Also tolerates leading preamble before the JSON starts (e.g. "Here's
    what I found:\n{...}"), by locating the first { or [ and parsing from
    there — needed for the web-search discovery prompt below, where Claude's
    response can have commentary before the final structured answer, but a
    safe generalization for every caller.
    """
    cleaned = strip_json_fences(text)
    starts = [i for i in (cleaned.find("{"), cleaned.find("[")) if i != -1]
    if starts:
        cleaned = cleaned[min(starts):]
    obj, _end_index = json.JSONDecoder().raw_decode(cleaned)
    return obj


def repair_truncated_json_array(text: str) -> list:
    """
    Recovers a partial JSON array when the model's response was cut off
    mid-stream by a max_tokens limit, leaving the array unclosed.

    Strategy: find the last complete `}` in the (fence-stripped, array-started)
    text, slice there, strip any trailing comma, and close the array with `]`.
    This preserves every fully-written object while discarding the incomplete
    one that was cut off.

    Returns the recovered list (may be shorter than what was requested).
    Raises json.JSONDecodeError when not even one complete object can be found.
    """
    cleaned = strip_json_fences(text)
    starts = [i for i in (cleaned.find("{"), cleaned.find("[")) if i != -1]
    if starts:
        cleaned = cleaned[min(starts):]

    last_brace = cleaned.rfind("}")
    if last_brace == -1:
        raise json.JSONDecodeError("No complete JSON objects found in truncated response", cleaned, 0)

    candidate = cleaned[: last_brace + 1].rstrip().rstrip(",") + "\n]"
    return json.loads(candidate)


# ============================================================
# TAVILY — page fetching (website context + bio-link pages)
# ============================================================

def tavily_extract(url: str, timeout: int = 15) -> str:
    """
    Fetches and extracts clean text from a URL via Tavily's Extract API.
    Used instead of raw requests+regex HTML-stripping — Tavily handles
    messier/JS-rendered pages far more reliably than a hand-rolled tag
    stripper. Returns '' on any failure rather than raising, since a
    failed fetch here should never crash the whole run.
    """
    api_key = os.environ.get("TAVILY_API_KEY")
    if not api_key:
        return ""
    try:
        resp = requests.post(
            "https://api.tavily.com/extract",
            json={"urls": [url], "api_key": api_key, "extract_depth": "basic", "format": "text"},
            timeout=timeout,
        )
        resp.raise_for_status()
    except requests.RequestException as e:
        print(f"[tavily] extract failed for {url}: {e}")
        return ""

    data = resp.json()
    results = data.get("results", [])
    if not results:
        return ""
    content = results[0].get("raw_content") or results[0].get("content") or ""
    return content[:8000]


def find_bio_link_url(bio_text: str) -> str:
    if not bio_text:
        return ""
    for match in URL_RE.findall(bio_text):
        for domain_pattern in BIO_LINK_DOMAINS:
            if re.search(domain_pattern, match, re.IGNORECASE):
                return match
    return ""


def fetch_bio_link_text(bio_text: str) -> str:
    url = find_bio_link_url(bio_text)
    if not url:
        return ""
    return tavily_extract(url)


# ============================================================
# STAGE 0.5 — WEBSITE CONTEXT (optional)
# ============================================================

def summarize_brand_context(website_url: str) -> dict:
    if not website_url:
        return {}

    page_text = tavily_extract(website_url if website_url.startswith("http") else f"https://{website_url}")
    if not page_text:
        print(f"[website_context] Couldn't fetch usable content from {website_url}")
        return {}

    prompt = f"""Summarize this brand's website content for use in influencer outreach targeting.

Website text:
{page_text}

Return ONLY valid JSON, no preamble, no markdown fences:
{{
  "product_summary": "1-2 sentences on what they sell and the core hook",
  "brand_tone": "3-5 words describing voice/tone",
  "audience_signals": "who the customer is, in a phrase"
}}"""

    client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    response = client.messages.create(
        model=SONNET_MODEL, max_tokens=1024,
        messages=[{"role": "user", "content": prompt}],
    )
    text = extract_claude_text(response)
    try:
        return extract_json_object(text)
    except json.JSONDecodeError:
        print(f"[website_context] Failed to parse Claude JSON: {text}")
        return {}


def build_brand_context(cfg: dict, scraped: dict) -> dict:
    """
    Merges the (optional) website scrape with the (optional) typed brief
    fields into one brand_context dict used everywhere downstream: search
    expansion, classification, DM drafting. Typed fields win when both are
    present, since they're a direct human statement of intent rather than an
    LLM's guess at a webpage. Either source alone is enough to work with —
    neither is required, but giving neither means discovery falls back to
    near-bare keyword search on `niche`, which is the weakest mode.
    """
    ctx = dict(scraped) if scraped else {}
    if cfg.get("brand_brief"):
        ctx["product_summary"] = cfg["brand_brief"]
    ctx["target_buyer"] = cfg.get("target_buyer", "")
    ctx["use_cases"] = cfg.get("use_cases", "")
    ctx["creator_types"] = cfg.get("creator_types", "")
    ctx["exclude"] = cfg.get("exclude", "")
    return ctx


# ============================================================
# STAGE 1 — NICHE, HASHTAG & ARCHETYPE EXPANSION
# ============================================================

def expand_niche_and_location(niche: str, location: str, brand_context: dict) -> dict:
    context_lines = []
    if brand_context.get("product_summary"):
        context_lines.append(f"- Product / brand brief: {brand_context['product_summary']}")
    if brand_context.get("brand_tone"):
        context_lines.append(f"- Brand tone: {brand_context['brand_tone']}")
    if brand_context.get("audience_signals"):
        context_lines.append(f"- Audience signals (from site): {brand_context['audience_signals']}")
    if brand_context.get("target_buyer"):
        context_lines.append(f"- Target buyer: {brand_context['target_buyer']}")
    if brand_context.get("use_cases"):
        context_lines.append(f"- Product use cases / moments it fits into: {brand_context['use_cases']}")
    if brand_context.get("creator_types"):
        context_lines.append(f"- Creator types the human researcher already has in mind: {brand_context['creator_types']}")
    if brand_context.get("exclude"):
        context_lines.append(f"- Explicitly exclude: {brand_context['exclude']}")
    context_block = ("\nBrand context:\n" + "\n".join(context_lines) + "\n") if context_lines else ""

    prompt = f"""Expand a niche + location into a DISCOVERY STRATEGY for finding Instagram/TikTok
creators for influencer outreach — not just a flat keyword list.

Niche: {niche}
Location: {location}
{context_block}
Return ONLY valid JSON, no preamble, no markdown fences:
{{
  "niche_variants": ["term1", "term2", "..."],
  "hashtags": ["#tag1", "#tag2", "..."],
  "search_lanes": [
    {{"lane": "<short name, e.g. 'Fitness / Recovery'>",
      "archetypes": ["<content persona 1>", "<content persona 2>", "..."],
      "priority": <1-3, 3 = most likely to find strong candidates>}}
  ],
  "location_variants": ["variant1", "variant2", "..."],
  "exclusion_signals": ["signal1", "signal2", "..."]
}}

niche_variants: 6-8 related sub-niches and synonyms. Keep this the SMALLEST list —
  plain keyword search is the weakest discovery mode and should not dominate results.

search_lanes: 4-7 DISTINCT discovery lanes, each covering a different angle on where this
  product could plausibly show up — not just the literal niche, but ADJACENT categories
  grounded in the product's real use cases and buyer, when given above. A creator doesn't
  need a stated niche that matches the product category; they need content that creates a
  natural moment for it. For a men's loungewear/comfort brand aimed at active men, useful
  lanes might be: "Men's Lifestyle", "Fitness / Recovery", "Dad / Family", "Men's Grooming",
  "Beach / Pool / Travel" — each with its OWN 3-5 archetypes (e.g. the Fitness/Recovery lane
  might contain "fitness dad", "post-workout recovery creator", "sauna session creator").
  Each lane gets searched with its own quota, so a lane with a niche, specific archetype
  list ISN'T starved by a more generic lane exhausting the search budget first — this is
  the primary discovery mechanism, invest the most effort here.

hashtags: 8-12 hashtags actually used on these platforms for this niche/these lanes.

location_variants: 3-6 (city, state/region, common local hashtag forms).

exclusion_signals: 5-10 short phrases that indicate an account is the WRONG kind of
  result for creator outreach even if it matches a niche or lane keyword — e.g. brand/store/
  retailer/reseller accounts, wrong sub-category, wrong audience gender for this
  product, inactive/abandoned accounts. Ground these in the "Explicitly exclude" line
  above if one was given, plus your own judgment of what would slip through a keyword
  match but isn't a real creator fit."""

    client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    response = client.messages.create(
        model=SONNET_MODEL, max_tokens=3072,
        messages=[{"role": "user", "content": prompt}],
    )
    text = extract_claude_text(response)
    try:
        result = extract_json_object(text)
    except json.JSONDecodeError:
        print(f"[niche_expansion] Failed to parse Claude JSON: {text}")
        result = {"niche_variants": [niche], "hashtags": [], "search_lanes": [],
                  "location_variants": [location], "exclusion_signals": []}
    result.setdefault("search_lanes", [])
    result.setdefault("exclusion_signals", [])
    # Defensive normalization — a malformed lane (missing archetypes, bad priority)
    # shouldn't crash discovery, just get dropped or defaulted rather than raising.
    clean_lanes = []
    for lane in result["search_lanes"]:
        if not isinstance(lane, dict) or not lane.get("archetypes"):
            continue
        clean_lanes.append({
            "lane": str(lane.get("lane", "Unnamed lane")),
            "archetypes": [str(a) for a in lane["archetypes"] if a],
            "priority": lane.get("priority") if lane.get("priority") in (1, 2, 3) else 2,
        })
    result["search_lanes"] = clean_lanes
    return result


# ============================================================
# STAGE 2 — DISCOVERY (Serper.dev, 4 passes)
# ============================================================

def serper_search(query: str, api_key: str, num: int = 20) -> list:
    if not api_key:
        print("[serper] SERPER_API_KEY not set — skipping discovery")
        return []
    try:
        resp = requests.post(
            "https://google.serper.dev/search",
            json={"q": query, "num": min(num, 20)},
            headers={"X-API-KEY": api_key, "Content-Type": "application/json"},
            timeout=15,
        )
        resp.raise_for_status()
    except requests.RequestException as e:
        print(f"[serper] discovery request failed for {query!r}: {e}")
        return []
    return resp.json().get("organic", [])


def extract_handle_from_url(url: str, platform: str) -> str:
    domain = PLATFORM_DOMAINS.get(platform, "")
    match = re.search(rf"{re.escape(domain)}/@?([A-Za-z0-9._]+)", url)
    if not match:
        return ""
    handle = match.group(1)
    if handle.lower() in {"explore", "reel", "reels", "tags", "p", "tag", "hashtag", "video"}:
        return ""
    return handle


def canonical_profile_link(handle: str, platform: str) -> str:
    if platform == "tiktok":
        return f"https://www.tiktok.com/@{handle}"
    return f"https://www.instagram.com/{handle}/"


def serper_discover(term: str, location: str, platform: str, api_key: str) -> list:
    """
    Query construction matters a lot here. The previous version wrapped the
    ENTIRE "{term} {location}" string in one exact-phrase quote, which meant
    Google would only return a hit if that literal combined string appeared
    verbatim on the page. That's a reasonable bar for a hashtag ("#dadlife
    USA" plausibly appears as running text near a bio's location line) but a
    near-impossible one for a natural-language archetype phrase ("fitness dad
    USA" essentially never appears as one literal string anywhere) — which is
    exactly why lane/archetype search was silently returning ~0 results while
    hashtag search dominated every candidate that reached Master or Excluded.

    Fix: quote only the term if it's a multi-word phrase (keeps "fitness dad"
    together as a concept), leave a single-word term/hashtag unquoted, and
    always leave location unquoted so it's a separate AND'd signal rather
    than part of one exact phrase requirement.
    """
    domain = PLATFORM_DOMAINS.get(platform)
    term = term.strip()
    needs_quotes = " " in term and not term.startswith("#")
    term_clause = f'"{term}"' if needs_quotes else term
    full_query = f"site:{domain} {term_clause} {location}".strip()

    results = serper_search(full_query, api_key)
    candidates = []
    for r in results:
        link = r.get("link", "")
        handle = extract_handle_from_url(link, platform)
        if handle:
            candidates.append({
                "handle": handle,
                "profile_link": canonical_profile_link(handle, platform),
                "matched_query": full_query,
                "snippet": r.get("snippet", ""),
            })
    return candidates


def discover_candidates(platform, niche_variants, search_lanes, hashtags,
                         competitor_brands, location_variants, search_budget, serper_key,
                         pre_seen_handles: set = None) -> list:
    """
    SUPPLEMENTARY discovery via Serper keyword search.

    When Gemini is enabled (GEMINI_WEB_DISCOVERY=true), Gemini runs first as
    the primary intelligent discovery engine, and discover_candidates() is
    called with pre_seen_handles set to Gemini's already-found handles.
    Serper then spends its entire budget on GENUINELY NEW candidates that
    Gemini didn't surface — no budget wasted re-discovering the same handles.

    When Gemini is not enabled, pre_seen_handles is empty and discover_candidates
    behaves exactly as before (primary broad discovery via keyword search).

    Search order: lanes (quota-guaranteed) -> niche terms -> hashtags -> competitor
    mentions. Lanes are the discovery mechanism most likely to surface actual
    creators rather than keyword-matched brand/store pages.

    `search_budget` is a RAW candidate cap for this discovery stage only — it's
    deliberately much larger than the final result count the person asked for,
    because most raw candidates get eliminated by account-type/follower/fit
    filtering downstream.

    Each lane gets its OWN sub-budget instead of all lanes sharing one pool in
    list order. Without this, a generic high-volume lane searched first can
    exhaust the whole budget before a narrower, more relevant lane ever runs —
    which is exactly what happened when archetypes were one flat list.
    Lane sub-budgets are weighted by priority (1-3) so a lane the model flagged
    as more promising gets proportionally more searches, but every lane with
    priority >= 1 is guaranteed at least a handful of searches.
    """
    candidates = []
    # Pre-populate seen_handles with Gemini's already-discovered handles so
    # Serper's budget is spent finding genuinely new candidates only.
    seen_handles = set(pre_seen_handles) if pre_seen_handles else set()

    def run_pass(terms, tag_field, discovery_method, local_cap=None, lane_label=""):
        start_count = len(seen_handles)
        cap = search_budget if local_cap is None else min(search_budget, start_count + local_cap)
        for term in terms:
            for loc in location_variants:
                if len(seen_handles) >= cap:
                    return
                results = serper_discover(term, loc, platform, serper_key)
                for r in results:
                    if r["handle"] in seen_handles:
                        continue
                    seen_handles.add(r["handle"])
                    r["matched_hashtag"] = ""
                    r["matched_archetype"] = ""
                    r["matched_lane"] = lane_label
                    r["competitor_affinity"] = ""
                    if tag_field:
                        r[tag_field] = term
                    r["discovery_method"] = discovery_method
                    candidates.append(r)
                    if len(seen_handles) >= cap:
                        break

    # Lane budget: reserve ~65% of the total search budget for lane-driven
    # (archetype) discovery, split across lanes weighted by priority. Niche
    # terms and hashtags share the rest, since they're the weaker signal.
    if search_lanes:
        lane_pool = int(search_budget * 0.65)
        total_priority = sum(l["priority"] for l in search_lanes) or 1
        for lane in sorted(search_lanes, key=lambda l: -l["priority"]):
            if len(seen_handles) >= search_budget:
                break
            lane_budget = max(4, int(lane_pool * (lane["priority"] / total_priority)))
            run_pass(lane["archetypes"], "matched_archetype", "serper_archetype",
                     local_cap=lane_budget, lane_label=lane["lane"])

    # BUG FIX: previously both passes ran with local_cap=None, meaning cap equalled
    # the full search_budget.  When lane discovery underperformed (few archetype
    # results), niche_variants alone could consume ALL remaining budget with
    # weak-signal keyword-match accounts — 86% of hard follower-rejects in the
    # run that exposed this traced back to this exact pass.  Fix: cap each pass to
    # at most 20% of the total search_budget (roughly "the unused 35% minus a
    # cushion for competitor searches"), so neither can crowd out the other or
    # mop up everything the lane stage didn't use.
    secondary_pool = search_budget - len(seen_handles)
    secondary_pool = min(secondary_pool, int(search_budget * 0.35))
    niche_pool = max(2, int(secondary_pool * 0.40))
    hashtag_pool = max(2, int(secondary_pool * 0.60))
    run_pass(niche_variants, None, "serper_search", local_cap=niche_pool)
    run_pass(hashtags, "matched_hashtag", "serper_hashtag", local_cap=hashtag_pool)

    if competitor_brands:
        competitor_terms = [f"{b} {kw}" for b in competitor_brands for kw in COMPETITOR_SEARCH_KEYWORDS]
        for term in competitor_terms:
            for loc in location_variants:
                if len(seen_handles) >= search_budget:
                    break
                results = serper_discover(term, loc, platform, serper_key)
                for r in results:
                    if r["handle"] in seen_handles:
                        continue
                    seen_handles.add(r["handle"])
                    r["matched_hashtag"] = ""
                    r["matched_archetype"] = ""
                    r["matched_lane"] = ""
                    matched_brand = next((b for b in competitor_brands if term.startswith(b)), term)
                    r["competitor_affinity"] = matched_brand
                    r["discovery_method"] = "serper_competitor"
                    candidates.append(r)

    return candidates


# ============================================================
# STAGE 2.5 — CLAUDE WEB DISCOVERY (optional, CLAUDE_WEB_DISCOVERY=true)
# ============================================================

# Bounds how many searches Claude can run within ONE discovery call (not
# per-candidate — this whole function is called once per run, not once per
# platform), so enabling this has a predictable, fixed cost ceiling
# regardless of SEARCH_BUDGET or how broad the campaign is.
CLAUDE_WEB_DISCOVERY_MAX_SEARCHES = 8


def claude_web_discover_candidates(niche: str, brand_context: dict, location: str, platform: str,
                                    target_gender: str, max_candidates: int = 20) -> list:
    """
    Optional ADDITIONAL discovery channel, merged into the same candidate
    pool as Serper's results and run through the identical downstream
    enrichment/verification/classification/refinement pipeline — not a
    replacement for Serper, and not a shortcut around any of the
    verification work already built.

    Why this exists: Serper's discovery is fundamentally keyword-driven
    (site:instagram.com "fitness dad" USA-style queries) — it finds pages
    where that literal phrase appears, not people who are semantically a
    good fit. Claude's web search can instead reason about the actual brief
    (product, buyer, use cases) and search for things like "top {niche}
    creators" roundups, directories, or brand-collab announcements that name
    real people a keyword search on the niche term alone would miss —
    closer to what manual research (the Gemini Deep Research comparison)
    does.

    The one hard rule this function exists to enforce: Claude is a finder
    here, never a judge. It is explicitly told not to report follower
    counts or verification status, and even if it did, nothing downstream
    reads those fields from this function's output — every candidate still
    goes through enrich_instagram/enrich_tiktok -> classify_creator ->
    Sonnet refinement exactly like a Serper-discovered one, with zero
    special-casing. If Claude's search turns out to produce a wrong or
    made-up handle, the verification pipeline treats it exactly like any
    other candidate that doesn't check out: hard-rejected on confirmed
    follower count, account-type gate, or MIN_OVERALL_FIT, same as always.
    """
    platforms_desc = "Instagram and TikTok" if platform == "both" else platform.capitalize()
    gender_desc = f"primarily {target_gender}" if target_gender != "both" else "any gender"

    prompt = f"""Find real, currently-active {platforms_desc} creator accounts that could plausibly be a
strong influencer-marketing fit for this brand. Use web search to find ACTUAL, NAMED creators — search
for things like "top {niche} creators", "best {niche} influencers {location}", roundup/listicle articles,
creator directories, podcast guest lists, or brand collaboration announcements that name real, specific
people. You're trying to surface people a human doing manual research would find by reading around the
topic, not just repeating a keyword search a search engine already covers directly.

Niche: {niche}
Location: {location}
Target creator gender: {gender_desc}
Brand: {brand_context.get('product_summary', 'N/A')}
Target buyer: {brand_context.get('target_buyer', 'N/A')}
Use cases / moments the product fits into: {brand_context.get('use_cases', 'N/A')}
Creator types already identified as relevant: {brand_context.get('creator_types', 'N/A')}

For each real creator you find with an identifiable Instagram or TikTok handle, note the handle and
platform. Do NOT report or estimate their follower count, location, or verification status — you don't
have reliable access to real-time data on that, and it will be independently verified afterward from
scratch. Just identify WHO they are and WHY they came up.

After searching, respond with ONLY a JSON array as the very last thing in your response, no other text
after it, in this exact shape:
[
  {{"handle": "<handle, no @ symbol>", "platform": "instagram|tiktok", "rationale": "<short phrase for why they came up>"}}
]
Include up to {max_candidates} candidates. If you found fewer real, specific creators than that, return
fewer — don't pad the list with guesses or invented handles."""

    client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    try:
        response = client.messages.create(
            model=SONNET_MODEL, max_tokens=4096,
            tools=[{"type": "web_search_20250305", "name": "web_search",
                    "max_uses": CLAUDE_WEB_DISCOVERY_MAX_SEARCHES}],
            messages=[{"role": "user", "content": prompt}],
        )
    except anthropic.APIError as e:
        print(f"[claude_web_discovery] API call failed: {e} — skipping this discovery channel for this run.")
        return []

    # A web-search-enabled response interleaves text with search tool-use
    # blocks — the structured JSON answer is instructed to be the LAST text
    # block, not necessarily the only one (extract_claude_text grabs the
    # FIRST text block, which would be pre-search commentary here instead).
    text_blocks = [b.text for b in response.content if getattr(b, "type", None) == "text"]
    if not text_blocks:
        print("[claude_web_discovery] No text block in response — skipping.")
        return []
    try:
        raw_candidates = extract_json_object(text_blocks[-1])
    except json.JSONDecodeError:
        print(f"[claude_web_discovery] Failed to parse candidate list JSON: {text_blocks[-1][:500]}")
        return []
    if not isinstance(raw_candidates, list):
        print("[claude_web_discovery] Expected a JSON array, got something else — skipping.")
        return []

    candidates = []
    for item in raw_candidates:
        if not isinstance(item, dict):
            continue
        handle = normalize_handle(str(item.get("handle", "")))
        item_platform = str(item.get("platform", "")).strip().lower()
        if not handle or item_platform not in ("instagram", "tiktok"):
            continue
        if platform != "both" and item_platform != platform:
            continue
        candidates.append({
            "handle": handle, "profile_link": canonical_profile_link(handle, item_platform),
            "platform": item_platform, "matched_query": "claude_web_discovery",
            "snippet": str(item.get("rationale", ""))[:300],
            "matched_hashtag": "", "matched_archetype": "", "matched_lane": "Claude Web Discovery",
            "competitor_affinity": "", "discovery_method": "claude_web_search",
        })
    return candidates


# ============================================================
# STAGE 2.7 — DEEP RESEARCH REPORT PARSING (Option D)
# ============================================================
#
# HOW TO USE THIS:
#   Set DEEP_RESEARCH_REPORT as a repo Variable (Settings → Secrets and
#   variables → Actions → Variables tab) to ONE of:
#
#   A) A Gemini share link:  https://share.gemini.google/xxxxxxxx
#      → Tavily fetches the rendered page (handles JS) and extracts text.
#      → USE THE LIVE CHAT LINK, not the Deep Research file link — the chat
#        updates as you keep asking follow-up questions, so re-running the
#        pipeline automatically picks up your latest research.
#
#   B) A Google Doc URL:     https://docs.google.com/document/d/DOC_ID/...
#      → Converted to the /export?format=txt endpoint for clean plain-text
#        extraction without JS. Doc must be shared "Anyone with the link".
#      → Paste your full Gemini chat into one permanent Google Doc per brand.
#        Update it between runs without changing the Variable.
#
#   C) Any other public URL: Tavily fetches and extracts the text.
#
#   D) Raw pasted text:      Paste the report content directly into the Variable.
#      → Works but limited to ~48KB. Use for quick one-off runs.
#
# Every candidate extracted from the report goes through the identical
# enrichment → follower filter → Haiku → Sonnet pipeline as Serper candidates.
# Duplicates within the report (same handle mentioned across multiple searches
# in the same chat) are deduplicated before they enter the pool.
# Claude is a PARSER here, not a judge — no scores, no ranking, no invention.
# Independent verification still happens downstream regardless.

# Chunk size for splitting long reports — each chunk gets its own Claude call
# so a 40K-char Gemini chat session doesn't get silently truncated.
_REPORT_CHUNK_SIZE = 10000   # chars per chunk — reduced from 14000 to keep each chunk's
                              # response within the 8192 max_tokens budget. The expanded
                              # 26-field schema produces ~500 tokens per creator, so a
                              # 14000-char chunk with 10 creators needs ~5000 tokens just
                              # for the JSON — too close to 4096, causing truncation.
                              # At 10000 chars (~6–7 creators), the response stays well
                              # under 8192 tokens even for dense profiles.
_REPORT_CHUNK_OVERLAP = 500  # chars of overlap between chunks to catch handles split across boundaries


def _fetch_report_content(raw_value: str) -> str:
    """
    Resolves DEEP_RESEARCH_REPORT to plain text, handling four cases:
      1. Google Doc URL   → /export?format=txt (no JS, always works if doc is shared)
      2. Gemini share URL → Tavily extract (Tavily uses a headless browser, handles JS SPAs)
      3. Any other URL    → Tavily extract
      4. Raw text         → returned as-is

    Returns empty string on any fetch failure so the pipeline degrades
    gracefully to Serper-only discovery.
    """
    stripped = raw_value.strip()
    if not stripped:
        return ""

    # Detect URL vs raw text — URLs always start with http(s)://
    if not stripped.lower().startswith("http"):
        # Raw pasted text — use directly
        print(f"[deep_research_parse] Input is raw text ({len(stripped)} chars) — using directly.")
        return stripped

    url = stripped

    # ── Case 1: Google Docs ──────────────────────────────────────────────────
    # /edit, /view, /pub — all get converted to /export?format=txt which
    # returns clean UTF-8 plain text without requiring JS or authentication,
    # as long as the doc is set to "Anyone with the link can view".
    gdoc_match = re.match(
        r"https://docs\.google\.com/document/d/([A-Za-z0-9_-]+)", url
    )
    if gdoc_match:
        doc_id = gdoc_match.group(1)
        export_url = f"https://docs.google.com/document/d/{doc_id}/export?format=txt"
        print(f"[deep_research_parse] Google Doc detected — fetching via export endpoint: {export_url}")
        try:
            resp = requests.get(export_url, timeout=20, allow_redirects=True)
            resp.raise_for_status()
            text = resp.text.strip()
            if text:
                print(f"[deep_research_parse] Google Doc fetched: {len(text)} chars.")
                return text
            print("[deep_research_parse] Google Doc export returned empty text — "
                  "check the doc is shared 'Anyone with the link can view'.")
            return ""
        except requests.RequestException as e:
            print(f"[deep_research_parse] Google Doc export fetch failed: {e} — "
                  f"check the doc is shared 'Anyone with the link can view'.")
            return ""

    # ── Cases 2 & 3: Gemini share link or any other URL ─────────────────────
    # Tavily's Extract API uses a real headless browser internally, so it can
    # render JS-heavy SPAs like Gemini's share pages that plain requests can't.
    # This is the same tavily_extract() already used for bio-link fetching.
    if "share.gemini.google" in url:
        print(f"[deep_research_parse] Gemini share link detected — fetching via Tavily (headless): {url}")
    else:
        print(f"[deep_research_parse] URL detected — fetching via Tavily: {url}")

    # Use a higher char limit than the bio-link default (8K) — a full Gemini
    # chat session can easily be 30–50K chars. Tavily caps at what the page
    # actually contains, so requesting more doesn't cost extra.
    api_key = os.environ.get("TAVILY_API_KEY")
    if not api_key:
        print("[deep_research_parse] TAVILY_API_KEY not set — cannot fetch URL. "
              "Paste report text directly into DEEP_RESEARCH_REPORT instead.")
        return ""
    try:
        resp = requests.post(
            "https://api.tavily.com/extract",
            json={"urls": [url], "api_key": api_key, "extract_depth": "advanced", "format": "text"},
            timeout=30,
        )
        resp.raise_for_status()
    except requests.RequestException as e:
        print(f"[deep_research_parse] Tavily fetch failed for {url}: {e}")
        return ""

    data = resp.json()
    results = data.get("results", [])
    if not results:
        print(f"[deep_research_parse] Tavily returned no results for {url}. "
              f"If this is a Gemini share link, try exporting to a Google Doc instead.")
        return ""

    content = results[0].get("raw_content") or results[0].get("content") or ""
    # No truncation here — full content passed to the chunked extractor below
    print(f"[deep_research_parse] Tavily fetched {len(content)} chars from {url}.")
    return content


def _extract_handles_from_chunk(chunk: str, chunk_num: int, total_chunks: int,
                                 platform: str, target_gender: str,
                                 location: str, niche: str,
                                 client) -> list:
    """
    Single Claude call for one chunk of report text.
    Returns a list of raw dicts from the JSON array Claude returns.
    """
    platforms_desc = "Instagram and TikTok" if platform == "both" else platform.capitalize()
    gender_desc = f"primarily {target_gender}" if target_gender != "both" else "any gender"
    chunk_note = (f" (chunk {chunk_num} of {total_chunks})" if total_chunks > 1 else "")

    prompt = f"""You are a data-extraction assistant. The text below is part of a creator research
session from Gemini{chunk_note}. Extract every named social media creator or influencer handle
that is EXPLICITLY MENTIONED in this text. Do NOT invent, guess, or add anyone not named here.

Campaign context (for platform/gender filtering only — do not use to invent candidates):
- Niche: {niche}
- Location: {location}
- Platform(s): {platforms_desc}
- Creator gender: {gender_desc}

EXTRACTION RULES:
1. Only extract handles/names EXPLICITLY stated in the text below.
2. HANDLE EXTRACTION PRIORITY — always prefer in this order:
      a) @username notation → strip the @, set handle_type = "username"
      b) instagram.com/username or tiktok.com/@username in a URL → extract the
         handle part only, set handle_type = "username"
      c) A bare lowercase username with no spaces (e.g. "fitdadlife") →
         set handle_type = "username"
      d) A display name / full name only (e.g. "Chip Leighton") → use as-is,
         set handle_type = "display_name"
   NEVER fall back to a display name if you found a @handle or profile URL
   anywhere in the same sentence or paragraph — look for the handle first.
3. Include Instagram and TikTok handles only. Skip YouTube, Twitter/X, etc.
   If the platform isn't specified for a name, make your best guess based on
   context and note it in the rationale.
4. Do NOT include brands, publications, agencies, or media outlets unless the
   text explicitly describes them as an individual creator's account.
5. For the rationale: use the exact short phrase from the text that names this
   person — quote it directly, don't rephrase.
6. If this chunk contains no named creators at all, return [].
7. followers_count: if the text EXPLICITLY states a follower count for this
   specific creator (e.g. "120K followers", "1.2 million", "around 300K"),
   extract it as an integer. Do NOT estimate or guess — only set this when a
   number is directly stated in the text for THIS creator. Set to null if not
   mentioned. Do NOT carry a count from one creator to another.
8. name: the creator's full display name (e.g. "Caleb Rogg"). Null if not stated.
9. profile_url: the full Instagram/TikTok profile URL if explicitly stated. Null otherwise.
10. followers_source: the named source of the follower count (e.g. "Modash", "Collabstr",
    "Influencer-Hero"). Null if not stated.
11. followers_confidence: HIGH, MEDIUM, or LOW if explicitly stated. Null otherwise.
12. engagement_rate: the engagement rate as a float (e.g. 21.13 for "21.13%"). Extract
    ONLY if a percentage is explicitly stated for THIS creator. Null otherwise.
13. avg_reel_views: average reel/video views as an integer
    (e.g. 26900 for "26.9K plays"). Only if explicitly stated. Null otherwise.
14. follower_growth: follower growth rate as a string if stated
    (e.g. "+104% over 6 months"). Null if not stated.
15. city: the creator's city if explicitly stated. Null if not.
16. state: the creator's US state if explicitly stated. Null if not.
17. country: the creator's country if explicitly stated. Null if not.
18. contact_email: any email address explicitly stated for this creator's
    business/partnership contact (e.g. "najm@kensingtongrey.co"). Null if not.
19. primary_niche: the creator's primary niche/category as stated in the report
    (e.g. "Men's Wellness & Grooming"). Null if not stated.
20. secondary_niche: secondary niche if explicitly stated. Null if not.
21. creator_type: the creator type or tier as stated
    (e.g. "Target Tier (10K-50K) / Fitness & Menswear Vlogger"). Null if not.
22. content_opportunity: the specific content integration moment where the brand
    product could appear naturally — copy the key sentence from the report
    (max 250 chars). Null if not stated.
23. use_cases: the relevant product use cases listed for this creator as a short
    phrase (e.g. "post-shower, morning routine, gym recovery"). Max 150 chars.
    Null if not stated.
24. partnership_evidence: brief note on their agency/brand partnership history
    (e.g. "Represented by The North Management; Box Menswear"). Max 150 chars.
    Null if not stated.
25. why_they_fit: 1-sentence summary of why they fit the brand from the report.
    Max 150 chars. Null if not stated.
26. audience_gender: audience gender split if stated
    (e.g. "62.4% Male / 37.6% Female"). Null if not.
27. research_confidence: HIGH, MEDIUM, or LOW if explicitly stated. Null otherwise.
28. concerns: any potential concerns noted for this creator (max 100 chars). Null
    if not stated.

TEXT:
---
{chunk}
---

Respond with ONLY a JSON array as the VERY LAST thing in your response:
[
  {{
    "handle": "<@username stripped of @, bare username, or full display name>",
    "name": "<creator full name or null>",
    "platform": "instagram|tiktok",
    "handle_type": "username|display_name",
    "profile_url": "<profile URL or null>",
    "followers_count": <integer or null>,
    "followers_source": "<source label or null>",
    "followers_confidence": "<HIGH|MEDIUM|LOW or null>",
    "engagement_rate": <float or null>,
    "avg_reel_views": <integer or null>,
    "follower_growth": "<growth phrase or null>",
    "city": "<city or null>",
    "state": "<US state or null>",
    "country": "<country or null>",
    "contact_email": "<email or null>",
    "primary_niche": "<primary niche or null>",
    "secondary_niche": "<secondary niche or null>",
    "creator_type": "<creator type/tier or null>",
    "content_opportunity": "<specific integration sentence or null>",
    "use_cases": "<use cases phrase or null>",
    "partnership_evidence": "<partnership note or null>",
    "why_they_fit": "<fit summary sentence or null>",
    "audience_gender": "<gender split or null>",
    "research_confidence": "<HIGH|MEDIUM|LOW or null>",
    "concerns": "<concerns phrase or null>",
    "rationale": "<exact phrase from text naming this person>"
  }}
]

Empty result: []
No preamble. No explanation after the JSON."""

    try:
        response = client.messages.create(
            model=SONNET_MODEL, max_tokens=8192,
            messages=[{"role": "user", "content": prompt}],
        )
    except anthropic.APIError as e:
        print(f"[deep_research_parse] Claude API error on chunk {chunk_num}: {e}")
        return []

    text_blocks = [b.text for b in response.content if getattr(b, "type", None) == "text"]
    if not text_blocks:
        return []

    raw_text = text_blocks[-1]
    was_truncated = getattr(response, "stop_reason", None) == "max_tokens"

    # Primary parse — handles normal responses and trailing-prose responses.
    try:
        result = extract_json_object(raw_text)
    except json.JSONDecodeError:
        # Secondary: attempt to recover partial results from a truncated response.
        # The expanded 26-field schema produces ~500 tokens per creator, so a
        # chunk with 10 creators needs ~5000 tokens. If the model hits max_tokens
        # mid-array the JSON is unclosed and extract_json_object raises. We try to
        # salvage every fully-written object rather than silently losing the chunk.
        if was_truncated:
            print(f"[deep_research_parse] Chunk {chunk_num} response was truncated "
                  f"(stop_reason=max_tokens) — attempting partial recovery.")
        try:
            result = repair_truncated_json_array(raw_text)
            recovered = len(result) if isinstance(result, list) else 0
            print(f"[deep_research_parse] Chunk {chunk_num}: partial-JSON repair "
                  f"recovered {recovered} complete object(s) from truncated response.")
        except json.JSONDecodeError:
            print(f"[deep_research_parse] JSON parse failed on chunk {chunk_num} "
                  f"(truncated={was_truncated}): {raw_text[:300]}")
            return []

    if not isinstance(result, list):
        return []

    if was_truncated and isinstance(result, list):
        print(f"[deep_research_parse] Chunk {chunk_num} was truncated — "
              f"{len(result)} object(s) recovered. Consider raising max_tokens "
              f"or reducing _REPORT_CHUNK_SIZE if creators are being missed.")

    return result


def parse_deep_research_report(raw_value: str, platform: str, target_gender: str,
                                location: str, niche: str) -> list:
    """
    Entry point for Stage 2.7.

    raw_value may be:
      - A URL (Gemini share link, Google Doc, or other public page)
      - Raw pasted report/chat text

    Steps:
      1. Resolve raw_value to plain text (_fetch_report_content)
      2. Split into overlapping chunks if the text is long
      3. Run each chunk through Claude Sonnet to extract handles
      4. Deduplicate across all chunks by normalised handle (same creator
         mentioned 5x across a multi-search session → appears once)
      5. Return candidate dicts in the same shape as every other discovery channel

    Never raises — returns [] on any failure so the pipeline degrades to
    Serper-only discovery.
    """
    report_text = _fetch_report_content(raw_value)
    if not report_text:
        return []

    # Split into overlapping chunks so long reports don't get truncated.
    # Overlap catches handles that happen to fall near a chunk boundary.
    chunks = []
    start = 0
    while start < len(report_text):
        end = min(start + _REPORT_CHUNK_SIZE, len(report_text))
        chunks.append(report_text[start:end])
        if end == len(report_text):
            break
        start += _REPORT_CHUNK_SIZE - _REPORT_CHUNK_OVERLAP

    total_chunks = len(chunks)
    if total_chunks > 1:
        print(f"[deep_research_parse] Report is {len(report_text)} chars — "
              f"splitting into {total_chunks} chunks for extraction.")

    client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    all_raw = []
    for i, chunk in enumerate(chunks, 1):
        raw = _extract_handles_from_chunk(
            chunk, i, total_chunks, platform, target_gender, location, niche, client
        )
        all_raw.extend(raw)

    if not all_raw:
        print("[deep_research_parse] No creator handles found in the report/chat. "
              "Serper will run as the sole discovery source.")
        return []

    # ── Deduplicate by normalised handle — MERGE not first-wins ─────────────
    # A Deep Research report mentions the same creator in multiple places:
    #   • The Candidate Table lists all 25 with basic fields (handle, followers,
    #     location, creator type) — typically in an early chunk.
    #   • The Detailed Creator Profiles section gives each one a full evidence
    #     block (engagement_rate, contact email, content_opportunity, etc.)
    #     — spread across later chunks.
    #
    # First-wins dedup discards whichever section appears second, so depending
    # on which chunk succeeds we either keep the sparse table row (and lose ER,
    # contact, content opportunity) OR keep the rich profile row (and lose the
    # table's location/follower-source). This is why different creators and
    # different amounts of data appeared in each run.
    #
    # Merge-wins keeps both: the FIRST occurrence reserves the slot; every
    # subsequent occurrence for the same handle fills in any dr_* fields that
    # are still None. Both table and profile data end up in one complete record.
    #
    # Fields that are eligible for merging — only dr_* fields and a few
    # structural ones. Core identity fields (handle, platform, discovery_method)
    # always come from the first occurrence.
    _DR_MERGE_FIELDS = [
        "dr_followers_count", "dr_name", "dr_engagement_rate",
        "dr_avg_reel_views", "dr_follower_growth",
        "dr_city", "dr_state", "dr_country", "dr_contact_email",
        "dr_primary_niche", "dr_secondary_niche", "dr_creator_type",
        "dr_content_opportunity", "dr_use_cases", "dr_partnership_evidence",
        "dr_why_they_fit", "dr_audience_gender", "dr_followers_source",
        "dr_followers_confidence", "dr_research_confidence", "dr_concerns",
        "snippet",           # prefer whichever has more text
        "profile_link",      # prefer the explicitly-stated URL over canonical
    ]

    handle_index: dict = {}   # dedup_key → index in candidates list
    candidates = []
    duplicates_merged = 0

    for item in all_raw:
        if not isinstance(item, dict):
            continue
        handle = normalize_handle(str(item.get("handle", "")))
        item_platform = str(item.get("platform", "")).strip().lower()
        if not handle or item_platform not in ("instagram", "tiktok"):
            continue
        if platform != "both" and item_platform != platform:
            continue
        dedup_key = f"{item_platform}:{handle}"

        # ── Build the full candidate dict for this item ───────────────────
        is_display_name = (
            str(item.get("handle_type", "username")).strip().lower() == "display_name"
        )

        raw_followers = item.get("followers_count")
        dr_followers = None
        if isinstance(raw_followers, (int, float)) and raw_followers > 0:
            dr_followers = int(raw_followers)
        elif isinstance(raw_followers, str) and raw_followers.strip():
            try:
                dr_followers = parse_follower_abbrev(raw_followers.strip())
            except Exception:
                pass

        raw_er = item.get("engagement_rate")
        dr_engagement_rate = None
        if isinstance(raw_er, (int, float)) and raw_er > 0:
            dr_engagement_rate = float(raw_er)
        elif isinstance(raw_er, str) and raw_er.strip():
            try:
                dr_engagement_rate = float(re.sub(r"[%\s]", "", raw_er))
            except Exception:
                pass

        raw_arv = item.get("avg_reel_views")
        dr_avg_reel_views = None
        if isinstance(raw_arv, (int, float)) and raw_arv > 0:
            dr_avg_reel_views = int(raw_arv)
        elif isinstance(raw_arv, str) and raw_arv.strip():
            try:
                dr_avg_reel_views = parse_follower_abbrev(raw_arv.strip())
            except Exception:
                pass

        # Profile URL — prefer explicitly stated URL from the report.
        # For display names (handle contains spaces), NEVER construct a
        # canonical URL: instagram.com/james%20pieratt is not a real profile
        # and causes Tavily to return empty results while polluting the sheet.
        # Leave it blank — enrichment will try Serper instead.
        profile_url = (item.get("profile_url") or "").strip()
        if not profile_url or " " in profile_url:
            if is_display_name:
                profile_url = ""   # no valid URL can be constructed
            else:
                profile_url = canonical_profile_link(handle, item_platform)

        cand = {
            "handle": handle,
            "profile_link": profile_url,
            "platform": item_platform,
            "matched_query": "deep_research_report",
            "snippet": str(item.get("rationale", ""))[:300],
            "matched_hashtag": "",
            "matched_archetype": "",
            "matched_lane": "Deep Research Report",
            "competitor_affinity": "",
            "discovery_method": "deep_research_report",
            "handle_is_display_name": is_display_name,
            "dr_followers_count":      dr_followers,
            "dr_name":                 (item.get("name") or "").strip() or None,
            "dr_engagement_rate":      dr_engagement_rate,
            "dr_avg_reel_views":       dr_avg_reel_views,
            "dr_follower_growth":      (item.get("follower_growth") or "").strip() or None,
            "dr_city":                 (item.get("city") or "").strip() or None,
            "dr_state":                (item.get("state") or "").strip() or None,
            "dr_country":              (item.get("country") or "").strip() or None,
            "dr_contact_email":        (item.get("contact_email") or "").strip() or None,
            "dr_primary_niche":        (item.get("primary_niche") or "").strip() or None,
            "dr_secondary_niche":      (item.get("secondary_niche") or "").strip() or None,
            "dr_creator_type":         (item.get("creator_type") or "").strip() or None,
            "dr_content_opportunity":  (item.get("content_opportunity") or "").strip()[:250] or None,
            "dr_use_cases":            (item.get("use_cases") or "").strip()[:150] or None,
            "dr_partnership_evidence": (item.get("partnership_evidence") or "").strip()[:150] or None,
            "dr_why_they_fit":         (item.get("why_they_fit") or "").strip()[:150] or None,
            "dr_audience_gender":      (item.get("audience_gender") or "").strip() or None,
            "dr_followers_source":     (item.get("followers_source") or "").strip() or None,
            "dr_followers_confidence": (item.get("followers_confidence") or "").strip() or None,
            "dr_research_confidence":  (item.get("research_confidence") or "").strip() or None,
            "dr_concerns":             (item.get("concerns") or "").strip()[:100] or None,
        }

        # ── Merge or append ───────────────────────────────────────────────
        if dedup_key in handle_index:
            # Creator already seen — merge non-null fields from this occurrence
            # into the existing record rather than discarding it.
            existing = candidates[handle_index[dedup_key]]
            for field in _DR_MERGE_FIELDS:
                new_val = cand.get(field)
                if new_val is None or new_val == "":
                    continue   # nothing to contribute
                existing_val = existing.get(field)
                if existing_val is None or existing_val == "":
                    existing[field] = new_val   # fill the blank
                elif field == "snippet" and len(new_val) > len(existing_val):
                    existing[field] = new_val   # prefer richer rationale
                elif field == "profile_link" and existing_val == "" and new_val != "":
                    existing[field] = new_val   # prefer a real URL over blank
                # For all other fields, keep the first (non-null) value
            duplicates_merged += 1
            continue

        handle_index[dedup_key] = len(candidates)
        candidates.append(cand)

    merge_note = f" ({duplicates_merged} duplicate mention(s) merged)" if duplicates_merged else ""
    print(f"[deep_research_parse] Extracted {len(candidates)} unique candidate(s) "
          f"from {len(all_raw)} raw mentions across {total_chunks} chunk(s){merge_note}. "
          f"Serper will supplement with handles not already covered.")
    return candidates


# ============================================================
# STAGE 3/7 — DEDUP
# ============================================================

def normalize_handle(raw: str) -> str:
    if not raw:
        return ""
    handle = raw.strip().lower().lstrip("@").rstrip("/")
    handle = re.sub(r"^https?://(www\.|m\.)?(instagram|tiktok)\.com/", "", handle)
    handle = handle.lstrip("@").split("?")[0]
    return handle


def make_dedup_key(platform: str, handle: str) -> str:
    return f"{platform.strip().lower()}:{normalize_handle(handle)}"


# ============================================================
# STAGE 4 — ENRICHMENT (tiered fallback)
# ============================================================

# Graph API versions get deprecated on a rolling schedule (v18.0 expired
# Jan 2026, v19.0 expired May 2026) — a hardcoded old version returns a
# generic 400 on every single call, indistinguishable at a glance from a
# bad token or bad account ID. Bump this periodically; check
# developers.facebook.com/docs/graph-api/changelog for the current version
# if Business Discovery starts failing again.
GRAPH_API_VERSION = "v26.0"

# Circuit breaker: if Business Discovery fails this many times in a row FOR A
# REASON THAT INDICATES A SYSTEMIC PROBLEM (see classify_business_discovery_error
# below), assume it's broken for the rest of this run rather than a
# per-candidate fluke, and stop spending time/requests on it. Deliberately NOT
# triggered by "target isn't a Business/Creator account" errors — that's an
# expected, normal outcome for a meaningful fraction of any real candidate
# pool (most personal accounts return exactly this), not evidence the
# credential is broken. Counting those toward the threshold was the bug: 3
# ordinary personal-account candidates in a row could previously disable
# Business Discovery for the whole rest of a run for no real reason.
_business_discovery_state = {
    "consecutive_systemic_failures": 0, "disabled": False, "disabled_reason": "",
    # Separate from the auth-failure circuit breaker above — this tracks
    # SUSTAINED rate-limiting specifically (candidates that exhaust the full
    # retry schedule and are still rate-limited), so a persistent episode
    # doesn't mean every subsequent candidate burns a full 3/8/20s retry
    # sequence that's likely doomed anyway. See RATE_LIMIT_COOLDOWN_* below.
    "consecutive_rate_limits": 0, "rate_limit_cooldown_until": 0.0,
}
RATE_LIMIT_COOLDOWN_THRESHOLD = 3
RATE_LIMIT_COOLDOWN_SECONDS = 45
BUSINESS_DISCOVERY_FAILURE_THRESHOLD = 3

# Substrings of Meta's error_user_msg/message that indicate the querying
# credential itself is broken (expired/invalid token, revoked permissions) —
# a DEFINITIVE, run-ending signal, not a per-candidate one. Checked first and
# trips the breaker immediately on a single occurrence, since there's no
# ambiguity here the way there is with "invalid user id".
AUTH_FAILURE_SIGNALS = [
    "session has expired", "access token", "oauth", "expired token",
    "invalid access token", "error validating access token",
]
# Substrings indicating the TARGET username isn't eligible for Business
# Discovery (not a Business/Creator account, doesn't exist, etc.) — expected
# and normal, never counted toward the circuit breaker.
INELIGIBLE_TARGET_SIGNALS = ["invalid user id", "does not exist", "cannot be loaded"]

# Meta's app-level rate limit ("(#4) Application request limit reached") is
# categorically different from both of the above: it's temporary and caused
# by call volume, not a broken credential or an ineligible target. Evidenced
# directly in production — 3 consecutive rate-limit hits (calling Business
# Discovery in a tight loop across many candidates) tripped the "3
# consecutive unexplained failures" circuit breaker and disabled Business
# Discovery for the rest of a 100-candidate run, even though nothing was
# actually wrong with the token or the request. A rate limit deserves a
# brief pause and retry, not "give up for the rest of the run" — and
# shouldn't count toward the systemic-failure threshold at all, since being
# temporarily rate-limited by volume isn't evidence the API itself is down.
RATE_LIMIT_SIGNALS = ["request limit reached", "rate limit", "too many requests", "(#4)"]

# A single 3-second retry (the previous fix) turned out to be insufficient in
# practice: production logs showed 15+ CONSECUTIVE candidates all hitting the
# rate limit, each one still rate-limited on that one retry too. That's a
# sustained condition, not a brief bump — the app was calling Business
# Discovery back-to-back with zero pacing between requests, so by the time
# any single retry fired 3 seconds later, the next call was already queued
# right behind it with no time for the rate window to actually clear.
# Two changes: (1) a real backoff schedule with more attempts and increasing
# delay instead of one fixed retry, and (2) BUSINESS_DISCOVERY_CALL_DELAY
# below — a small pause before EVERY call (not just after hitting a limit)
# to spread requests out and avoid bursting the limit in the first place,
# which matters much more than backoff once you're already over it.
RATE_LIMIT_RETRY_DELAYS_SECONDS = [3, 8, 20]
# Applied before every Business Discovery attempt, successful or not. Costs
# real run time on a large SEARCH_BUDGET (e.g. 200 candidates * 0.5s = 100s),
# which is a deliberate trade — reliably getting real Meta data for more
# candidates is worth more than shaving a couple minutes off the run.
BUSINESS_DISCOVERY_CALL_DELAY_SECONDS = 0.5


def classify_business_discovery_error(detail: str) -> str:
    """Returns 'auth_failure', 'ineligible_target', 'rate_limited', or 'other'."""
    lower = (detail or "").lower()
    if any(sig in lower for sig in AUTH_FAILURE_SIGNALS):
        return "auth_failure"
    if any(sig in lower for sig in INELIGIBLE_TARGET_SIGNALS):
        return "ineligible_target"
    if any(sig in lower for sig in RATE_LIMIT_SIGNALS):
        return "rate_limited"
    return "other"


def parse_follower_abbrev(text: str):
    text = text.replace(",", "").strip().lower()
    mult = 1
    if text.endswith("k"):
        mult, text = 1_000, text[:-1]
    elif text.endswith("m"):
        mult, text = 1_000_000, text[:-1]
    try:
        return int(float(text) * mult)
    except ValueError:
        return None


def numbers_agree(a: int, b: int) -> bool:
    """Two follower counts 'agree' within a tolerance rather than requiring an
    exact match — platforms round display counts differently (158,170 vs
    "158K"), and counts drift slightly between two live/cached reads taken
    minutes apart. Tolerance: 10% of the larger number, floor of 2,000, so
    small accounts aren't held to an unrealistically tight absolute bar."""
    if a is None or b is None:
        return False
    tolerance = max(2000, 0.10 * max(a, b))
    return abs(a - b) <= tolerance


def serper_follower_snippet(handle: str, platform: str, serper_key: str) -> dict:
    """One independent read: Serper's cached Google snippet for this handle.
    Returns None if nothing usable was found, rather than a dict with blank
    fields — callers need to distinguish 'this source found nothing' from
    'this source confirmed a bio with no follower number in it'."""
    if not serper_key:
        return None
    domain = PLATFORM_DOMAINS.get(platform)
    results = serper_search(f"site:{domain} {handle}", serper_key, num=3)

    # A query for a handle can return a post, tag page, or another similarly
    # named account. Never attribute the first result's bio/follower data to
    # this creator unless its URL resolves back to the same normalized handle.
    target_handle = normalize_handle(handle)
    matched_result = next(
        (r for r in results if normalize_handle(extract_handle_from_url(r.get("link", ""), platform)) == target_handle),
        None,
    )
    if not matched_result:
        return None

    snippet = matched_result.get("snippet", "")
    followers = re.search(r"([\d.,]+[KMkm]?)\s*Followers", snippet)
    posts = re.search(r"([\d.,]+[KMkm]?)\s*Posts", snippet)
    return {
        "followers_count": parse_follower_abbrev(followers.group(1)) if followers else None,
        "total_posts": parse_follower_abbrev(posts.group(1)) if posts else None,
        "bio": snippet,
    }


def tavily_follower_snippet(handle: str, platform: str) -> dict:
    """The second independent read: a live fetch of the actual profile page
    via Tavily, rather than Serper's cached snippet. Same 'None if nothing
    usable' contract as serper_follower_snippet — not guaranteed, both
    platforms rate-limit/login-wall anonymous fetches a meaningful fraction
    of the time. A genuine second source, not a fix for the underlying
    constraint that real reliability still needs a licensed provider."""
    if not os.environ.get("TAVILY_API_KEY"):
        return None
    # A real Instagram/TikTok handle can never contain a space — this is
    # what a Deep Research report display name (e.g. "James Pieratt",
    # handle_type=display_name) looks like once it's fallen through to this
    # generic enrichment path. canonical_profile_link() would otherwise
    # build "https://www.instagram.com/james pieratt/", which Tavily
    # rejects outright — the same failure mode the display-name check
    # already prevents for Meta's Business Discovery call, just not yet
    # applied here. No API call attempted; same "nothing usable" contract
    # as any other miss.
    if " " in handle:
        print(f"[tavily] skipping follower lookup for {handle!r} — contains a space, "
              f"not a real handle (likely a display name with no confirmed @username).")
        return None
    page_text = tavily_extract(canonical_profile_link(handle, platform))
    if not page_text:
        return None
    followers = re.search(r"([\d.,]+[KMkm]?)\s*Followers", page_text)
    posts = re.search(r"([\d.,]+[KMkm]?)\s*Posts", page_text)
    if not followers:
        return None
    return {
        "followers_count": parse_follower_abbrev(followers.group(1)),
        "total_posts": parse_follower_abbrev(posts.group(1)) if posts else None,
        "bio": page_text[:500],
    }


# ============================================================
# GEMINI — web search helper, discovery channel, verification fallback
# ============================================================

# Rate-limit and model-retirement state for Gemini calls.  Shared across all
# gemini_generate calls in one run.
#
# consecutive_429s  — RPM (per-minute) rate limit hit count. Escalating
#   backoff (5→15→30→60s) handles these. Resets on any success.
#
# daily_quota_exhausted — True when a 429 body identifies the error as
#   RESOURCE_EXHAUSTED / quota exceeded (daily RPD limit, not RPM).
#   Retrying with delays does nothing for a daily limit — disable immediately
#   and log clearly. Resets only when the project's quota window resets
#   (midnight Pacific), so there is no point retrying in the same run.
#
# model_retired — True on the first 404 whose body says "no longer available".
#   All subsequent calls return "" immediately without hitting the API again.
#
# total_calls — running count of successful Gemini API calls this run.
#   Logged at end of run so you can see how fast the daily quota is burning.
#
# verify_calls — count of gemini_verify_handle calls made this run.
#   Capped at GEMINI_VERIFY_MAX_PER_RUN to protect daily quota from
#   verification eating into the discovery budget across large candidate pools.
_gemini_state = {
    "consecutive_429s": 0,
    "daily_quota_exhausted": False,
    "model_retired": False,
    "total_calls": 0,
    "verify_calls": 0,
}
GEMINI_BACKOFF_DELAYS = [5, 15, 30, 60]   # seconds for RPM backoff
GEMINI_429_DISABLE_THRESHOLD = 4           # disable after this many consecutive RPM 429s

# Hard cap on gemini_verify_handle calls per run.  Each verify call consumes
# one RPD slot — the same daily quota shared with the 1-call primary discovery.
# Free tier is typically 50-500 RPD depending on the model; with 15+ Serper
# candidates each potentially needing verification, an uncapped verify loop
# can exhaust the daily quota before the run even reaches Claude scoring.
# Set to 0 to disable verification fallback entirely (same as
# GEMINI_VERIFICATION_FALLBACK=false).
# Configurable via GEMINI_VERIFY_MAX_PER_RUN repo Variable.
GEMINI_VERIFY_MAX_PER_RUN = int(os.environ.get("GEMINI_VERIFY_MAX_PER_RUN", "5"))


def gemini_generate(prompt: str, api_key: str, use_search: bool = True) -> str:
    """
    Single Gemini generateContent call with optional Google Search grounding.
    Returns the text of the first candidate on success.  Returns "" on any
    failure (rate-limit, quota, network, parse) — callers must handle empty
    returns gracefully, same contract as tavily_extract.

    Rate-limit handling:
    - RPM (per-minute) 429s: escalating backoff (5/15/30/60s), then disabled
      for the run after GEMINI_429_DISABLE_THRESHOLD consecutive hits.
    - Daily quota (RPD) 429s: detected from the response body/reason; disabled
      immediately — retrying doesn't help, quota resets at midnight Pacific.
      Log message tells you exactly what happened so you don't keep retrying.

    Model-retirement handling: a 404 whose body contains "no longer available"
    or "please update your code" immediately disables Gemini for the run.
    The error message names the currently supported replacement model — fix by
    updating GEMINI_MODEL in repo Variables (Settings → Secrets and variables
    → Actions → Variables tab).
    """
    if not api_key:
        return ""
    if _gemini_state["model_retired"]:
        return ""  # already logged at first occurrence — don't spam
    if _gemini_state["daily_quota_exhausted"]:
        return ""  # daily RPD limit hit — no point retrying in this run
    if _gemini_state["consecutive_429s"] >= GEMINI_429_DISABLE_THRESHOLD:
        # Only printed once per run — subsequent calls hit the check above
        # after daily_quota_exhausted is set, or just return "" quietly.
        print(f"[gemini] {GEMINI_429_DISABLE_THRESHOLD} consecutive RPM 429s — Gemini disabled "
              f"for the rest of this run.  Re-enable by starting a new run.")
        return ""

    url = f"{GEMINI_API_BASE}/{GEMINI_MODEL}:generateContent?key={api_key}"
    body: dict = {
        "contents": [{"role": "user", "parts": [{"text": prompt}]}],
        "generationConfig": {"temperature": 0.1},
    }
    if use_search:
        body["tools"] = [{"google_search": {}}]

    for attempt, delay in enumerate(GEMINI_BACKOFF_DELAYS + [None]):
        try:
            resp = requests.post(url, json=body, timeout=60)
        except requests.RequestException as e:
            print(f"[gemini] network error (attempt {attempt + 1}): {e}")
            return ""

        if resp.status_code == 429:
            body_text = resp.text[:600].lower()
            # Distinguish daily quota exhaustion from per-minute rate limiting.
            # RPD quota phrases seen in Google API error bodies:
            is_daily_quota = any(phrase in body_text for phrase in (
                "quota exceeded", "resource_exhausted", "ratequotaexceeded",
                "daily", "per day", "requests per day",
            ))
            # Also check Retry-After header — if > 300s it's almost certainly
            # a daily limit, not an RPM window (RPM windows are 60s max).
            retry_after = int(resp.headers.get("Retry-After", "0") or "0")
            if is_daily_quota or retry_after > 300:
                _gemini_state["daily_quota_exhausted"] = True
                quota_hint = (
                    f"  Retry-After header: {retry_after}s (~{retry_after // 3600}h)"
                    if retry_after else "  No Retry-After header returned."
                )
                print(
                    f"[gemini] DAILY QUOTA EXHAUSTED (HTTP 429 — daily RPD limit, not RPM). "
                    f"Gemini disabled for this run — retrying won't help.\n"
                    f"  Cause: too many runs today consumed the free-tier daily request budget.\n"
                    f"{quota_hint}\n"
                    f"  Fix options:\n"
                    f"    1. Wait until midnight Pacific time for the quota to reset, then rerun.\n"
                    f"    2. Reduce GEMINI_VERIFY_MAX_PER_RUN (currently {GEMINI_VERIFY_MAX_PER_RUN}) "
                    f"so verification uses fewer slots per run.\n"
                    f"    3. Enable billing on your Google Cloud project to move off the free tier.\n"
                    f"  The primary discovery call (1 call/run) + verification fallback "
                    f"(up to {GEMINI_VERIFY_MAX_PER_RUN} calls/run) = up to "
                    f"{1 + GEMINI_VERIFY_MAX_PER_RUN} Gemini calls per pipeline run."
                )
                return ""

            # RPM rate limit — back off and retry.
            _gemini_state["consecutive_429s"] += 1
            if delay is None:
                print(f"[gemini] still RPM rate-limited after {len(GEMINI_BACKOFF_DELAYS)} retries "
                      f"(total_calls this run: {_gemini_state['total_calls']}) — giving up.")
                return ""
            print(f"[gemini] 429 RPM rate-limited — waiting {delay}s "
                  f"(attempt {attempt + 1}/{len(GEMINI_BACKOFF_DELAYS)}, "
                  f"consecutive_429s={_gemini_state['consecutive_429s']}).")
            time.sleep(delay)
            continue

        _gemini_state["consecutive_429s"] = 0   # any non-429 resets the RPM counter

        if resp.status_code == 404:
            # Detect model retirement (Google returns 404 with an explicit
            # "no longer available / please update your code" message).
            body_text = resp.text[:600]
            is_retirement = any(phrase in body_text.lower() for phrase in (
                "no longer available", "please update your code", "is not found",
            ))
            if is_retirement:
                _gemini_state["model_retired"] = True
                suggested = ""
                m = re.search(r"use\s+(models/[\w.\-]+)", body_text, re.IGNORECASE)
                if not m:
                    m = re.search(r"(gemini[\w.\-]+flash[\w.\-]*|gemini[\w.\-]+pro[\w.\-]*)",
                                  body_text, re.IGNORECASE)
                if m:
                    suggested = m.group(1).replace("models/", "")
                fix_hint = (
                    f"  → Fix: go to your repo's Settings → Secrets and variables → Actions → Variables"
                    f" and set GEMINI_MODEL={suggested!r}."
                ) if suggested else (
                    "  → Fix: check the error body above for the suggested replacement model name, "
                    "then set GEMINI_MODEL to that value in repo Variables."
                )
                print(
                    f"[gemini] FATAL — model {GEMINI_MODEL!r} is retired (HTTP 404). "
                    f"Gemini disabled for the entire run.\n"
                    f"  API message: {body_text.strip()}\n"
                    f"{fix_hint}"
                )
            else:
                print(f"[gemini] HTTP 404 (not a retirement): {resp.text[:200]}")
            return ""

        if not resp.ok:
            print(f"[gemini] HTTP {resp.status_code}: {resp.text[:200]}")
            return ""

        try:
            data = resp.json()
            parts = data["candidates"][0]["content"]["parts"]
            result = "".join(p.get("text", "") for p in parts)
            _gemini_state["total_calls"] += 1
            return result
        except (KeyError, IndexError, ValueError) as e:
            print(f"[gemini] unexpected response shape: {e} — {str(resp.text)[:200]}")
            return ""

    return ""


# ============================================================
# STAGE 2.6 — GEMINI WEB DISCOVERY (optional, GEMINI_WEB_DISCOVERY=true)
# ============================================================

def gemini_web_discover_candidates(niche: str, brand_context: dict, location: str, platform: str,
                                    target_gender: str, api_key: str,
                                    max_candidates: int = GEMINI_WEB_DISCOVERY_MAX_RESULTS,
                                    min_followers: str = "", max_followers: str = "",
                                    niche_variants: list = None,
                                    search_lanes: list = None) -> list:
    """
    PRIMARY intelligent discovery engine.

    Gemini's Google Search grounding is used to research the creator landscape
    like a human researcher would — reading roundup articles, collab
    announcements, directories, and named individual profiles — rather than
    just pattern-matching keyword queries against profile URLs (which is what
    Serper does). Serper then supplements with handles Gemini didn't surface.

    The "finder, not judge" contract is strict:
    - Gemini is NOT asked to score, rank, or filter.
    - Gemini is NOT asked to verify follower counts or account status.
    - Everything it returns goes through the identical enrichment → follower
      filter → Haiku → Sonnet pipeline as Serper candidates.
    - Nothing Gemini says about a creator is trusted without independent
      verification downstream.

    The prompt sends the COMPLETE campaign brief so Gemini's searches are
    targeted, not generic. Multi-angle search instructions cause Gemini's
    grounding to issue several different queries internally.
    """
    platforms_desc = "Instagram and TikTok" if platform == "both" else platform.capitalize()
    gender_desc = f"primarily {target_gender}" if target_gender != "both" else "any gender"

    # Build follower range descriptor for the prompt
    follower_range_parts = []
    if min_followers:
        follower_range_parts.append(f"minimum {min_followers}")
    if max_followers:
        follower_range_parts.append(f"maximum {max_followers}")
    follower_range_desc = ", ".join(follower_range_parts) if follower_range_parts else "no strict range"

    # Build archetype list from search_lanes if available — gives Gemini the
    # same vocabulary Claude generated for Serper so its searches are aligned.
    archetype_hint = ""
    if search_lanes:
        flat = [a for lane in search_lanes for a in lane.get("archetypes", [])][:15]
        if flat:
            archetype_hint = f"\nKnown creator archetypes for this niche (use as search vocabulary):\n" \
                             f"{', '.join(flat)}"

    # Build niche terms hint
    niche_terms_hint = ""
    if niche_variants:
        niche_terms_hint = (f"\nRelated niche search terms: "
                            f"{', '.join(niche_variants[:10])}")

    prompt = f"""You are a creator research specialist.  Your task is to find real, currently-active
{platforms_desc} creator accounts for an influencer marketing campaign.

Your job is to return a LARGE CANDIDATE POOL of {max_candidates} creators.
The brand's own pipeline will independently verify follower counts, confirm identity,
and decide who qualifies.  Do NOT pre-select or rank — surface as many plausible
real people as you can across multiple search angles.  The pipeline needs options to
filter from; too few candidates is a worse outcome than including someone who later
gets filtered out.

══════════════════════ CAMPAIGN BRIEF ══════════════════════
Niche: {niche}
Platform(s): {platforms_desc}
Location: {location}
Creator gender: {gender_desc}
Follower range: {follower_range_desc}

Brand: {brand_context.get('brand_name', '')}
Product / brand brief: {brand_context.get('product_summary', 'N/A')}
Target buyer: {brand_context.get('target_buyer', 'N/A')}
Product use cases / moments it fits: {brand_context.get('use_cases', 'N/A')}
Creator types we want: {brand_context.get('creator_types', 'N/A')}
Explicitly EXCLUDE these even if the niche matches: {brand_context.get('exclude', 'none specified')}
{archetype_hint}{niche_terms_hint}
════════════════════════════════════════════════════════════

SEARCH STRATEGY — use ALL of these angles, not just the first:

1. Roundups and curated lists: search for "{niche} creators in {location}", "best {niche}
   influencers {location}", "top creators {location} {niche}" — read the articles and extract
   the actual named people mentioned.

2. Creator type searches: for EACH of the creator types listed above, search
   "{{creator_type}} {location}" on {platforms_desc} and in Google results.

3. Brand collab discovery: search for "{niche} brand partnership", "gifted {niche}
   {location}", "collab {niche}" — look for announcements that name real creators.

4. Community + hashtag research: search for "{niche} {location}" hashtag communities,
   find the people who consistently create in this space, not just the hashtag page.

5. Press and media: search for "{niche} creator interview {location}", podcast guest lists,
   event speaker bios — journalists quote real people.

6. Cross-reference: use any creator names you find to discover their actual platform
   handle if not immediately obvious.

FOR EACH creator you find, report:
  - Their exact platform handle (no @ symbol, exactly as it appears)
  - Which platform (instagram or tiktok)
  - One phrase saying WHERE or HOW you found them (roundup URL, press mention, etc.)

CRITICAL RULES:
- Only report handles you actually found via search — NO invented or guessed handles.
- Do NOT report follower counts, scores, or rankings.
- Do NOT include brands, retailers, publications, or meme/repost pages unless they are
  individual creator accounts.
- Do NOT include anyone whose content clearly matches the EXCLUDE list above.

Respond with ONLY a JSON array as the VERY LAST thing in your response:
[
  {{"handle": "<exact handle, no @>", "platform": "instagram|tiktok",
    "rationale": "<where/how found>"}}
]
Return up to {max_candidates} entries.  Fewer is fine if fewer real specific people
were actually found — do NOT pad with guesses."""

    text = gemini_generate(prompt, api_key, use_search=True)
    if not text:
        return []

    try:
        raw = extract_json_object(text)
    except json.JSONDecodeError:
        print(f"[gemini_discovery] JSON parse failed — response snippet: {text[:500]}")
        return []
    if not isinstance(raw, list):
        print("[gemini_discovery] Expected a JSON array, got something else — skipping.")
        return []

    candidates = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        handle = normalize_handle(str(item.get("handle", "")))
        item_platform = str(item.get("platform", "")).strip().lower()
        if not handle or item_platform not in ("instagram", "tiktok"):
            continue
        if platform != "both" and item_platform != platform:
            continue
        candidates.append({
            "handle": handle,
            "profile_link": canonical_profile_link(handle, item_platform),
            "platform": item_platform,
            "matched_query": "gemini_primary_discovery",
            "snippet": str(item.get("rationale", ""))[:300],
            "matched_hashtag": "",
            "matched_archetype": "",
            "matched_lane": "Gemini Primary Discovery",
            "competitor_affinity": "",
            "discovery_method": "gemini_web_search",
        })

    print(f"[gemini_discovery] Primary search returned {len(candidates)} candidate(s) "
          f"(from {len(raw)} raw items). Serper will now supplement with any handles "
          f"not already found.")
    return candidates


def gemini_verify_handle(handle: str, platform: str, api_key: str) -> dict:
    """
    Gemini-powered verification fallback — used when Meta Business Discovery
    returns 'Invalid user id' and Serper/Tavily also found no follower data.

    Asks Gemini to search for the profile and return structured evidence.
    IMPORTANT: result is tagged follower_source_quality='probable_gemini',
    NOT 'verified' or 'verified_cross_source'.  It feeds the same
    evaluate_follower_status() logic as any other web-sourced count, and
    gets the same 'probable' treatment when a single source only is
    present — it does NOT bypass any hard MIN/MAX_FOLLOWERS gate.

    Returns None if Gemini finds nothing useful (same contract as
    serper_follower_snippet / tavily_follower_snippet).
    """
    if not api_key:
        return None

    platform_url = f"{'www.instagram.com' if platform == 'instagram' else 'www.tiktok.com'}/@{handle}"
    prompt = f"""Search for information about this social media account and return a JSON object.

Account: @{handle} on {platform.capitalize()}
Profile URL: {platform_url}

Search for this creator and return ONLY a JSON object, nothing else:
{{
  "exists": true or false,
  "followers_count": <integer or null if not found>,
  "total_posts": <integer or null if not found>,
  "bio": "<bio text snippet, or null>",
  "location": "<city and/or country if found, or null>",
  "gender": "<male|female|unclear>",
  "account_type": "<creator|brand|unknown>",
  "active_recently": <true|false|null>,
  "evidence_url": "<URL where you found this info, or null>"
}}

If you cannot find any evidence this account exists, set exists to false and all other
fields to null.  Do NOT guess or invent values."""

    text = gemini_generate(prompt, api_key, use_search=True)
    if not text:
        return None

    try:
        data = extract_json_object(text)
    except json.JSONDecodeError:
        print(f"[gemini_verify] Could not parse JSON for @{handle}: {text[:300]}")
        return None

    if not isinstance(data, dict) or not data.get("exists"):
        return None

    followers = data.get("followers_count")
    if isinstance(followers, str):
        followers = parse_follower_abbrev(followers)

    return {
        "followers_count": followers if isinstance(followers, int) else None,
        "total_posts": data.get("total_posts") if isinstance(data.get("total_posts"), int) else None,
        "bio": str(data.get("bio") or "")[:500],
        "location_hint": str(data.get("location") or ""),
        "follower_source_quality": "probable_gemini",
        "data_source": "gemini_web_search",
        "data_confidence": "medium",
        "_gemini_verified": True,
    }


def _call_business_discovery(handle: str, access_token: str, ig_business_id: str):
    """
    A single Business Discovery request attempt. Returns (data, None) on
    success, or (None, (error_kind, detail, exception)) on failure. Factored
    out of enrich_instagram so the rate-limit retry logic can call this
    multiple times without duplicating the request-building code.

    Includes a small proactive pacing delay before every attempt (not just
    after hitting a rate limit) — spreading requests out is what actually
    prevents bursting Meta's limit in a tight loop across many candidates;
    reactive backoff after the fact only helps once you're already over it.
    """
    time.sleep(BUSINESS_DISCOVERY_CALL_DELAY_SECONDS)
    try:
        resp = requests.get(
            f"https://graph.facebook.com/{GRAPH_API_VERSION}/{ig_business_id}",
            params={
                "fields": f"business_discovery.username({handle})"
                          "{followers_count,media_count,biography,"
                          "media.limit(5){caption,timestamp,media_type}}",
                "access_token": access_token,
            },
            timeout=15,
        )
        resp.raise_for_status()
        return resp.json().get("business_discovery"), None
    except requests.RequestException as e:
        detail = ""
        try:
            detail = e.response.json().get("error", {}).get("message", "") if e.response is not None else ""
        except (ValueError, AttributeError):
            pass
        return None, (classify_business_discovery_error(detail), detail, e)


def enrich_instagram(handle: str, serper_key: str, cross_check: bool = False,
                     skip_meta: bool = False) -> dict:
    """
    skip_meta=True bypasses the Meta Business Discovery tier entirely.
    Set this when:
      - The candidate came from a Deep Research report that already includes
        a follower count (no need to burn a Meta API call to re-fetch it).
      - The handle is a display name / full name (e.g. "Chip Leighton") rather
        than a real @username — Meta's Business Discovery API rejects these with
        "Invalid user id" every time, so calling it is guaranteed to fail and
        wastes both API quota and run time.
    Falls straight through to Serper/Tavily enrichment in both cases.
    """
    # Priority 1: licensed provider (Modash/HypeAuditor-style)
    licensed_key = os.environ.get("LICENSED_IG_API_KEY")
    if licensed_key:
        # TODO: wire up your chosen licensed IG provider's real endpoint here.
        # Left unimplemented on purpose — falls through to the tiers below
        # until you've picked a provider and filled this in.
        print("[licensed_ig] LICENSED_IG_API_KEY is set but no provider is wired up yet — falling through")

    # Priority 2: official Business Discovery API (requires your own IG Business account)
    # Skipped entirely when skip_meta=True — see docstring above.
    access_token = os.environ.get("META_ACCESS_TOKEN")
    ig_business_id = os.environ.get("IG_BUSINESS_ACCOUNT_ID")
    if not skip_meta and access_token and ig_business_id and not _business_discovery_state["disabled"]:
        now = time.monotonic()
        if now < _business_discovery_state["rate_limit_cooldown_until"]:
            # In a cooldown from sustained rate-limiting — skip Business
            # Discovery entirely for this candidate rather than burning up
            # to ~33s (3 escalating retries) on an attempt that's likely to
            # fail again immediately anyway. Falls straight to Serper/Tavily
            # below, same as any other miss.
            remaining = _business_discovery_state["rate_limit_cooldown_until"] - now
            print(f"[business_discovery] in a {remaining:.0f}s rate-limit cooldown — skipping Business "
                  f"Discovery for {handle!r}, using Serper/Tavily directly for this candidate.")
            return web_source_enrich(handle, "instagram", serper_key, cross_check=cross_check)

        data, error = _call_business_discovery(handle, access_token, ig_business_id)

        # Real backoff schedule, not one fixed retry — production showed 15+
        # candidates in a row still rate-limited after a single 3s wait,
        # which means the app is sustained over the limit, not just briefly
        # over it. Escalating delays give the rate window an actual chance
        # to clear rather than retrying at the same pace that caused the
        # problem.
        for delay in RATE_LIMIT_RETRY_DELAYS_SECONDS:
            if not (error and error[0] == "rate_limited"):
                break
            print(f"[business_discovery] rate limited for {handle!r} — waiting {delay}s and retrying "
                  f"before falling back.")
            time.sleep(delay)
            data, error = _call_business_discovery(handle, access_token, ig_business_id)

        if error and error[0] == "rate_limited":
            _business_discovery_state["consecutive_rate_limits"] += 1
            print(f"[business_discovery] still rate limited for {handle!r} after "
                  f"{len(RATE_LIMIT_RETRY_DELAYS_SECONDS)} retries — falling back to Serper/Tavily for this "
                  f"candidate only. Business Discovery stays enabled for the rest of the run (this alone isn't "
                  f"evidence the credential is broken).")
            if _business_discovery_state["consecutive_rate_limits"] >= RATE_LIMIT_COOLDOWN_THRESHOLD:
                _business_discovery_state["rate_limit_cooldown_until"] = time.monotonic() + RATE_LIMIT_COOLDOWN_SECONDS
                print(f"[business_discovery] {RATE_LIMIT_COOLDOWN_THRESHOLD} consecutive candidates exhausted "
                      f"retries while rate limited — this looks like a sustained episode, not a brief bump. "
                      f"Pausing Business Discovery entirely for {RATE_LIMIT_COOLDOWN_SECONDS}s to let the rate "
                      f"window actually clear, instead of retrying every subsequent candidate individually. "
                      f"If this keeps recurring across runs, your app's Meta rate tier may genuinely be too "
                      f"low for a SEARCH_BUDGET this large — worth checking your app's rate limit tier in Meta "
                      f"for Developers.")
        else:
            _business_discovery_state["consecutive_rate_limits"] = 0

        if error is None:
            _business_discovery_state["consecutive_systemic_failures"] = 0
            if data:
                media_items = (data.get("media") or {}).get("data", [])
                recent_captions = [m["caption"] for m in media_items if m.get("caption")]
                last_post_date = media_items[0].get("timestamp") if media_items else None
                # Business Discovery is Meta's own authoritative source — no
                # cross-check needed, this is as good as follower data gets.
                return {
                    "followers_count": data.get("followers_count"), "total_posts": data.get("media_count"),
                    "bio": data.get("biography"), "engagement_rate": None, "last_post_date": last_post_date,
                    "location_hint": None, "data_source": "business_discovery_api", "data_confidence": "high",
                    "recent_captions": recent_captions, "follower_source_quality": "verified",
                }
        else:
            error_kind, detail, e = error
            if error_kind != "rate_limited":
                # The rate-limited case already printed its own request-failed
                # line inside the retry loop above (once per attempt) — no
                # need to repeat it here, and error_kind == "rate_limited" is
                # already fully handled by the retry loop's own messaging.
                print(f"[business_discovery] request failed for {handle!r}: {e}" + (f" — {detail}" if detail else ""))

            if error_kind == "ineligible_target":
                # Normal outcome — personal/creator IG accounts are not
                # accessible via Business Discovery (which only works for
                # accounts that opted into Business/Creator mode). Not evidence
                # of anything wrong with the token; does NOT count toward the
                # circuit breaker.
                #
                # Order matters for free-tier quota: run Serper/Tavily first
                # (always free, already happening for every other miss), then
                # only call Gemini when both came back with no follower count.
                # Previously Gemini fired BEFORE Serper — wasting one Gemini
                # call per candidate that Serper would have answered anyway.
                web_result = web_source_enrich(handle, "instagram", serper_key,
                                               cross_check=cross_check)
                gemini_key = os.environ.get("GEMINI_API_KEY")
                gemini_fallback = (os.environ.get("GEMINI_VERIFICATION_FALLBACK", "false")
                                   .lower() == "true")
                if (gemini_key and gemini_fallback
                        and web_result.get("followers_count") is None
                        and _gemini_state["verify_calls"] < GEMINI_VERIFY_MAX_PER_RUN):
                    # Serper AND Tavily both found nothing — Gemini is last resort.
                    # Capped at GEMINI_VERIFY_MAX_PER_RUN to protect daily quota.
                    _gemini_state["verify_calls"] += 1
                    g_result = gemini_verify_handle(handle, "instagram", gemini_key)
                    if g_result:
                        print(f"[gemini_verify] @{handle} (IG): Serper/Tavily both empty — "
                              f"Gemini found {g_result.get('followers_count')} followers "
                              f"(probable_gemini, NOT verified).")
                        web_result["followers_count"] = g_result.get("followers_count")
                        web_result["follower_source_quality"] = "probable_gemini"
                        web_result["data_source"] = "gemini_web_search"
                        if not web_result.get("bio"):
                            web_result["bio"] = g_result.get("bio")
                        if not web_result.get("location_hint"):
                            web_result["location_hint"] = g_result.get("location_hint")
                        web_result["_gemini_verified"] = True
                return web_result  # always return here — Serper already ran above
            elif error_kind == "rate_limited":
                pass  # already logged and handled above; just fall through to Serper
            elif error_kind == "auth_failure":
                # Unambiguous, run-ending — no reason to wait for a 3rd
                # occurrence when the very first one already proves the
                # credential itself is broken.
                _business_discovery_state["disabled"] = True
                _business_discovery_state["disabled_reason"] = detail or str(e)
                print(f"[business_discovery] AUTH FAILURE detected ({detail!r}) — disabling Business Discovery "
                      f"for the rest of this run immediately. This is a credential problem (expired/invalid "
                      f"META_ACCESS_TOKEN), not a per-candidate issue — falling back to Serper/Tavily for all "
                      f"remaining candidates. Fix by generating a new long-lived Page token.")
            else:
                _business_discovery_state["consecutive_systemic_failures"] += 1
                if _business_discovery_state["consecutive_systemic_failures"] >= BUSINESS_DISCOVERY_FAILURE_THRESHOLD:
                    _business_discovery_state["disabled"] = True
                    _business_discovery_state["disabled_reason"] = detail or str(e)
                    print(f"[business_discovery] {BUSINESS_DISCOVERY_FAILURE_THRESHOLD} consecutive unexplained "
                          f"failures — disabling Business Discovery for the rest of this run and falling back to "
                          f"Serper/Tavily. Likely a systemic issue (deprecated API version or wrong "
                          f"IG_BUSINESS_ACCOUNT_ID) — check the error detail above.")

    # Priority 3: web sources, genuinely cross-checked when it matters (see
    # web_source_enrich below for what "cross_check" actually changes).
    return web_source_enrich(handle, "instagram", serper_key, cross_check=cross_check)


def verify_reported_followers_with_meta(creators: list, access_token: str, ig_business_id: str) -> None:
    """Late-stage, bounded verification pass — runs ONCE, after ranking and
    Sonnet refinement have already cut the pool down to the small,
    RESULT_LIMIT-sized set of rows that are actually about to be written to
    Master. Upgrades follower_verification from "reported" (an unconfirmed
    number from a Deep Research report) to "verified" (Meta's own data)
    wherever Business Discovery succeeds for these specific finalists.

    Deliberately NOT the same thing as calling enrich_instagram() again:
    that function cascades to Serper/Tavily/Gemini on a Meta miss, which
    would spend extra API calls (including scarce Gemini quota) re-deriving
    a number we already have a perfectly reasonable one for. A Meta miss
    here just means "keep the Deep Research report's number as-is" — no
    cascade, since the DR figure is already a fine fallback.

    Mutates each candidate dict in place. Reuses _call_business_discovery
    and the module's existing rate-limit retry schedule / circuit-breaker
    state rather than duplicating that logic.
    """
    if not access_token or not ig_business_id:
        return
    candidates_to_verify = [
        c for c in creators
        if c.get("platform") == "instagram"
        and c.get("follower_verification") == "reported"
        and " " not in (c.get("handle") or c.get("username", ""))
    ]
    if not candidates_to_verify:
        return

    print(f"[meta_verify] Verifying {len(candidates_to_verify)} DR-reported Instagram follower "
          f"count(s) against Meta Business Discovery before writing to Master (bounded to this "
          f"run's final, RESULT_LIMIT-sized set — not the full candidate pool).")

    for c in candidates_to_verify:
        if _business_discovery_state["disabled"]:
            print("[meta_verify] Business Discovery was disabled earlier this run (credential "
                  "problem) — stopping verification here; remaining candidate(s) keep their "
                  "Deep Research report numbers as-is.")
            break
        now = time.monotonic()
        if now < _business_discovery_state["rate_limit_cooldown_until"]:
            print(f"[meta_verify] in a rate-limit cooldown — skipping verification for "
                  f"{c.get('handle')!r}, keeping its Deep Research report number as-is.")
            continue

        handle = c.get("handle") or c.get("username", "")
        data, error = _call_business_discovery(handle, access_token, ig_business_id)
        for delay in RATE_LIMIT_RETRY_DELAYS_SECONDS:
            if not (error and error[0] == "rate_limited"):
                break
            time.sleep(delay)
            data, error = _call_business_discovery(handle, access_token, ig_business_id)

        if error is None and data:
            c["followers_count"] = data.get("followers_count", c.get("followers_count"))
            c["total_posts"] = data.get("media_count", c.get("total_posts"))
            c["follower_verification"] = "verified"
            c["follower_source"] = "business_discovery_api"
            c["data_confidence"] = "high"
            print(f"[meta_verify] {handle!r}: confirmed {data.get('followers_count')} followers via "
                  f"Meta — upgraded from 'reported' to 'verified'.")
        else:
            reason = error[0] if error else "unknown"
            print(f"[meta_verify] {handle!r}: Meta verification unsuccessful ({reason}) — keeping "
                  f"the Deep Research report's reported follower count as-is.")


def enrich_tiktok(handle: str, serper_key: str, cross_check: bool = False) -> dict:
    licensed_key = os.environ.get("LICENSED_TIKTOK_API_KEY")
    if licensed_key:
        # TODO: wire up your chosen licensed TikTok provider's real endpoint here.
        print("[licensed_tiktok] LICENSED_TIKTOK_API_KEY is set but no provider is wired up yet — falling through")

    result = web_source_enrich(handle, "tiktok", serper_key, cross_check=cross_check)

    # Gemini verification fallback — mirrors the Instagram ineligible_target path.
    # TikTok has no Business Discovery equivalent so web_source_enrich is always
    # the first (and normally only) data tier.  Gemini runs only when Serper AND
    # Tavily both came up empty, keeping free-tier API call count minimal.
    gemini_key = os.environ.get("GEMINI_API_KEY")
    gemini_fallback = (os.environ.get("GEMINI_VERIFICATION_FALLBACK", "false")
                       .lower() == "true")
    if (gemini_key and gemini_fallback
            and result.get("followers_count") is None
            and _gemini_state["verify_calls"] < GEMINI_VERIFY_MAX_PER_RUN):
        # Capped at GEMINI_VERIFY_MAX_PER_RUN (shared with Instagram verify calls).
        _gemini_state["verify_calls"] += 1
        g_result = gemini_verify_handle(handle, "tiktok", gemini_key)
        if g_result:
            print(f"[gemini_verify] @{handle} (TikTok): Serper/Tavily both empty — "
                  f"Gemini found {g_result.get('followers_count')} followers "
                  f"(probable_gemini, NOT verified).")
            result["followers_count"] = g_result.get("followers_count")
            result["follower_source_quality"] = "probable_gemini"
            result["data_source"] = "gemini_web_search"
            if not result.get("bio"):
                result["bio"] = g_result.get("bio")
            if not result.get("location_hint"):
                result["location_hint"] = g_result.get("location_hint")
            result["_gemini_verified"] = True

    return result


def web_source_enrich(handle: str, platform: str, serper_key: str, cross_check: bool = False) -> dict:
    """
    Replaces the old serper_snippet_enrich/tavily_follower_fallback pairing,
    which only tried Tavily as a fallback WHEN Serper found nothing — meaning
    two candidates could both show follower_verification="verified" with
    wildly different actual reliability (a number Serper happened to cache
    vs. one nobody ever double-checked), and no genuine two-source agreement
    check ever happened even when both sources were available.

    cross_check=True (the run() caller sets this when MIN_FOLLOWERS/
    MAX_FOLLOWERS is actually configured — the only situation where the
    difference between "one source says X" and "two sources agree on X"
    materially changes a filtering decision) makes this ALWAYS attempt both
    Serper and Tavily rather than treating Tavily as fallback-on-empty, and
    compares them:

      both agree      -> follower_source_quality = "verified_cross_source"
      only one present -> follower_source_quality = "probable"
      both present, disagree -> follower_source_quality = "conflicting",
                                 followers_count set to None (neither number
                                 is trusted enough to gate a hard MIN/MAX
                                 filter on) — surfaced as its own value in
                                 the sheet rather than folded into "unverified"
                                 so it's distinguishable from "no data" during
                                 manual review
      neither          -> follower_source_quality = "unverified"

    When cross_check=False (no follower range configured — the number isn't
    gating anything), Tavily is only spent as a fallback when Serper found
    nothing, same as before, to avoid doubling API cost in the common case
    where follower precision doesn't actually matter for this run.
    """
    empty = {
        "followers_count": None, "total_posts": None, "bio": None, "engagement_rate": None,
        "last_post_date": None, "location_hint": None, "data_source": "unavailable",
        "data_confidence": "manual_review_needed", "posting_frequency": "", "audience_quality_score": "",
        "follower_source_quality": "unverified",
    }

    serper_result = serper_follower_snippet(handle, platform, serper_key)
    serper_count = serper_result.get("followers_count") if serper_result else None

    need_tavily = cross_check or serper_result is None or serper_count is None
    tavily_result = tavily_follower_snippet(handle, platform) if need_tavily else None
    tavily_count = tavily_result.get("followers_count") if tavily_result else None

    if serper_count is not None and tavily_count is not None:
        if numbers_agree(serper_count, tavily_count):
            quality = "verified_cross_source"
            # Prefer the live fetch over the cached snippet when both agree
            # closely enough that it doesn't matter which exact figure is used.
            final_count = tavily_count
            confidence = "high"
        else:
            quality = "conflicting"
            final_count = None  # neither trusted alone for a hard range filter
            confidence = "low"
            print(f"[follower_verification] {handle!r} on {platform}: Serper says {serper_count}, "
                  f"Tavily says {tavily_count} — disagreement beyond tolerance, treating as unverified "
                  f"for filtering purposes rather than guessing which is right.")
    elif serper_count is not None or tavily_count is not None:
        quality = "probable"
        final_count = serper_count if serper_count is not None else tavily_count
        confidence = "medium"
    else:
        quality = "unverified"
        final_count = None
        confidence = "manual_review_needed" if not (serper_result or tavily_result) else "medium"

    bio = (serper_result or {}).get("bio") or (tavily_result or {}).get("bio")
    total_posts = (serper_result or {}).get("total_posts") or (tavily_result or {}).get("total_posts")
    sources_used = []
    if serper_result:
        sources_used.append("serper_snippet")
    if tavily_result:
        sources_used.append("tavily_profile_fetch")
    data_source = "+".join(sources_used) if sources_used else "unavailable"

    if not bio and not sources_used:
        return empty

    return {
        "followers_count": final_count, "total_posts": total_posts, "bio": bio,
        "engagement_rate": None, "last_post_date": None, "location_hint": None,
        "data_source": data_source, "data_confidence": confidence,
        "posting_frequency": "", "audience_quality_score": "",
        "follower_source_quality": quality,
    }


# ============================================================
# STAGE 5 — FILTERS
# ============================================================

def evaluate_follower_status(followers, min_f, max_f, unknown_policy: str, source_quality: str = None) -> tuple:
    """
    Returns (verdict, verification_label) where verdict is one of:
      "pass"               — keep, proceeds to scoring
      "fail"                — hard reject, follower count is known and out of range
      "needs_verification" — follower count unknown (or untrustworthy — see
                              "conflicting" below) AND a hard MIN/MAX_FOLLOWERS
                              is set; held out of Master, written to Excluded
                              instead, because a "5 accounts" result showing
                              two 0-follower pages when you asked for 100K+ is
                              the exact failure mode this exists to prevent

    verification_label reflects HOW the number was established, not just
    whether one exists:
      "verified"              — Meta Business Discovery (authoritative)
      "verified_cross_source" — two independent web sources agree
      "probable"              — one web source only, no independent confirmation
      "reported"              — Deep Research report stated the count; no live
                                 source confirmed it. Preserved via
                                 follower_source_quality="reported" set in the
                                 DR backfill block — without that, this function
                                 would receive source_quality="unverified" (from
                                 web_source_enrich's empty dict) and overwrite
                                 follower_verification back to "unverified".
                                 Scored as neutral by compute_creator_quality_score
                                 (no bonus, no penalty — see that function).
      "conflicting"           — two sources found DIFFERENT numbers; followers
                                 will be None here (see web_source_enrich), so
                                 this always routes through the "followers is
                                 None" branch below like any other unknown
      "unverified"            — no source produced a usable number
      "not_applicable"        — no MIN/MAX_FOLLOWERS was requested this run,
                                 so follower count isn't gating anything

    source_quality (from enrichment) overrides the blanket "verified" that a
    non-null followers_count used to get automatically — a number Business
    Discovery returned and a number a single unconfirmed snippet happened to
    contain are NOT equally trustworthy, and treating them identically was
    the actual gap the "aidanlovesfitness vs mrderrickbrownie" comparison
    was pointing at.
    """
    if followers is not None:
        verified_ok = True
        if min_f and followers < min_f:
            verified_ok = False
        if max_f and followers > max_f:
            verified_ok = False
        label = source_quality or "verified"
        return ("pass" if verified_ok else "fail"), label

    # followers is None — either genuinely no data, or a "conflicting" cross-check
    # (source_quality carries that distinction through even though followers is None).
    label_when_unknown = source_quality if source_quality == "conflicting" else "unverified"
    if (min_f or max_f) and unknown_policy != "include":
        return "needs_verification", label_when_unknown
    return "pass", ("not_applicable" if not (min_f or max_f) else label_when_unknown)


def activity_status(last_post_date: str, cutoff_days: int) -> str:
    if not last_post_date:
        return "unverified"
    try:
        posted = datetime.fromisoformat(last_post_date.replace("Z", "+00:00"))
        if posted.tzinfo is None:
            posted = posted.replace(tzinfo=timezone.utc)
    except (ValueError, AttributeError):
        return "unverified"
    age_days = (datetime.now(timezone.utc) - posted).days
    return "active" if age_days <= cutoff_days else "stale"


RESERVED_PLATFORM_HANDLES = {
    "discover", "popular", "business", "us", "explore", "help", "support",
    "about", "login", "signup", "terms", "privacy", "home", "search",
    "trending", "live", "shop", "store", "creator", "creators", "official",
    # Added after a real run sent these to Claude unnecessarily — they
    # aren't in TikTok's reserved-namespace list but behave the same way
    # in practice: generic single-word handles with no personal content.
    "music", "ph", "foryou", "fyp", "video", "tag",
}

# Common aliases for country-level location matching. Deliberately modest —
# covers the countries most likely to come up for this kind of run, not a
# full geo-database. Add to this if you're regularly targeting a country not
# listed here rather than expecting it to fuzzy-match.
COUNTRY_ALIASES = {
    "usa": "united states", "us": "united states", "u.s.": "united states",
    "u.s.a.": "united states", "america": "united states", "united states of america": "united states",
    "uk": "united kingdom", "britain": "united kingdom", "great britain": "united kingdom",
    "uae": "united arab emirates",
}


def normalize_country(text: str) -> str:
    text = (text or "").strip().lower()
    return COUNTRY_ALIASES.get(text, text)


def location_target_conflicts(target_location: str, classified_country: str, location_verified: bool) -> bool:
    """
    True when the classified country doesn't match the target. Two bugs
    fixed here, both found directly from production data (a run where
    Claude confidently classified candidates as being in Germany, Canada,
    India, and South Africa against a USA-only target, and every single one
    of them sailed through this gate untouched — total_rejected_location was
    0 for the entire run):

    1. `known_countries` used to be a tiny hardcoded whitelist (US/UK/UAE/
       Canada/Australia/India) — Germany and South Africa, both actually
       observed in production, weren't in it, so the gate silently declined
       to fire even when both target and classified country were perfectly
       legitimate, spelled-out country names. Fixed by removing the
       whitelist requirement entirely: any two non-empty, normalized country
       strings that differ now count as a conflict, rather than requiring
       both to appear in a short hardcoded list.

    2. Requiring `location_verified` on top of a non-empty `classified_country`
       was redundant given classify_creator's own prompt instruction: "leave
       city/country/brand_affinity_note empty rather than inventing a signal
       that isn't there." A non-empty country field is already Claude
       declining to guess — demanding a SEPARATE boolean also be true before
       trusting that field is stricter than the classifier's own standard
       for stating it in the first place. Dropped as a hard requirement;
       still logged/available on the row for context, just not gating.

    Still only catches country-vs-country mismatches, not city/region-level
    targets, where "conflict" is much fuzzier to define, and still returns
    False on an empty classified_country ("unknown" is not "wrong" — that
    stays handled by the softer location_match weight instead of a hard gate).
    """
    if not classified_country:
        return False
    target_norm = normalize_country(target_location)
    classified_norm = normalize_country(classified_country)
    if not target_norm or not classified_norm:
        return False
    return target_norm != classified_norm


def deterministic_candidate_check(candidate: dict, extra_exclude_terms: list = None) -> tuple:
    """
    Return (keep, relevance_score, reason) without an LLM call.

    extra_exclude_terms: user-typed EXCLUDE input, split into individual
    terms. Previously this input was read into config and never actually
    used anywhere — typing "lingerie, kids, retailer" into the Exclude
    field silently did nothing. Fixed: these now reject a candidate the
    same way the hardcoded OBVIOUS_NON_CREATOR_PATTERNS do, cheaply, before
    any Claude call is spent on them.
    """
    handle_lower = str(candidate.get("handle") or "").strip().lower()
    if handle_lower in RESERVED_PLATFORM_HANDLES:
        return False, -100, f"reserved platform handle: @{handle_lower}"

    text = " ".join(str(candidate.get(k) or "") for k in ("handle", "snippet", "bio")).lower()
    for pattern in OBVIOUS_NON_CREATOR_PATTERNS:
        if re.search(pattern, text, re.IGNORECASE):
            return False, -100, f"deterministic exclusion: {pattern}"

    for term in (extra_exclude_terms or []):
        term = term.strip().lower()
        if not term:
            continue
        # Word-boundary match, not raw substring. A raw "in" check means a
        # single-word EXCLUDE term like "brands" (a common one — see the
        # default EXCLUDE examples in the README) matches inside a real
        # creator's own bio text too, e.g. "I've worked with great brands"
        # or "favorite brands" — someone TALKING ABOUT brands is not the
        # same as being one. \b still isn't perfect (doesn't understand
        # context, just word edges), but it stops the more obvious class of
        # false positive: partial-word matches and substrings inside an
        # unrelated larger word.
        if re.search(rf"\b{re.escape(term)}\b", text):
            return False, -100, f"user exclude match: {term!r}"

    # Rank rather than reject positive matches: sparse snippets must not be
    # treated as proof of irrelevance, but stronger evidence gets scarce LLM
    # capacity first.
    positive_terms = ("dad", "father", "family", "fitness", "gym", "recovery", "sauna",
                      "menswear", "men's fashion", "lifestyle", "grooming", "travel", "beach", "pool")
    score = sum(1 for term in positive_terms if term in text)
    return True, score, ""


def validate_classification(scores: dict) -> dict:
    """Normalize untrusted model JSON into the narrow schema the pipeline uses."""
    defaults = {
        "account_type": "unknown", "account_type_confidence": "low",
        "product_fit_score": 0, "content_opportunity_score": 0, "niche_match": 0,
        "audience_match": 0, "location_match": 0, "location_verified": False,
        "content_angle": "Insufficient verified evidence — review manually.",
        "content_angle_strength": 0, "brand_affinity_note": "",
        "fit_explanation": "Insufficient verified evidence — review manually.",
        "gender_inferred": "unclear", "gender_confidence": "low", "city": "", "country": "",
    }
    if not isinstance(scores, dict):
        return defaults
    result = dict(defaults)
    result.update({k: scores[k] for k in defaults if k in scores})
    result["account_type"] = str(result["account_type"]).strip().lower()
    if result["account_type"] not in {"creator", "brand", "retailer", "reseller", "media", "organization", "community", "unknown"}:
        result["account_type"] = "unknown"
    for key in ("product_fit_score", "content_opportunity_score", "niche_match", "audience_match",
                "location_match", "content_angle_strength"):
        try:
            result[key] = max(0, min(10, int(float(result[key]))))
        except (TypeError, ValueError):
            result[key] = 0
    result["location_verified"] = bool(result["location_verified"])
    return result


# ============================================================
# STAGE 6 — CLASSIFICATION (Haiku 4.5)
# ============================================================

def classify_creator(creator: dict, niche: str, location: str, brand_context: dict,
                      exclusion_signals: list = None) -> dict:
    bio = creator.get("bio") or "(no bio available)"
    exclusion_block = ""
    if exclusion_signals:
        exclusion_block = ("\nKnown signals that this is the WRONG kind of result even if the "
                            f"niche keyword matches: {'; '.join(exclusion_signals)}\n")
    buyer_block = ""
    if brand_context.get("target_buyer") or brand_context.get("use_cases"):
        buyer_block = (f"\nTarget buyer: {brand_context.get('target_buyer', 'N/A')}"
                        f"\nProduct use cases / moments: {brand_context.get('use_cases', 'N/A')}\n")

    # Recent post captions (Instagram, when Business Discovery is configured
    # and succeeds) are real content evidence — a meaningfully different input
    # than judging fit off a bio alone. When present, the model is instructed
    # to ground content_opportunity_score in what's actually there rather than
    # what the account "could plausibly" post based on category/bio alone.
    captions = creator.get("recent_captions") or []
    captions_block = ""
    if captions:
        numbered = "\n".join(f"  {i+1}. {c[:200]}" for i, c in enumerate(captions))
        captions_block = f"\nRecent post captions (real content evidence, not inferred):\n{numbered}\n"

    # Deep Research report evidence — pre-researched by a human analyst using
    # tools like Modash, Collabstr, Influencer-Hero. This is more reliable than
    # bio inference alone. When specific and concrete, let it lift scores;
    # when vague or generic, weight it lightly.
    dr_block = ""
    dr_evidence = []
    if creator.get("dr_why_they_fit"):
        dr_evidence.append(f"Brand fit rationale (analyst): {creator['dr_why_they_fit']}")
    if creator.get("dr_content_opportunity"):
        dr_evidence.append(f"Specific content opportunity: {creator['dr_content_opportunity']}")
    if creator.get("dr_use_cases"):
        dr_evidence.append(f"Relevant product use cases: {creator['dr_use_cases']}")
    if creator.get("dr_partnership_evidence"):
        dr_evidence.append(f"Partnership evidence: {creator['dr_partnership_evidence']}")
    if creator.get("dr_audience_gender"):
        dr_evidence.append(f"Audience gender split: {creator['dr_audience_gender']}")
    if creator.get("dr_engagement_rate") is not None:
        dr_evidence.append(f"Engagement rate: {creator['dr_engagement_rate']}%")
    if creator.get("dr_avg_reel_views") is not None:
        arv = creator["dr_avg_reel_views"]
        arv_str = (f"{arv/1_000_000:.1f}M" if arv >= 1_000_000
                   else f"{arv/1_000:.1f}K" if arv >= 1_000 else str(arv))
        dr_evidence.append(f"Avg Reel views: {arv_str}")
    if creator.get("dr_follower_growth"):
        dr_evidence.append(f"Follower growth: {creator['dr_follower_growth']}")
    if creator.get("dr_concerns"):
        dr_evidence.append(f"Potential concerns: {creator['dr_concerns']}")
    if dr_evidence:
        dr_block = (
            "\nPre-researched evidence from Deep Research report (human analyst, "
            "treat as reliable prior knowledge — specific and concrete evidence here "
            "should raise content_opportunity_score and product_fit_score confidence; "
            "vague or generic notes should have less impact):\n"
            + "\n".join(f"  • {e}" for e in dr_evidence) + "\n"
        )

    prompt = f"""You're evaluating a social media account for influencer outreach fit.

The core question is NOT "is this account in the {niche} niche" — it's "could this brand's
product plausibly and naturally show up in this account's content," which includes creators
in ADJACENT categories with no literal keyword overlap (e.g. a fitness/recovery creator can
be a strong fit for a comfort-apparel brand via a post-workout or sauna moment, without ever
posting about "loungewear").

Target niche: {niche}
Target location: {location}
Brand: {brand_context.get('product_summary', 'N/A')} | Tone: {brand_context.get('brand_tone', 'N/A')} | Audience: {brand_context.get('audience_signals', 'N/A')}
{buyer_block}{exclusion_block}
Creator data:
Platform: {creator.get('platform')}
Bio: {bio}
Followers: {creator.get('followers_count', 'unknown')} (verification: {creator.get('follower_verification', 'not_applicable')} — "verified"/"verified_cross_source" means confirmed by Meta or two independent sources; "probable" means one unconfirmed source, treat as a rough estimate not a fact; "unverified"/"conflicting" means don't treat this number as established at all)
Total posts: {creator.get('total_posts', 'unknown')}
Location hint: {creator.get('location_hint', 'none')}
Discovery lane this candidate was found through: {creator.get('matched_lane') or creator.get('matched_archetype') or 'niche keyword search'}
{captions_block}{dr_block}
Return ONLY valid JSON, no preamble, no markdown fences:
{{
  "account_type": "<one of: creator, brand, retailer, reseller, media, organization, community, unknown>",
  "account_type_confidence": "<high|medium|low>",
  "product_fit_score": <0-10 int, could this SPECIFIC product plausibly and naturally
     integrate into this creator's content, including via an adjacent category, not just
     a literal niche match — this is the primary fit signal, weight it accordingly>,
  "content_opportunity_score": <0-10 int, are there CONCRETE, specific moments/scenes in
     this creator's actual content type where the product would show up organically
     (e.g. "post-workout recovery clips", "morning routine videos") — 0 if you can't name
     a real one, not a generic "could work for any lifestyle creator". If recent captions
     are provided above, ground this score in what they actually show, not what the
     account's category/bio suggests they might post>,
  "audience_match": <0-10 int, does their follower base resemble the brand's buyer —
     base this on bio/content signals primarily; if the follower COUNT is unverified
     or conflicting, don't let an unconfirmed number itself drive this score either
     direction, since it may simply be wrong>,
  "niche_match": <0-10 int, literal content-category overlap with the stated niche —
     informational only, not the primary fit signal>,
  "location_match": <0-10 int, regional relevance>,
  "location_verified": <true|false — true only if the bio/location hint actually states or
     strongly implies the location; false if you're guessing from the search query alone>,
  "content_angle": "<one specific sentence: the natural, real-life moment/setting where
     this product could organically appear in THIS creator's content — this is the
     evidence behind content_opportunity_score, not a generic restatement of their niche>",
  "content_angle_strength": <0-10 int, how genuinely natural that fit is — informational,
     folded into content_opportunity_score above>,
  "brand_affinity_note": "<one sentence, soft inference only; empty string if no real signal>",
  "fit_explanation": "<1-2 sentences: WHY this creator specifically, referencing the actual
     content angle — write this as the answer to 'why would we reach out to this person',
     not a restatement of the numeric scores>",
  "gender_inferred": "<male|female|unclear>",
  "gender_confidence": "<high|medium>",
  "city": "<best-guess city, empty string if not statable>",
  "country": "<best-guess country, empty string if not statable>"
}}

account_type is the single most important field here: a business/store/retailer/reseller
page should get account_type set accordingly EVEN IF its content matches the niche keyword —
that mismatch (e.g. a pajama retailer showing up under a "loungewear" search) is exactly the
kind of result this field exists to catch. Only use "creator" for an account that is an
individual or persona posting their own content, not a storefront or brand account.
This includes MEDIA/PUBLICATION accounts specifically — a magazine, blog, or editorial brand
(e.g. an account named after or operating as a well-known publication, posting curated/
aggregated content rather than one person's personal life) is account_type "media", not
"creator", even if it has a large engaged following and posts in the right niche. The test:
does this read as ONE PERSON's life/persona, or as a branded content operation/outlet? A large
follower count and topically-relevant posts are not evidence of being an individual creator —
publications have both of those too.

gender_inferred must be based on actual evidence: the bio stating a name, pronouns, or explicit
self-description ("dad", "husband", "she/her"), not on what audience the content style seems to
"appeal to". A fitness/lifestyle content style appealing to men is NOT evidence the creator is a
man — plenty of female creators produce content that reads as appealing to a male audience. If
the only signal is content style/tone with no actual name/pronoun/self-description evidence, set
gender_inferred to "unclear" rather than guessing, even if a guess feels intuitively likely.

product_fit_score and content_opportunity_score deserve the most thought: a creator can score
LOW on niche_match and HIGH on these if their content creates a real integration moment for
the product through an adjacent angle. The reverse can also happen — literal niche overlap
with no plausible natural moment for the product should score product_fit_score low despite a
high niche_match. Don't let a high niche_match inflate these if you can't articulate a real
moment.

location_match: score this on the EVIDENCE, not the search query that found them. If you
can't actually tell where they're based, set location_verified to false and score location_match
low-to-mid (2-4) rather than defaulting to a neutral 5 — an unverified location shouldn't look
identical to a confirmed one in the data.

Score conservatively when the bio is sparse. Leave city/country/brand_affinity_note
empty rather than inventing a signal that isn't there."""

    client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    response = client.messages.create(
        model=HAIKU_MODEL, max_tokens=1280,
        messages=[{"role": "user", "content": prompt}],
    )
    text = extract_claude_text(response)
    parse_failed = False
    try:
        scores = extract_json_object(text)
    except json.JSONDecodeError:
        print(f"[classification] Failed to parse Claude JSON for {creator.get('handle')}: {text}")
        parse_failed = True
        scores = {
            "account_type": "unknown", "account_type_confidence": "low",
            "product_fit_score": 5, "content_opportunity_score": 5,
            "niche_match": 5, "audience_match": 5, "location_match": 3, "location_verified": False,
            "content_angle": "Could not be automatically assessed — review manually.",
            "content_angle_strength": 5, "brand_affinity_note": "",
            "fit_explanation": "Could not be automatically scored — review manually.",
            "gender_inferred": "unclear", "gender_confidence": "medium", "city": "", "country": "",
        }
    result = {**creator, **validate_classification(scores)}
    # validate_classification only copies recognized schema keys, so this
    # flag has to be attached after, not passed through `scores` — otherwise
    # a parse failure would be completely invisible in the run log, exactly
    # the gap that made "0 written to Master" impossible to diagnose without
    # manually grep-ing the Action logs for "Failed to parse".
    result["_llm_parse_failed"] = parse_failed
    return result


def refine_creator_with_sonnet(creator: dict, niche: str, location: str, brand_context: dict,
                                exclusion_signals: list = None) -> dict:
    """
    Second, more critical pass — same evidence Haiku already saw, but a
    stronger model explicitly instructed to distinguish "plausible fit" from
    "proven fit" rather than just re-deriving the same scores. Only called on
    a small, bounded pool of near-finalists (see run()), not every candidate
    Haiku classified — this is a precision pass on people already competing
    for the final list, not a wholesale model swap.

    Returns the creator dict with scores REPLACED (not blended) by Sonnet's
    judgment where Sonnet responds, since the point is a more careful second
    opinion superseding the first, not an average of a careful and a rough one.
    On any parse failure, the original Haiku scores are left untouched rather
    than overwritten with something worse.
    """
    bio = creator.get("bio") or "(no bio available)"
    exclusion_block = ""
    if exclusion_signals:
        exclusion_block = ("\nKnown signals that this is the WRONG kind of result even if the "
                            f"niche keyword matches: {'; '.join(exclusion_signals)}\n")
    buyer_block = ""
    if brand_context.get("target_buyer") or brand_context.get("use_cases"):
        buyer_block = (f"\nTarget buyer: {brand_context.get('target_buyer', 'N/A')}"
                        f"\nProduct use cases / moments: {brand_context.get('use_cases', 'N/A')}\n")
    captions = creator.get("recent_captions") or []
    captions_block = ""
    if captions:
        numbered = "\n".join(f"  {i+1}. {c[:200]}" for i, c in enumerate(captions))
        captions_block = f"\nRecent post captions (real content evidence, not inferred):\n{numbered}\n"
    else:
        captions_block = ("\nNo recent post captions available — you're working from bio and metadata only. "
                           "Be more conservative on content_opportunity_score than you would with real "
                           "content evidence; a bio-only guess is not proof of an actual content pattern.\n")

    prompt = f"""A faster model already scored this candidate for influencer outreach fit. Your job is a
SECOND, MORE CRITICAL pass — not to re-derive the same scores, but to specifically catch the gap between
"this creator PLAUSIBLY fits" (category/bio/audience alignment) and "this creator has PROVEN, VISIBLE
content opportunities" (actual demonstrated moments in their real content where the product would appear).

Those are genuinely different claims. A creator can have excellent audience alignment and a somewhat
generic or unrelated actual posting pattern — don't let strong audience/category fit inflate
content_opportunity_score if the evidence for an actual content moment is thin or absent.

Target niche: {niche}
Target location: {location}
Brand: {brand_context.get('product_summary', 'N/A')} | Tone: {brand_context.get('brand_tone', 'N/A')} | Audience: {brand_context.get('audience_signals', 'N/A')}
{buyer_block}{exclusion_block}
Creator data:
Platform: {creator.get('platform')}
Bio: {bio}
Followers: {creator.get('followers_count', 'unknown')} (verification: {creator.get('follower_verification', 'not_applicable')})
Discovery lane: {creator.get('matched_lane') or creator.get('matched_archetype') or 'niche keyword search'}
{captions_block}
The first-pass model gave this candidate:
  product_fit_score: {creator.get('product_fit_score')}
  content_opportunity_score: {creator.get('content_opportunity_score')}
  audience_match: {creator.get('audience_match')}
  content_angle: {creator.get('content_angle')}
  fit_explanation: {creator.get('fit_explanation')}

Return ONLY valid JSON, no preamble, no markdown fences:
{{
  "product_fit_score": <0-10 int, your own judgment, not a rubber-stamp of the first pass>,
  "content_opportunity_score": <0-10 int — the score to scrutinize hardest, and the one most likely to be
     wrong if you just pick a "safe" middle value. Use the full low end deliberately:
       0 = literally nothing in the evidence suggests any related content — no lifestyle, no routine, no
           relevant setting at all
       1 = one tenuous, indirect signal at most (e.g. a single word in the bio, no actual content backing it)
       2 = some weak but real evidence exists (e.g. a lifestyle-adjacent post that isn't quite the right
           moment) — NOT the default answer just because evidence is thin; only use 2 if you can name that
           specific weak signal
       3-5 = a plausible but not fully demonstrated pattern
       6+ = an actual, nameable, evidenced content moment
     Don't default to 2 just because it feels like the "safe, honest" answer when evidence is thin — that's
     exactly the failure mode this scrutiny is meant to catch. Pick 0 or 1 when the evidence genuinely
     supports that instead.>,
  "audience_match": <0-10 int>,
  "content_angle": "<one specific sentence grounded in what you can actually point to as evidence>",
  "fit_explanation": "<1-2 sentences: your independent judgment on why (or why not) this is a strong
     outreach candidate, naming explicitly if you're revising the first pass's assessment and why>",
  "outreach_readiness": "<one of: strong, promising_needs_review, weak — 'strong' means proven content fit
     AND verified follower data AND active; 'promising_needs_review' means plausible fit but thin content
     evidence or unverified data; 'weak' means you would not prioritize this candidate>"
}}"""

    client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    try:
        response = client.messages.create(
            model=SONNET_MODEL, max_tokens=1024,
            messages=[{"role": "user", "content": prompt}],
        )
        text = extract_claude_text(response)
        refined = extract_json_object(text)
    except (anthropic.APIError, json.JSONDecodeError, KeyError) as e:
        print(f"[sonnet_refinement] Failed for {creator.get('handle')}: {e} — keeping first-pass scores.")
        failed = dict(creator)
        failed["_sonnet_parse_failed"] = True
        return failed

    updated = dict(creator)
    for key in ("product_fit_score", "content_opportunity_score", "audience_match"):
        try:
            updated[key] = max(0, min(10, int(float(refined[key]))))
        except (KeyError, TypeError, ValueError):
            pass  # keep the original Haiku value for this one field rather than drop it
    if refined.get("content_angle"):
        updated["content_angle"] = str(refined["content_angle"])
    if refined.get("fit_explanation"):
        updated["fit_explanation"] = str(refined["fit_explanation"])
    updated["outreach_readiness"] = refined.get("outreach_readiness", "")
    updated["sonnet_refined"] = True
    return updated


def compute_creator_quality_score(creator: dict, follower_range_requested: bool = False) -> int:
    """
    Deterministic (not LLM) account-credibility score, separate from product/
    audience fit — an active, verified account is more valuable to contact than
    an equally "on-brief" one that's abandoned or unconfirmed, and that
    shouldn't require spending a Claude judgment call to establish. Combines
    signals already gathered elsewhere in the pipeline: posting activity,
    enrichment data confidence, and whether the follower count was confirmed.

    follower_range_requested: when a MIN/MAX_FOLLOWERS was actually set for
    this run, an unconfirmed follower count represents real unmet-requirement
    risk (you asked for 50K-250K and this candidate might be 500 or 5M for
    all the data shows) and gets penalized accordingly — not just "no bonus"
    as before, which let unverified-follower candidates rank identically to
    verified ones and compete for Master on equal footing. When no range was
    requested, an unknown follower count is irrelevant to what was asked for,
    so no penalty applies.
    """
    score = 5
    activity = creator.get("activity_status")
    if activity == "active":
        score += 3
    elif activity == "stale":
        score -= 2
    # "unverified" activity: no adjustment, we simply don't know

    confidence = creator.get("data_confidence")
    if confidence == "high":
        score += 2
    elif confidence in ("manual_review_needed", "low"):
        score -= 2

    # Follower verification now has more than two states (see
    # evaluate_follower_status), and they're not equally trustworthy even
    # when a number is present: Meta's own data and two independent web
    # sources agreeing both earn the same bonus, a single unconfirmed source
    # earns neither bonus nor penalty, and "conflicting" (two sources gave
    # different numbers) is worse than plain "unverified" — it's actively
    # contradictory data, not just an absence of data.
    verification = creator.get("follower_verification")
    if verification in ("verified", "verified_cross_source"):
        score += 1
    elif verification == "reported":
        # Deep Research report stated the count; no live independent source
        # confirmed it. Explicitly neutral: no bonus, but critically NO penalty
        # even when a follower range is set. A human researcher noted this
        # figure, so it's meaningfully better than a completely unknown count
        # (which earns -2 when follower_range_requested). Without this explicit
        # branch, "reported" would fall through to the unverified penalty branch
        # if the elif order ever changed — making this intent-preserving.
        pass
    elif verification == "conflicting":
        score -= 1
    elif verification == "unverified" and follower_range_requested:
        score -= 2
    # "probable" / "probable_gemini" (single unconfirmed source — one web
    # snippet or Gemini search result respectively): neutral, no bonus or
    # penalty either way. Both are treated identically here.

    return max(0, min(10, score))


# ============================================================
# STAGE 6.5 — PARTNERSHIP SIGNAL + WEIGHTED FIT SCORE
# ============================================================

def detect_partnership_signal(bio_text: str) -> dict:
    if not bio_text:
        return {"partnership_signal_score": None, "partnership_signal_matched": ""}
    matched, strong_hit = [], False
    for pattern in PARTNERSHIP_PATTERNS:
        m = re.search(pattern, bio_text, re.IGNORECASE)
        if m:
            matched.append(m.group(0))
            if pattern in STRONG_PARTNERSHIP_PATTERNS:
                strong_hit = True
    if not matched:
        return {"partnership_signal_score": 0, "partnership_signal_matched": ""}
    return {"partnership_signal_score": 10 if strong_hit else 5, "partnership_signal_matched": ", ".join(matched[:3])}


def compute_overall_fit(scores: dict, weights: dict) -> float:
    available = {k: (scores.get(k), weights.get(k, 0)) for k in weights if scores.get(k) is not None}
    if not available:
        return 5.0
    total_weight = sum(w for _, w in available.values())
    if total_weight == 0:
        return 5.0
    weighted_sum = sum(score * w for score, w in available.values())
    return round(weighted_sum / total_weight, 1)


def extract_contact_info(bio_text: str, linktree_text: str = "") -> dict:
    def extract_email(text):
        m = EMAIL_RE.search(text) if text else None
        return m.group(0) if m else None

    def extract_phone(text):
        if not text:
            return None
        lower = text.lower()
        if not any(w in lower for w in BUSINESS_CONTEXT_WORDS):
            return None
        m = PHONE_RE.search(text)
        if m:
            digits = re.sub(r"\D", "", m.group(0))
            if 7 <= len(digits) <= 15:
                return m.group(0).strip()
        return None

    email = extract_email(bio_text) or extract_email(linktree_text)
    email_source = "bio" if extract_email(bio_text) else ("linktree" if email else None)
    phone = extract_phone(bio_text) or extract_phone(linktree_text)
    phone_source = "bio" if extract_phone(bio_text) else ("linktree" if phone else None)

    return {"contact_email": email, "contact_phone": phone, "contact_source": email_source or phone_source}


# ============================================================
# STAGE 8 — GOOGLE SHEETS
# ============================================================

def sanitize_tab_name(name: str) -> str:
    cleaned = re.sub(r"[\[\]\*\?/\\:]", "-", name.strip())
    return cleaned[:100] if cleaned else "Untitled"


def column_letter(index: int) -> str:
    value, result = index, ""
    while value:
        value, remainder = divmod(value - 1, 26)
        result = chr(65 + remainder) + result
    return result


def ensure_tab_headers(ws, required_headers: list) -> list:
    """Append missing columns without moving existing data.

    Older sheets created by previous releases have fewer columns.  Reordering
    their existing header row would corrupt historic values, so new fields are
    appended and every new row is written by the sheet's *actual* header order.
    """
    actual = with_backoff(ws.row_values, 1)
    if not actual:
        with_backoff(ws.append_row, required_headers)
        return list(required_headers)
    missing = [h for h in required_headers if h not in actual]
    if missing:
        start = column_letter(len(actual) + 1)
        with_backoff(ws.update, f"{start}1", [missing])
        actual.extend(missing)
        print(f"[sheets] Added {len(missing)} missing column(s) to '{ws.title}' without moving existing data.")
    return actual


def get_or_create_tab(sheet, name: str, headers: list):
    try:
        ws = sheet.worksheet(name)
    except gspread.WorksheetNotFound:
        ws = sheet.add_worksheet(title=name, rows=2000, cols=len(headers) + 2)
        with_backoff(ws.append_row, headers)
    ensure_tab_headers(ws, headers)
    return ws


def load_master_keys(master_ws) -> set:
    """Returns (dedup_key, Campaign) tuples, not bare dedup_keys — the same
    account can be a genuine candidate for two different campaigns, each
    with its own scores/review/outreach state, so "already known" has to
    be checked per campaign, not per account. Rows written before Campaign
    existed have "" for that slot, which just means they'll never match a
    real (key, campaign) tuple going forward — harmless, not an error."""
    rows = with_backoff(master_ws.get_all_records)
    return {(r["dedup_key"], r.get("Campaign", "")) for r in rows if r.get("dedup_key")}


def load_master_index(master_ws) -> dict:
    """Indexed by (dedup_key, Campaign) — see load_master_keys() for why."""
    rows = with_backoff(master_ws.get_all_values)
    if not rows:
        return {}
    header = rows[0]
    key_idx = header.index("dedup_key")
    campaign_idx = header.index("Campaign")
    niche_idxs = [header.index(c) for c in NICHE_COLS]
    niche_col_start = niche_idxs[0] + 1

    index = {}
    for i, row in enumerate(rows[1:], start=2):
        if len(row) <= key_idx or not row[key_idx]:
            continue
        campaign_val = row[campaign_idx] if campaign_idx < len(row) else ""
        niches = [row[j] if j < len(row) else "" for j in niche_idxs]
        index[(row[key_idx], campaign_val)] = {
            "row_num": i, "niches": niches, "niche_col_start": niche_col_start,
        }
    return index


def fill_next_niche_slot(master_ws, entry: dict, new_niche: str):
    niches = entry["niches"]
    if new_niche in niches:
        return
    for i, val in enumerate(niches):
        if not val:
            col = entry["niche_col_start"] + i
            with_backoff(master_ws.update_cell, entry["row_num"], col, new_niche)
            return
    print(f"[sheets] Row {entry['row_num']} already has 3 niches — '{new_niche}' not added, review manually.")


def build_master_row(c: dict, primary_niche: str, headers: list = None) -> list:
    row = []
    for h in headers or MASTER_HEADERS:
        if h == "Niche 1":
            row.append(primary_niche)
        elif h in ("Niche 2", "Niche 3"):
            # Use DR-provided niche labels when available (pre-populated from the
            # Deep Research report in the enrichment loop). Defaults to empty string
            # for non-DR candidates so existing behaviour is unchanged.
            row.append(c.get(h, ""))
        else:
            row.append(c.get(h, ""))
    return row


def write_batch(sheet, master_ws, sector_name: str, creators: list, sector_label: str, campaign: str):
    sector_ws = get_or_create_tab(sheet, sanitize_tab_name(sector_name), SECTOR_HEADERS)
    master_headers = ensure_tab_headers(master_ws, MASTER_HEADERS)
    sector_headers = ensure_tab_headers(sector_ws, SECTOR_HEADERS)
    master_index = load_master_index(master_ws)

    new_master_rows, new_sector_rows = [], []
    for c in creators:
        c["Campaign"] = campaign
        key = (c["dedup_key"], campaign)
        c["date_added"] = c.get("date_added") or datetime.now(timezone.utc).date().isoformat()
        new_sector_rows.append([c.get(h, "") for h in sector_headers])

        if key not in master_index:
            new_master_rows.append(build_master_row(c, primary_niche=sector_label, headers=master_headers))
        else:
            fill_next_niche_slot(master_ws, master_index[key], sector_label)

    if new_sector_rows:
        with_backoff(sector_ws.append_rows, new_sector_rows)
    if new_master_rows:
        with_backoff(master_ws.append_rows, new_master_rows)

    print(f"[sheets] Wrote {len(new_sector_rows)} rows to '{sanitize_tab_name(sector_name)}', "
          f"{len(new_master_rows)} new rows to Master.")
    print(f"[sheets] Master tab direct link: https://docs.google.com/spreadsheets/d/{sheet.id}/edit#gid={master_ws.id}")


def write_excluded(sheet, excluded: list, campaign: str):
    """
    Rows that were found but didn't make Master — either the wrong account
    type (business/retailer/etc.), a follower count that couldn't be
    verified/failed a hard MIN/MAX_FOLLOWERS, a confirmed location mismatch,
    or a deterministic keyword exclusion. Written here instead of just
    dropped so a run that returns fewer results than requested is legible:
    you can see what was found and rejected, and why, rather than wondering
    if discovery just failed silently.
    """
    if not excluded:
        return
    for c in excluded:
        c["Campaign"] = campaign
    ws = get_or_create_tab(sheet, "Excluded", EXCLUDED_HEADERS)
    headers = ensure_tab_headers(ws, EXCLUDED_HEADERS)
    rows = [[c.get(h, "") for h in headers] for c in excluded]
    with_backoff(ws.append_rows, rows)
    print(f"[sheets] Wrote {len(rows)} row(s) to 'Excluded' (review manually if needed).")
    print(f"[sheets] Excluded tab direct link: https://docs.google.com/spreadsheets/d/{sheet.id}/edit#gid={ws.id}")


def log_run(run_log_ws, summary: dict):
    with_backoff(run_log_ws.append_row, [summary.get(h, "") for h in RUN_LOG_HEADERS])


# ============================================================
# MAIN
# ============================================================

def run():
    cfg = get_config()
    print(f"[run] Starting: niche={cfg['niche']!r} location={cfg['location']!r} "
          f"platform={cfg['platform']!r} gender={cfg['target_gender']!r} "
          f"result_limit={cfg['result_limit']} search_budget={cfg['search_budget']} "
          f"competitors={cfg['competitor_brands']}")

    serper_key = os.environ.get("SERPER_API_KEY", "")

    scraped_context = summarize_brand_context(cfg["brand_website"]) if cfg["brand_website"] else {}
    brand_context = build_brand_context(cfg, scraped_context)

    fully_manual = bool(cfg["manual_terms"] and cfg["manual_hashtags"] and cfg["manual_archetypes"])

    if fully_manual:
        # All three categories typed by hand — skip the Claude expansion call
        # entirely rather than pay for output we're going to discard anyway.
        niche_variants = cfg["manual_terms"]
        hashtags = cfg["manual_hashtags"]
        search_lanes = [{"lane": "Manual", "priority": 3, "archetypes": cfg["manual_archetypes"]}]
        location_variants = [cfg["location"]]
        exclusion_signals = cfg["exclude_terms"]
        print("[run] SEARCH_VOCABULARY fully specified — skipping Claude expansion call, "
              "using your terms/hashtags/archetypes directly.")
    else:
        expansion = expand_niche_and_location(cfg["niche"], cfg["location"], brand_context)
        niche_variants = cfg["manual_terms"] or expansion.get("niche_variants", [cfg["niche"]])
        hashtags = cfg["manual_hashtags"] or expansion.get("hashtags", [])
        if cfg["manual_archetypes"]:
            search_lanes = [{"lane": "Manual", "priority": 3, "archetypes": cfg["manual_archetypes"]}]
        else:
            search_lanes = expansion.get("search_lanes", [])
        location_variants = expansion.get("location_variants", [cfg["location"]])
        exclusion_signals = cfg["exclude_terms"] or expansion.get("exclusion_signals", [])
        if cfg["manual_terms"] or cfg["manual_hashtags"] or cfg["manual_archetypes"]:
            print("[run] SEARCH_VOCABULARY partially specified — your terms override Claude's "
                  "for the categories you provided; Claude still filled in the rest.")

    flat_archetypes = [a for lane in search_lanes for a in lane["archetypes"]]

    if cfg["size_bias_terms"]:
        search_lanes.append({"lane": "Size Bias (top/best)", "priority": 2, "archetypes": cfg["size_bias_terms"]})
        flat_archetypes.extend(cfg["size_bias_terms"])
        print(f"[run] CREATOR_SIZE_TIER={cfg['creator_size_tier']!r} — added a 'top/best' phrasing lane "
              f"as a soft bias toward more established creators. This does not verify follower counts "
              f"any more reliably than before; it only changes what gets searched for.")

    print(f"[run] Using {len(niche_variants)} niche variants, {len(hashtags)} hashtags, "
          f"{len(search_lanes)} search lanes ({len(flat_archetypes)} archetypes total: "
          f"{', '.join(l['lane'] for l in search_lanes)}), {len(location_variants)} location variants, "
          f"{len(exclusion_signals)} exclusion signals")

    creds = Credentials.from_service_account_file(
        os.environ["GOOGLE_SERVICE_ACCOUNT_JSON"], scopes=SHEETS_SCOPES
    )
    gc = gspread.authorize(creds)
    sheet = gc.open_by_key(os.environ["SPREADSHEET_ID"])
    master_ws = get_or_create_tab(sheet, "Master", MASTER_HEADERS)
    run_log_ws = get_or_create_tab(sheet, "Run Log", RUN_LOG_HEADERS)

    known_keys = load_master_keys(master_ws)

    enrich_fn = {"instagram": enrich_instagram, "tiktok": enrich_tiktok}

    total_found = 0
    total_already_known = 0
    total_rejected_account_type = 0
    total_rejected_location = 0
    total_needs_verification = 0
    total_hard_follower_reject = 0
    total_activity_rejected = 0
    total_deterministically_excluded = 0
    total_llm_budget_cutoff = 0
    total_llm_classified = 0
    total_llm_parse_failed = 0
    total_sonnet_parse_failed = 0
    total_gender_rejected = 0
    total_below_min_fit = 0
    total_below_min_content_opportunity = 0
    final_creators = []
    excluded_rows = []
    remaining_search_budget = cfg["search_budget"]
    remaining_llm_candidates = cfg["llm_candidate_limit"]
    source_tally = {
        "instagram": {"business_api": 0, "serper": 0, "tavily": 0, "deep_research": 0, "unverified": 0},
        "tiktok":    {"business_api": 0, "serper": 0, "tavily": 0, "deep_research": 0, "unverified": 0},
    }
    total_gemini_verified = 0   # candidates where Gemini provided the follower data
    total_deep_research_discovered = 0  # candidates sourced from the pasted Deep Research report

    # ── STAGE 2: GEMINI PRIMARY DISCOVERY ──────────────────────────────────
    # Gemini runs ONCE for the whole run (not per-platform) using the complete
    # campaign brief including follower range, exclusions, and the expanded
    # niche vocabulary.  Its candidates form the primary discovery pool.
    # Serper then supplements, informed of what Gemini already found so its
    # budget is spent on genuinely new handles.
    #
    # Enabled automatically when GEMINI_API_KEY is present.
    # Disable explicitly with GEMINI_WEB_DISCOVERY=false.
    gemini_web_candidates = []
    total_gemini_web_discovered = 0
    gemini_key = os.environ.get("GEMINI_API_KEY", "")
    if cfg["gemini_web_discovery"] and gemini_key:
        print(f"[run] Gemini PRIMARY discovery — searching with full brief "
              f"(target: {cfg['gemini_web_discovery_max_candidates']} candidates). "
              f"Serper will supplement with any handles Gemini does not surface.")
        gemini_web_candidates = gemini_web_discover_candidates(
            cfg["niche"], brand_context, cfg["location"], cfg["platform"], cfg["target_gender"],
            api_key=gemini_key,
            max_candidates=cfg["gemini_web_discovery_max_candidates"],
            min_followers=cfg.get("min_followers_raw", ""),
            max_followers=cfg.get("max_followers_raw", ""),
            niche_variants=niche_variants,
            search_lanes=search_lanes,
        )
    elif cfg["gemini_web_discovery"] and not gemini_key:
        print("[run] GEMINI_WEB_DISCOVERY=true but GEMINI_API_KEY is not set — "
              "falling back to Serper-only discovery.")

    # ── STAGE 2.7: DEEP RESEARCH REPORT (Option D) ──────────────────────────
    # If DEEP_RESEARCH_REPORT is set as a repo Variable, Claude parses the
    # report text and extracts named creator handles. These form the highest-
    # confidence discovery pool (a human researcher actually read the report).
    # Serper then supplements with handles the report didn't name.
    #
    # Set as a repo Variable (Settings → Secrets and variables → Actions →
    # Variables tab), NOT a Secret — the report text is not sensitive.
    # An empty/missing value is silently skipped; the pipeline runs normally.
    deep_research_candidates = []
    total_deep_research_discovered = 0
    deep_research_report = os.environ.get("DEEP_RESEARCH_REPORT", "").strip()
    if deep_research_report:
        print(f"[run] DEEP_RESEARCH_REPORT is set ({len(deep_research_report)} chars) — "
              f"parsing report with Claude to extract named creator handles.")
        deep_research_candidates = parse_deep_research_report(
            deep_research_report, cfg["platform"], cfg["target_gender"],
            cfg["location"], cfg["niche"],
        )
    else:
        print("[run] DEEP_RESEARCH_REPORT not set — using Serper/Gemini/Claude web discovery only.")

    # ── STAGE 2.5: CLAUDE SUPPLEMENTARY DISCOVERY (optional) ────────────────
    # Claude web search adds candidates via a different search path.
    # Runs only when CLAUDE_WEB_DISCOVERY=true (opt-in, separate from Gemini).
    claude_web_candidates = []
    total_claude_web_discovered = 0
    if cfg["claude_web_discovery"]:
        claude_web_candidates = claude_web_discover_candidates(
            cfg["niche"], brand_context, cfg["location"], cfg["platform"], cfg["target_gender"],
            max_candidates=cfg["claude_web_discovery_max_candidates"],
        )
        print(f"[run] CLAUDE_WEB_DISCOVERY enabled — found {len(claude_web_candidates)} "
              f"candidate(s) via Claude web search (supplementary, same pipeline as all others).")

    for platform_index, platform in enumerate(cfg["platforms"]):
        if remaining_search_budget <= 0:
            print(f"[run] Search budget exhausted before {platform}; skipping remaining platform(s).")
            break
        platforms_left = len(cfg["platforms"]) - platform_index
        platform_search_budget = max(1, (remaining_search_budget + platforms_left - 1) // platforms_left)

        # ── Pool step 0: Deep Research Report candidates (HIGHEST PRIORITY) ───
        # Human-curated report goes in first — these are the highest-confidence
        # candidates (a researcher actually read the source material).
        # Gemini and Serper are both told about these so neither wastes budget
        # rediscovering handles already in the pool.
        deep_research_platform = [c for c in deep_research_candidates if c["platform"] == platform]
        candidates = list(deep_research_platform)
        total_deep_research_discovered += len(deep_research_platform)
        if deep_research_platform:
            print(f"[run] {platform}: {len(deep_research_platform)} candidate(s) from Deep Research "
                  f"report. Gemini and Serper will supplement with handles not already covered.")

        # ── Pool step 1: Gemini candidates (PRIMARY API discovery) ──────────
        # Gemini's handles go in next. Serper's discover_candidates() receives
        # all already-seen handles so it spends its entire budget on genuinely
        # new candidates — no duplicate searches, no wasted budget.
        gemini_platform = [c for c in gemini_web_candidates if c["platform"] == platform]
        already_in_pool = {normalize_handle(c["handle"]) for c in candidates}
        new_from_gemini = [c for c in gemini_platform
                           if normalize_handle(c["handle"]) not in already_in_pool]
        pre_seen_for_serper = already_in_pool | {normalize_handle(c["handle"]) for c in new_from_gemini}
        candidates.extend(new_from_gemini)
        total_gemini_web_discovered += len(new_from_gemini)
        if new_from_gemini:
            print(f"[run] {platform}: {len(new_from_gemini)} Gemini candidate(s) added. "
                  f"Serper now supplementing (budget: {platform_search_budget}).")
        elif gemini_platform:
            print(f"[run] {platform}: all {len(gemini_platform)} Gemini candidate(s) already "
                  f"covered by the Deep Research report — Serper supplementing.")

        # ── Pool step 2: Serper supplementary discovery ─────────────────────
        serper_candidates = discover_candidates(
            platform, niche_variants, search_lanes, hashtags, cfg["competitor_brands"],
            location_variants, platform_search_budget, serper_key,
            pre_seen_handles=pre_seen_for_serper,
        )
        candidates.extend(serper_candidates)
        remaining_search_budget -= min(platform_search_budget, len(serper_candidates))

        # ── Pool step 3: Claude supplementary discovery (optional) ──────────
        if claude_web_candidates:
            already_found = {normalize_handle(c["handle"]) for c in candidates}
            new_from_claude = [c for c in claude_web_candidates
                               if c["platform"] == platform
                               and normalize_handle(c["handle"]) not in already_found]
            candidates.extend(new_from_claude)
            total_claude_web_discovered += len(new_from_claude)

        total_found += len(candidates)

        # This dedup drop used to be silent — a candidate already in Master from
        # an earlier run just vanished here with no counter anywhere. On a
        # pipeline that's been run repeatedly against similar niche/location
        # inputs, this can plausibly be the LARGEST loss in the whole funnel,
        # and its invisibility is exactly what made "60 found -> 5 reached
        # Claude" look like unexplained over-aggressive filtering rather than
        # what it likely was: most of those 60 already being known creators.
        before_dedup = len(candidates)
        candidates = [c for c in candidates
                      if (make_dedup_key(platform, c["handle"]), cfg["campaign"]) not in known_keys]
        total_already_known += before_dedup - len(candidates)

        cheap_survivors = []
        for c in candidates:
            c["platform"] = platform
            keep, pre_score, reason = deterministic_candidate_check(c, extra_exclude_terms=cfg["exclude_terms"])
            if not keep:
                c["dedup_key"] = make_dedup_key(platform, c["handle"])
                c["username"] = c["handle"]
                c["rejection_reason"] = reason
                excluded_rows.append(c)
                total_deterministically_excluded += 1
                continue
            c["_pre_score"] = pre_score
            cheap_survivors.append(c)
        candidates = cheap_survivors

        follower_range_requested = bool(cfg["min_followers"] or cfg["max_followers"])
        for c in candidates:
            # ── Deep Research skip-Meta logic ────────────────────────────────
            # Two separate reasons to bypass Meta Business Discovery:
            #
            # (a) The DR report already stated a follower count for this creator.
            #     Calling Meta would be redundant and wastes API quota/rate-limit.
            #
            # (b) The extracted handle is a display name ("Chip Leighton") rather
            #     than a real @username. Meta's Business Discovery API expects a
            #     username — it returns "Invalid user id" for every full-name
            #     lookup, no exceptions. Calling it is a guaranteed failure that
            #     also consumes a rate-limit slot and triggers the "Invalid user id"
            #     error log noise seen in the review run.
            #
            # In both cases we fall straight to Serper/Tavily. Serper handles
            # display names better (full-name site:instagram.com queries return
            # usable snippets more often than Meta does), and the DR follower
            # count is backfilled below if Serper/Tavily also find nothing.
            dr_followers = c.get("dr_followers_count")
            is_display_name = c.get("handle_is_display_name", False)
            skip_meta = bool(dr_followers is not None or is_display_name)

            if skip_meta and is_display_name:
                print(f"[enrich] @{c['handle']!r} is a display name — skipping Meta "
                      f"(would return 'Invalid user id'), using Serper/Tavily only.")
            elif skip_meta and dr_followers is not None:
                print(f"[enrich] @{c['handle']!r}: DR report has {dr_followers:,} followers — "
                      f"skipping Meta lookup, verifying via Serper/Tavily.")

            if platform == "instagram":
                enriched = enrich_instagram(
                    c["handle"], serper_key,
                    cross_check=follower_range_requested,
                    skip_meta=skip_meta,
                )
            else:
                enriched = enrich_tiktok(c["handle"], serper_key, cross_check=follower_range_requested)

            c.update(enriched)
            c.setdefault("posting_frequency", "")
            c.setdefault("audience_quality_score", "")

            # ── Deep Research follower backfill + provenance ─────────────────
            # If the DR report stated a follower count AND the live enrichment
            # sources (Serper/Tavily) found nothing, backfill from the report
            # rather than leaving followers_count as None. The report number
            # is tagged as "reported" (not "verified") so scoring and filtering
            # treat it with the appropriate confidence level.
            #
            # If live enrichment DID find a count, that takes precedence — a
            # real-time independent read is more current than the research doc.
            # Either way, follower_source records where the number came from so
            # Master shows the full provenance chain at a glance.
            # dr_backfill_used is set to True only when the DR-reported count is
            # actually written into followers_count. Used below in follower_source
            # assignment so we don't mis-attribute the count to "deep_research_report"
            # when Meta or Serper happened to return the exact same number as the
            # DR report (value-equality check was ambiguous in that edge case).
            dr_backfill_used = False
            if dr_followers is not None and c.get("followers_count") is None:
                c["followers_count"] = dr_followers
                c["follower_verification"] = "reported"
                # CRITICAL: also update follower_source_quality so that
                # evaluate_follower_status (called in the separate loop below,
                # AFTER this enrichment loop completes) sees "reported" as the
                # source_quality and returns it unchanged.  Without this line,
                # follower_source_quality still holds "unverified" from
                # web_source_enrich's empty-result dict (set via c.update(enriched)
                # just above), and evaluate_follower_status then overwrites
                # c["follower_verification"] from "reported" back to "unverified".
                c["follower_source_quality"] = "reported"
                # Append the DR provenance to whatever data_source enrichment set
                # (may be "unavailable" if Serper/Tavily also found nothing).
                c["data_source"] = (c.get("data_source") or "unavailable") + "+deep_research_report"
                c["data_confidence"] = "high"
                dr_backfill_used = True
                print(f"[deep_research] @{c['handle']}: no live count found — "
                      f"using DR report's {dr_followers:,} followers "
                      f"(follower_verification=reported).")

            # follower_source records the definitive origin of the number now in
            # followers_count — DR report, Meta, Serper, Tavily, or unknown.
            if dr_backfill_used:
                c["follower_source"] = "deep_research_report"
            elif c.get("data_source") == "business_discovery_api":
                c["follower_source"] = "business_discovery_api"
            elif c.get("data_source", "").startswith("serper") or "serper" in c.get("data_source", ""):
                c["follower_source"] = "serper_snippet"
            elif "tavily" in c.get("data_source", ""):
                c["follower_source"] = "tavily_profile_fetch"
            elif c.get("_gemini_verified"):
                c["follower_source"] = "gemini_web_search"
            else:
                c["follower_source"] = "unknown"

            # Tally which enrichment tier supplied the follower count using
            # follower_source (set just above) rather than data_source.
            # follower_source tracks specifically where the FOLLOWER COUNT came
            # from, not just where any enrichment data (e.g. bio from Serper)
            # was found. This fixes two problems with the old data_source check:
            #   1. DR-backfilled candidates fell through all elif branches and
            #      went untallied — has_count was True but data_source was
            #      "unavailable+deep_research_report", matching nothing.
            #   2. A candidate whose bio came from Serper but whose follower
            #      count came from a different source was mis-attributed.
            has_count = c.get("followers_count") is not None
            follower_src = c.get("follower_source", "")
            if not has_count:
                source_tally[platform]["unverified"] += 1
            elif follower_src == "business_discovery_api":
                source_tally[platform]["business_api"] += 1
            elif follower_src == "tavily_profile_fetch":
                source_tally[platform]["tavily"] += 1
            elif follower_src == "serper_snippet":
                source_tally[platform]["serper"] += 1
            elif follower_src == "deep_research_report":
                source_tally[platform]["deep_research"] += 1
            # "gemini_web_search" / "unknown" — Gemini tallied separately below.
            if c.get("_gemini_verified"):
                total_gemini_verified += 1

            # ── DR pre-population: fill sheet fields from Deep Research data ──
            # These fields were researched by a human analyst (Modash, Collabstr,
            # Influencer-Hero data) and are more reliable than what Serper/Tavily
            # alone could produce for these candidates. Applied as a fallback:
            # if live enrichment already set the field, the live value wins.

            # Engagement rate — DR reports frequently include analyst-sourced ER
            # (e.g. "24.18%") that Serper cannot return. Previously this field was
            # always blank for DR-sourced candidates. Now it flows directly into
            # the Master sheet AND into the classify_creator prompt as evidence.
            if c.get("dr_engagement_rate") is not None and not c.get("engagement_rate"):
                c["engagement_rate"] = c["dr_engagement_rate"]

            # Audience quality score — proxy from avg_reel_views when available.
            # Maps to a 0-10 tier so the column isn't blank for DR candidates.
            # This is an approximation — treat as directional, not authoritative.
            if c.get("dr_avg_reel_views") is not None and not c.get("audience_quality_score"):
                arv = c["dr_avg_reel_views"]
                if arv >= 500_000:   aq = 10
                elif arv >= 100_000: aq = 9
                elif arv >= 20_000:  aq = 7
                elif arv >= 5_000:   aq = 5
                elif arv >= 1_000:   aq = 3
                else:                aq = 1
                c["audience_quality_score"] = aq

            # Location hint — feeds into classify_creator's Location hint field
            # so Claude has the DR-verified location as evidence when scoring
            # location_match and setting location_verified.
            if not c.get("location_hint"):
                parts = [p for p in (c.get("dr_city"), c.get("dr_state"), c.get("dr_country")) if p]
                if parts:
                    c["location_hint"] = ", ".join(parts)

            # Niche 2 / Niche 3 — DR gives specific labels (e.g. "Men's Wellness
            # & Grooming") that are more precise than the run-level niche keyword.
            # Set directly on the candidate so build_master_row writes them through.
            if c.get("dr_primary_niche") and not c.get("Niche 2"):
                c["Niche 2"] = c["dr_primary_niche"]
            if c.get("dr_secondary_niche") and not c.get("Niche 3"):
                c["Niche 3"] = c["dr_secondary_niche"]

            # Contact email — DR partnership sections frequently contain management
            # emails (e.g. "Managed by Kensington Grey (najm@kensingtongrey.co)")
            # that are more reliable than scraping a bio alone. Applied as initial
            # value here; extract_contact_info (run after classify_creator) will
            # OVERWRITE with a live bio email if it finds one — live wins.
            if c.get("dr_contact_email") and not c.get("contact_email"):
                c["contact_email"] = c["dr_contact_email"]
                c["contact_source"] = "deep_research_report"

        surviving = []
        for c in candidates:
            verdict, verification = evaluate_follower_status(
                c.get("followers_count"), cfg["min_followers"], cfg["max_followers"],
                cfg["unknown_followers_policy"], source_quality=c.get("follower_source_quality"),
            )
            c["follower_verification"] = verification
            if verdict == "fail":
                # Known follower count, confirmed outside the requested range.
                # Cheap, definitive, no Claude cost involved — but previously
                # uncounted, which was part of why funnel totals didn't add up.
                # Written to Excluded (not just dropped) so it's visible.
                c["dedup_key"] = make_dedup_key(platform, c["handle"])
                c["username"] = c["handle"]
                c["rejection_reason"] = (f"follower count confirmed as {c.get('followers_count')}, "
                                          f"outside requested range")
                excluded_rows.append(c)
                total_hard_follower_reject += 1
                continue
            if verdict == "needs_verification":
                c["dedup_key"] = make_dedup_key(platform, c["handle"])
                c["username"] = c["handle"]
                c["rejection_reason"] = "follower count unverified — a follower range is set but no count could be confirmed"
                excluded_rows.append(c)
                total_needs_verification += 1
                continue
            surviving.append(c)
        candidates = surviving

        for c in candidates:
            c["activity_status"] = activity_status(c.get("last_post_date"), cfg["activity_cutoff_days"])
            c["creator_quality_score"] = compute_creator_quality_score(
                c, follower_range_requested=follower_range_requested
            )

        if cfg["require_activity_verified"]:
            before_activity = len(candidates)
            candidates = [c for c in candidates if c.get("activity_status") == "active"]
            total_activity_rejected += before_activity - len(candidates)

        # Everything past this point is a candidate that made it through every
        # cheap filter and is genuinely competing for a slot in the LLM budget.
        # If more candidates survive than remaining_llm_candidates allows, the
        # lowest _pre_score ones get cut here — previously an uncounted drop.
        platform_llm_limit = max(0, (remaining_llm_candidates + platforms_left - 1) // platforms_left)
        candidates.sort(key=lambda c: c.get("_pre_score", 0), reverse=True)
        total_llm_budget_cutoff += max(0, len(candidates) - platform_llm_limit)
        candidates = candidates[:platform_llm_limit]
        remaining_llm_candidates -= len(candidates)
        total_llm_classified += len(candidates)

        for c in candidates:
            c.update(classify_creator(c, cfg["niche"], cfg["location"], brand_context, exclusion_signals))
            if c.get("_llm_parse_failed"):
                total_llm_parse_failed += 1

        # DR city/country fallback — classify_creator infers city/country from
        # bio alone (which is often empty for DR candidates). When Claude outputs
        # empty strings but the DR report provided a verified location, apply the
        # DR values so the location gate and location_match score have real data.
        # Only applied when Claude left the field blank — a Claude-confirmed city
        # always takes precedence over the DR-reported one.
        for c in candidates:
            if c.get("dr_city") and not c.get("city"):
                c["city"] = c["dr_city"]
                # Mark as verified — the DR analyst confirmed this location.
                c["location_verified"] = True
            if c.get("dr_country") and not c.get("country"):
                c["country"] = c["dr_country"]
                c["location_verified"] = True

        # Account-type gate: reject business/retailer/reseller/media/organization
        # accounts here, before they ever reach a fit score, unless explicitly
        # allowed via ALLOW_BUSINESS_ACCOUNTS=true.
        kept = []
        for c in candidates:
            account_type = (c.get("account_type") or "unknown").strip().lower()
            if account_type not in ("creator", "unknown") and not cfg["allow_business_accounts"]:
                c["dedup_key"] = make_dedup_key(platform, c["handle"])
                c["username"] = c["handle"]
                c["rejection_reason"] = f"account_type: {account_type}"
                excluded_rows.append(c)
                total_rejected_account_type += 1
                continue
            kept.append(c)
        candidates = kept

        # Location gate: a CONFIRMED country mismatch (not an unverified guess)
        # is treated as a hard exclusion rather than left to the location_match
        # weight (5% by default) to sort out. At that weight, a confirmed
        # out-of-country creator with strong product/audience fit can still
        # rank in the final results ahead of genuinely in-market candidates.
        # Only fires when location_verified=True and the classified country is
        # a recognized one that actually conflicts; unverified/unknown
        # locations are unaffected and still handled by the softer weighted
        # signal, since "unknown" isn't evidence of "wrong."
        kept = []
        for c in candidates:
            if location_target_conflicts(cfg["location"], c.get("country", ""), c.get("location_verified", False)):
                c["dedup_key"] = make_dedup_key(platform, c["handle"])
                c["username"] = c["handle"]
                c["rejection_reason"] = f"location mismatch: confirmed in {c.get('country')}, target was {cfg['location']}"
                excluded_rows.append(c)
                total_rejected_location += 1
                continue
            kept.append(c)
        candidates = kept

        for c in candidates:
            c.update(detect_partnership_signal(c.get("bio", "")))
            # DR partnership enhancement — detect_partnership_signal reads bio text
            # only. DR candidates typically have sparse/empty Serper-sourced bios, so
            # the score defaults to 0 even when the DR report explicitly states agency
            # representation or documented brand deals. Apply DR evidence as a fallback
            # when the bio scan found nothing, so partnership_signal_score reflects what
            # the human analyst actually researched rather than bio-scraping gaps.
            if c.get("dr_partnership_evidence") and (c.get("partnership_signal_score") or 0) == 0:
                dr_pe = c["dr_partnership_evidence"].lower()
                strong = ["managed by", "represented by", " agency", "management",
                          "ambassador", "sponsored", "brand deal", "ltk", "collab"]
                medium = ["partnership", "commercial", "brand partner", "integration",
                          "collaboration", "affili", "sponsor", "deal"]
                score, label = 0, ""
                for kw in strong:
                    if kw in dr_pe:
                        score, label = 10, kw.strip()
                        break
                if score == 0:
                    for kw in medium:
                        if kw in dr_pe:
                            score, label = 5, kw.strip()
                            break
                if score > 0:
                    c["partnership_signal_score"] = score
                    existing = c.get("partnership_signal_matched", "") or ""
                    c["partnership_signal_matched"] = (
                        (existing + ", " if existing else "") + f"DR: {label}"
                    )

        for c in candidates:
            c["overall_fit"] = compute_overall_fit(c, cfg["fit_weights"])

        if cfg["target_gender"] != "both":
            # Only hard-exclude a CONFIRMED opposite-gender match, not
            # "unclear" — same principle already applied to location
            # (location_target_conflicts only fires on a verified mismatch).
            # This run's own data is why: classify_creator was deliberately
            # tightened earlier to require actual bio/pronoun evidence for
            # gender rather than inferring it from content style/tone (to
            # avoid stereotyping) — but that means a genuinely relevant
            # creator whose bio simply doesn't state gender explicitly now
            # gets marked "unclear" far more often than before. Treating
            # "unclear" as equivalent to "confirmed wrong gender" was
            # silently discarding those candidates: 29 of 40 classified
            # candidates were cut here in one run, which is what actually
            # emptied Master, not a scoring or discovery problem. Audience/
            # product-fit signals still do the real discriminating work for
            # "unclear" candidates that survive this gate.
            other_genders = {"male", "female"} - {cfg["target_gender"]}
            before_gender = len(candidates)
            candidates = [c for c in candidates if c.get("gender_inferred") not in other_genders]
            total_gender_rejected += before_gender - len(candidates)

        if cfg["min_overall_fit"]:
            before_min_fit = len(candidates)
            below_threshold = [c for c in candidates if c.get("overall_fit", 0) < cfg["min_overall_fit"]]
            candidates = [c for c in candidates if c.get("overall_fit", 0) >= cfg["min_overall_fit"]]
            total_below_min_fit += before_min_fit - len(candidates)
            for c in below_threshold:
                c["dedup_key"] = make_dedup_key(platform, c["handle"])
                c["username"] = c["handle"]
                c["rejection_reason"] = (f"overall_fit {c.get('overall_fit')} below MIN_OVERALL_FIT="
                                          f"{cfg['min_overall_fit']} on first-pass score")
                excluded_rows.append(c)

        if cfg["min_content_opportunity"]:
            # A separate floor from MIN_OVERALL_FIT — catches candidates
            # where strong audience/creator-quality scores mask a near-zero
            # content_opportunity_score (i.e. "plausible profile, no evidence
            # this person ever posts anything relevant"), which an aggregate
            # weighted threshold alone can let through.
            before_min_co = len(candidates)
            below_co_threshold = [c for c in candidates
                                   if c.get("content_opportunity_score", 0) < cfg["min_content_opportunity"]]
            candidates = [c for c in candidates
                          if c.get("content_opportunity_score", 0) >= cfg["min_content_opportunity"]]
            total_below_min_content_opportunity += before_min_co - len(candidates)
            for c in below_co_threshold:
                c["dedup_key"] = make_dedup_key(platform, c["handle"])
                c["username"] = c["handle"]
                c["rejection_reason"] = (f"content_opportunity_score {c.get('content_opportunity_score')} below "
                                          f"MIN_CONTENT_OPPORTUNITY={cfg['min_content_opportunity']} — passed "
                                          f"MIN_OVERALL_FIT but has little to no evidenced content moment")
                excluded_rows.append(c)

        for c in candidates:
            linktree_text = fetch_bio_link_text(c.get("bio", ""))
            live_contact = extract_contact_info(c.get("bio", ""), linktree_text)
            # Additive merge: live bio/linktree data takes precedence when it
            # actually found something. If the bio returned None for a field,
            # keep whatever the DR pre-population stage set (e.g. a management
            # email from the DR partnership section) rather than overwriting it
            # with None. A live bio email is always more current than DR, but
            # a DR management email beats no email at all.
            for field, value in live_contact.items():
                if value is not None:
                    c[field] = value
                elif field not in c or c[field] is None:
                    c[field] = value

        for c in candidates:
            c["dedup_key"] = make_dedup_key(platform, c["handle"])
            c["username"] = c["handle"]
            c["review_status"] = ""
            c["outreach_channel"] = ""
            c["campaign_push_status"] = ""

        final_batch = []
        for c in candidates:
            key = (c["dedup_key"], cfg["campaign"])
            if key not in known_keys:
                known_keys.add(key)
                final_batch.append(c)
        final_creators.extend(final_batch)

    # RESULT_LIMIT applies to the entire run, not once per selected platform.
    final_creators.sort(key=lambda c: c.get("overall_fit", 0), reverse=True)

    total_sonnet_refined = 0
    if cfg["sonnet_refinement"] and final_creators:
        # Bounded pool: the top candidates that would plausibly make the final
        # cut, not everyone Haiku classified. 3x result_limit gives room for
        # refinement to actually reorder the ranking (a candidate refined
        # downward needs someone currently just outside the cut to refine
        # upward past them, or refinement can only ever remove candidates,
        # never promote a new one in) while staying bounded regardless of how
        # large SEARCH_BUDGET or LLM_CANDIDATE_LIMIT were set for this run.
        pool_size = min(len(final_creators), max(cfg["result_limit"] * 3, 6), MAX_SONNET_REFINEMENT_POOL)
        print(f"[run] SONNET_REFINEMENT enabled — running the top {pool_size} candidate(s) through a "
              f"second, more critical Sonnet pass before final ranking.")
        refined_pool = []
        for c in final_creators[:pool_size]:
            refined = refine_creator_with_sonnet(c, cfg["niche"], cfg["location"], brand_context, exclusion_signals)
            refined["overall_fit"] = compute_overall_fit(refined, cfg["fit_weights"])
            if refined.get("sonnet_refined"):
                total_sonnet_refined += 1
            elif refined.get("_sonnet_parse_failed"):
                total_sonnet_parse_failed += 1
            refined_pool.append(refined)

        # Re-apply MIN_OVERALL_FIT after refinement. Without this, a candidate
        # that only cleared the bar on Haiku's first-pass score could have
        # Sonnet's more critical pass mark it down below the threshold the
        # person actually configured, and it would still end up in Master
        # anyway — the exact case that happened in testing (two Sonnet-refined
        # candidates landed at 4.6 and 4.2 against a configured MIN_OVERALL_FIT
        # of 5, both still written to Master). This isn't a new filter or a
        # changed threshold value; it's making the existing one actually hold
        # after the score it's checking gets recomputed.
        total_sonnet_downgraded_below_threshold = 0
        if cfg["min_overall_fit"]:
            kept, downgraded = [], []
            for c in refined_pool:
                if c.get("overall_fit", 0) < cfg["min_overall_fit"]:
                    downgraded.append(c)
                else:
                    kept.append(c)
            for c in downgraded:
                c["rejection_reason"] = (f"Sonnet refinement lowered overall_fit to {c.get('overall_fit')}, "
                                          f"below MIN_OVERALL_FIT={cfg['min_overall_fit']} (was above threshold "
                                          f"on the first-pass Haiku score)")
                excluded_rows.append(c)
                total_sonnet_downgraded_below_threshold += 1
            refined_pool = kept

        # Same content_opportunity floor as the first pass, re-checked here
        # because Sonnet can (and, per its own prompt, is specifically
        # instructed to) revise content_opportunity_score downward when the
        # first pass over-credited category/bio inference without actual
        # evidence — exactly the pattern that needs catching post-refinement
        # too, not just pre-refinement.
        if cfg["min_content_opportunity"]:
            kept, downgraded = [], []
            for c in refined_pool:
                if c.get("content_opportunity_score", 0) < cfg["min_content_opportunity"]:
                    downgraded.append(c)
                else:
                    kept.append(c)
            for c in downgraded:
                c["rejection_reason"] = (f"Sonnet refinement set content_opportunity_score to "
                                          f"{c.get('content_opportunity_score')}, below "
                                          f"MIN_CONTENT_OPPORTUNITY={cfg['min_content_opportunity']}")
                excluded_rows.append(c)
                total_below_min_content_opportunity += 1
            refined_pool = kept

        if total_sonnet_downgraded_below_threshold:
            print(f"[run] SONNET_REFINEMENT: {total_sonnet_downgraded_below_threshold} candidate(s) that "
                  f"cleared MIN_OVERALL_FIT on the first-pass score fell below it after refinement — moved "
                  f"to Excluded rather than kept in Master below the configured threshold. This can mean "
                  f"Master ends up with fewer than RESULT_LIMIT rows on a given run; that's correct behavior, "
                  f"not a bug — it means refinement is doing its job and there genuinely weren't enough "
                  f"candidates that held up under closer scrutiny.")

        final_creators = refined_pool + final_creators[pool_size:]
        final_creators.sort(key=lambda c: c.get("overall_fit", 0), reverse=True)
    else:
        total_sonnet_downgraded_below_threshold = 0

    final_creators = final_creators[:cfg["result_limit"]]

    verify_reported_followers_with_meta(
        final_creators, os.environ.get("META_ACCESS_TOKEN"), os.environ.get("IG_BUSINESS_ACCOUNT_ID"),
    )

    if final_creators:
        write_batch(sheet, master_ws, cfg["niche"], final_creators, sector_label=cfg["niche"],
                    campaign=cfg["campaign"])
    write_excluded(sheet, excluded_rows, campaign=cfg["campaign"])

    log_run(run_log_ws, {
        "run_date": datetime.now(timezone.utc).isoformat(),
        "campaign": cfg["campaign"],
        "brand_name": cfg["brand_name"], "brand_website": cfg["brand_website"],
        "niche_input": cfg["niche"], "expanded_terms": ", ".join(niche_variants),
        "expanded_hashtags": ", ".join(hashtags), "expanded_archetypes": ", ".join(flat_archetypes),
        "search_lanes": ", ".join(f"{l['lane']} (p{l['priority']})" for l in search_lanes),
        "exclusion_signals": ", ".join(exclusion_signals),
        "location_input": cfg["location"], "gender_filter": cfg["target_gender"],
        "competitor_brands_input": ", ".join(cfg["competitor_brands"]),
        "fit_weights_used": str(cfg["fit_weights"]), "search_budget_used": cfg["search_budget"],
        "llm_candidate_limit_used": cfg["llm_candidate_limit"],
        "min_followers_used": cfg["min_followers"] or "", "max_followers_used": cfg["max_followers"] or "",
        "creator_size_tier_used": cfg["creator_size_tier"] or "",
        "unknown_followers_policy_used": cfg["unknown_followers_policy"],
        "min_overall_fit_used": cfg["min_overall_fit"],
        "total_found": total_found, "total_already_known": total_already_known,
        "total_rejected_account_type": total_rejected_account_type,
        "total_rejected_location": total_rejected_location,
        "total_needs_follower_verification": total_needs_verification,
        "total_hard_follower_reject": total_hard_follower_reject,
        "total_activity_rejected": total_activity_rejected,
        "total_deterministically_excluded": total_deterministically_excluded,
        "total_llm_budget_cutoff": total_llm_budget_cutoff,
        "llm_candidates_classified": total_llm_classified,
        "total_llm_parse_failed": total_llm_parse_failed,
        "total_gender_rejected": total_gender_rejected,
        "total_below_min_fit": total_below_min_fit,
        "total_below_min_content_opportunity": total_below_min_content_opportunity,
        "total_after_filters": len(final_creators),
        "instagram_verified_via_business_api": source_tally["instagram"]["business_api"],
        "instagram_verified_via_serper":        source_tally["instagram"]["serper"],
        "instagram_verified_via_tavily":        source_tally["instagram"]["tavily"],
        "instagram_via_deep_research":          source_tally["instagram"]["deep_research"],
        "instagram_unverified":                 source_tally["instagram"]["unverified"],
        "tiktok_verified_via_serper":           source_tally["tiktok"]["serper"],
        "tiktok_verified_via_tavily":           source_tally["tiktok"]["tavily"],
        "tiktok_via_deep_research":             source_tally["tiktok"]["deep_research"],
        "tiktok_unverified":                    source_tally["tiktok"]["unverified"],
        "sonnet_refinement_used": cfg["sonnet_refinement"], "total_sonnet_refined": total_sonnet_refined,
        "total_sonnet_parse_failed": total_sonnet_parse_failed,
        "total_sonnet_downgraded_below_threshold": total_sonnet_downgraded_below_threshold,
        "claude_web_discovery_used": cfg["claude_web_discovery"],
        "total_claude_web_discovered": total_claude_web_discovered,
        "gemini_web_discovery_used": cfg["gemini_web_discovery"],
        "total_gemini_web_discovered": total_gemini_web_discovered,
        "gemini_verification_fallback_used": cfg["gemini_verification_fallback"],
        "total_gemini_verified": total_gemini_verified,
        "deep_research_report_used": bool(deep_research_report),
        "total_deep_research_discovered": total_deep_research_discovered,
    })

    accounted_for = (total_already_known + total_deterministically_excluded + total_needs_verification
                     + total_hard_follower_reject + total_activity_rejected + total_llm_budget_cutoff
                     + total_llm_classified)
    print(f"[run] Done. Found {total_found} raw candidates -> {total_already_known} already known + "
          f"{total_deterministically_excluded} deterministically excluded + {total_needs_verification} held "
          f"for follower verification + {total_hard_follower_reject} hard-rejected on confirmed follower count "
          f"+ {total_activity_rejected} rejected for inactivity + {total_llm_budget_cutoff} cut for LLM budget "
          f"+ {total_llm_classified} sent to Claude = {accounted_for} accounted for "
          f"({'FULLY RECONCILED' if accounted_for == total_found else f'GAP OF {total_found - accounted_for} — see note below'}). "
          f"Of the {total_llm_classified} sent to Claude: {total_llm_parse_failed} had a JSON parse failure "
          f"(scored with conservative fallback defaults), {total_rejected_account_type} were rejected for wrong "
          f"account type, {total_rejected_location} for a confirmed location mismatch, {total_gender_rejected} "
          f"for gender mismatch, {total_below_min_fit} scored below MIN_OVERALL_FIT on the first pass"
          + (f", {total_sonnet_parse_failed} had a Sonnet refinement parse failure (kept first-pass scores), "
             f"{total_sonnet_downgraded_below_threshold} were downgraded below MIN_OVERALL_FIT by refinement"
             if cfg["sonnet_refinement"] else "")
          + f" — {len(final_creators)} written to Master after ranking.")
    if accounted_for != total_found:
        print(f"[run] NOTE: accounting gap of {total_found - accounted_for} — if this appears, it means a "
              f"drop point still isn't counted somewhere in this run. Worth reporting with the exact numbers "
              f"above so it can be traced, rather than assumed to be one of the categories already listed.")
    if total_already_known >= total_found * 0.5:
        print(f"[run] NOTE: {total_already_known}/{total_found} raw candidates this run were already known "
              f"(already in Master from a prior run with overlapping niche/location/lanes) — that's the single "
              f"biggest reason few candidates reached Claude, more than any filter below. This isn't a bug: it "
              f"means repeated runs on similar inputs increasingly rediscover the same people. If you want a "
              f"genuinely wider net on a repeat run, vary the niche/location/SEARCH_VOCABULARY rather than just "
              f"re-running the same inputs with a higher search budget.")
    if excluded_rows:
        print(f"[run] {len(excluded_rows)} row(s) written to 'Excluded' tab for manual review.")
    if total_llm_classified and total_llm_parse_failed >= total_llm_classified * 0.15:
        print(f"[run] NOTE: {total_llm_parse_failed}/{total_llm_classified} Claude classification calls had a "
              f"JSON parse failure this run — higher than expected. These still get a conservative fallback "
              f"score rather than crashing, but that's a worse signal than a real classification. If this stays "
              f"high across runs, worth checking the [classification] Failed to parse lines in the Action log "
              f"for a pattern (e.g. one particular kind of response consistently breaking the parser).")
    if total_found and total_needs_verification >= total_found * 0.5:
        print(f"[run] DIAGNOSIS: {total_needs_verification}/{total_found} candidates were held out for "
              f"follower verification — that's the dominant bottleneck this run, not account-type rejection "
              f"or the Claude classification cap. This happens when MIN_FOLLOWERS/MAX_FOLLOWERS is set but no "
              f"licensed provider or Business Discovery API is configured, since Serper-snippet parsing can only "
              f"confirm a follower count when the exact phrase appears in a cached Google snippet — which is "
              f"uncommon. To fix: either drop the follower range for this niche, set "
              f"UNKNOWN_FOLLOWERS_POLICY=include to test the rest of the pipeline with the gate open, or set up "
              f"a licensed enrichment provider so follower counts are actually available most of the time. Note: "
              f"candidates let through under UNKNOWN_FOLLOWERS_POLICY=include now take a creator_quality_score "
              f"penalty for the unconfirmed count when a follower range is set, so they no longer rank identically "
              f"to verified candidates — see creator_quality_score on affected Master rows.")
    if deep_research_report:
        print(f"[run] DEEP_RESEARCH_REPORT summary: {total_deep_research_discovered} candidate(s) seeded from "
              f"the pasted report (across all platforms). Filter Master/Excluded by "
              f"discovery_method='deep_research_report' or matched_lane='Deep Research Report' to see how "
              f"these performed after independent verification, separate from Serper/Gemini candidates. "
              f"To update: paste a new report into Settings → Secrets and variables → Actions → Variables → "
              f"DEEP_RESEARCH_REPORT, then trigger the pipeline again.")
    if cfg["claude_web_discovery"]:
        print(f"[run] CLAUDE_WEB_DISCOVERY summary: {total_claude_web_discovered} candidate(s) contributed to "
              f"this run's {total_found} total (not already found by Serper). Filter Excluded/Master by "
              f"discovery_method='claude_web_search' or matched_lane='Claude Web Discovery' to see specifically "
              f"how this channel's candidates performed after independent verification, separate from Serper's.")
    if cfg["gemini_web_discovery"]:
        print(f"[run] GEMINI_WEB_DISCOVERY summary: {total_gemini_web_discovered} unique candidate(s) added "
              f"by Gemini's Google Search (not already found by Serper/Claude). Filter by "
              f"discovery_method='gemini_web_search' or matched_lane='Gemini Web Discovery'.")
    if cfg["gemini_verification_fallback"]:
        print(f"[run] GEMINI_VERIFICATION_FALLBACK summary: {total_gemini_verified} candidate(s) had follower "
              f"data sourced from Gemini's web search (tagged probable_gemini — treated as 'probable', "
              f"NOT 'verified', in all follower filtering and scoring logic).")
    if cfg["gemini_web_discovery"] or cfg["gemini_verification_fallback"]:
        _tc = _gemini_state["total_calls"]
        _vc = _gemini_state["verify_calls"]
        _dq = " — DAILY QUOTA EXHAUSTED this run (see [gemini] log above)" if _gemini_state["daily_quota_exhausted"] else ""
        print(f"[run] GEMINI API USAGE this run: {_tc} successful call(s) total "
              f"(1 discovery + {_vc} verification fallback). "
              f"Free-tier daily RPD is shared across ALL runs today{_dq}. "
              f"Verification capped at GEMINI_VERIFY_MAX_PER_RUN={GEMINI_VERIFY_MAX_PER_RUN} (set as repo Variable to adjust).")
    print("[run] Next: review Master tab, set review_status = Approved and pick an "
          "outreach_channel (email/dm/none), then run the 'Sync Shortlist' and "
          "'Draft DMs' workflows from the Actions tab.")


if __name__ == "__main__":
    run()
