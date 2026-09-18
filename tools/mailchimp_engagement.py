"""
Tool: mailchimp_engagement.py
Purpose: Pure, dependency-free functions that build the CAMPAIGN -> LINK ->
         CONTACT -> ACTIVITY behavioral intelligence model described in the
         Engagement Intelligence Report spec, from raw Mailchimp API data.

No network calls here (see mailchimp_report_client.py for that) — everything
below takes already-fetched dicts and returns normalized structures. Kept
separate from mailchimp_metrics.py because that module is about campaign-level
RATES; this one is about link/contact/activity-level BEHAVIOR.

Data model (dicts, matching the rest of this codebase's style):

  ActivityEvent:
    campaign_id, campaign_title, email_id, email, name, company,
    event_type ("open"|"click"|"bounce"), timestamp (ISO 8601 or None),
    url (only for "click"), bounce_type (only for "bounce")

Every function here reports only what Mailchimp actually returned — nothing
is inferred or fabricated. Where data is missing (e.g. no timestamp), it is
surfaced as None / "unavailable", never silently dropped or guessed.
"""

from __future__ import annotations

from collections import defaultdict
from typing import Optional
from urllib.parse import urlsplit, urlunsplit, parse_qsl, urlencode

# ── URL canonicalization (for cross-campaign content aggregation) ─────────

_UTM_PARAMS = {"utm_source", "utm_campaign", "utm_medium", "utm_term", "utm_content"}


def canonicalize_url(url: Optional[str]) -> str:
    """
    Strip Mailchimp's per-campaign UTM tracking params so the SAME destination
    URL sent in different campaigns can be aggregated together (Phase 12).
    The original, unstripped URL is still preserved wherever it's displayed
    per-campaign — this is only used as a grouping key.
    """
    if not url:
        return ""
    parts = urlsplit(url)
    kept = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True) if k.lower() not in _UTM_PARAMS]
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(kept), ""))


# ── Building ActivityEvents from raw email-activity ────────────────────────

def _contact_name(profile: dict) -> str:
    first = (profile.get("first_name") or "").strip()
    last = (profile.get("last_name") or "").strip()
    full = f"{first} {last}".strip()
    return full


def build_activity_events(
    campaign_id: str,
    campaign_title: str,
    raw_email_activity: list[dict],
    member_profiles: dict[str, dict],
) -> list[dict]:
    """
    Normalize GET /reports/{id}/email-activity into flat ActivityEvent dicts.
    member_profiles is the {email -> {first_name,last_name,company,job_title}}
    map from mailchimp_report_client.get_member_profiles() — used only to
    attach a human-readable name/company; never fetched per-event.
    """
    events: list[dict] = []
    for member in raw_email_activity:
        email = (member.get("email_address") or "").lower()
        email_id = member.get("email_id")
        profile = member_profiles.get(email, {})
        name = _contact_name(profile)
        company = (profile.get("company") or "").strip()

        for act in member.get("activity", []) or []:
            action = act.get("action")
            if action not in ("open", "click", "bounce"):
                continue
            events.append({
                "campaign_id": campaign_id,
                "campaign_title": campaign_title,
                "email_id": email_id,
                "email": email,
                "name": name,
                "company": company,
                "event_type": action,
                "timestamp": act.get("timestamp"),
                "url": act.get("url") if action == "click" else None,
                "bounce_type": act.get("type") if action == "bounce" else None,
            })
    return events


def dedup_events(events: list[dict]) -> list[dict]:
    """
    Remove exact duplicate events (Phase 19: 'duplicate events'). A duplicate
    is the same contact, same event type, same timestamp, same URL, in the
    same campaign — this can happen when merging newly-fetched incremental
    events with previously-stored ones.
    """
    seen = set()
    out = []
    for e in events:
        key = (e["campaign_id"], e["email_id"], e["event_type"], e["timestamp"], e.get("url"))
        if key in seen:
            continue
        seen.add(key)
        out.append(e)
    return out


