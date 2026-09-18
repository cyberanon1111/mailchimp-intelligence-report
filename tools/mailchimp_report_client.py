"""
Tool: mailchimp_report_client.py
Purpose: Thin, read-only Mailchimp REST client for the daily performance report.
         Follows the same requests + HTTP Basic Auth pattern used by the rest of
         tools/mailchimp_*.py — no SDK, no new auth mechanism.

Reuses the existing env vars (checks both naming conventions already used in
this repo): MAILCHIMP_API_KEY, MAILCHIMP_SERVER / MAILCHIMP_SERVER_PREFIX,
MAILCHIMP_AUDIENCE_ID / MAILCHIMP_LIST_ID.
"""

from __future__ import annotations

import logging
import os
import time
from typing import Any, Optional

import requests
from dotenv import load_dotenv

load_dotenv()

log = logging.getLogger("mailchimp_report")

MAILCHIMP_API_KEY = os.getenv("MAILCHIMP_API_KEY")
MAILCHIMP_SERVER = os.getenv("MAILCHIMP_SERVER_PREFIX") or os.getenv("MAILCHIMP_SERVER")
MAILCHIMP_LIST_ID = os.getenv("MAILCHIMP_LIST_ID") or os.getenv("MAILCHIMP_AUDIENCE_ID")

BASE_URL = f"https://{MAILCHIMP_SERVER}.api.mailchimp.com/3.0" if MAILCHIMP_SERVER else None
AUTH = ("anystring", MAILCHIMP_API_KEY)


class MailchimpConfigError(RuntimeError):
    """Raised when required Mailchimp env vars are missing."""


class MailchimpAPIError(RuntimeError):
    """Raised when the Mailchimp API returns a non-recoverable error."""


def _require_config() -> None:
    missing = [
        name for name, val in [
            ("MAILCHIMP_API_KEY", MAILCHIMP_API_KEY),
            ("MAILCHIMP_SERVER or MAILCHIMP_SERVER_PREFIX", MAILCHIMP_SERVER),
        ] if not val
    ]
    if missing:
        raise MailchimpConfigError(
            f"Missing required Mailchimp env var(s): {', '.join(missing)}"
        )


def _get(endpoint: str, params: Optional[dict] = None, max_retries: int = 3) -> dict:
    """GET with basic retry/backoff on 429 and 5xx. Raises MailchimpAPIError otherwise."""
    _require_config()
    url = f"{BASE_URL}{endpoint}"
    last_error: Optional[str] = None

    for attempt in range(1, max_retries + 1):
        try:
            resp = requests.get(url, auth=AUTH, params=params or {}, timeout=30)
        except requests.RequestException as exc:
            last_error = str(exc)
            log.warning("Mailchimp request error (attempt %d/%d) for %s: %s",
                        attempt, max_retries, endpoint, exc)
            time.sleep(min(2 ** attempt, 10))
            continue

        if resp.status_code == 429:
            wait = int(resp.headers.get("Retry-After", 5))
            log.warning("Mailchimp rate limit hit on %s — waiting %ds (attempt %d/%d)",
                        endpoint, wait, attempt, max_retries)
            time.sleep(wait)
            continue

        if resp.status_code >= 500:
            last_error = f"HTTP {resp.status_code}: {resp.text[:300]}"
            log.warning("Mailchimp server error on %s (attempt %d/%d): %s",
                        endpoint, attempt, max_retries, last_error)
            time.sleep(min(2 ** attempt, 10))
            continue

        if resp.status_code >= 400:
            raise MailchimpAPIError(
                f"Mailchimp API error {resp.status_code} on {endpoint}: {resp.text[:300]}"
            )

        return resp.json()

    raise MailchimpAPIError(
        f"Mailchimp API unreachable after {max_retries} attempts on {endpoint}: {last_error}"
    )


def get_account_info() -> dict:
    return _get("/")


