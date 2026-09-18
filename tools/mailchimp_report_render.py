"""
Tool: mailchimp_report_render.py
Purpose: Render the daily Mailchimp report as (A) a founder-friendly HTML page
         and (B) a downloadable PDF, from a single shared template — using
         weasyprint (already installed in this repo) for HTML->PDF so there's
         no second templating system to maintain.

Brand tokens match tools/publish_to_mailchimp.py for visual consistency with
the existing newsletter output.
"""

from __future__ import annotations

import base64
import logging
from datetime import datetime
from pathlib import Path
from typing import Optional

log = logging.getLogger("mailchimp_report")

NAVY = "#091D2B"
BRIGHT_BLUE = "#00A0E4"
BLUE_MID = "#0077B6"
BODY_BG = "#F2F4F6"
CARD_BG = "#FFFFFF"
TEXT = "#1A2A35"
TEXT_LIGHT = "#6B7C8D"
DIVIDER = "#E0E8EF"
RED = "#C0392B"
GREEN = "#27AE60"


def _pct(x: float) -> str:
    return f"{x * 100:.1f}%"


def _img_data_uri(path: Optional[str]) -> Optional[str]:
    if not path or not Path(path).exists():
        return None
    data = Path(path).read_bytes()
    return f"data:image/png;base64,{base64.b64encode(data).decode('ascii')}"


def _trend_badge(delta: Optional[dict], unit: str = "pp") -> str:
    if not delta:
        return '<span style="color:#6B7C8D;font-size:12px;">No prior period</span>'
    color = GREEN if delta["direction"] == "up" else (RED if delta["direction"] == "down" else TEXT_LIGHT)
    arrow = "&#9650;" if delta["direction"] == "up" else ("&#9660;" if delta["direction"] == "down" else "&#8226;")
    if unit == "pp":
        val = f"{delta['change_pct_points']:+.1f}pp"
    else:
        val = f"{delta['change_pct']:+.1f}%"
    return f'<span style="color:{color};font-size:12px;font-weight:bold;">{arrow} {val} vs previous</span>'


def _metric_card(label: str, value: str, trend_html: str) -> str:
    return f"""
    <td style="background:{CARD_BG};border:1px solid {DIVIDER};border-radius:8px;
               padding:16px;vertical-align:top;width:20%;">
      <p style="margin:0 0 4px 0;font-size:11px;color:{TEXT_LIGHT};text-transform:uppercase;
                letter-spacing:1px;">{label}</p>
      <p style="margin:0 0 6px 0;font-size:24px;font-weight:bold;color:{NAVY};">{value}</p>
      {trend_html}
    </td>"""


def _campaign_rows(campaigns: list[dict]) -> str:
    if not campaigns:
        return '<tr><td colspan="9" style="padding:16px;text-align:center;color:#6B7C8D;">No campaigns in this period.</td></tr>'
    rows = []
    for c in campaigns:
        send_time = c.get("send_time") or ""
        try:
            send_display = datetime.fromisoformat(send_time.replace("Z", "+00:00")).strftime("%Y-%m-%d %H:%M UTC")
        except ValueError:
            send_display = send_time or "—"
        rows.append(f"""
        <tr>
          <td style="padding:8px 10px;border-bottom:1px solid {DIVIDER};">{c.get('campaign_title') or '—'}</td>
          <td style="padding:8px 10px;border-bottom:1px solid {DIVIDER};font-size:11px;color:{TEXT_LIGHT};">{c.get('campaign_id') or '—'}</td>
          <td style="padding:8px 10px;border-bottom:1px solid {DIVIDER};font-size:11px;">{send_display}</td>
          <td style="padding:8px 10px;border-bottom:1px solid {DIVIDER};">{(c.get('subject_line') or '—')[:60]}</td>
          <td style="padding:8px 10px;border-bottom:1px solid {DIVIDER};text-align:right;">{c['recipients']:,}</td>
          <td style="padding:8px 10px;border-bottom:1px solid {DIVIDER};text-align:right;">{c['delivered']:,}</td>
          <td style="padding:8px 10px;border-bottom:1px solid {DIVIDER};text-align:right;">{c['unique_opens']:,} ({_pct(c['open_rate'])})</td>
          <td style="padding:8px 10px;border-bottom:1px solid {DIVIDER};text-align:right;">{c['unique_clicks']:,} ({_pct(c['click_rate'])})</td>
          <td style="padding:8px 10px;border-bottom:1px solid {DIVIDER};text-align:right;">{c['unsubscribes']:,} / {c['bounces']:,}</td>
        </tr>""")
    return "".join(rows)


