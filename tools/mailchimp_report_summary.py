"""
Tool: mailchimp_report_summary.py
Purpose: Deterministic, rule-based "Founder's Summary" generator, structured
         per the Engagement Intelligence spec into six labeled sections:
         WHAT HAPPENED / WHERE ENGAGEMENT HAPPENED / WHO ENGAGED /
         WHAT CHANGED / REPEATED BEHAVIOR / DATA WARNINGS.

Important: this is intentionally NOT a live LLM call. Per the WAT framework's
own separation of concerns (workflows reason, tools execute deterministically),
a daily cron job should not depend on a network LLM call to describe numbers
it already has. Every sentence here is generated directly from computed
metrics/engagement data — no causal explanations, no intent/interest
inference, no "lead" or "hot prospect" language. Behavior only:

  GOOD: "12 contacts clicked more than one link."
  GOOD: "Download Compliance Guide generated the highest number of unique clicks."
  BAD:  "These contacts are highly interested in buying."
  BAD:  "The campaign succeeded because the subject line was better."
"""

from __future__ import annotations

from typing import Optional


def _fmt_pct(x: float) -> str:
    return f"{x * 100:.1f}%"


def _fmt_pp(x: float) -> str:
    sign = "+" if x >= 0 else ""
    return f"{sign}{x:.1f} percentage points"


def _short_link_label(url: Optional[str]) -> str:
    if not url:
        return "unknown link"
    # Strip query string / scheme for a readable label; never invent a CTA name.
    label = url.split("?")[0]
    if len(label) > 70:
        label = label[:67] + "..."
    return label