# ── Phase 5: Link / Content Intelligence ────────────────────────────────

def build_link_table(raw_click_details: list[dict]) -> list[dict]:
    """
    GET /reports/{id}/click-details -> ranked link table.
    click_share comes directly from Mailchimp's own click_percentage field
    (share of this campaign's total clicks) — not recomputed, to stay
    consistent with what Mailchimp itself reports.
    """
    links = [{
        "link_id": l.get("id"),
        "url": l.get("url"),
        "canonical_url": canonicalize_url(l.get("url")),
        "total_clicks": l.get("total_clicks", 0) or 0,
        "unique_clicks": l.get("unique_clicks", 0) or 0,
        "click_share": l.get("click_percentage", 0.0) or 0.0,
        "last_click": l.get("last_click"),
    } for l in raw_click_details]
    links.sort(key=lambda l: l["total_clicks"], reverse=True)
    for i, link in enumerate(links, start=1):
        link["rank"] = i
    return links


# ── Phase 6: Who Clicked What ────────────────────────────────────────────

def build_link_clickers(events: list[dict]) -> dict[str, list[dict]]:
    """
    Group click events by URL -> list of {email, name, company, clicks,
    first_click, last_click}. Preserves click counts and timestamps per
    contact per link — never collapses to a single number without the detail
    behind it (Phase 6 requirement).
    """
    by_url: dict[str, dict[str, dict]] = defaultdict(dict)
    for e in events:
        if e["event_type"] != "click" or not e.get("url"):
            continue
        url = e["url"]
        contact = by_url[url].setdefault(e["email_id"], {
            "email_id": e["email_id"],
            "email": e["email"],
            "name": e["name"],
            "company": e["company"],
            "clicks": 0,
            "first_click": None,
            "last_click": None,
        })
        contact["clicks"] += 1
        ts = e.get("timestamp")
        if ts:
            if contact["first_click"] is None or ts < contact["first_click"]:
                contact["first_click"] = ts
            if contact["last_click"] is None or ts > contact["last_click"]:
                contact["last_click"] = ts

    result: dict[str, list[dict]] = {}
    for url, contacts in by_url.items():
        rows = sorted(contacts.values(), key=lambda c: c["clicks"], reverse=True)
        result[url] = rows
    return result


# ── Phase 7: Individual Activity Timeline ───────────────────────────────

def build_contact_activity(events: list[dict]) -> dict[str, dict]:
    """
    Per-contact chronological timeline of everything Mailchimp recorded for
    them in these events (opens, clicks with URL, bounces). Events with no
    timestamp are kept and explicitly marked unavailable, never dropped.
    """
    by_contact: dict[str, dict] = {}
    for e in events:
        cid = e["email_id"]
        rec = by_contact.setdefault(cid, {
            "email_id": cid,
            "email": e["email"],
            "name": e["name"],
            "company": e["company"],
            "timeline": [],
        })
        rec["timeline"].append({
            "timestamp": e.get("timestamp"),
            "timestamp_available": e.get("timestamp") is not None,
            "event_type": e["event_type"],
            "url": e.get("url"),
            "campaign_id": e["campaign_id"],
            "campaign_title": e["campaign_title"],
        })

    for rec in by_contact.values():
        rec["timeline"].sort(key=lambda t: t["timestamp"] or "")
    return by_contact


# ── Phase 8: Multi-Link Behavior ─────────────────────────────────────────

def build_multi_link_behavior(events: list[dict]) -> list[dict]:
    """Contacts who clicked more than one distinct link within the given events."""
    by_contact: dict[str, dict] = {}
    for e in events:
        if e["event_type"] != "click" or not e.get("url"):
            continue
        cid = e["email_id"]
        rec = by_contact.setdefault(cid, {
            "email_id": cid, "email": e["email"], "name": e["name"], "company": e["company"],
            "links": set(), "total_clicks": 0,
        })
        rec["links"].add(e["url"])
        rec["total_clicks"] += 1

    rows = []
    for rec in by_contact.values():
        if len(rec["links"]) > 1:
            rows.append({
                "email_id": rec["email_id"], "email": rec["email"], "name": rec["name"],
                "company": rec["company"],
                "unique_links_count": len(rec["links"]),
                "total_clicks": rec["total_clicks"],
                "links": sorted(rec["links"]),
            })
    rows.sort(key=lambda r: (r["unique_links_count"], r["total_clicks"]), reverse=True)
    return rows