def _chart_block(title: str, data_uri: Optional[str]) -> str:
    if not data_uri:
        return f"""
        <div style="margin-bottom:20px;padding:24px;background:#FAFBFC;border:1px dashed {DIVIDER};
                    border-radius:8px;text-align:center;color:{TEXT_LIGHT};font-size:12px;">
          {title}: chart unavailable for this report run.
        </div>"""
    return f"""
    <div style="margin-bottom:20px;">
      <img src="{data_uri}" alt="{title}" style="width:100%;max-width:700px;display:block;
           border:1px solid {DIVIDER};border-radius:8px;" />
    </div>"""


def _summary_section_html(sections: dict[str, list[str]]) -> str:
    """Render the structured Founder's Summary (Phase 13) as labeled subsections."""
    labels = [
        ("what_happened", "What Happened"),
        ("where_engagement", "Where Engagement Happened"),
        ("who_engaged", "Who Engaged"),
        ("what_changed", "What Changed"),
        ("repeated_behavior", "Repeated Behavior"),
        ("data_warnings", "Data Warnings"),
    ]
    blocks = []
    for key, label in labels:
        lines = sections.get(key, [])
        if not lines:
            continue
        items = "".join(f"<p style='margin:0 0 6px 0;line-height:1.6;'>{s}</p>" for s in lines)
        warn = key == "data_warnings" and lines != ["No data quality issues detected."]
        border = "#C0392B" if warn else BRIGHT_BLUE
        blocks.append(f"""
        <div style="margin-bottom:12px;">
          <p style="margin:0 0 4px 0;font-size:11px;font-weight:bold;color:{TEXT_LIGHT};
                    text-transform:uppercase;letter-spacing:1px;">{label}</p>
          <div style="border-left:3px solid {border};padding-left:12px;">{items}</div>
        </div>""")
    return "".join(blocks)


def _link_table_html(links: list[dict]) -> str:
    if not links:
        return f'<p style="color:{TEXT_LIGHT};font-size:12px;">No tracked links with click activity.</p>'
    rows = []
    for l in links:
        rows.append(f"""
        <tr>
          <td style="padding:6px 8px;border-bottom:1px solid {DIVIDER};">{l['rank']}</td>
          <td style="padding:6px 8px;border-bottom:1px solid {DIVIDER};word-break:break-all;font-size:11px;">
            <a href="{l['url']}" style="color:{BLUE_MID};">{l['url'][:90]}</a></td>
          <td style="padding:6px 8px;border-bottom:1px solid {DIVIDER};text-align:right;">{l['unique_clicks']:,}</td>
          <td style="padding:6px 8px;border-bottom:1px solid {DIVIDER};text-align:right;">{l['total_clicks']:,}</td>
          <td style="padding:6px 8px;border-bottom:1px solid {DIVIDER};text-align:right;">{_pct(l['click_share'])}</td>
        </tr>""")
    return f"""
    <table width="100%" cellpadding="0" cellspacing="0" style="font-size:12px;border-collapse:collapse;margin-bottom:10px;">
      <thead><tr style="background:{NAVY};color:#FFFFFF;">
        <th style="padding:6px 8px;text-align:left;">Rank</th>
        <th style="padding:6px 8px;text-align:left;">URL</th>
        <th style="padding:6px 8px;text-align:right;">Unique Clickers</th>
        <th style="padding:6px 8px;text-align:right;">Total Clicks</th>
        <th style="padding:6px 8px;text-align:right;">Click Share</th>
      </tr></thead>
      <tbody>{"".join(rows)}</tbody>
    </table>"""


def _clicker_table_html(url: str, clickers: list[dict], limit: int = 15) -> str:
    rows = []
    for c in clickers[:limit]:
        label = c.get("name") or "—"
        company = c.get("company") or "—"
        rows.append(f"""
        <tr>
          <td style="padding:5px 8px;border-bottom:1px solid {DIVIDER};">{label}</td>
          <td style="padding:5px 8px;border-bottom:1px solid {DIVIDER};">{company}</td>
          <td style="padding:5px 8px;border-bottom:1px solid {DIVIDER};font-size:11px;">{c.get('email','')}</td>
          <td style="padding:5px 8px;border-bottom:1px solid {DIVIDER};text-align:right;">{c['clicks']}</td>
          <td style="padding:5px 8px;border-bottom:1px solid {DIVIDER};font-size:11px;">{c.get('first_click') or '—'}</td>
          <td style="padding:5px 8px;border-bottom:1px solid {DIVIDER};font-size:11px;">{c.get('last_click') or '—'}</td>
        </tr>""")
    more = f'<p style="font-size:11px;color:{TEXT_LIGHT};margin:4px 0 0 0;">+{len(clickers)-limit} more not shown</p>' if len(clickers) > limit else ""
    return f"""
    <p style="font-size:12px;font-weight:bold;margin:10px 0 4px 0;word-break:break-all;">{url[:100]}</p>
    <table width="100%" cellpadding="0" cellspacing="0" style="font-size:11px;border-collapse:collapse;">
      <thead><tr style="background:{BLUE_MID};color:#FFFFFF;">
        <th style="padding:5px 8px;text-align:left;">Contact</th>
        <th style="padding:5px 8px;text-align:left;">Company</th>
        <th style="padding:5px 8px;text-align:left;">Email</th>
        <th style="padding:5px 8px;text-align:right;">Clicks</th>
        <th style="padding:5px 8px;text-align:left;">First Click</th>
        <th style="padding:5px 8px;text-align:left;">Last Click</th>
      </tr></thead>
      <tbody>{"".join(rows)}</tbody>
    </table>{more}"""


