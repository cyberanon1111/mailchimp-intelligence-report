"""
Tool: mailchimp_engagement_store.py
Purpose: Lightweight, file-based persistence for engagement events — no
         database. One JSON file per campaign under
         .tmp/mailchimp_reports/engagement/<campaign_id>.json, containing a
         deduplicated event log plus a watermark timestamp so subsequent runs
         only fetch NEW email-activity (Mailchimp's `since` param) instead of
         re-pulling everything (Phase 16/17).

Cross-campaign engagement (Phase 9) is computed by reading all persisted
per-campaign files — cheap and simple at this data volume; no separate
rollup index needed.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Optional

from mailchimp_engagement import dedup_events

log = logging.getLogger("mailchimp_report")

STORE_DIR = Path(__file__).parent.parent / ".tmp" / "mailchimp_reports" / "engagement"


def _campaign_path(campaign_id: str) -> Path:
    return STORE_DIR / f"{campaign_id}.json"


def load_campaign_events(campaign_id: str) -> dict:
    """Returns {"campaign_id", "watermark", "events": [...], "link_table": [...]}. Empty shell if none stored yet."""
    path = _campaign_path(campaign_id)
    if not path.exists():
        return {"campaign_id": campaign_id, "watermark": None, "events": [], "link_table": []}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        data.setdefault("link_table", [])
        return data
    except (json.JSONDecodeError, OSError) as exc:
        log.warning("Could not read engagement store for %s (%s) — starting fresh.", campaign_id, exc)
        return {"campaign_id": campaign_id, "watermark": None, "events": [], "link_table": []}


def save_campaign_events(
    campaign_id: str, events: list[dict], watermark: Optional[str],
    link_table: Optional[list[dict]] = None,
) -> None:
    STORE_DIR.mkdir(parents=True, exist_ok=True)
    deduped = dedup_events(events)
    payload = {
        "campaign_id": campaign_id,
        "watermark": watermark,
        "event_count": len(deduped),
        "events": deduped,
        "link_table": link_table if link_table is not None else load_campaign_events(campaign_id).get("link_table", []),
    }
    _campaign_path(campaign_id).write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")


def merge_and_save(campaign_id: str, new_events: list[dict], link_table: Optional[list[dict]] = None) -> list[dict]:
    """
    Merge newly-fetched events with whatever's already stored for this
    campaign, dedup, compute the new watermark (latest timestamp seen), and
    persist (including the latest link table snapshot). Returns the full
    deduplicated event list for this campaign.
    """
    existing = load_campaign_events(campaign_id)
    combined = dedup_events(existing.get("events", []) + new_events)

    timestamps = [e["timestamp"] for e in combined if e.get("timestamp")]
    watermark = max(timestamps) if timestamps else existing.get("watermark")

    save_campaign_events(campaign_id, combined, watermark, link_table=link_table)
    return combined


def get_watermark(campaign_id: str) -> Optional[str]:
    return load_campaign_events(campaign_id).get("watermark")


def load_all_events(campaign_ids: Optional[list[str]] = None) -> list[dict]:
    """
    Load events across ALL persisted campaigns (or a specific subset), for
    cross-campaign engagement analysis (Phase 9). Missing/corrupt files are
    skipped with a warning, never fatal.
    """
    if not STORE_DIR.exists():
        return []
    all_events: list[dict] = []
    paths = (
        [_campaign_path(cid) for cid in campaign_ids]
        if campaign_ids is not None
        else sorted(STORE_DIR.glob("*.json"))
    )
    for path in paths:
        if not path.exists():
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            all_events.extend(data.get("events", []))
        except (json.JSONDecodeError, OSError) as exc:
            log.warning("Skipping unreadable engagement file %s: %s", path, exc)
    return all_events


def load_all_link_tables() -> dict[str, list[dict]]:
    """All persisted per-campaign link tables, for cross-campaign content performance (Phase 12)."""
    if not STORE_DIR.exists():
        return {}
    tables: dict[str, list[dict]] = {}
    for path in sorted(STORE_DIR.glob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if data.get("link_table"):
                tables[data["campaign_id"]] = data["link_table"]
        except (json.JSONDecodeError, OSError) as exc:
            log.warning("Skipping unreadable engagement file %s: %s", path, exc)
    return tables
