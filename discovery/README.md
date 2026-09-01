# Creator Discovery & Outreach Pipeline

Finds Instagram/TikTok creators by niche, region, and gender for brand outreach — scores fit, extracts public contact info, writes to Google Sheets, and drafts personalized DMs for creators you've manually shortlisted.

**Runs only via GitHub Actions.** No local setup, no `.env` file, no CLI arguments — everything is triggered from the repo's Actions tab.

---

## Structure — 3 files, one per workflow step

```
discover.py     — Stages 0–8: expand the niche, find creators, enrich, score, write to Sheets
shortlist.py    — Stage 9: copies your Shortlisted = Y rows into a Shortlist tab
dm_drafting.py  — Stage 10: drafts DMs for shortlisted creators, checks Contact History first
```

Each file is self-contained — no imports between them. That means a few small pieces (the sheet header list, the Sheets-retry helper, the Claude-response-parsing helper) are duplicated across files rather than shared from a common module. That's intentional: three files you can read top to bottom beat twenty files you have to trace through to find one bug.

```
creator_discovery_pipeline/
├── README.md
├── requirements.txt
├── .gitignore
├── discover.py
├── shortlist.py
├── dm_drafting.py
└── .github/workflows/
    ├── discover.yml
    ├── sync_shortlist.yml
    └── draft_dms.yml
```

---

## Setup (one-time)

### 1. Add repo secrets

Settings → Secrets and variables → Actions → New repository secret:

| Secret | Where to get it | Required? |
|---|---|---|
| `ANTHROPIC_API_KEY` | console.anthropic.com | Yes |
| `SERPER_API_KEY` | serper.dev | Yes |
| `TAVILY_API_KEY` | tavily.com | Yes — used to fetch brand websites and bio-link (Linktree-style) pages |
| `GOOGLE_SERVICE_ACCOUNT_JSON` | Google Cloud Console → paste the entire JSON key file contents | Yes |
| `SPREADSHEET_ID` | from your Google Sheet's URL | Yes |
| `LICENSED_IG_API_KEY` / `LICENSED_TIKTOK_API_KEY` | whichever licensed creator-data provider you pick later | No — pipeline runs fine on the Serper fallback tier without these |
| `META_ACCESS_TOKEN` / `IG_BUSINESS_ACCOUNT_ID` | Meta for Developers, requires your own IG Business account | No |

### 2. Google Sheet

Create a blank Google Sheet, copy its ID from the URL, share it with your service account's email (the `client_email` field in the JSON key) as **Editor**. Tabs (`Master`, `Run Log`, sector tabs, `Shortlist`, `Contact History`) are all created automatically on first use — nothing to set up manually.

---

## Running it

### 1. Discover creators

Actions tab → **Creator Discovery Pipeline** → Run workflow → fill in:
- Required: niche, brand name, location, platform, target gender, result limit (start with 5; cost-controlled maximum is 60)
- Optional: brand website, **brand/product brief, target buyer, use cases, creator types, exclude** (see below), follower range, competitor brands (comma-separated), minimum Overall Fit, fit-score weight overrides, search budget

All currently supported discovery fields, including `SEARCH_BUDGET`, `LLM_CANDIDATE_LIMIT`, and activity verification, are available in the GitHub Actions form.

**Cost controls** — `RESULT_LIMIT` is the total number of *qualified* creators returned across every selected platform. `SEARCH_BUDGET` is a run-wide raw-candidate cap (default: `max(30, 8 × RESULT_LIMIT)`, maximum 400), not a per-platform cap. `LLM_CANDIDATE_LIMIT` is a second, run-wide cap on pre-filtered candidates that may receive a Claude classification (default: `max(12, 4 × RESULT_LIMIT)`, maximum 60). Obvious storefront/repository/memorial results are excluded deterministically before enrichment or Claude.

**Brand/product brief** — a free-text description of the product, who buys it, and where it naturally shows up (e.g. *"Hoodie-style towel robe for men — fits naturally into post-shower, gym recovery, beach/pool days, sauna sessions, travel, morning routines."*). This isn't just cosmetic: it feeds directly into (1) the creator-archetype and search-term generation, so search moves beyond literal niche-keyword matching toward "who would actually use this and when," (2) the fit-scoring prompt, and (3) the DM-drafting persona if you reuse it there via `BRAND_BRIEF`. It's merged with `BRAND_WEBSITE` if both are given — the typed brief takes priority since it's a direct statement of intent rather than an LLM's guess at a webpage. Either alone is enough to use; giving neither falls back to near-bare keyword search on `niche`, which is the weakest mode.

**Account-type filtering** — every candidate is now classified as `creator`, `brand`, `retailer`, `reseller`, `media`, `organization`, `community`, or `unknown`. Anything other than `creator`/`unknown` is rejected before it reaches Master (a business page selling loungewear no longer gets scored as a loungewear creator) — set `ALLOW_BUSINESS_ACCOUNTS=true` to disable this if you actually want business accounts back.