def _campaign_drilldown_html(engagement: dict) -> str:
    """Phase 4/5/6/8: per-campaign link table + who-clicked-what + multi-link behavior."""
    if not engagement:
        return f'<p style="color:{TEXT_LIGHT};font-size:12px;">No campaigns with meaningful activity in this period required a drill-down.</p>'

    blocks = []
    for campaign_id, data in engagement.items():
        title = data.get("campaign_title") or campaign_id
        link_table = data.get("link_table", [])
        clickers_by_url = data.get("link_clickers", {})
        multi_link = data.get("multi_link_behavior", [])

        clicker_blocks = "".join(
            _clicker_table_html(link["url"], clickers_by_url.get(link["url"], []))
            for link in link_table[:8] if clickers_by_url.get(link["url"])
        )

        multi_link_html = ""
        if multi_link:
            rows = "".join(f"""
            <tr>
              <td style="padding:5px 8px;border-bottom:1px solid {DIVIDER};">{m.get('name') or m.get('email')}</td>
              <td style="padding:5px 8px;border-bottom:1px solid {DIVIDER};">{m.get('company') or '—'}</td>
              <td style="padding:5px 8px;border-bottom:1px solid {DIVIDER};text-align:right;">{m['unique_links_count']}</td>
              <td style="padding:5px 8px;border-bottom:1px solid {DIVIDER};text-align:right;">{m['total_clicks']}</td>
            </tr>""" for m in multi_link[:15])
            multi_link_html = f"""
            <p style="font-size:12px;font-weight:bold;margin:14px 0 4px 0;">Multi-Link Engagers</p>
            <table width="100%" cellpadding="0" cellspacing="0" style="font-size:11px;border-collapse:collapse;">
              <thead><tr style="background:{NAVY};color:#FFFFFF;">
                <th style="padding:5px 8px;text-align:left;">Contact</th>
                <th style="padding:5px 8px;text-align:left;">Company</th>
                <th style="padding:5px 8px;text-align:right;">Unique Links</th>
                <th style="padding:5px 8px;text-align:right;">Total Clicks</th>
              </tr></thead>
              <tbody>{rows}</tbody>
            </table>"""

        blocks.append(f"""
        <div style="margin-bottom:28px;padding:16px;border:1px solid {DIVIDER};border-radius:8px;">
          <p style="margin:0 0 10px 0;font-size:14px;font-weight:bold;color:{NAVY};">{title}</p>
          <p style="margin:0 0 8px 0;font-size:11px;font-weight:bold;color:{TEXT_LIGHT};text-transform:uppercase;">
            Link / Content Intelligence
          </p>
          {_link_table_html(link_table)}
          <p style="margin:14px 0 0 0;font-size:11px;font-weight:bold;color:{TEXT_LIGHT};text-transform:uppercase;">
            Who Clicked What
          </p>
          {clicker_blocks or f'<p style="color:{TEXT_LIGHT};font-size:12px;">No link clicks recorded.</p>'}
          {multi_link_html}
        </div>""")
    return "".join(blocks)


def _contact_timeline_html(contact_activity: dict, limit: int = 15) -> str:
    """Phase 7: individual activity timelines, capped to the most active contacts to keep the report readable."""
    if not contact_activity:
        return f'<p style="color:{TEXT_LIGHT};font-size:12px;">No individual activity recorded.</p>'

    ranked = sorted(contact_activity.values(), key=lambda c: len(c["timeline"]), reverse=True)[:limit]
    blocks = []
    for c in ranked:
        label = c.get("name") or c.get("email") or "Unknown contact"
        company = f" &mdash; {c['company']}" if c.get("company") else ""
        rows = []
        for t in c["timeline"]:
            ts = t["timestamp"] if t["timestamp_available"] else "timestamp unavailable"
            action = t["event_type"]
            detail = f' &mdash; {t["url"][:70]}' if t.get("url") else ""
            rows.append(f'<p style="margin:0 0 3px 0;font-size:12px;"><span style="color:{TEXT_LIGHT};">{ts}</span> — {action}{detail}</p>')
        blocks.append(f"""
        <div style="margin-bottom:14px;padding:12px 14px;background:#FAFBFC;border:1px solid {DIVIDER};border-radius:6px;">
          <p style="margin:0 0 6px 0;font-size:13px;font-weight:bold;">{label}{company}</p>
          {''.join(rows)}
        </div>""")
    more = len(contact_activity) - limit
    footer = f'<p style="font-size:11px;color:{TEXT_LIGHT};">+{more} more contacts with activity not shown</p>' if more > 0 else ""
    return "".join(blocks) + footer


