"""
Tool: mailchimp_report_charts.py
Purpose: Generate the 5 required charts for the daily Mailchimp report as PNGs.
         Uses matplotlib with the non-interactive Agg backend (safe for cron).
         Charts are per-campaign, chronological (most recent N campaigns) —
         this makes them useful from day one without depending on an
         accumulated local history file.

Visual style: reuses the SST brand tokens already defined in
tools/publish_to_mailchimp.py (navy / bright blue) for consistency with the
newsletter's existing branded output.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.dates as mdates
from datetime import datetime

log = logging.getLogger("mailchimp_report")

NAVY = "#091D2B"
BRIGHT_BLUE = "#00A0E4"
BLUE_MID = "#0077B6"
GREY = "#6B7C8D"
RED = "#C0392B"
GREEN = "#27AE60"
BG = "#F2F4F6"

plt.rcParams.update({
    "font.family": "sans-serif",
    "font.size": 10,
    "axes.edgecolor": "#E0E8EF",
    "axes.labelcolor": NAVY,
    "text.color": NAVY,
    "xtick.color": GREY,
    "ytick.color": GREY,
    "axes.titleweight": "bold",
    "axes.titlesize": 12,
    "figure.facecolor": "white",
    "axes.facecolor": "white",
})


def _parse_dt(send_time: Optional[str]) -> Optional[datetime]:
    if not send_time:
        return None
    try:
        return datetime.fromisoformat(send_time.replace("Z", "+00:00"))
    except ValueError:
        return None


def _save(fig, path: Path) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(path, dpi=150, bbox_inches="tight", facecolor="white")
    plt.close(fig)
    return str(path)


def _chronological(campaigns: list[dict]) -> list[dict]:
    """Campaign metrics dicts sorted oldest -> newest, dropping unparseable dates."""
    dated = [(c, _parse_dt(c.get("send_time"))) for c in campaigns]
    dated = [(c, d) for c, d in dated if d is not None]
    dated.sort(key=lambda pair: pair[1])
    return [c for c, _ in dated]


def chart_open_rate_trend(campaigns: list[dict], out_path: Path) -> Optional[str]:
    ordered = _chronological(campaigns)
    if not ordered:
        return None
    dates = [_parse_dt(c["send_time"]) for c in ordered]
    rates = [c["open_rate"] * 100 for c in ordered]

    fig, ax = plt.subplots(figsize=(7, 3.5))
    ax.plot(dates, rates, color=BRIGHT_BLUE, marker="o", linewidth=2, markersize=5)
    ax.fill_between(dates, rates, color=BRIGHT_BLUE, alpha=0.08)
    ax.set_title("Open Rate Trend")
    ax.set_ylabel("Open rate (%)")
    ax.grid(axis="y", color="#E0E8EF", linewidth=0.7)
    ax.spines[["top", "right"]].set_visible(False)
    fig.autofmt_xdate()
    return _save(fig, out_path)


def chart_click_rate_trend(campaigns: list[dict], out_path: Path) -> Optional[str]:
    ordered = _chronological(campaigns)
    if not ordered:
        return None
    dates = [_parse_dt(c["send_time"]) for c in ordered]
    rates = [c["click_rate"] * 100 for c in ordered]

    fig, ax = plt.subplots(figsize=(7, 3.5))
    ax.plot(dates, rates, color=NAVY, marker="o", linewidth=2, markersize=5)
    ax.fill_between(dates, rates, color=NAVY, alpha=0.08)
    ax.set_title("Click Rate Trend")
    ax.set_ylabel("Click rate (%)")
    ax.grid(axis="y", color="#E0E8EF", linewidth=0.7)
    ax.spines[["top", "right"]].set_visible(False)
    fig.autofmt_xdate()
    return _save(fig, out_path)


def chart_emails_sent(campaigns: list[dict], out_path: Path) -> Optional[str]:
    ordered = _chronological(campaigns)
    if not ordered:
        return None
    labels = [(c.get("campaign_title") or c["campaign_id"] or "")[:22] for c in ordered]
    sent = [c["recipients"] for c in ordered]

    fig, ax = plt.subplots(figsize=(7, 3.5))
    ax.bar(range(len(labels)), sent, color=BRIGHT_BLUE)
    ax.set_title("Emails Sent — Recent Campaigns")
    ax.set_ylabel("Emails sent")
    ax.set_xticks(range(len(labels)))
    ax.set_xticklabels(labels, rotation=40, ha="right", fontsize=8)
    ax.grid(axis="y", color="#E0E8EF", linewidth=0.7)
    ax.spines[["top", "right"]].set_visible(False)
    return _save(fig, out_path)


def chart_opens_vs_clicks(campaigns: list[dict], out_path: Path) -> Optional[str]:
    ordered = _chronological(campaigns)
    if not ordered:
        return None
    labels = [(c.get("campaign_title") or c["campaign_id"] or "")[:18] for c in ordered]
    opens = [c["unique_opens"] for c in ordered]
    clicks = [c["unique_clicks"] for c in ordered]

    x = range(len(labels))
    width = 0.38
    fig, ax = plt.subplots(figsize=(7, 3.5))
    ax.bar([i - width / 2 for i in x], opens, width, label="Unique opens", color=BRIGHT_BLUE)
    ax.bar([i + width / 2 for i in x], clicks, width, label="Unique clicks", color=NAVY)
    ax.set_title("Opens vs Clicks — Recent Campaigns")
    ax.set_ylabel("Count")
    ax.set_xticks(list(x))
    ax.set_xticklabels(labels, rotation=40, ha="right", fontsize=8)
    ax.legend(frameon=False, fontsize=9)
    ax.grid(axis="y", color="#E0E8EF", linewidth=0.7)
    ax.spines[["top", "right"]].set_visible(False)
    return _save(fig, out_path)


def chart_campaign_comparison(campaigns: list[dict], out_path: Path, limit: int = 8) -> Optional[str]:
    ordered = _chronological(campaigns)[-limit:]
    if not ordered:
        return None
    labels = [(c.get("campaign_title") or c["campaign_id"] or "")[:18] for c in ordered]
    open_rates = [c["open_rate"] * 100 for c in ordered]
    click_rates = [c["click_rate"] * 100 for c in ordered]

    x = range(len(labels))
    width = 0.38
    fig, ax = plt.subplots(figsize=(7, 3.5))
    ax.bar([i - width / 2 for i in x], open_rates, width, label="Open rate %", color=BRIGHT_BLUE)
    ax.bar([i + width / 2 for i in x], click_rates, width, label="Click rate %", color=NAVY)
    ax.set_title("Campaign Comparison — Latest Campaigns")
    ax.set_ylabel("Rate (%)")
    ax.set_xticks(list(x))
    ax.set_xticklabels(labels, rotation=40, ha="right", fontsize=8)
    ax.legend(frameon=False, fontsize=9)
    ax.grid(axis="y", color="#E0E8EF", linewidth=0.7)
    ax.spines[["top", "right"]].set_visible(False)
    return _save(fig, out_path)


def generate_all_charts(campaigns: list[dict], out_dir: Path) -> dict:
    """
    Generate all 5 charts. Failures are caught per-chart so one bad chart
    doesn't take down the whole report — returns a dict of {name: path_or_None}
    plus an "errors" list for the Data Quality section.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    chart_fns = {
        "open_rate_trend": (chart_open_rate_trend, "open_rate_trend.png"),
        "click_rate_trend": (chart_click_rate_trend, "click_rate_trend.png"),
        "emails_sent": (chart_emails_sent, "emails_sent.png"),
        "opens_vs_clicks": (chart_opens_vs_clicks, "opens_vs_clicks.png"),
        "campaign_comparison": (chart_campaign_comparison, "campaign_comparison.png"),
    }
    results: dict = {}
    errors: list[str] = []
    for name, (fn, filename) in chart_fns.items():
        try:
            results[name] = fn(campaigns, out_dir / filename)
        except Exception as exc:  # noqa: BLE001 - must not abort the report
            log.error("Chart generation failed for %s: %s", name, exc)
            results[name] = None
            errors.append(f"{name}: {exc}")
    results["errors"] = errors
    return results
