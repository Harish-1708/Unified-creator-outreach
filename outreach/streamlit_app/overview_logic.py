"""Pure logic for the cross-campaign Overview page. Reuses
outreach.compute_all_campaigns_row directly — the exact same math the
CLI's `dashboard --all` command uses — plus one derived column
(Pending = Total Leads - Unique Contacted) since "how much is left to
send" was the actual question this page exists to answer.
"""
import os
import sys
from typing import Dict, List, Tuple

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

import outreach  # noqa: E402

OVERVIEW_COLUMNS = outreach.ALL_CAMPAIGNS_DASHBOARD_COLUMNS[:2] + ["Pending (Not Yet Contacted)"] + \
    outreach.ALL_CAMPAIGNS_DASHBOARD_COLUMNS[2:]


def build_campaign_overview_row(campaign_cfg: Dict, leads: List[Dict], responses: List[Dict],
                                 send_log: List[Dict]) -> List[str]:
    """One campaign's row, in OVERVIEW_COLUMNS order."""
    stages = campaign_cfg["stages"]
    base_row = outreach.compute_all_campaigns_row(campaign_cfg["_campaign_name"], leads, responses, send_log, stages)
    total_leads = int(base_row[1])
    contacted = int(base_row[2])
    pending = max(total_leads - contacted, 0)
    return base_row[:2] + [str(pending)] + base_row[2:]


def build_all_campaigns_overview(
    campaign_names: List[str],
    fetch_campaign_data,  # callable: (name) -> (campaign_cfg, leads, responses, send_log) or raises
) -> Tuple[List[List[str]], List[Tuple[str, str]]]:
    """Returns (rows, errors). `errors` is [(campaign_name, message)] for
    any campaign whose data couldn't be read (e.g. it's never had a
    Preview/Send/Check Replies run yet, so its tabs don't exist) — those
    are skipped from `rows` rather than failing the whole page."""
    rows: List[List[str]] = []
    errors: List[Tuple[str, str]] = []
    for name in campaign_names:
        try:
            campaign_cfg, leads, responses, send_log = fetch_campaign_data(name)
        except Exception as exc:  # noqa: BLE001 - one campaign's read failure shouldn't block the rest
            errors.append((name, str(exc)))
            continue
        rows.append(build_campaign_overview_row(campaign_cfg, leads, responses, send_log))
    return rows, errors