def build_founders_summary(
    current_agg: dict,
    previous_agg: Optional[dict],
    trends: dict,
    campaigns: list[dict],
    top_links: Optional[list[dict]] = None,
    top_clickers: Optional[list[dict]] = None,
    multi_link_engagers: Optional[list[dict]] = None,
    cross_campaign_engagers: Optional[list[dict]] = None,
    anomalies: Optional[list[str]] = None,
    data_warnings: Optional[list[str]] = None,
) -> dict[str, list[str]]:
    """
    Returns {"what_happened": [...], "where_engagement": [...], "who_engaged": [...],
             "what_changed": [...], "repeated_behavior": [...], "data_warnings": [...]}
    Every list is a set of short, factual sentences. Any section with nothing
    to report gets a single explanatory sentence rather than being silently empty.
    """
    top_links = top_links or []
    top_clickers = top_clickers or []
    multi_link_engagers = multi_link_engagers or []
    cross_campaign_engagers = cross_campaign_engagers or []
    anomalies = anomalies or []
    data_warnings = list(data_warnings or [])

    sections: dict[str, list[str]] = {
        "what_happened": [],
        "where_engagement": [],
        "who_engaged": [],
        "what_changed": [],
        "repeated_behavior": [],
        "data_warnings": [],
    }

    # ── WHAT HAPPENED ─────────────────────────────────────────────────
    n = current_agg.get("campaigns_count", 0)
    if n == 0:
        sections["what_happened"].append("No campaigns were sent in this reporting period.")
    else:
        sections["what_happened"].append(
            f"{n} campaign{'s' if n != 1 else ''} sent this period, reaching "
            f"{current_agg['delivered']:,} delivered recipients with an open rate of "
            f"{_fmt_pct(current_agg['open_rate'])} and a click rate of {_fmt_pct(current_agg['click_rate'])}."
        )

    # ── WHERE ENGAGEMENT HAPPENED ────────────────────────────────────
    if len(campaigns) > 1:
        by_open = sorted(campaigns, key=lambda c: c["open_rate"], reverse=True)
        best = by_open[0]
        sections["where_engagement"].append(
            f"Most-engaged campaign: \"{best.get('campaign_title') or best['campaign_id']}\" "
            f"at {_fmt_pct(best['open_rate'])} open rate, {_fmt_pct(best['click_rate'])} click rate."
        )
    elif len(campaigns) == 1:
        c = campaigns[0]
        sections["where_engagement"].append(
            f"\"{c.get('campaign_title') or c['campaign_id']}\" — {_fmt_pct(c['open_rate'])} open rate, "
            f"{_fmt_pct(c['click_rate'])} click rate."
        )
    if top_links:
        top = top_links[0]
        sections["where_engagement"].append(
            f"Most-clicked link: {_short_link_label(top.get('url'))} "
            f"({top.get('total_clicks', 0):,} total clicks, {_fmt_pct(top.get('click_share', 0))} of this campaign's clicks)."
        )
    if not sections["where_engagement"]:
        sections["where_engagement"].append("No campaign or link engagement data available for this period.")

    # ── WHO ENGAGED ───────────────────────────────────────────────────
    if top_clickers:
        names = []
        for c in top_clickers[:5]:
            label = c.get("name") or c.get("email") or "unknown contact"
            company = f" ({c['company']})" if c.get("company") else ""
            names.append(f"{label}{company} — {c.get('clicks', 0)} click(s)")
        sections["who_engaged"].append("Most active contacts by click count: " + "; ".join(names) + ".")
    if multi_link_engagers:
        sections["who_engaged"].append(
            f"{len(multi_link_engagers)} contact(s) clicked more than one distinct link in this period."
        )
    if not sections["who_engaged"]:
        sections["who_engaged"].append("No individual contact-level click activity recorded for this period.")

    # ── WHAT CHANGED ──────────────────────────────────────────────────
    if trends.get("has_previous_period"):
        opn = trends["open_rate"]
        cr = trends["click_rate"]
        if opn:
            verb = "increased" if opn["direction"] == "up" else ("decreased" if opn["direction"] == "down" else "held steady")
            sections["what_changed"].append(
                f"Open rate {verb} from {opn['previous_pct']:.1f}% to {opn['current_pct']:.1f}% "
                f"({_fmt_pp(opn['change_pct_points'])}) compared with the previous period."
            )
        if cr:
            verb = "increased" if cr["direction"] == "up" else ("decreased" if cr["direction"] == "down" else "held steady")
            sections["what_changed"].append(
                f"Click rate {verb} from {cr['previous_pct']:.1f}% to {cr['current_pct']:.1f}% "
                f"({_fmt_pp(cr['change_pct_points'])}) compared with the previous period."
            )
        ur = trends.get("unsubscribe_rate")
        if ur and ur["direction"] == "up" and ur["change_pct_points"] > 0.05:
            sections["what_changed"].append(
                f"Unsubscribe rate increased ({_fmt_pp(ur['change_pct_points'])}) compared with the previous period."
            )
        br = trends.get("bounce_rate")
        if br and br["direction"] == "up" and br["change_pct_points"] > 0.5:
            sections["what_changed"].append(
                f"Bounce rate increased ({_fmt_pp(br['change_pct_points'])}) compared with the previous period."
            )
    else:
        sections["what_changed"].append("No prior comparable period is available yet — trend comparison will appear from the next report onward.")

    # ── REPEATED BEHAVIOR ─────────────────────────────────────────────
    if cross_campaign_engagers:
        sections["repeated_behavior"].append(
            f"{len(cross_campaign_engagers)} contact(s) have engaged with more than one campaign in the stored history."
        )
        top = cross_campaign_engagers[0]
        label = top.get("name") or top.get("email") or "unknown contact"
        sections["repeated_behavior"].append(
            f"{label} has engaged with {top['campaigns_engaged']} campaigns "
            f"({top['opens']} opens, {top['clicks']} clicks recorded)."
        )
    repeat_link_clickers = [c for c in top_clickers if c.get("clicks", 0) > 1]
    if repeat_link_clickers:
        sections["repeated_behavior"].append(
            f"{len(repeat_link_clickers)} contact(s) clicked the same link more than once."
        )
    if not sections["repeated_behavior"]:
        sections["repeated_behavior"].append("No repeated engagement (same contact across multiple campaigns or multiple clicks) observed yet in stored history.")

    # ── DATA WARNINGS ─────────────────────────────────────────────────
    sections["data_warnings"].extend(anomalies)
    sections["data_warnings"].extend(data_warnings)
    if current_agg.get("bounce_rate", 0) > 0.02:
        sections["data_warnings"].append(f"Bounce rate this period is {_fmt_pct(current_agg['bounce_rate'])}, above the typical <2% benchmark.")
    if not sections["data_warnings"]:
        sections["data_warnings"].append("No data quality issues detected.")

    return sections


def flatten_summary(sections: dict[str, list[str]]) -> list[str]:
    """Flatten the structured summary into a single ordered list of sentences (e.g. for a plain-text digest)."""
    order = ["what_happened", "where_engagement", "who_engaged", "what_changed", "repeated_behavior", "data_warnings"]
    out: list[str] = []
    for key in order:
        out.extend(sections.get(key, []))
    return out