**Follower verification** — if either a minimum or maximum follower bound is set and a candidate's follower count can't be confirmed, it is held out of Master and written to the `Excluded` tab instead of silently passing the filter. Set `UNKNOWN_FOLLOWERS_POLICY=include` to restore the old permissive behavior.

**Quality floor** — `MIN_OVERALL_FIT` defaults to `5`. This keeps accounts with insufficient or clearly irrelevant evidence out of Master; raise it for a narrower list or lower it only for exploratory research.

Writes to the `Master` tab and a sector-specific tab named after your niche. Anything rejected for account type or unverifiable followers goes to the `Excluded` tab with a `rejection_reason`, so a run that returns fewer results than you asked for is legible instead of silent.

### 2. Review and shortlist (in the sheet, no workflow needed)

Open the Google Sheet, review `Master` (sort by `overall_fit` descending to see best matches first), mark `Shortlisted = Y` on rows you've actually checked.

### 3. Sync the shortlist

Actions tab → **Sync Shortlist** → Run workflow (no inputs). Copies `Shortlisted = Y` rows into a `Shortlist` tab.

Back in the sheet: fill in `fit_reasoning` on each `Shortlist` row — your own note from reviewing their content. This is required; rows without it get skipped by the next step.

### 4. Draft DMs

Actions tab → **Draft DMs** → Run workflow → enter brand name (and website, optional). For each shortlisted row:
- Checks `Contact History` first — already **accepted** or **rejected** for this brand → skipped entirely, no Claude call spent. Any other prior contact → still drafts, but flags it so you can decide if a follow-up makes sense.
- Writes `personalization_notes` (what it observed, what it plans to reference) before writing the DM itself, so you can see and edit the reasoning.
- Writes the DM to `dm_draft`, records the chosen message structure in `dm_reasoning`.

### 5. Review and send

Review each draft in the sheet, edit if needed, send manually on the platform. Nothing auto-sends. After you hear back, update `outreach_outcome` in the `Contact History` tab (`accepted` / `rejected` / `replied` / `ghosted` / `interested_later`) — that's what prevents the same creator getting pitched twice on a future run.

---

## Sheet structure

- **Master** — every unique creator found, ever, across all niches/platforms searched. `Niche 1/2/3` fill left-to-right as a creator matches multiple sector searches.
- **[Sector tabs]** — auto-created per niche searched, subset of Master.
- **Run Log** — one row per discovery run (which terms/hashtags/archetypes were used, competitor brands, fit weights, result counts) — useful for debugging a weak run.
- **Shortlist** — your reviewed picks, plus `fit_reasoning`, `personalization_notes`, `dm_draft`, `dm_reasoning`, `dm_status`.
- **Contact History** — one row per (creator, brand) pair, tracks `outreach_outcome` over time so nothing gets double-pitched.
- **Excluded** — candidates found but not written to Master, with a `rejection_reason`: either the wrong `account_type` (business/retailer/reseller/etc.) or a follower count that couldn't be verified against a hard `MIN_FOLLOWERS`. Worth a periodic skim in case the account-type classifier is being too aggressive for your niche.

---

## How discovery actually works (`discover.py`)

Four search passes per platform, each tagged so you can see why a creator showed up. **Search lanes run first**, then niche terms, then hashtags, then competitors.

1. **Search lanes** — the primary discovery mechanism. Instead of one flat archetype list, Claude generates 4-7 distinct *lanes* (e.g. "Men's Lifestyle", "Fitness / Recovery", "Dad / Family", "Men's Grooming"), each with its own archetypes and its own guaranteed search-budget quota (weighted by a 1-3 priority Claude assigns). This matters because a flat list sharing one budget meant a generic, high-volume lane could exhaust the whole search before a narrower, more relevant lane ever ran. Lanes are built from the niche *and*, if given, the brand/product brief — so they cover adjacent categories where the product could plausibly show up, not just literal niche synonyms.
2. **Niche terms** — plain keyword search (e.g. "loungewear USA"), deliberately kept to a small list since this is the weakest discovery mode and shouldn't dominate results
3. **Hashtags** — platform-native tags
4. **Competitor mentions** (only if competitor brands given) — searches for creators already using partnership language alongside a named competitor

---

## Scoring: product fit, not just niche fit

Every candidate gets two kinds of signal:

**LLM-judged (Claude Haiku), per-candidate:**
- `product_fit_score` — the primary dimension. Could this specific product plausibly integrate into this creator's content, *including via an adjacent category* — a fitness/recovery creator can score high for a comfort-apparel brand through a post-workout moment, with zero literal keyword overlap with the niche.
- `content_opportunity_score` — are there concrete, nameable moments in their actual content (not a generic "could work for any lifestyle creator")
- `niche_match` / `content_angle_strength` — still computed and shown for transparency, but no longer drive the score by default (see weighting below)
- `account_type`, `audience_match`, `location_match` (+ `location_verified`) — as before

