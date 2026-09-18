"""
Tool: mailchimp_metrics.py
Purpose: Pure, dependency-free metric calculations for the daily Mailchimp report.
         No network calls here — takes raw Mailchimp /reports objects (or the
         aggregated equivalent) and returns computed metrics with correct
         denominators and safe division everywhere.

Kept deliberately free of I/O so it's trivial to unit test.
"""

from __future__ import annotations

from typing import Optional


def safe_div(numerator: float, denominator: float) -> float:
    """Returns 0.0 instead of raising/NaN/inf when denominator is 0."""
    if not denominator:
        return 0.0
    return numerator / denominator


def campaign_metrics(report: dict) -> dict:
    """
    Compute standardized metrics for a single Mailchimp campaign report object
    (as returned by GET /reports or GET /reports/{id}).

    "Delivered" is derived as emails_sent minus hard+soft bounces, since
    Mailchimp's reports API does not return an explicit "delivered" field.

    IMPORTANT — click metric definitions (verified against Mailchimp's own
    schema, see tools/mailchimp_report_client.py docstrings):
      - clicks.unique_clicks is "unique clicks for LINKS across a campaign" —
        i.e. unique-per-link, summed across every link. One person clicking 3
        different links counts as 3. This is NOT a people-count, and Mailchimp's
        own documented click_rate (unique_clicks / deliveries) can legitimately
        exceed 100% on multi-link campaigns.
      - clicks.unique_subscriber_clicks is "total subscribers who clicked" —
        the true people-count, bounded by delivered. This is what we use for
        "unique_clickers" and for click_rate, so click_rate can never exceed
        100% by construction (barring the data anomalies detect_anomalies()
        guards against, e.g. Apple Mail Privacy Protection proxy artifacts).
    """
    emails_sent = report.get("emails_sent", 0) or 0
    bounces = report.get("bounces", {}) or {}
    hard_bounces = bounces.get("hard_bounces", 0) or 0
    soft_bounces = bounces.get("soft_bounces", 0) or 0
    bounce_count = hard_bounces + soft_bounces
    delivered = max(emails_sent - bounce_count, 0)

    opens = report.get("opens", {}) or {}
    clicks = report.get("clicks", {}) or {}

    opens_total = opens.get("opens_total", 0) or 0
    unique_opens = opens.get("unique_opens", 0) or 0
    clicks_total = clicks.get("clicks_total", 0) or 0
    unique_link_clicks = clicks.get("unique_clicks", 0) or 0
    unique_clickers = clicks.get("unique_subscriber_clicks", 0) or 0
    unsubscribes = report.get("unsubscribed", 0) or 0

    return {
        "campaign_id": report.get("id"),
        "campaign_title": report.get("campaign_title"),
        "subject_line": report.get("subject_line"),
        "send_time": report.get("send_time"),
        "recipients": emails_sent,
        "delivered": delivered,
        "opens_total": opens_total,
        "unique_opens": unique_opens,
        "open_rate": safe_div(unique_opens, delivered),
        "clicks_total": clicks_total,
        # People who clicked (bounded by delivered) — use this for "click rate".
        "unique_clicks": unique_clickers,
        "click_rate": safe_div(unique_clickers, delivered),
        # Unique clicks aggregated per-link (NOT a people-count — can exceed
        # delivered on multi-link campaigns). Kept separate and clearly labeled
        # so it's never mistaken for "how many people clicked".
        "unique_link_clicks": unique_link_clicks,
        "click_to_open_rate": safe_div(unique_clickers, unique_opens),
        "unsubscribes": unsubscribes,
        "unsubscribe_rate": safe_div(unsubscribes, delivered),
        "bounces": bounce_count,
        "hard_bounces": hard_bounces,
        "soft_bounces": soft_bounces,
        "bounce_rate": safe_div(bounce_count, emails_sent),
        "abuse_reports": report.get("abuse_reports", 0) or 0,
    }


def detect_anomalies(m: dict) -> list[str]:
    """
    Flag logically-impossible or suspicious metric combinations instead of
    silently presenting them (or clamping them, which would hide the
    underlying data problem). Returns a list of human-readable anomaly
    descriptions — empty if nothing looks wrong.
    """
    issues: list[str] = []
    campaign = m.get("campaign_title") or m.get("campaign_id") or "unknown campaign"

    if m["delivered"] and m["unique_clicks"] > m["delivered"]:
        issues.append(
            f"{campaign}: unique clickers ({m['unique_clicks']}) exceeds delivered "
            f"({m['delivered']}) — metric anomaly detected, requires validation."
        )
    if m["delivered"] and m["unique_opens"] > m["delivered"]:
        issues.append(
            f"{campaign}: unique opens ({m['unique_opens']}) exceeds delivered "
            f"({m['delivered']}) — metric anomaly detected, requires validation."
        )
    if m["click_rate"] > 1.0:
        issues.append(f"{campaign}: click_rate computed above 100% ({m['click_rate']*100:.1f}%) — requires validation.")
    if m["open_rate"] > 1.0:
        issues.append(f"{campaign}: open_rate computed above 100% ({m['open_rate']*100:.1f}%) — requires validation.")
    if m["unique_clicks"] and m["unique_opens"] and m["unique_clicks"] > m["unique_opens"]:
        # Not strictly impossible (Mailchimp can record a click without a matching
        # logged open, e.g. image-blocking clients), but worth surfacing.
        issues.append(
            f"{campaign}: unique clickers ({m['unique_clicks']}) exceeds unique opens "
            f"({m['unique_opens']}) — plausible (opens can go untracked) but flagged for review."
        )
    return issues