# ── Phase 9: Repeat Engagement Across Campaigns ─────────────────────────

def build_cross_campaign_engagement(all_events: list[dict], min_campaigns: int = 2) -> list[dict]:
    """
    Contacts who engaged (opened or clicked) with more than one campaign,
    across the full persisted event history passed in. Reports observed
    behavior only — no scoring, no "lead" labeling (per Phase 9 instruction).
    """
    by_contact: dict[str, dict] = {}
    for e in all_events:
        if e["event_type"] not in ("open", "click"):
            continue
        cid = e["email_id"]
        rec = by_contact.setdefault(cid, {
            "email_id": cid, "email": e["email"], "name": e["name"], "company": e["company"],
            "campaigns": set(), "opens": 0, "clicks": 0, "unique_links": set(),
        })
        rec["campaigns"].add(e["campaign_id"])
        if e["event_type"] == "open":
            rec["opens"] += 1
        elif e["event_type"] == "click":
            rec["clicks"] += 1
            if e.get("url"):
                rec["unique_links"].add(e["url"])

    rows = []
    for rec in by_contact.values():
        if len(rec["campaigns"]) >= min_campaigns:
            rows.append({
                "email_id": rec["email_id"], "email": rec["email"], "name": rec["name"],
                "company": rec["company"],
                "campaigns_engaged": len(rec["campaigns"]),
                "opens": rec["opens"], "clicks": rec["clicks"],
                "unique_links": len(rec["unique_links"]),
            })
    rows.sort(key=lambda r: (r["campaigns_engaged"], r["clicks"], r["opens"]), reverse=True)
    return rows


# ── Phase 10: Behavioral Segmentation (deterministic, documented) ───────

SEGMENT_NO_ENGAGEMENT = "NO_ENGAGEMENT"
SEGMENT_OPENED_ONLY = "OPENED_ONLY"
SEGMENT_SINGLE_CLICKER = "SINGLE_CLICKER"
SEGMENT_REPEAT_CLICKER = "REPEAT_CLICKER"
SEGMENT_MULTI_LINK_ENGAGER = "MULTI_LINK_ENGAGER"


def classify_contact_segment(opens: int, clicks: int, unique_links: int) -> str:
    """
    Deterministic single-campaign segment for one contact. Rules (in priority
    order, documented here rather than left implicit):
      1. unique_links > 1                -> MULTI_LINK_ENGAGER
      2. clicks > 1 (single link)        -> REPEAT_CLICKER
      3. clicks == 1                     -> SINGLE_CLICKER
      4. opens > 0 and clicks == 0       -> OPENED_ONLY
      5. otherwise (delivered, no activity) -> NO_ENGAGEMENT
    No score is computed here — buckets only, per Phase 10 instruction.
    """
    if unique_links > 1:
        return SEGMENT_MULTI_LINK_ENGAGER
    if clicks > 1:
        return SEGMENT_REPEAT_CLICKER
    if clicks == 1:
        return SEGMENT_SINGLE_CLICKER
    if opens > 0:
        return SEGMENT_OPENED_ONLY
    return SEGMENT_NO_ENGAGEMENT