def _cross_campaign_html(rows: list[dict]) -> str:
    if not rows:
        return f'<p style="color:{TEXT_LIGHT};font-size:12px;">No contact has engaged with more than one campaign yet in stored history.</p>'
    trs = "".join(f"""
    <tr>
      <td style="padding:6px 8px;border-bottom:1px solid {DIVIDER};">{r.get('name') or r.get('email')}</td>
      <td style="padding:6px 8px;border-bottom:1px solid {DIVIDER};">{r.get('company') or '—'}</td>
      <td style="padding:6px 8px;border-bottom:1px solid {DIVIDER};text-align:right;">{r['campaigns_engaged']}</td>
      <td style="padding:6px 8px;border-bottom:1px solid {DIVIDER};text-align:right;">{r['opens']}</td>
      <td style="padding:6px 8px;border-bottom:1px solid {DIVIDER};text-align:right;">{r['clicks']}</td>
      <td style="padding:6px 8px;border-bottom:1px solid {DIVIDER};text-align:right;">{r['unique_links']}</td>
    </tr>""" for r in rows[:25])
    return f"""
    <table width="100%" cellpadding="0" cellspacing="0" style="font-size:12px;border-collapse:collapse;">
      <thead><tr style="background:{NAVY};color:#FFFFFF;">
        <th style="padding:6px 8px;text-align:left;">Contact</th>
        <th style="padding:6px 8px;text-align:left;">Company</th>
        <th style="padding:6px 8px;text-align:right;">Campaigns Engaged</th>
        <th style="padding:6px 8px;text-align:right;">Opens</th>
        <th style="padding:6px 8px;text-align:right;">Clicks</th>
        <th style="padding:6px 8px;text-align:right;">Unique Links</th>
      </tr></thead>
      <tbody>{trs}</tbody>
    </table>"""


def _segmentation_html(segmentation: dict) -> str:
    counts = segmentation.get("counts", {})
    labels = {
        "NO_ENGAGEMENT": "No Engagement (delivered, no open/click)",
        "OPENED_ONLY": "Opened Only",
        "SINGLE_CLICKER": "Single Clicker",
        "REPEAT_CLICKER": "Repeat Clicker (multiple clicks)",
        "MULTI_LINK_ENGAGER": "Multi-Link Engager (clicked multiple links)",
    }
    rows = "".join(f"""
    <tr>
      <td style="padding:8px 10px;border-bottom:1px solid {DIVIDER};">{labels.get(k,k)}</td>
      <td style="padding:8px 10px;border-bottom:1px solid {DIVIDER};text-align:right;font-weight:bold;">{v:,}</td>
    </tr>""" for k, v in counts.items())
    return f"""
    <table width="100%" cellpadding="0" cellspacing="0" style="font-size:12px;border-collapse:collapse;">
      <thead><tr style="background:{NAVY};color:#FFFFFF;">
        <th style="padding:8px 10px;text-align:left;">Segment (deterministic, rule-based)</th>
        <th style="padding:8px 10px;text-align:right;">Contacts</th>
      </tr></thead>
      <tbody>{rows}</tbody>
    </table>"""


def _matrix_html(matrix: dict, limit: int = 20) -> str:
    links = matrix.get("links", [])
    rows = matrix.get("rows", [])[:limit]
    if not links or not rows:
        return f'<p style="color:{TEXT_LIGHT};font-size:12px;">No multi-contact, multi-link data available for a matrix view.</p>'
    header = "".join(f'<th style="padding:5px 6px;text-align:right;font-size:10px;">{u.split("?")[0][-24:]}</th>' for u in links)
    trs = []
    for r in rows:
        cells = "".join(f'<td style="padding:5px 6px;border-bottom:1px solid {DIVIDER};text-align:right;">{v if v else ""}</td>' for v in r["cells"])
        trs.append(f"""
        <tr>
          <td style="padding:5px 6px;border-bottom:1px solid {DIVIDER};">{r.get('name') or r.get('email')}</td>
          {cells}
        </tr>""")
    return f"""
    <div style="overflow-x:auto;">
    <table cellpadding="0" cellspacing="0" style="font-size:11px;border-collapse:collapse;white-space:nowrap;">
      <thead><tr style="background:{NAVY};color:#FFFFFF;">
        <th style="padding:5px 6px;text-align:left;">Contact</th>{header}
      </tr></thead>
      <tbody>{"".join(trs)}</tbody>
    </table>
    </div>"""