def aggregate_metrics(campaigns: list[dict]) -> dict:
    """
    Aggregate a list of campaign_metrics() dicts into period-level totals.

    Rates are recomputed from summed numerators/denominators (NOT averaged
    per-campaign rates) — averaging rates across campaigns of different sizes
    would misrepresent the true period-level rate.
    """
    if not campaigns:
        return {
            "campaigns_count": 0,
            "recipients": 0,
            "delivered": 0,
            "opens_total": 0,
            "unique_opens": 0,
            "open_rate": 0.0,
            "clicks_total": 0,
            "unique_clicks": 0,
            "click_rate": 0.0,
            "click_to_open_rate": 0.0,
            "unsubscribes": 0,
            "unsubscribe_rate": 0.0,
            "bounces": 0,
            "bounce_rate": 0.0,
        }

    recipients = sum(c["recipients"] for c in campaigns)
    delivered = sum(c["delivered"] for c in campaigns)
    opens_total = sum(c["opens_total"] for c in campaigns)
    unique_opens = sum(c["unique_opens"] for c in campaigns)
    clicks_total = sum(c["clicks_total"] for c in campaigns)
    unique_clicks = sum(c["unique_clicks"] for c in campaigns)
    unsubscribes = sum(c["unsubscribes"] for c in campaigns)
    bounces = sum(c["bounces"] for c in campaigns)

    return {
        "campaigns_count": len(campaigns),
        "recipients": recipients,
        "delivered": delivered,
        "opens_total": opens_total,
        "unique_opens": unique_opens,
        "open_rate": safe_div(unique_opens, delivered),
        "clicks_total": clicks_total,
        "unique_clicks": unique_clicks,
        "click_rate": safe_div(unique_clicks, delivered),
        "click_to_open_rate": safe_div(unique_clicks, unique_opens),
        "unsubscribes": unsubscribes,
        "unsubscribe_rate": safe_div(unsubscribes, delivered),
        "bounces": bounces,
        "bounce_rate": safe_div(bounces, recipients),
    }


def rate_delta(current: float, previous: Optional[float]) -> Optional[dict]:
    """
    Percentage-POINT delta between two rates (e.g. 0.314 vs 0.278 -> +3.6pp).
    Returns None if there's no previous value to compare against.
    """
    if previous is None:
        return None
    pct_points = (current - previous) * 100
    return {
        "current_pct": round(current * 100, 2),
        "previous_pct": round(previous * 100, 2),
        "change_pct_points": round(pct_points, 2),
        "direction": "up" if pct_points > 0 else ("down" if pct_points < 0 else "flat"),
    }


def volume_delta(current: float, previous: Optional[float]) -> Optional[dict]:
    """
    Percentage CHANGE (not percentage points) between two absolute volumes,
    e.g. emails sent 847 vs 620 -> +36.6%. Returns None if no previous value.
    """
    if previous is None:
        return None
    pct_change = safe_div(current - previous, previous) * 100
    return {
        "current": current,
        "previous": previous,
        "change_abs": current - previous,
        "change_pct": round(pct_change, 2),
        "direction": "up" if current > previous else ("down" if current < previous else "flat"),
    }


def compute_trends(current_agg: dict, previous_agg: Optional[dict]) -> dict:
    """Build the full trend block comparing two aggregate_metrics() dicts."""
    if previous_agg is None or previous_agg.get("campaigns_count", 0) == 0:
        return {
            "has_previous_period": False,
            "open_rate": None,
            "click_rate": None,
            "unsubscribe_rate": None,
            "bounce_rate": None,
            "emails_sent": None,
        }

    return {
        "has_previous_period": True,
        "open_rate": rate_delta(current_agg["open_rate"], previous_agg["open_rate"]),
        "click_rate": rate_delta(current_agg["click_rate"], previous_agg["click_rate"]),
        "unsubscribe_rate": rate_delta(current_agg["unsubscribe_rate"], previous_agg["unsubscribe_rate"]),
        "bounce_rate": rate_delta(current_agg["bounce_rate"], previous_agg["bounce_rate"]),
        "emails_sent": volume_delta(current_agg["recipients"], previous_agg["recipients"]),
    }
