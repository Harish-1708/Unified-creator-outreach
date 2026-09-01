"""
dm_drafting.py — Stage 10: DM Drafting.

Self-contained. Run after you've filled in `fit_reasoning` on Shortlist
tab rows (your own judgment note from actually reviewing their content).

Before drafting, checks Contact History for this (creator, brand) pair:
  - already accepted/rejected -> hard skip, no Claude call spent
  - any other prior contact -> soft flag, DM still drafts, you decide
Every drafted DM creates/updates a Contact History row, so the same
creator never gets silently pitched twice for the same brand.

Claude first writes Personalization Notes (what it observed, what it
plans to reference) in their own column, THEN writes the DM from those
notes — grounded in something visible and editable, not a black box.

DM prompt follows the "Universal Creator Outreach Engine" framework:
Recognition -> Relevance -> Brand Context -> Experience First -> Future
Collaboration -> One CTA, with automatic skeleton selection and hard
writing constraints (90-word cap, no buzzwords/links/pricing, one CTA).

Run only via GitHub Actions — the "Draft DMs" workflow. Never auto-sends.
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

SONNET_MODEL = "claude-sonnet-5"
SHEETS_SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]

SHORTLIST_EXTRA_HEADERS = ["fit_reasoning", "personalization_notes", "dm_draft", "dm_reasoning", "dm_status"]

CONTACT_HISTORY_HEADERS = [
    "dedup_key", "platform", "username", "brand_name",
    "first_contact_date", "last_contact_date", "last_campaign",
    "gift_sent", "outreach_outcome", "manager_name", "manager_email",
    "preferred_contact", "notes",
]
HARD_SKIP_OUTCOMES = {"accepted", "rejected"}

MAX_RETRIES = 5

DM_FRAMEWORK_INSTRUCTIONS = """You are an elite Creator Partnership Strategist writing a FIRST-CONTACT DM
to open a conversation with a creator — not to close a deal. Business terms
get discussed later, separately, by someone else.

CORE PHILOSOPHY — follow this order, never reverse it:
Recognition -> Relevance -> Brand Context -> Experience First -> Future
Collaboration -> One CTA.
The creator should feel: you found THEIR specific content, there's a real
reason you contacted THEM, you're offering value before asking for
anything, and this reads like a conversation, not a pitch.

SKELETON SELECTION — pick the one that matches what the brand actually sells:
- Physical product / consumer brand (apparel, food, beauty, outdoor, pets) -> Skeleton A: Experience First. Offer to send the product, no strings.
- Events, hotels, restaurants, travel -> Skeleton B: Invitation First.
- App / software / early-stage tech -> Skeleton C: Early Access.
- B2B / developer tools / professional software -> Skeleton D: Demo First.
- Memberships, courses, communities -> Skeleton E: Community First (offer access).
- Novel/Kickstarter-style/unique consumer product -> Skeleton F: Curiosity First (explain the product simply, then offer to send it).
Always offer the smallest possible commitment — never ask for a call, a
meeting, or a formal collaboration in this first message.

WRITING RULES (hard constraints, not suggestions):
- Maximum 90 words.
- Sounds like one person talking to another — natural, friendly, simple.
- Never sound like a company or like marketing copy.
- Never over-compliment or exaggerate.
- Never use: "exciting opportunity", "collaboration opportunity",
  "ambassador program", "influencer program", "campaign",
  "partnership opportunity", or similar buzzwords.
- Never include: links, commission, pricing, affiliate percentages,
  contracts, sponsorship details, media kits, ROI, engagement rates, or
  follower counts.
