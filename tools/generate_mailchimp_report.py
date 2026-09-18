#!/usr/bin/env python3
"""
Tool: generate_mailchimp_report.py
Purpose: Daily automated Mailchimp ENGAGEMENT INTELLIGENCE report.
         Fetches campaign performance from Mailchimp, computes metrics/trends,
         builds the Campaign -> Link -> Contact -> Activity behavioral model,
         generates charts, renders a founder email digest + detailed HTML/PDF
         report + a normalized JSON export, and (optionally, gated by
         TEST_MODE) emails the digest to REPORT_RECIPIENTS with the PDF attached.

Usage:
    python3 tools/generate_mailchimp_report.py                     # generate only, no email
    python3 tools/generate_mailchimp_report.py --send              # also attempt email
                                                                     # (still no-ops if TEST_MODE=true)
    python3 tools/generate_mailchimp_report.py --window-days 7
    python3 tools/generate_mailchimp_report.py --campaign-limit 20
    python3 tools/generate_mailchimp_report.py --engagement-campaign-limit 5
    python3 tools/generate_mailchimp_report.py --json              # print raw metrics JSON

Output:
    .tmp/mailchimp_reports/<date>/report.html          (detailed Engagement Intelligence Report)
    .tmp/mailchimp_reports/<date>/email_digest.html     (concise founder email body)
    .tmp/mailchimp_reports/<date>/report.pdf
    .tmp/mailchimp_reports/<date>/data.json             (normalized Campaign->Link->Contact->Activity export)
    .tmp/mailchimp_reports/<date>/charts/*.png
    .tmp/mailchimp_reports/history.json                 (appended daily snapshot log)
    .tmp/mailchimp_reports/engagement/<campaign_id>.json (persisted per-campaign event log, incremental)
    .tmp/mailchimp_reports/logs/run_<date>.log
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from mailchimp_report_client import (  # noqa: E402
    MailchimpAPIError,
    MailchimpConfigError,
    get_campaign_reports,
    get_click_details,
    get_email_activity,
    get_member_profiles,
)
from mailchimp_metrics import (  # noqa: E402
    aggregate_metrics, campaign_metrics, compute_trends, detect_anomalies,
)
from mailchimp_engagement import (  # noqa: E402
    build_activity_events, build_content_performance, build_contact_activity,
    build_cross_campaign_engagement, build_engagement_matrix, build_link_clickers,
    build_link_table, build_multi_link_behavior, build_segmentation,
)
import mailchimp_engagement_store as engagement_store  # noqa: E402
from mailchimp_report_charts import generate_all_charts  # noqa: E402
from mailchimp_report_render import build_html_report, build_founder_email_html, render_pdf  # noqa: E402
from mailchimp_report_summary import build_founders_summary  # noqa: E402
from mailchimp_report_email import send_report_email  # noqa: E402

BASE_DIR = Path(__file__).parent.parent
TMP_ROOT = BASE_DIR / ".tmp" / "mailchimp_reports"
HISTORY_PATH = TMP_ROOT / "history.json"

log = logging.getLogger("mailchimp_report")


def _setup_logging(report_date: str) -> None:
    log_dir = TMP_ROOT / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / f"run_{report_date}.log"

    formatter = logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    file_handler = logging.FileHandler(log_path, encoding="utf-8")
    file_handler.setFormatter(formatter)
    # stderr, not stdout — keeps --json output on stdout pipeable/parseable
    stream_handler = logging.StreamHandler(sys.stderr)
    stream_handler.setFormatter(formatter)

    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.handlers = [file_handler, stream_handler]


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%S+00:00")


def _fetch_period(since: datetime, before: datetime, api_errors: list[str]) -> list[dict]:
    log.info("API request: GET /reports since=%s before=%s", _iso(since), _iso(before))
    try:
        raw = get_campaign_reports(since_send_time=_iso(since), before_send_time=_iso(before))
        log.info("Campaigns retrieved for window: %d", len(raw))
        return [campaign_metrics(r) for r in raw]
    except (MailchimpAPIError, MailchimpConfigError) as exc:
        log.error("Mailchimp API failure fetching period %s to %s: %s", since, before, exc)
        api_errors.append(str(exc))
        return []


def _fetch_recent(limit: int, api_errors: list[str]) -> list[dict]:
    log.info("API request: GET /reports (most recent %d, for charts)", limit)
    try:
        raw = get_campaign_reports(count=limit)
        log.info("Campaigns retrieved for charts: %d", len(raw))
        return [campaign_metrics(r) for r in raw]
    except (MailchimpAPIError, MailchimpConfigError) as exc:
        log.error("Mailchimp API failure fetching recent campaigns: %s", exc)
        api_errors.append(str(exc))
        return []


def _select_drilldown_campaigns(campaigns: list[dict], limit: int) -> list[dict]:
    """
    Phase 4/16: only campaigns with MEANINGFUL activity get the expensive
    link/contact-level drill-down (2 extra API calls each), capped at `limit`
    so cost stays bounded as campaign volume grows. Ranked by delivered size.
    """
    meaningful = [c for c in campaigns
                  if c["delivered"] > 0 and (c["unique_opens"] > 0 or c["unique_clicks"] > 0)]
    meaningful.sort(key=lambda c: c["delivered"], reverse=True)
    return meaningful[:limit]


def _fetch_campaign_engagement(
    campaign: dict, member_profiles: dict, api_errors: list[str], missing_fields: list[str],
) -> tuple[list[dict], list[dict], set]:
    """
    Fetch + persist click-details and email-activity for ONE campaign.
    Uses the stored watermark to fetch only NEW activity since the last run
    (Phase 16/17 incremental strategy) — falls back to a full fetch the first
    time a campaign is drilled into.

    Returns (all_events_for_campaign, link_table, all_recipient_email_ids).
    """
    campaign_id = campaign["campaign_id"]
    campaign_title = campaign.get("campaign_title") or campaign_id

    watermark = engagement_store.get_watermark(campaign_id)
    log.info("API request: GET /reports/%s/click-details", campaign_id)
    log.info("API request: GET /reports/%s/email-activity since=%s", campaign_id, watermark or "(full history)")

    try:
        raw_links = get_click_details(campaign_id)
        raw_activity = get_email_activity(campaign_id, since=watermark)
    except (MailchimpAPIError, MailchimpConfigError) as exc:
        log.error("Mailchimp API failure fetching engagement for campaign %s: %s", campaign_id, exc)
        api_errors.append(f"{campaign_title}: {exc}")
        stored = engagement_store.load_campaign_events(campaign_id)
        return stored.get("events", []), stored.get("link_table", []), set()

    if not raw_activity:
        missing_fields.append(f"{campaign_title}: no email-activity data returned")

    all_recipient_email_ids = {m.get("email_id") for m in raw_activity if m.get("email_id")}
    new_events = build_activity_events(campaign_id, campaign_title, raw_activity, member_profiles)
    link_table = build_link_table(raw_links)

    combined_events = engagement_store.merge_and_save(campaign_id, new_events, link_table=link_table)
    log.info("Campaign %s: %d new events fetched, %d total stored, %d tracked links",
              campaign_id, len(new_events), len(combined_events), len(link_table))

    return combined_events, link_table, all_recipient_email_ids


def _append_history(entry: dict) -> None:
    HISTORY_PATH.parent.mkdir(parents=True, exist_ok=True)
    history: list[dict] = []
    if HISTORY_PATH.exists():
        try:
            history = json.loads(HISTORY_PATH.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            log.warning("Could not read existing history.json (%s) — starting fresh.", exc)
    history.append(entry)
    HISTORY_PATH.write_text(json.dumps(history, indent=2, ensure_ascii=False), encoding="utf-8")


def main(window_days: int = 30, campaign_limit: int = 15, engagement_campaign_limit: int = 10,
         send: bool = False, dump_json: bool = False) -> dict:
    run_start = datetime.now(timezone.utc)
    report_date = run_start.strftime("%Y-%m-%d")
    _setup_logging(report_date)

    log.info("=== Daily Mailchimp Engagement Intelligence report run started: %s ===", report_date)

    api_errors: list[str] = []
    missing_fields: list[str] = []
    excluded_campaigns: list[str] = []

    now = run_start
    current_since = now - timedelta(days=window_days)
    previous_since = now - timedelta(days=2 * window_days)
    previous_before = current_since

    current_campaigns = _fetch_period(current_since, now, api_errors)
    previous_campaigns = _fetch_period(previous_since, previous_before, api_errors)
    recent_for_charts = _fetch_recent(campaign_limit, api_errors)

    # Exclude campaigns with no send_time (never actually sent, e.g. malformed/draft leaking into report)
    def _valid(c: dict) -> bool:
        ok = bool(c.get("send_time"))
        if not ok:
            excluded_campaigns.append(f"{c.get('campaign_id')} — missing send_time")
        return ok

    current_campaigns = [c for c in current_campaigns if _valid(c)]
    previous_campaigns = [c for c in previous_campaigns if _valid(c)]
    recent_for_charts = [c for c in recent_for_charts if c.get("send_time")]

    log.info("Metrics calculated: current period=%d campaigns, previous period=%d campaigns",
              len(current_campaigns), len(previous_campaigns))

    # Phase 15: anomaly detection — flag, never silently present or clamp.
    for c in current_campaigns + previous_campaigns:
        api_errors.extend(detect_anomalies(c))

    current_agg = aggregate_metrics(current_campaigns)
    previous_agg = aggregate_metrics(previous_campaigns) if previous_campaigns else None
    trends = compute_trends(current_agg, previous_agg)

    # ── Engagement intelligence layer (Phases 4-12) ──────────────────────
    engagement: dict[str, dict] = {}
    combined_events: list[dict] = []
    all_recipient_ids: set = set()

    drilldown_campaigns = _select_drilldown_campaigns(current_campaigns, engagement_campaign_limit)
    log.info("Campaigns selected for engagement drill-down: %d of %d (meaningful activity, capped at %d)",
              len(drilldown_campaigns), len(current_campaigns), engagement_campaign_limit)

    member_profiles: dict = {}
    if drilldown_campaigns:
        log.info("API request: GET /lists/{id}/members (bulk profile fetch for contact identity)")
        try:
            member_profiles = get_member_profiles()
            log.info("Member profiles retrieved: %d", len(member_profiles))
        except (MailchimpAPIError, MailchimpConfigError) as exc:
            log.error("Could not fetch member profiles — contact names/companies will be blank: %s", exc)
            api_errors.append(f"Member profile fetch failed: {exc}")

    for campaign in drilldown_campaigns:
        campaign_id = campaign["campaign_id"]
        events, link_table, recipient_ids = _fetch_campaign_engagement(
            campaign, member_profiles, api_errors, missing_fields
        )
        combined_events.extend(events)
        all_recipient_ids.update(recipient_ids)
        engagement[campaign_id] = {
            "campaign_title": campaign.get("campaign_title") or campaign_id,
            "link_table": link_table,
            "link_clickers": build_link_clickers(events),
            "multi_link_behavior": build_multi_link_behavior(events),
        }

    contact_activity = build_contact_activity(combined_events)
    segmentation = build_segmentation(combined_events, all_recipient_email_ids=all_recipient_ids or None)
    engagement_matrix = build_engagement_matrix(combined_events)

    # Cross-campaign engagement uses the FULL persisted history, not just this window.
    all_history_events = engagement_store.load_all_events()
    cross_campaign_engagers = build_cross_campaign_engagement(all_history_events)

    # Content performance aggregates link tables across ALL persisted campaigns.
    all_link_tables = engagement_store.load_all_link_tables()
    content_performance = build_content_performance(all_link_tables)

    top_links = sorted(
        (l for lt in engagement.values() for l in lt["link_table"]),
        key=lambda l: l["total_clicks"], reverse=True,
    )
    top_clickers = sorted(
        (c for lt in engagement.values() for clickers in lt["link_clickers"].values() for c in clickers),
        key=lambda c: c["clicks"], reverse=True,
    )
    multi_link_engagers_all = [m for lt in engagement.values() for m in lt["multi_link_behavior"]]

    founders_summary = build_founders_summary(
        current_agg, previous_agg, trends, current_campaigns,
        top_links=top_links, top_clickers=top_clickers,
        multi_link_engagers=multi_link_engagers_all,
        cross_campaign_engagers=cross_campaign_engagers,
        anomalies=[e for e in api_errors if "anomaly" in e.lower()],
    )

    engagement_totals = {
        "unique_clickers": len(top_clickers),
        "repeat_clickers": segmentation["counts"].get("REPEAT_CLICKER", 0),
        "multi_link_engagers": segmentation["counts"].get("MULTI_LINK_ENGAGER", 0),
        "repeat_campaign_engagers": len(cross_campaign_engagers),
    }

    # ── Charts ────────────────────────────────────────────────────────
    out_dir = TMP_ROOT / report_date
    charts_dir = out_dir / "charts"
    chart_results = generate_all_charts(recent_for_charts, charts_dir)
    log.info("Charts generated: %s",
              {k: bool(v) for k, v in chart_results.items() if k != "errors"})
    if chart_results.get("errors"):
        api_errors.extend(chart_results["errors"])

    status = "OK" if not api_errors else ("PARTIAL" if (current_campaigns or previous_campaigns) else "FAILED")
    data_quality = {
        "status": status,
        "retrieved_at": _iso(run_start),
        "campaigns_retrieved": len(current_campaigns) + len(previous_campaigns),
        "missing_fields": missing_fields,
        "api_errors": api_errors,
        "excluded_campaigns": excluded_campaigns,
    }

    ctx = {
        "report_date": report_date,
        "generated_at": _iso(run_start),
        "window_days": window_days,
        "current": current_agg,
        "previous": previous_agg,
        "trends": trends,
        "campaigns": sorted(current_campaigns, key=lambda c: c.get("send_time") or "", reverse=True),
        "chart_paths": chart_results,
        "founders_summary": founders_summary,
        "engagement": engagement,
        "contact_activity": contact_activity,
        "cross_campaign_engagers": cross_campaign_engagers,
        "segmentation": segmentation,
        "engagement_matrix": engagement_matrix,
        "content_performance": content_performance,
        "top_links": top_links,
        "engagement_totals": engagement_totals,
        "data_quality": data_quality,
    }

    # ── Render: detailed report (extended, existing file) + concise email digest ──
    out_dir.mkdir(parents=True, exist_ok=True)

    html = build_html_report(ctx)
    html_path = out_dir / "report.html"
    html_path.write_text(html, encoding="utf-8")
    log.info("Detailed Engagement Intelligence report generated: %s", html_path)

    email_html = build_founder_email_html(ctx)
    email_path = out_dir / "email_digest.html"
    email_path.write_text(email_html, encoding="utf-8")
    log.info("Founder email digest generated: %s", email_path)

    pdf_path = out_dir / "report.pdf"
    pdf_result = render_pdf(html, pdf_path)
    if pdf_result:
        log.info("PDF report generated: %s", pdf_result)
    else:
        log.warning("PDF generation failed — HTML report is still available.")

    # Phase 21: normalized JSON export (Campaign -> Link -> Contact -> Activity)
    normalized_export = {
        "report_date": report_date,
        "generated_at": _iso(run_start),
        "campaigns": ctx["campaigns"],
        "engagement": engagement,
        "contact_activity": contact_activity,
        "cross_campaign_engagers": cross_campaign_engagers,
        "segmentation": segmentation,
        "content_performance": content_performance,
    }
    data_json_path = out_dir / "data.json"
    data_json_path.write_text(json.dumps(normalized_export, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
    log.info("Normalized JSON export written: %s", data_json_path)

    _append_history({
        "run_at": _iso(run_start),
        "report_date": report_date,
        "window_days": window_days,
        "current_period": current_agg,
        "previous_period": previous_agg,
        "status": status,
    })

    email_result = {"sent": False, "reason": "not requested"}
    if send:
        subject = f"Daily Mailchimp Intelligence — {report_date}"
        email_result = send_report_email(
            subject=subject,
            html_body=email_html,
            pdf_path=pdf_path if pdf_result else None,
            force_send=True,
        )
        log.info("Email result: %s", email_result)

    log.info("=== Report run complete: %s ===", status)

    result = {
        "report_date": report_date,
        "status": status,
        "html_path": str(html_path),
        "email_digest_path": str(email_path),
        "pdf_path": pdf_result,
        "data_json_path": str(data_json_path),
        "charts": chart_results,
        "current": current_agg,
        "previous": previous_agg,
        "trends": trends,
        "founders_summary": founders_summary,
        "engagement_totals": engagement_totals,
        "data_quality": data_quality,
        "email_result": email_result,
    }

    if dump_json:
        print(json.dumps(result, indent=2, ensure_ascii=False, default=str))

    return result


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Generate the daily Mailchimp Engagement Intelligence report")
    ap.add_argument("--window-days", type=int, default=30,
                     help="Reporting period length in days, compared against the equal-length prior period (default: 30)")
    ap.add_argument("--campaign-limit", type=int, default=15,
                     help="Number of most recent campaigns to include in charts (default: 15)")
    ap.add_argument("--engagement-campaign-limit", type=int, default=10,
                     help="Max number of campaigns (by delivered size) that get the full link/contact "
                          "drill-down per run — bounds API cost as campaign volume grows (default: 10)")
    ap.add_argument("--send", action="store_true",
                     help="Attempt to email the report (still no-ops if TEST_MODE=true in .env)")
    ap.add_argument("--json", dest="dump_json", action="store_true",
                     help="Print the full result as JSON to stdout")
    args = ap.parse_args()

    try:
        res = main(
            window_days=args.window_days,
            campaign_limit=args.campaign_limit,
            engagement_campaign_limit=args.engagement_campaign_limit,
            send=args.send,
            dump_json=args.dump_json,
        )
        sys.exit(0 if res["status"] != "FAILED" else 1)
    except Exception:  # noqa: BLE001 - top-level guard: report failures must be visible, never silent
        logging.getLogger("mailchimp_report").exception("Fatal error generating report")
        sys.exit(1)
