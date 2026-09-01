# Outreach Control Panel (Streamlit)

A control surface on top of `outreach.py` + GitHub Actions — not a second
sending system. Preview runs the exact same code in-app (read-only, no
SMTP credentials involved). Send, Check Replies, and the Backfill tool
trigger the real GitHub Actions workflows, with the same typed-`SEND`
confirmation gate Send always had. New Campaign / Add Stage commits
directly — no GitHub trip, no pull request to approve.

## What each page does

- **🗂️ Campaigns** — the everyday view, and the only page you need for
  day-to-day work. Every phase (A–H) is now real: search, create a
  campaign inline, see status at a glance, Launch/Pause/Resume, a
  "🗑️ Deleted Campaigns" section to Restore anything temporarily removed,
  and inside a campaign: Analytics, Data, Sequences (+ Delete Variant,
  Delete Stage, and ThreadSubject Maintenance — all near the bottom),
  Schedule, Settings (+ Send, only available while the campaign is
  actually Running, and a Danger Zone with two tiers — Temporarily
  Remove, which just hides it and can be undone, and Permanently Delete,
  which can't be — neither ever touches the Sheet, only templates), and
  Responses (+ Check Replies at the top, reply directly from the app —
  Cc/Bcc, file attachments, correctly threaded into the same
  conversation). Each action lives with the thing it's most related to,
  rather than grouped into its own separate tab — Send sits with sending
  config, Check Replies sits with the replies themselves. A single-lead
  template preview already lives inline in Sequences, so there's no
  separate Preview tab. A fuller quoted-thread view and scheduling a
  reply for later are deliberately not built yet — see below.
- **💬 Responses** — every reply across every campaign in one place, not
  scoped to one campaign like the tab above. Filter by status — sales
  intent (Interested / Not Interested / Lead-Needs-Follow-up / Unclear,
  optional, see Known limitations) alongside the system's own mechanical
  classifications (Genuine Reply, Auto-Reply, Out of Office, Bounce
  Hard/Soft) — by campaign, or to unread only — plus free-text search.
  Click "💬 View full conversation" on any response for the real thread:
  every stage actually sent, re-rendered live from your templates, and
  every reply, in order. Reply directly from here too. Unread tracking
  persists (an explicit "Mark as read" + batched sync) — see Known
  limitations for exactly how.
- **📈 Overview** — every campaign at a glance: total leads, pending,
  sent, replies, reply rate.
- **📊 Dashboard** — read-only deep-dive into one campaign. Uses a
  Viewer-scoped Google credential and the exact same
  `outreach.compute_campaign_dashboard` math the Sheet's own Dashboard tab
  uses, so the two always agree.
- **📧 Email Accounts** — which sender accounts are configured, how much
  each has sent today across all campaigns, and their live connection
  status (🟢 Connected / 🔴 Disconnected with a reason / ⚪ Unknown before
  the first check). **Add, edit, and remove accounts directly here** —
  no more editing `EMAIL_ACCOUNTS_JSON` by hand. Supports Gmail (address +
  app password) and any custom SMTP/IMAP provider (Hostinger, etc. — its
  own host, port, and username, plus a separate IMAP password if the
  provider issues one). Manage one account at a time, or add many at
  once with a CSV upload (an in-app example shows the exact columns).
  A password only ever passes through this app's memory for the instant
  it takes to encrypt and send it to GitHub; it's never stored, logged,
  or displayed. See "Email account management" below for the one-time
  setup this needs.

## One-time setup

### 1. Deploy

Push this repo (must stay **private**) to GitHub, then on
[Streamlit Community Cloud](https://share.streamlit.io):
- New app → pick this repo → main file path: `streamlit_app/app.py`
- Deployed from a private repo, the app is private by default. Add
  colleagues as viewers under the app's "Share" menu if you also want
  GitHub/Google-identity gating in addition to the username/password login
  below (defense in depth, not required).

Free tier note: one private app, ~1GB memory, sleeps after 12h idle (next
visitor waits ~30s). Fine for occasional internal use; upgrade if that
becomes annoying.

### 2. Create a read-only Google service account

Separate from the one `GOOGLE_SERVICE_ACCOUNT_JSON` (GitHub Actions) uses.

1. In Google Cloud Console, create a second service account (e.g.
   `streamlit-readonly`).
2. Download its JSON key.
3. Open your Google Sheet → Share → paste that service account's
   `client_email` → give it **Viewer** access (not Editor).

### 3. Create a fine-grained GitHub token

Settings → Developer settings → Fine-grained personal access tokens → New
token, scoped to **only this repository**:
- `Actions`: Read and write (Send, Check Replies, Backfill, status
  polling)
- `Contents`: Read and write (New Campaign / Add Stage, template edits,
  campaign settings — all commit files directly)
- `Secrets`: Read and write — **only if you want the Email Accounts
  page's Add/Edit/Remove buttons to work.** This is a materially bigger
  grant than the other two: it lets the token overwrite or delete your
  sending accounts' credentials (it still can't ever read an existing
  one back — GitHub Secrets don't support that for any token). Skip this
  scope entirely if you'd rather keep managing `EMAIL_ACCOUNTS_JSON` by
  hand; everything else in this app works fine without it.

No `Pull requests` permission needed — campaign creation no longer opens
one.

### 4. Set up login credentials

For each colleague:

```bash
python streamlit_app/tools/generate_password_hash.py
```

Paste the printed `[auth_users.<name>]` block into Streamlit Secrets. No
plaintext password is ever stored — only a salted PBKDF2 hash.

### 5. Fill in Streamlit Secrets

Copy `secrets.toml.example`, fill in real values, paste into the app's
Secrets settings in Streamlit Community Cloud (never commit a real
secrets.toml to the repo). Includes an optional `[email_accounts_directory]`
block (names + addresses only, no passwords) — only needed for accounts
still managed the legacy way; accounts added through the app's own Add
Account button need nothing added here.

### 6. Email account management (optional)

Skip this entirely if you're fine managing `EMAIL_ACCOUNTS_JSON` by hand
— everything else in this app works without it. To use the Add/Edit/
Remove buttons on the Email Accounts page instead:

1. Add the `Secrets: Read and write` scope to your GitHub token (see
   step 3 above) — this is the only setup step; nothing extra goes in
   Streamlit Secrets or `EMAIL_ACCOUNTS_JSON`.
2. That's it. Each account you add through the app lives in its own
   `EMAIL_ACCOUNT_SLOT_N` secret (10 slots by default —
   `outreach.EMAIL_ACCOUNT_SLOT_COUNT`), tracked by name/slot/address in
   a small, non-secret file the app commits itself
   (`config/email_account_slots.yaml`) — never a password in that file,
   only in the actual encrypted secret.
3. **Migrating an existing account** already in `EMAIL_ACCOUNTS_JSON`:
   use Add Account with the *same name*. A slot always takes precedence
   over a same-named `EMAIL_ACCOUNTS_JSON` entry, so this adopts it under
   the app's management immediately — remove the old entry from
   `EMAIL_ACCOUNTS_JSON` whenever you're ready; there's no rush, and
   nothing breaks either way in the meantime.

## Known limitations (by design, not bugs)

- **Intent classification (Interested / Not Interested / Lead-Needs-Follow-up
  / Unclear) is a genuinely separate layer from mechanical classification**
  (Genuine Reply / Auto-Reply / Out of Office / Bounce), only ever run for
  a Genuine Reply, only once per reply ever (the same Message-ID dedup
  that already prevents logging a reply twice also prevents re-classifying
  it). Optional — set `ANTHROPIC_API_KEY` as a secret to enable it; leave
  it unset and Intent columns just stay blank, exactly like before this
  existed. A low-confidence result always shows as "Unclear" rather than
  a specific category — never trust an uncertain guess at face value on
  something that could affect a real business decision.
- **Unread tracking persists across sessions now** — a response's
  `IsRead` column in the Response Sheet is the source of truth, marked
  via an explicit "✓ Mark as read" button. Marking is batched: click
  "🔄 Sync read status (N pending)" to actually write it back, rather
  than triggering a GitHub Actions run per response. Between marking and
  syncing, the current session shows it as read immediately (an
  optimistic local overlay) without waiting for the sync to land.
- **This app never connects to SMTP/IMAP directly** — only GitHub
  Actions does, using a credential Streamlit never holds. Real email
  credentials living inside a public-facing web app would be a
  meaningfully bigger risk than anything else here. "Check Replies Now"
  (per-campaign or all-at-once on the Responses page) gives you an
  on-demand check without waiting for the schedule; shortening
  `check_replies.yml`'s cron interval is the safe way to reduce that
  wait generally, if 30 minutes is too slow for your use case.

- **Deleting a campaign has two tiers.** "Temporarily Remove" (Settings →
  Danger Zone) just changes its status — the campaign disappears from
  the everyday Campaigns list but shows up in "🗑️ Deleted Campaigns"
  (Campaigns Hub page) with a Restore button, nothing about it is
  touched. "Permanently Delete" removes its template files (and its
  settings override, if any) outright and isn't reversible from this
  app. Neither ever touches the Google Sheet — leads, sends, and replies
  stay exactly where they are, fully readable directly in the Sheet,
  either way.
- **You can only delete the *last* stage**, never a middle one — stages
  must stay contiguous from Intro (the same rule
  `outreach.discover_stages_and_variants` already enforces), so deleting
  a middle stage would silently orphan every stage after it.
- **Deleting a variant always removes it from every stage at once**,
  never just one — every stage must offer the exact same variant
  letters, so a per-stage deletion would immediately break that
  invariant. You also can't delete the last remaining variant.

- **Only 10 account slots by default** (`EMAIL_ACCOUNT_SLOT_1..10`) —
  deliberately not jumped straight to a large number; if you need more,
  raise `EMAIL_ACCOUNT_SLOT_COUNT` in `outreach.py` and add the matching
  `EMAIL_ACCOUNT_SLOT_N` lines to every workflow's `env:` block that
  calls `load_email_accounts()` (`check_account_health.yml`,
  `check_replies.yml`, `send_batch.yml`, `send_reply.yml`).
- **Account health is a snapshot, not a log.** Every check overwrites the
  whole "Email Accounts Health" tab — a removed account's old row
  disappears on the next run rather than lingering.
- **A bulk CSV account upload makes one GitHub API call per account** —
  unavoidable, since each account is its own secret and GitHub has no
  "set several secrets at once" endpoint. A very large upload (100+ rows)
  will take a little while; the mapping file itself is still committed
  once for the whole batch, not once per account.

- **Replying supports file/image attachments, up to 10 MB total.** Sent
  as regular email attachments, not inline/embedded HTML images — this
  codebase doesn't have an HTML-email body path, so an "image in the
  message body" the way a rich-text editor shows it isn't built. A fuller
  quoted email-thread view (like a real inbox shows) also isn't built —
  this only shows the response's snippet, not the full back-and-forth.
- **No "schedule a reply for later."** Every reply sends within a minute
  or two of clicking Send — there's no deferred/scheduled send yet. That
  would need a genuinely new subsystem (a pending-sends store plus a
  periodic checker), deliberately left for later rather than bolted on.
- **Every GitHub Actions workflow that pipes its output through `tee`
  now uses `set -o pipefail` first.** Without it, `command | tee file`
  reports the exit code of `tee` (which almost always succeeds), not
  `command` — meaning a real crash or failure inside `outreach.py` could
  previously show as a green checkmark in the Actions tab. Found while
  building the reply-send workflow (which specifically needed a correct
  non-zero exit on failure) and fixed retroactively across every
  existing workflow, including `send_batch.yml`.

- **Launch/Pause/Resume are never gated by campaign readiness.** The
  readiness check (no sender configured, no templates, no approved
  leads) is shown as an FYI on the Launch confirmation, never a blocker —
  `outreach.send_batch()` already naturally does nothing if there's
  nothing eligible to send, so blocking Launch on it would just be
  friction with no real safety benefit.

- **New Campaign is an inline modal now, not a separate page** — and
  deliberately asks for nothing but a name. Git can't store an empty
  folder, so a placeholder Intro template is created automatically; write
  the real email afterward in the Sequences tab. Right after creating a
  campaign, it deliberately does NOT auto-navigate into it — Streamlit
  Cloud's local checkout won't have the new file until it redeploys
  (triggered by the commit, not instant), so jumping straight in would
  likely hit a real "No templates found" error. The same redeploy delay
  applies more mildly to Sequences/Settings/Schedule saves (you may
  briefly see the old values reflected here — the change is already live
  for actual sending regardless).
- **Any page showing a dialog/form driven by session_state calls
  `page_state.mark_active_page()` at its top.** Without it, a dialog left
  open (not explicitly cancelled) would silently reopen every time you
  returned to that page from somewhere else — session_state persists
  across navigation, and a plain "is this open" flag has no way to tell
  "still filling this out" apart from "came back much later." Add the
  same call to any future page that needs this pattern.
- **Schedule restricts Send Batch, never Preview.** A campaign outside
  its configured sending window can still be freely previewed; only an
  actual Send is blocked, with a clear reason. No schedule configured
  (the default) means "always allowed," exactly matching every
  campaign's behavior before this feature existed.
- **Timezones are always real IANA names** (e.g. `America/Los_Angeles`),
  never fixed offsets like "PST" — this is what makes Daylight Saving
  transitions handled correctly automatically, rather than silently
  sending an hour off twice a year.

- **`create_file` now correctly handles updating files that already
  exist** (fetches the current SHA first, as GitHub's API requires) —
  this was a latent bug from Phase D that Phase F's settings-file
  overwrites would have hit immediately; fixed retroactively for both.
- **Settings are saved to the campaign's config override file**
  (`config/campaigns/<name>.yaml`), touching only the `sending` key —
  `status`, `schedule`, and anything else already in that file survive a
  Settings save untouched.

- **Variants are campaign-wide, not per-stage.** Every stage must offer
  the exact same variant letters (a hard rule in `outreach.py` itself —
  see `discover_stages_and_variants`). "Add a variant" in Sequences
  therefore always adds it to every existing stage at once, in one
  commit, never just one stage.
- **Template edits are locked by default.** Unlock a variant to edit it;
  Save Changes commits everything you've unlocked and changed as ONE
  commit, not one per field — same batching principle as everywhere else
  writes happen in this app.

- **Sheets reads on the campaign detail page are cached for 30 seconds.**
  This isn't just a performance nicety — Google's Sheets API caps reads at
  60/minute/user, and Streamlit reruns the entire script on nearly every
  widget interaction. Without caching, a few minutes of ordinary use
  (adjusting several CSV mapping dropdowns in a row, for example) could
  exceed that quota and return a `429`. If you need to see a change
  immediately rather than waiting up to 30s, use the **🔄 Refresh data**
  button at the top of the campaign detail page.

- **Leads are imported/removed via a commit-then-trigger-workflow
  pattern**, same as template creation — Streamlit commits a JSON payload
  file (`imports/<campaign>/...` or `removals/<campaign>/...`), then
  triggers `import_leads.yml` / `remove_leads.yml`, which does the actual
  Sheet write with the Editor-scoped credential and deletes the payload
  file afterward. Streamlit itself never gets Sheets write access, here
  or anywhere else in this app.
- **Removing a lead never deletes anything.** It sets `Status = Removed`
  — the row, and everything ever sent to that lead, stays intact. A
  removed lead is simply excluded from all future eligibility checks.
- **New leads always start as Pending approval**, even if your CSV had an
  "Approval" column you didn't map — approve them in the Data tab (or the
  Master Sheet directly) before they're eligible to send.

- **A campaign's `status` (draft/active/paused) lives in its config
  override file** (`config/campaigns/<name>.yaml`), not the Sheet.
  Pausing/resuming currently means editing that file directly (a proper
  Pause/Resume button is Phase G). Unset `status` always means "active" —
  this was chosen specifically so introducing the field never silently
  paused a pre-existing campaign.
- **No persistent login session.** Username/password here is intentionally
  simple — no OAuth means no "forgot password" flow and no cross-session
  cookie. Closing the tab logs you out. If this becomes annoying,
  `streamlit-authenticator` (cookie-based) or Streamlit's native
  `st.login()`/OIDC are the upgrade paths.
- **Run status is manual-refresh, not live-streaming.** After triggering
  Send/Check Replies/Backfill, click "Refresh run status" — this app
  doesn't auto-poll in the background. A link to the full GitHub Actions
  run is always shown for complete logs.
- **New Campaign only creates the Intro stage** when starting a brand new
  campaign. Auto-discovery treats a single-stage campaign as fully valid.
  Use "Add the next stage to an existing campaign" (same page) to add
  follow-ups later.
- **Campaign creation is a direct commit, not a PR.** The in-app "I've
  reviewed this content" checkbox is the only remaining confirmation step
  — there's no second human review before it's live. If you want that
  back, the previous PR-based flow is straightforward to restore (open an
  issue/ask if you need it).

## Testing

`streamlit_app/tests/` covers every non-UI module (auth, github_client,
sheets_readonly, preview_logic's pure pieces, send_logic, campaign_builder,
overview_logic, replies_logic, accounts_logic) with mocked HTTP/Sheets
calls, plus page-level smoke tests that actually execute each page script
via Streamlit's own `AppTest` harness — no real network, no real
credentials needed. Run with:

```bash
cd streamlit_app
python -m pytest tests/ -v
```

This does **not** verify a live deployment — the Streamlit UI itself, the
real GitHub token, and the real Google credential all need one manual pass
against your actual repo/Sheet after deploying. Use the checklist below.

## Manual verification checklist (do this once, after deploying)

Everything above is verified with mocked Google/GitHub calls — this
section is the real pass against your actual repo, Sheet, and GitHub
Actions. Go through it in order; each step depends on the one before it.

**Setup**
- [ ] Read-only service account created, shared to the Sheet as **Viewer**
      (not Editor) — confirm in the Sheet's Share dialog.
- [ ] Fine-grained GitHub PAT created, scoped to **only this repo**.
- [ ] `secrets.toml` filled in on Streamlit Community Cloud and the app
      deploys without a "secrets not found" error.
- [ ] At least one user's hash generated via
      `tools/generate_password_hash.py` and added to `[auth_users]`.

**Login**
- [ ] Wrong password is rejected with an error, correct password logs in.
- [ ] 5 wrong attempts in a row lock you out (matches the automated test —
      confirming the real deployment behaves the same as the mocked one).
- [ ] "Log out" in the sidebar actually requires logging in again.
- [ ] Sidebar icons render correctly (📈 📊 🚀 📧 ➕), not as garbled text —
      if they still look broken, hard-refresh the browser tab first.

**Overview / Dashboard (read-only — safe to test freely)**
- [ ] Campaign selector lists your real campaign(s) from `templates/`.
- [ ] Numbers shown match the Sheet's own Dashboard tab (run
      `dashboard.yml` manually first if it hasn't run recently, so both
      are reading the same underlying data).
- [ ] "Refresh now" actually re-fetches (change something in the Sheet by
      hand, confirm it shows up after refresh, not just after 30s).

**Campaigns — Preview tab (safe, nothing is sent)**
- [ ] Preview returns the same eligible leads and rendered content you'd
      get from `python outreach.py preview` locally, for the same
      campaign/stage/batch size.
- [ ] A lead with `Approval` blank or `No` correctly does NOT appear.

**Campaigns — Settings tab's Send section (uses a REAL test campaign /
low daily limit for this)**
- [ ] Send is only offered while the campaign's status is 🟢 Running —
      switch it to Draft/Paused and confirm the Send button disappears,
      replaced by an explanation, with nothing dispatched.