- Never ask for a meeting or call, and never stack more than one CTA.
- End with exactly ONE clear, low-pressure call to action.
"""


def with_backoff(fn, *args, **kwargs):
    last_error = None
    for attempt in range(MAX_RETRIES):
        try:
            return fn(*args, **kwargs)
        except gspread.exceptions.APIError as e:
            last_error = e
            wait = 2 ** attempt
            print(f"[sheets] API error ({e}), retrying in {wait}s...")
            time.sleep(wait)
    raise RuntimeError(f"Exceeded {MAX_RETRIES} retries writing to Google Sheets") from last_error


def extract_claude_text(response) -> str:
    for block in response.content:
        if getattr(block, "type", None) == "text":
            return block.text
    block_types = [getattr(b, "type", "unknown") for b in response.content]
    raise RuntimeError(f"No text block in Claude response — got: {block_types}")


def tavily_extract(url: str, timeout: int = 15) -> str:
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
    results = resp.json().get("results", [])
    if not results:
        return ""
    return (results[0].get("raw_content") or results[0].get("content") or "")[:8000]


def summarize_brand_context(website_url: str) -> dict:
    if not website_url:
        return {}
    page_text = tavily_extract(website_url if website_url.startswith("http") else f"https://{website_url}")
    if not page_text:
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
        model=SONNET_MODEL, max_tokens=1024, messages=[{"role": "user", "content": prompt}],
    )
    text = re.sub(r"^```json|```$", "", extract_claude_text(response).strip()).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return {}


# ============================================================
# CONTACT HISTORY
# ============================================================

def get_or_create_contact_history_tab(sheet):
    try:
        return sheet.worksheet("Contact History")
    except gspread.WorksheetNotFound:
        ws = sheet.add_worksheet(title="Contact History", rows=2000, cols=len(CONTACT_HISTORY_HEADERS) + 2)
        with_backoff(ws.append_row, CONTACT_HISTORY_HEADERS)
        return ws


def load_contact_history_index(ws) -> dict:
    rows = with_backoff(ws.get_all_records)
    index = {}
    for i, r in enumerate(rows, start=2):
        key = (r.get("dedup_key"), r.get("brand_name"))
        if key[0] and key[1]:
            index[key] = {
                "row_num": i,
                "outreach_outcome": (r.get("outreach_outcome") or "").strip().lower(),
                "first_contact_date": r.get("first_contact_date", ""),
            }
    return index


def check_contact_status(index: dict, dedup_key: str, brand_name: str) -> dict:
    entry = index.get((dedup_key, brand_name))
    if not entry:
        return {"should_skip": False, "reason": "", "prior_outcome": None}
    outcome = entry["outreach_outcome"]
    if outcome in HARD_SKIP_OUTCOMES:
        return {"should_skip": True, "reason": f"Prior outreach already resolved as '{outcome}' — not re-pitching.",
                "prior_outcome": outcome}
    if outcome:
        return {"should_skip": False,
                "reason": f"Prior contact exists (outcome: '{outcome}') — review before sending as a follow-up.",
                "prior_outcome": outcome}
    return {"should_skip": False, "reason": "Prior contact exists, no outcome recorded yet.", "prior_outcome": "pending"}


def upsert_contact_record(ws, index: dict, creator: dict, brand_name: str, campaign: str = ""):
    key = (creator["dedup_key"], brand_name)
    today = datetime.now(timezone.utc).date().isoformat()

    if key in index:
        row_num = index[key]["row_num"]
        header = with_backoff(ws.row_values, 1)
        with_backoff(ws.update_cell, row_num, header.index("last_contact_date") + 1, today)
        if campaign:
            with_backoff(ws.update_cell, row_num, header.index("last_campaign") + 1, campaign)
        return

    row = [
        creator["dedup_key"], creator.get("platform", ""), creator.get("username", ""),
        brand_name, today, today, campaign, "", "pending", "",
        creator.get("contact_email", "") or "", "", "",
    ]
    with_backoff(ws.append_row, row)
    index[key] = {"row_num": None, "outreach_outcome": "pending", "first_contact_date": today}


# ============================================================
# DM DRAFTING
# ============================================================

def draft_dm(creator: dict, brand_name: str, brand_context: dict, prior_contact_note: str = "") -> dict:
    prior_block = ""
    if prior_contact_note:
        prior_block = f"\nNOTE: {prior_contact_note} Write this as a natural follow-up, not a cold open, if that fits.\n"

    prompt = f"""{DM_FRAMEWORK_INSTRUCTIONS}

BRAND
Name: {brand_name}
Description: {brand_context.get('product_summary', 'N/A')}
Tone: {brand_context.get('brand_tone', 'casual, self-aware, friendly')}
Audience: {brand_context.get('audience_signals', 'N/A')}

CREATOR
Handle: @{creator.get('username')}
Platform: {creator.get('platform')}
Niche: {creator.get('Niche 1', creator.get('niche', ''))}
Content angle: {creator.get('content_angle', '')}
Why they were shortlisted (your own reviewer's note — use this, don't invent a different one):
{creator.get('fit_reasoning')}
{prior_block}
TASK — do this in order:
1. Write personalization_notes: 2-4 short observations about this creator and
   exactly what you plan to reference in the DM, based on the fit reasoning
   and content angle above.
2. Pick the correct skeleton per the selection rules above.
3. Write the DM using ONLY what's in personalization_notes as the specific
   reference.

