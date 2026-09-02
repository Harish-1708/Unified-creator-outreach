import streamlit as st

from auth import login_gate, current_user, logout
from page_state import mark_active_page

st.set_page_config(page_title="Outreach Control Panel", page_icon="📬", layout="wide")

if not login_gate():
    st.stop()

with st.sidebar:
    st.success(f"Logged in as **{current_user()}**")
    if st.button("Log out"):
        logout()
        st.rerun()


def _home():
    mark_active_page("home")
    st.title("📬 Outreach Control Panel")
    st.markdown(
        """
Welcome. This is a **control surface**, not a second sending system —
every Preview, Send, and Check Replies action here either runs the exact
same `outreach.py` logic directly (Preview) or triggers the same GitHub
Actions workflows you'd run manually (Send, Check Replies). Nothing here
bypasses the safety checks already built into the repo: duplicate
protection, per-account capacity, header-verified reply matching, and the
typed `SEND` confirmation gate.

Use the sidebar to navigate:

- **🗂️ Campaigns** — the everyday view, and the only place you need for
  day-to-day work. Search, see every campaign's status (Draft/Running/
  Paused/Completed/Attention needed) at a glance, create a new one right
  from here, and open any campaign for Analytics, Preview, Data,
  Sequences, Schedule, Settings (with Send), and Responses (with Check
  Replies). Start here.
- **💬 Responses** — every reply across every campaign in one place,
  filterable by status, campaign, and unread. Reply directly from here
  too — same tools as each campaign's own Responses tab, just spanning
  everything at once instead of one campaign at a time.
- **📈 Overview** — every campaign at a glance: sent, pending, replies.
- **📊 Dashboard** — deep-dive into one campaign's leads, sends, replies,
  and errors.
- **📧 Email Accounts** — add, edit, or remove sender accounts, see how
  much each has sent today, and their live connection status.
        """
    )


# Explicit titles/icons here (not embedded in filenames) — filename-embedded
# emoji is what caused the sidebar icons to render as broken/garbled
# characters for some people (a filesystem/encoding issue, not a Streamlit
# bug). Page files themselves now have plain ASCII names.
home_page = st.Page(_home, title="Home", icon="📬", default=True)
workspace_page = st.Page("pages/workspace.py", title="Workspace", icon="🧭")
creator_research_page = st.Page("pages/creator_research.py", title="Creator Research", icon="🔎")
dm_queue_page = st.Page("pages/dm_queue.py", title="DM Queue", icon="📱")
campaigns_page = st.Page("pages/campaigns.py", title="Campaigns", icon="🗂️")
settings_page = st.Page("pages/settings.py", title="Settings", icon="⚙️")
responses_page = st.Page("pages/responses.py", title="Responses", icon="💬")
overview_page = st.Page("pages/overview.py", title="Overview", icon="📈")
dashboard_page = st.Page("pages/dashboard.py", title="Dashboard", icon="📊")
email_accounts_page = st.Page("pages/email_accounts.py", title="Email Accounts", icon="📧")

nav = st.navigation([home_page, workspace_page, creator_research_page, dm_queue_page, campaigns_page,
                     settings_page, responses_page, overview_page, dashboard_page, email_accounts_page])
nav.run()