def build_segmentation(events: list[dict], all_recipient_email_ids: Optional[set] = None) -> dict:
    """
    Build the segment buckets for one campaign's events. `all_recipient_email_ids`
    should be every email_id Mailchimp reported as sent-to (even with empty
    activity) so NO_ENGAGEMENT is counted correctly — GET /email-activity
    already returns the full sent-to list (including zero-activity members),
    so pass the email_ids seen there, not just the ones with events.
    """
    per_contact: dict[str, dict] = {}
    for e in events:
        cid = e["email_id"]
        rec = per_contact.setdefault(cid, {"opens": 0, "clicks": 0, "links": set(),
                                            "email": e["email"], "name": e["name"], "company": e["company"]})
        if e["event_type"] == "open":
            rec["opens"] += 1
        elif e["event_type"] == "click":
            rec["clicks"] += 1
            if e.get("url"):
                rec["links"].add(e["url"])

    if all_recipient_email_ids:
        for cid in all_recipient_email_ids:
            per_contact.setdefault(cid, {"opens": 0, "clicks": 0, "links": set(),
                                          "email": "", "name": "", "company": ""})

    buckets: dict[str, list[dict]] = defaultdict(list)
    for cid, rec in per_contact.items():
        segment = classify_contact_segment(rec["opens"], rec["clicks"], len(rec["links"]))
        buckets[segment].append({
            "email_id": cid, "email": rec["email"], "name": rec["name"], "company": rec["company"],
            "opens": rec["opens"], "clicks": rec["clicks"], "unique_links": len(rec["links"]),
        })

    return {
        "counts": {seg: len(buckets.get(seg, [])) for seg in
                   (SEGMENT_NO_ENGAGEMENT, SEGMENT_OPENED_ONLY, SEGMENT_SINGLE_CLICKER,
                    SEGMENT_REPEAT_CLICKER, SEGMENT_MULTI_LINK_ENGAGER)},
        "members": dict(buckets),
    }


# ── Phase 11: Engagement Matrix (Contact x Link) ────────────────────────

def build_engagement_matrix(events: list[dict], max_links: int = 12) -> dict:
    """
    Contact x Link matrix of click counts. Limited to the top `max_links` by
    total clicks to keep the table readable — this is a display concern, not
    a data-loss concern (the full link table is available separately).
    """
    link_totals: dict[str, int] = defaultdict(int)
    cell_counts: dict[tuple, int] = defaultdict(int)
    contacts: dict[str, dict] = {}

    for e in events:
        if e["event_type"] != "click" or not e.get("url"):
            continue
        link_totals[e["url"]] += 1
        cell_counts[(e["email_id"], e["url"])] += 1
        contacts.setdefault(e["email_id"], {"email": e["email"], "name": e["name"], "company": e["company"]})

    top_links = sorted(link_totals, key=lambda u: link_totals[u], reverse=True)[:max_links]

    rows = []
    for cid, info in contacts.items():
        row_cells = [cell_counts.get((cid, url), 0) for url in top_links]
        if any(row_cells):
            rows.append({"email_id": cid, **info, "cells": row_cells, "row_total": sum(row_cells)})
    rows.sort(key=lambda r: r["row_total"], reverse=True)

    return {"links": top_links, "rows": rows}


# ── Phase 12: Content Performance (cross-campaign) ──────────────────────

def build_content_performance(link_tables_by_campaign: dict[str, list[dict]]) -> list[dict]:
    """
    Aggregate the same destination URL (canonicalized, UTM stripped) across
    multiple campaigns' link tables to see which content assets consistently
    attract engagement, per Phase 12.
    """
    agg: dict[str, dict] = {}
    for campaign_id, links in link_tables_by_campaign.items():
        for link in links:
            key = link["canonical_url"] or link["url"]
            rec = agg.setdefault(key, {
                "canonical_url": key, "total_clicks": 0, "unique_clicks": 0,
                "campaigns": set(),
            })
            rec["total_clicks"] += link["total_clicks"]
            rec["unique_clicks"] += link["unique_clicks"]
            rec["campaigns"].add(campaign_id)

    rows = [{
        "canonical_url": r["canonical_url"],
        "total_clicks": r["total_clicks"],
        "unique_clicks": r["unique_clicks"],
        "campaigns_appeared_in": len(r["campaigns"]),
    } for r in agg.values()]
    rows.sort(key=lambda r: r["total_clicks"], reverse=True)
    return rows