Return ONLY valid JSON, no preamble, no markdown fences:
{{
  "personalization_notes": "<2-4 short observations + what you'll mention>",
  "chosen_skeleton": "<letter + short name, e.g. 'A - Experience First'>",
  "final_dm": "<the actual DM text, ready to send, <=90 words>"
}}"""

    client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    response = client.messages.create(
        model=SONNET_MODEL, max_tokens=1536, messages=[{"role": "user", "content": prompt}],
    )
    text = re.sub(r"^```json|```$", "", extract_claude_text(response).strip()).strip()

    try:
        return json.loads(text)
    except json.JSONDecodeError:
        print(f"[dm_drafting] Failed to parse JSON for @{creator.get('username')}: {text}")
        return {
            "personalization_notes": creator.get("fit_reasoning", ""),
            "chosen_skeleton": "unknown",
            "final_dm": text,
        }


def draft_all_pending(campaign: str, brand_name: str, brand_website: str = "", brand_brief: str = ""):
    """campaign filters which Shortlist rows this run even looks at — with
    multiple campaigns sharing one Shortlist tab, drafting without this
    filter would draft DMs for every pending row regardless of which brand
    approved it, using whichever brand_name/brief happened to be passed in
    for THIS run. Rows are also required to have outreach_channel == "dm";
    an "email" or "none" row with fit_reasoning filled in is a real,
    intentional decision that this function must not silently override."""
    brand_context = summarize_brand_context(brand_website) if brand_website else {}
    # If a typed brief was given (same BRAND_BRIEF used in the discovery run),
    # it overrides the scraped product_summary so the DM persona stays aligned
    # with whatever targeting language was used to find the creator in the
    # first place, rather than drifting from a generic website scrape.
    if brand_brief:
        brand_context["product_summary"] = brand_brief

    creds = Credentials.from_service_account_file(
        os.environ["GOOGLE_SERVICE_ACCOUNT_JSON"], scopes=SHEETS_SCOPES
    )
    client = gspread.authorize(creds)
    sheet = client.open_by_key(os.environ["SPREADSHEET_ID"])
    shortlist_tab = sheet.worksheet("Shortlist")
    contact_history_tab = get_or_create_contact_history_tab(sheet)
    contact_index = load_contact_history_index(contact_history_tab)

    rows = with_backoff(shortlist_tab.get_all_records)
    header = with_backoff(shortlist_tab.row_values, 1)

    for needed in SHORTLIST_EXTRA_HEADERS:
        if needed not in header:
            raise RuntimeError(
                f"Shortlist tab is missing column '{needed}'. Run the 'Sync Shortlist' "
                f"workflow first — it creates the tab with the right columns."
            )

    dm_draft_col = header.index("dm_draft") + 1
    dm_reasoning_col = header.index("dm_reasoning") + 1
    dm_status_col = header.index("dm_status") + 1
    personalization_col = header.index("personalization_notes") + 1

    drafted_count = skipped_count = 0

    for i, r in enumerate(rows, start=2):
        if r.get("Campaign", "") != campaign:
            continue
        if r.get("outreach_channel", "").strip().lower() != "dm":
            continue
        if not r.get("fit_reasoning", "").strip():
            continue
        if r.get("dm_draft", "").strip():
            continue

        status = check_contact_status(contact_index, r.get("dedup_key"), brand_name)
        if status["should_skip"]:
            with_backoff(shortlist_tab.update_cell, i, dm_status_col, f"skipped: {status['reason']}")
            skipped_count += 1
            print(f"[dm_drafting] Skipped @{r.get('username')} — {status['reason']}")
            continue

        result = draft_dm(r, brand_name, brand_context, prior_contact_note=status["reason"])

        with_backoff(shortlist_tab.update_cell, i, personalization_col, result["personalization_notes"])
        with_backoff(shortlist_tab.update_cell, i, dm_draft_col, result["final_dm"])
        with_backoff(shortlist_tab.update_cell, i, dm_reasoning_col, result["chosen_skeleton"])
        with_backoff(shortlist_tab.update_cell, i, dm_status_col, "drafted")

        upsert_contact_record(contact_history_tab, contact_index, r, brand_name, campaign=campaign)

        drafted_count += 1
        print(f"[dm_drafting] Drafted DM for @{r.get('username')} (skeleton: {result['chosen_skeleton']})")

    print(f"[dm_drafting] Done — {drafted_count} new DM draft(s), {skipped_count} skipped "
          f"(already accepted/rejected). Review drafts in the sheet before sending.")


if __name__ == "__main__":
    campaign = os.environ.get("CAMPAIGN", "")
    brand = os.environ.get("BRAND_NAME", "")
    website = os.environ.get("BRAND_WEBSITE", "")
    brief = os.environ.get("BRAND_BRIEF", "")
    missing = [k for k, v in {"CAMPAIGN": campaign, "BRAND_NAME": brand}.items() if not v]
    if missing:
        raise ValueError(
            f"Missing required input(s): {', '.join(missing)} — check they were filled in "
            f"when you triggered the 'Draft DMs' GitHub Actions workflow."
        )
    draft_all_pending(campaign, brand, website, brief)