def _content_performance_html(rows: list[dict]) -> str:
    if not rows:
        return f'<p style="color:{TEXT_LIGHT};font-size:12px;">No cross-campaign link data available yet.</p>'
    trs = "".join(f"""
    <tr>
      <td style="padding:6px 8px;border-bottom:1px solid {DIVIDER};word-break:break-all;font-size:11px;">{r['canonical_url'][:90]}</td>
      <td style="padding:6px 8px;border-bottom:1px solid {DIVIDER};text-align:right;">{r['campaigns_appeared_in']}</td>
      <td style="padding:6px 8px;border-bottom:1px solid {DIVIDER};text-align:right;">{r['unique_clicks']:,}</td>
      <td style="padding:6px 8px;border-bottom:1px solid {DIVIDER};text-align:right;">{r['total_clicks']:,}</td>
    </tr>""" for r in rows[:20])
    return f"""
    <table width="100%" cellpadding="0" cellspacing="0" style="font-size:12px;border-collapse:collapse;">
      <thead><tr style="background:{NAVY};color:#FFFFFF;">
        <th style="padding:6px 8px;text-align:left;">Content (URL, tracking params stripped)</th>
        <th style="padding:6px 8px;text-align:right;">Campaigns</th>
        <th style="padding:6px 8px;text-align:right;">Unique Clicks</th>
        <th style="padding:6px 8px;text-align:right;">Total Clicks</th>
      </tr></thead>
      <tbody>{trs}</tbody>
    </table>"""