**Deterministic (no LLM call), per-candidate:**
- `creator_quality_score` — account credibility from signals already gathered elsewhere: posting activity, enrichment data confidence, follower-count verification. Kept separate from product/audience fit since "is this account real and active" shouldn't need a judgment call.

**Overall Fit weighting (defaults, override with `WEIGHT_*` inputs):**

| Factor | Weight |
|---|---:|
| Product Fit | 30% |
| Audience Match | 25% |
| Content Opportunity | 20% |
| Creator Quality | 10% |
| Partnership Signal | 10% |
| Location Match | 5% |

`niche_match` and `content_angle_strength` are 0% by default — they're informational columns now, not scoring inputs — because raw niche-keyword match was outweighing genuine adjacent-niche fit (the "Nick Bare doesn't need to be a loungewear creator" problem).

**Setting overrides:** GitHub caps `workflow_dispatch` at 25 inputs total, so on the Actions form these live in one field, `weight_overrides`, as comma-separated `key=value` pairs using short aliases: `product_fit`, `audience`, `content_opportunity`, `creator_quality`, `partnership`, `location`, `niche`, `content_angle`. Example: `product_fit=0.4,audience=0.2,niche=0.1` (unset keys keep their default). If running `discover.py` directly rather than through Actions, individual `WEIGHT_PRODUCT_FIT` / `WEIGHT_AUDIENCE` / etc. env vars also work and are applied first, with `FIT_WEIGHT_OVERRIDES` taking precedence if both are set.

Enrichment (follower count, bio, posting data) tries a licensed provider first if configured, falls through to Instagram's official Business Discovery API if configured, and always has a Serper-snippet-parsing fallback so the pipeline never hard-fails even with zero paid providers set up. Every row is tagged with `data_source` and `data_confidence` so you know how reliable a given number is.

**Overall Fit** is computed as a deterministic weighted sum of niche match, audience match, location match, content angle strength, and partnership signal — not an LLM guess — so the weight overrides you set actually get respected exactly. Defaults: niche 0.35, audience 0.25, location 0.15, content angle 0.15, partnership 0.10.

**Partnership signal** (regex-detected ambassador/discount-code language in the bio) is kept separate from **brand affinity note** (a soft LLM inference when there's no explicit signal) — different reliability tiers, not blended together.

---

## Tavily

Used for two things: fetching a brand's website (Stage 0.5) and fetching a creator's link-in-bio page like Linktree (for contact info extraction). Swapped in over plain HTML scraping because it handles messier or JS-rendered pages far more reliably.

Note the division of labor: Tavily fetches and cleans the page *text*. Regex still does the actual *finding* — pulling an email address, a phone number, or ambassador language out of that text. Those are different jobs; Tavily doesn't replace pattern-matching, it just gets cleaner text for the pattern-matching to run against.

---

## Known limitations

- `LICENSED_IG_API_KEY` / `LICENSED_TIKTOK_API_KEY` are read as a signal but not wired to a real provider yet — `discover.py`'s `enrich_instagram()` / `enrich_tiktok()` functions have a clearly marked spot to add your chosen provider's actual API call once you've picked one. Until then, setting the key does nothing extra; the pipeline just runs on the Business Discovery / Serper fallback tiers.
- `posting_frequency` and `audience_quality_score` columns exist but stay blank unless a licensed provider actually supplies them — never estimated from a Serper snippet.
- No bot/fake-follower detection. That needs comment-level data neither Serper nor most base-tier licensed providers expose — usually a separate paid add-on even within platforms like HypeAuditor. Not built, rather than built badly.
- **No partnership-history research yet** (previous brand collaborations, sponsored-post response, affiliate activity). `partnership_signal_score` is still just a regex check on the bio for ambassador/discount-code language — a real signal, but a shallow one. This is the deliberate next phase, not bundled into this update — the idea is to confirm discovery/scoring quality (search lanes, product-fit scoring) actually holds up on a few real runs before adding a second, more expensive research stage on top of it.
- **No pre-classification heuristic filter.** Account-type rejection currently always costs one Haiku call per candidate — there's no regex-based "obviously a store, skip the LLM call" pre-filter. Haiku is cheap enough that this wasn't worth the false-positive risk of a heuristic wrongly rejecting a real creator before the LLM ever saw them; worth revisiting only if API cost becomes a real constraint at higher search budgets.
- Account-type classification (`creator` vs `brand`/`retailer`/etc.) is an LLM judgment call, same reliability tier as the other classification fields — not infallible. Check the `Excluded` tab occasionally rather than assuming every rejection was correct.
- `GitHub Actions workflow YAML` files weren't part of this review — if the new `discover.py` inputs (`BRAND_BRIEF`, `SEARCH_BUDGET`, etc.) should be selectable from the Actions "Run workflow" form rather than only settable as repo/environment variables, `.github/workflows/discover.yml` needs those added to its `inputs:` block.