def get_campaign_reports(
    since_send_time: Optional[str] = None,
    before_send_time: Optional[str] = None,
    count: int = 1000,
) -> list[dict]:
    """
    Fetch campaign performance reports directly from Mailchimp's /reports endpoint.

    This returns opens/clicks/bounces/unsubscribes already computed by Mailchimp
    for every *sent* campaign — no need to cross-reference a local history file,
    so it captures all campaigns sent from this account, not just ones a specific
    publishing tool happened to log.
    """
    params: dict[str, Any] = {
        "count": count,
        "sort_field": "send_time",
        "sort_dir": "DESC",
    }
    if since_send_time:
        params["since_send_time"] = since_send_time
    if before_send_time:
        params["before_send_time"] = before_send_time

    reports: list[dict] = []
    offset = 0
    while True:
        params["offset"] = offset
        data = _get("/reports", params)
        batch = data.get("reports", [])
        reports.extend(batch)
        total = data.get("total_items", len(reports))
        offset += len(batch)
        if not batch or offset >= total or offset >= count:
            break
    return reports


def get_click_details(campaign_id: str, filter_bots: bool = True, count: int = 1000) -> list[dict]:
    """
    GET /reports/{campaign_id}/click-details — the link inventory for one campaign.

    Verified against the account's own Mailchimp schema: each item has
    id, url, total_clicks, click_percentage, unique_clicks, unique_click_percentage,
    last_click. This is ONE call per campaign regardless of how many links or
    subscribers exist — no per-link fan-out.
    """
    params: dict[str, Any] = {"count": count, "filter_bots": filter_bots}
    links: list[dict] = []
    offset = 0
    while True:
        params["offset"] = offset
        data = _get(f"/reports/{campaign_id}/click-details", params)
        batch = data.get("urls_clicked", [])
        links.extend(batch)
        total = data.get("total_items", len(links))
        offset += len(batch)
        if not batch or offset >= total or offset >= count:
            break
    return links


def get_email_activity(
    campaign_id: str,
    since: Optional[str] = None,
    filter_bots: bool = True,
    count: int = 1000,
) -> list[dict]:
    """
    GET /reports/{campaign_id}/email-activity — per-subscriber, per-event timeline
    (open/click/bounce with ISO timestamps, and the URL for clicks).

    Verified against the account's own Mailchimp schema. This single endpoint
    supplies WHO / WHAT / WHEN for both opens and clicks — no need to also call
    click-details/{link_id}/members, which only gives aggregate counts with no
    timestamps. Supports `since` for incremental fetching (Phase 16/17): pass
    the last-seen timestamp from local storage to only pull new events.

    ONE call per campaign (paginated), not per-subscriber or per-link.
    """
    params: dict[str, Any] = {"count": count, "filter_bots": filter_bots}
    if since:
        params["since"] = since

    members: list[dict] = []
    offset = 0
    while True:
        params["offset"] = offset
        data = _get(f"/reports/{campaign_id}/email-activity", params)
        batch = data.get("emails", [])
        members.extend(batch)
        total = data.get("total_items", len(members))
        offset += len(batch)
        if not batch or offset >= total or offset >= count:
            break
    return members


def get_member_profiles(list_id: Optional[str] = None, count: int = 1000) -> dict[str, dict]:
    """
    Bulk-fetch audience member profiles (email -> {first_name, last_name, company,
    job_title}) in one paginated pass, so contact-level intelligence doesn't do a
    per-contact lookup. Mirrors the existing bulk-fetch pattern already used in
    tools/generate_ceo_pdf_report_v2.py for phone numbers.

    Returns a dict keyed by lowercase email address.
    """
    list_id = list_id or MAILCHIMP_LIST_ID
    if not list_id:
        raise MailchimpConfigError("MAILCHIMP_LIST_ID or MAILCHIMP_AUDIENCE_ID must be set")

    profiles: dict[str, dict] = {}
    offset = 0
    fields = "members.email_address,members.merge_fields"
    while True:
        data = _get(f"/lists/{list_id}/members", {
            "count": count, "offset": offset, "fields": fields,
        })
        batch = data.get("members", [])
        for m in batch:
            mf = m.get("merge_fields", {}) or {}
            profiles[m["email_address"].lower()] = {
                "first_name": mf.get("FNAME", "") or "",
                "last_name": mf.get("LNAME", "") or "",
                "company": mf.get("COMPANY", "") or "",
                "job_title": mf.get("MMERGE7", "") or "",
            }
        offset += len(batch)
        if not batch or len(batch) < count:
            break
    return profiles