def build_html_report(ctx: dict) -> str:
    """
    Builds the DETAILED Engagement Intelligence Report (Phase 14-B). This is
    the same report.html the pipeline has always produced (sections 1-3 and
    Data Quality are unchanged) — Phases 4-12 are appended underneath as new
    additive sections. Nothing existing was removed.

    ctx keys expected (existing):
      report_date, generated_at, window_days,
      current (aggregate_metrics dict), previous (aggregate_metrics dict or None),
      trends (compute_trends dict), campaigns (list of campaign_metrics dicts, most recent first),
      chart_paths (dict from generate_all_charts), founders_summary (structured dict),
      data_quality (dict)

    ctx keys expected (new, all optional — sections degrade gracefully if absent):
      engagement: {campaign_id: {campaign_title, link_table, link_clickers, multi_link_behavior}}
      contact_activity: {email_id: {...timeline}}  (merged across drill-down campaigns)
      cross_campaign_engagers: list[dict]
      segmentation: {"counts": {...}}
      engagement_matrix: {"links": [...], "rows": [...]}
      content_performance: list[dict]
    """
    cur = ctx["current"]
    trends = ctx["trends"]
    dq = ctx["data_quality"]

    summary_html = _summary_section_html(ctx["founders_summary"])

    charts = ctx.get("chart_paths", {})
    charts_html = "".join([
        _chart_block("Open Rate Trend", _img_data_uri(charts.get("open_rate_trend"))),
        _chart_block("Click Rate Trend", _img_data_uri(charts.get("click_rate_trend"))),
        _chart_block("Emails Sent", _img_data_uri(charts.get("emails_sent"))),
        _chart_block("Opens vs Clicks", _img_data_uri(charts.get("opens_vs_clicks"))),
        _chart_block("Campaign Comparison", _img_data_uri(charts.get("campaign_comparison"))),
    ])

    dq_errors = "".join(f"<li>{e}</li>" for e in dq.get("api_errors", [])) or "<li>None</li>"
    dq_excluded = "".join(f"<li>{e}</li>" for e in dq.get("excluded_campaigns", [])) or "<li>None</li>"
    dq_missing = ", ".join(dq.get("missing_fields", [])) or "None"

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<title>Daily Mailchimp Performance Report — {ctx['report_date']}</title>
</head>
<body style="margin:0;padding:0;background:{BODY_BG};font-family:Arial,Helvetica,sans-serif;color:{TEXT};">
<div style="max-width:760px;margin:0 auto;padding:24px 16px;">

  <!-- Header -->
  <div style="background:{NAVY};border-radius:8px 8px 0 0;padding:24px 28px;">
    <p style="margin:0;color:{BRIGHT_BLUE};font-size:11px;letter-spacing:2px;text-transform:uppercase;">
      Daily Mailchimp Performance Report
    </p>
    <p style="margin:6px 0 0 0;color:#FFFFFF;font-size:26px;font-weight:bold;">{ctx['report_date']}</p>
    <p style="margin:4px 0 0 0;color:rgba(255,255,255,0.5);font-size:11px;">
      Window: last {ctx['window_days']} days &nbsp;|&nbsp; Generated {ctx['generated_at']}
    </p>
  </div>

  <div style="background:{CARD_BG};padding:28px;">

    <!-- Executive Summary -->
    <h2 style="margin:0 0 14px 0;font-size:16px;color:{NAVY};border-bottom:2px solid {BRIGHT_BLUE};padding-bottom:8px;">
      1. Executive Summary
    </h2>
    <table width="100%" cellpadding="0" cellspacing="0" style="margin-bottom:8px;">
      <tr>
        {_metric_card("Campaigns Sent", str(cur['campaigns_count']), "")}
        {_metric_card("Emails Sent", f"{cur['recipients']:,}", _trend_badge(trends.get('emails_sent'), unit='pct'))}
        {_metric_card("Delivered", f"{cur['delivered']:,}", "")}
        {_metric_card("Open Rate", _pct(cur['open_rate']), _trend_badge(trends.get('open_rate')))}
        {_metric_card("Click Rate", _pct(cur['click_rate']), _trend_badge(trends.get('click_rate')))}
      </tr>
    </table>
    <table width="100%" cellpadding="0" cellspacing="0" style="margin:8px 0 20px 0;">
      <tr>
        {_metric_card("Unique Opens", f"{cur['unique_opens']:,}", "")}
        {_metric_card("Unique Clicks", f"{cur['unique_clicks']:,}", "")}
        {_metric_card("Unsubscribe Rate", _pct(cur['unsubscribe_rate']), _trend_badge(trends.get('unsubscribe_rate')))}
        {_metric_card("Bounce Rate", _pct(cur['bounce_rate']), _trend_badge(trends.get('bounce_rate')))}
        {_metric_card("Unsubscribes / Bounces", f"{cur['unsubscribes']:,} / {cur['bounces']:,}", "")}
      </tr>
    </table>

    <!-- Founder's Summary -->
    <h2 style="margin:24px 0 14px 0;font-size:16px;color:{NAVY};border-bottom:2px solid {BRIGHT_BLUE};padding-bottom:8px;">
      Founder's Summary
    </h2>
    <div style="background:#F5FAFD;border-left:4px solid {BRIGHT_BLUE};border-radius:4px;padding:14px 18px;margin-bottom:20px;">
      {summary_html}
    </div>

    <!-- Visual Reporting -->
    <h2 style="margin:24px 0 14px 0;font-size:16px;color:{NAVY};border-bottom:2px solid {BRIGHT_BLUE};padding-bottom:8px;">
      2. Visual Reporting
    </h2>
    {charts_html}

    <!-- Campaign Performance -->
    <h2 style="margin:24px 0 14px 0;font-size:16px;color:{NAVY};border-bottom:2px solid {BRIGHT_BLUE};padding-bottom:8px;">
      3. Campaign Performance
    </h2>
    <div style="overflow-x:auto;">
    <table width="100%" cellpadding="0" cellspacing="0" style="font-size:12px;border-collapse:collapse;margin-bottom:20px;">
      <thead>
        <tr style="background:{NAVY};color:#FFFFFF;">
          <th style="padding:8px 10px;text-align:left;">Campaign</th>
          <th style="padding:8px 10px;text-align:left;">ID</th>
          <th style="padding:8px 10px;text-align:left;">Sent</th>
          <th style="padding:8px 10px;text-align:left;">Subject</th>
          <th style="padding:8px 10px;text-align:right;">Recipients</th>
          <th style="padding:8px 10px;text-align:right;">Delivered</th>
          <th style="padding:8px 10px;text-align:right;">Opens</th>
          <th style="padding:8px 10px;text-align:right;">Clicks</th>
          <th style="padding:8px 10px;text-align:right;">Unsub / Bounce</th>
        </tr>
      </thead>
      <tbody>
        {_campaign_rows(ctx['campaigns'])}
      </tbody>
    </table>
    </div>

    <!-- Campaign Drill-Down: Link / Content Intelligence + Who Clicked What -->
    <h2 style="margin:24px 0 14px 0;font-size:16px;color:{NAVY};border-bottom:2px solid {BRIGHT_BLUE};padding-bottom:8px;">
      4. Campaign Engagement Drill-Down
    </h2>
    <p style="font-size:12px;color:{TEXT_LIGHT};margin:0 0 12px 0;">
      Link-level and contact-level detail for campaigns with recorded activity in this period.
      Link labels are the tracked URL itself — CTA intent is not inferred.
    </p>
    {_campaign_drilldown_html(ctx.get('engagement', {}))}

    <!-- Individual Activity Timelines -->
    <h2 style="margin:24px 0 14px 0;font-size:16px;color:{NAVY};border-bottom:2px solid {BRIGHT_BLUE};padding-bottom:8px;">
      5. Individual Activity (Most Active Contacts)
    </h2>
    <p style="font-size:12px;color:{TEXT_LIGHT};margin:0 0 12px 0;">
      Chronological event history per contact, as recorded by Mailchimp. Only events Mailchimp
      actually returned are shown; a missing timestamp is labeled, never invented.
    </p>
    {_contact_timeline_html(ctx.get('contact_activity', {}))}

    <!-- Repeat Engagement Across Campaigns -->
    <h2 style="margin:24px 0 14px 0;font-size:16px;color:{NAVY};border-bottom:2px solid {BRIGHT_BLUE};padding-bottom:8px;">
      6. Repeat Engagement Across Campaigns
    </h2>
    <p style="font-size:12px;color:{TEXT_LIGHT};margin:0 0 12px 0;">
      Contacts who have engaged (opened or clicked) with more than one campaign, based on stored history.
      Observed behavior only — not a lead classification.
    </p>
    {_cross_campaign_html(ctx.get('cross_campaign_engagers', []))}

    <!-- Behavioral Segmentation -->
    <h2 style="margin:24px 0 14px 0;font-size:16px;color:{NAVY};border-bottom:2px solid {BRIGHT_BLUE};padding-bottom:8px;">
      7. Behavioral Segmentation
    </h2>
    <p style="font-size:12px;color:{TEXT_LIGHT};margin:0 0 12px 0;">
      Deterministic, rule-based buckets (see tools/mailchimp_engagement.py:classify_contact_segment) — no lead score.
    </p>
    {_segmentation_html(ctx.get('segmentation', {}))}

    <!-- Engagement Matrix -->
    <h2 style="margin:24px 0 14px 0;font-size:16px;color:{NAVY};border-bottom:2px solid {BRIGHT_BLUE};padding-bottom:8px;">
      8. Engagement Matrix (Contact &times; Link)
    </h2>
    <p style="font-size:12px;color:{TEXT_LIGHT};margin:0 0 12px 0;">
      Cell values are actual recorded click counts. Limited to the most active contacts and links for readability.
    </p>
    {_matrix_html(ctx.get('engagement_matrix', {}))}

    <!-- Content Performance -->
    <h2 style="margin:24px 0 14px 0;font-size:16px;color:{NAVY};border-bottom:2px solid {BRIGHT_BLUE};padding-bottom:8px;">
      9. Content Performance (Cross-Campaign)
    </h2>
    <p style="font-size:12px;color:{TEXT_LIGHT};margin:0 0 12px 0;">
      The same destination URL aggregated across campaigns (tracking parameters stripped for grouping only).
    </p>
    {_content_performance_html(ctx.get('content_performance', []))}

    <!-- Data Quality / API Status -->
    <h2 style="margin:24px 0 14px 0;font-size:16px;color:{NAVY};border-bottom:2px solid {BRIGHT_BLUE};padding-bottom:8px;">
      Data Quality / API Status
    </h2>
    <table width="100%" cellpadding="0" cellspacing="0" style="font-size:12px;background:#FAFBFC;
           border:1px solid {DIVIDER};border-radius:6px;">
      <tr>
        <td style="padding:14px 18px;">
          <p style="margin:0 0 6px 0;"><b>Status:</b> {dq.get('status')}</p>
          <p style="margin:0 0 6px 0;"><b>Retrieved at:</b> {dq.get('retrieved_at')}</p>
          <p style="margin:0 0 6px 0;"><b>Campaigns retrieved:</b> {dq.get('campaigns_retrieved')}</p>
          <p style="margin:0 0 6px 0;"><b>Missing fields:</b> {dq_missing}</p>
          <p style="margin:8px 0 2px 0;"><b>API errors:</b></p>
          <ul style="margin:0 0 8px 0;padding-left:18px;">{dq_errors}</ul>
          <p style="margin:8px 0 2px 0;"><b>Campaigns excluded:</b></p>
          <ul style="margin:0;padding-left:18px;">{dq_excluded}</ul>
        </td>
      </tr>
    </table>

    <p style="margin:24px 0 0 0;font-size:11px;color:{TEXT_LIGHT};text-align:center;">
      Synergetic Shipping Technologies &mdash; Automated Daily Mailchimp Performance Report
    </p>
  </div>
