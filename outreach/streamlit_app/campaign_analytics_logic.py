"""Restructures outreach.compute_campaign_dashboard's flat
(section, metric, value) rows into the per-stage / per-variant table shape
the Campaigns Hub Analytics tab shows. No new computation — this is
exactly Phase B's promise: the data already exists, this just organizes
it. Parsing is done by section name and a fixed metric-key convention
(f"{stage}-{variant} - Sent" etc.) that compute_campaign_dashboard already
uses, so this stays a thin presentation layer, not a second source of truth.
"""
from typing import Dict, List, Tuple


def group_rows_by_section(rows: List[Tuple[str, str, str]]) -> Dict[str, List[Tuple[str, str]]]:
    grouped: Dict[str, List[Tuple[str, str]]] = {}
    for section, metric, value in rows:
        grouped.setdefault(section, []).append((metric, value))
    return grouped


def build_overview_summary(rows: List[Tuple[str, str, str]]) -> Dict[str, str]:
    grouped = group_rows_by_section(rows)
    return dict(grouped.get("Overview", []))


def build_per_stage_table(rows: List[Tuple[str, str, str]]) -> List[Dict[str, str]]:
    """[{"stage": "intro", "sent": "30"}, ...] in the order stages were
    computed (compute_campaign_dashboard iterates stages in order)."""
    grouped = group_rows_by_section(rows)
    table = []
    for metric, value in grouped.get("Per-Stage", []):
        stage = metric.rsplit(" - Sent", 1)[0]
        table.append({"stage": stage, "sent": value})
    return table


def build_per_variant_table(rows: List[Tuple[str, str, str]]) -> List[Dict[str, str]]:
    """One row per stage-variant combination that's actually been sent:
    [{"stage": "intro", "variant": "A", "sent": "30",
      "replies": "3", "reply_rate": "10.0%"}, ...]"""
    grouped = group_rows_by_section(rows)
    by_key: Dict[str, Dict[str, str]] = {}
    for metric, value in grouped.get("Variant Performance", []):
        # metric is "{stage}-{variant} - Sent" / "... - Replies (approx.)" / "... - Reply Rate (approx.)"
        key, field = metric.rsplit(" - ", 1)
        stage, variant = key.rsplit("-", 1)
        entry = by_key.setdefault(key, {"stage": stage, "variant": variant})
        if field == "Sent":
            entry["sent"] = value
        elif field.startswith("Replies"):
            entry["replies"] = value
        elif field.startswith("Reply Rate"):
            entry["reply_rate"] = value
    # Stable order: by stage-variant key, matching how they were computed.
    return [by_key[k] for k in sorted(by_key.keys())]


def build_sender_table(rows: List[Tuple[str, str, str]]) -> List[Dict[str, str]]:
    grouped = group_rows_by_section(rows)
    by_account: Dict[str, Dict[str, str]] = {}
    for metric, value in grouped.get("Sender Performance", []):
        account, field = metric.rsplit(" - ", 1)
        entry = by_account.setdefault(account, {"account": account})
        if field == "Sent":
            entry["sent"] = value
        elif field == "Replies":
            entry["replies"] = value
        elif field == "Reply Rate":
            entry["reply_rate"] = value
    return [by_account[k] for k in sorted(by_account.keys())]


def build_error_summary(rows: List[Tuple[str, str, str]]) -> List[Dict[str, str]]:
    grouped = group_rows_by_section(rows)
    return [{"error_type": metric, "count": value} for metric, value in grouped.get("Errors (All Time)", [])]