- [ ] Submitting without typing `SEND` is rejected — no dispatch call
      happens (check the repo's Actions tab: no new run appears).
- [ ] Typing `SEND` and clicking Send Batch actually triggers a real
      `send_batch.yml` run — confirm in the repo's Actions tab.
- [ ] "Refresh run status" reflects real progress (queued → in_progress →
      completed).
- [ ] The Sheet's Send Log gets the new row(s) after the run completes,
      and the Analytics tab reflects them after "🔄 Refresh data".

**Campaigns — Responses tab's Check Replies section**
- [ ] Triggers a real `check_replies.yml` run, visible in the Actions tab.
- [ ] The reply list shows real Response Sheet rows, with `ActionTaken`
      clearly labeled (🛑 vs 📝, "NOT stopped") when a reply did NOT stop
      the sequence.

**Campaigns — Sequences tab's Maintenance section (Backfill)**
- [ ] Dry run shows what would be backfilled without writing anything.
- [ ] Turning off dry run and re-running actually writes `ThreadSubject`
      values to the Master Sheet.

**Email Accounts**
- [ ] Shows every account (from `[email_accounts_directory]` and/or the
      slot mapping file), with today's real send count from the Send Log
      and live connection status.
- [ ] "＋ Add Account" is disabled until the confirmation checkbox is
      checked; submitting creates a real `EMAIL_ACCOUNT_SLOT_N` secret —
      confirm in the repo's Settings → Secrets tab (you'll see the name,
      never the value) and a real commit to `config/email_account_slots.yaml`.