</div>
</body>
</html>"""


def build_founder_email_html(ctx: dict, detailed_report_url: Optional[str] = None) -> str:
    """
    Phase 14-A: the concise daily founder EMAIL body — a digest, not the full
    report. Points to the detailed report for anyone who wants the drill-down.
    """
    cur = ctx["current"]
    trends = ctx["trends"]
    summary = ctx["founders_summary"]
    top_links = ctx.get("top_links", [])
    engagement_totals = ctx.get("engagement_totals", {})
    dq = ctx["data_quality"]

    top_campaign = None
    if ctx.get("campaigns"):
        top_campaign = max(ctx["campaigns"], key=lambda c: c["open_rate"], default=None)
    top_link = top_links[0] if top_links else None

    key_change = (summary.get("what_changed") or ["No prior comparable period available yet."])[0]
    data_warning = (summary.get("data_warnings") or ["No data quality issues detected."])[0]

    button_html = ""
    if detailed_report_url:
        button_html = f"""
        <div style="text-align:center;margin:22px 0 4px 0;">
          <a href="{detailed_report_url}" style="display:inline-block;background:{BRIGHT_BLUE};color:#FFFFFF;
             text-decoration:none;padding:12px 28px;border-radius:5px;font-weight:bold;font-size:13px;">
            VIEW DETAILED ENGAGEMENT REPORT
          </a>
        </div>"""

    return f"""<!DOCTYPE html>
<html lang="en">
<head><meta charset="UTF-8"><title>Daily Mailchimp Intelligence — {ctx['report_date']}</title></head>
<body style="margin:0;padding:0;background:{BODY_BG};font-family:Arial,Helvetica,sans-serif;color:{TEXT};">
<div style="max-width:600px;margin:0 auto;padding:20px 12px;">
  <div style="background:{NAVY};border-radius:8px 8px 0 0;padding:20px 24px;text-align:center;">
    <p style="margin:0;color:{BRIGHT_BLUE};font-size:11px;letter-spacing:2px;text-transform:uppercase;">Daily Mailchimp Intelligence</p>
    <p style="margin:6px 0 0 0;color:#FFFFFF;font-size:22px;font-weight:bold;">{ctx['report_date']}</p>
  </div>
  <div style="background:{CARD_BG};padding:24px;">

    <p style="margin:0 0 6px 0;font-size:11px;font-weight:bold;color:{TEXT_LIGHT};text-transform:uppercase;">Campaign Activity</p>
    <p style="margin:0 0 16px 0;font-size:14px;line-height:1.8;">
      {cur['campaigns_count']} campaign{'s' if cur['campaigns_count'] != 1 else ''} sent<br>
      {cur['delivered']:,} delivered<br>
      {_pct(cur['open_rate'])} open rate<br>
      {_pct(cur['click_rate'])} click rate
    </p>

    <p style="margin:0 0 6px 0;font-size:11px;font-weight:bold;color:{TEXT_LIGHT};text-transform:uppercase;">Top Engagement</p>
    <p style="margin:0 0 16px 0;font-size:14px;line-height:1.8;">
      Most-engaged campaign: {(top_campaign.get('campaign_title') if top_campaign else None) or '—'}<br>
      Most-clicked link: {(top_link.get('url')[:60] if top_link else None) or '—'}
    </p>

    <p style="margin:0 0 6px 0;font-size:11px;font-weight:bold;color:{TEXT_LIGHT};text-transform:uppercase;">Contact Activity</p>
    <p style="margin:0 0 16px 0;font-size:14px;line-height:1.8;">
      {engagement_totals.get('unique_clickers', 0):,} unique clickers<br>
      {engagement_totals.get('repeat_clickers', 0):,} repeat clickers<br>
      {engagement_totals.get('multi_link_engagers', 0):,} multi-link engagers<br>
      {engagement_totals.get('repeat_campaign_engagers', 0):,} repeat campaign engagers
    </p>

    <p style="margin:0 0 6px 0;font-size:11px;font-weight:bold;color:{TEXT_LIGHT};text-transform:uppercase;">Key Change</p>
    <p style="margin:0 0 16px 0;font-size:14px;line-height:1.6;">{key_change}</p>

    <p style="margin:0 0 6px 0;font-size:11px;font-weight:bold;color:{TEXT_LIGHT};text-transform:uppercase;">Data Warning</p>
    <p style="margin:0 0 0 0;font-size:13px;line-height:1.6;color:{TEXT_LIGHT};">{data_warning}</p>

    {button_html}
  </div>
  <p style="margin:14px 0 0 0;font-size:10px;color:{TEXT_LIGHT};text-align:center;">
    Data retrieved {dq.get('retrieved_at')} &middot; status {dq.get('status')}
  </p>
</div>
</body>
</html>"""


def render_pdf(html_str: str, output_path: Path) -> Optional[str]:
    """
    Render the HTML report to PDF via weasyprint. Fails gracefully.

    weasyprint prints a raw troubleshooting notice straight to stdout (not
    through logging) when its native libraries are missing — redirected here
    so it never corrupts --json output on stdout; the real error is still
    logged via log.error() below.
    """
    import contextlib
    import io

    try:
        with contextlib.redirect_stdout(io.StringIO()):
            from weasyprint import HTML
            output_path.parent.mkdir(parents=True, exist_ok=True)
            HTML(string=html_str).write_pdf(str(output_path))
        return str(output_path)
    except Exception as exc:  # noqa: BLE001 - PDF generation must not crash the report
        log.error("PDF generation failed: %s", exc)
        return None