- [ ] Editing an account with the password field left blank updates only
      the address (check the commit only touched the mapping file, not
      a secret) — filling in a new password updates the secret too.
- [ ] Removing requires the confirmation checkbox; confirm the secret is
      actually gone from Settings → Secrets afterward.

**New Campaign — via the Campaigns tab's ➕ New Campaign, creates a REAL
campaign, live immediately**
- [ ] The "Create Campaign" / "Add Stage" button is disabled until the
      confirmation checkbox is checked.
- [ ] Submitting commits directly — check the repo's commit history, NOT
      the Pull Requests tab (there shouldn't be one).
- [ ] The campaign appears in the Campaigns list within a minute or two,
      with its Sheet tabs already created (no "tab doesn't exist" error)
      — this confirms the auto-triggered Dashboard-workflow tab
      initialization worked.
- [ ] Adding a follow-up stage (Sequences tab → "Add a follow-up stage")
      only offers variant letters matching the campaign's existing
      ones — try it on a multi-variant test campaign to confirm.

If any step fails, check the specific module it exercises (`auth.py`,
`github_client.py`, `sheets_readonly.py`, `campaign_builder.py`) against
the automated tests for that module first — a live-only failure usually
means a secrets/permission mismatch, not a logic bug (the logic is what
the automated test suite covers).
