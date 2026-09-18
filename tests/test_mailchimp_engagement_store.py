"""
Tests for tools/mailchimp_engagement_store.py — the file-based (no database)
persistence layer for engagement events: dedup on merge, watermark tracking
for incremental fetch, and cross-campaign loading.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "tools"))

import mailchimp_engagement_store as store  # noqa: E402


def _event(campaign_id, email_id, event_type, timestamp, url=None):
    return {"campaign_id": campaign_id, "campaign_title": "T", "email_id": email_id,
            "email": f"{email_id}@x.com", "name": "", "company": "",
            "event_type": event_type, "timestamp": timestamp, "url": url, "bounce_type": None}


def test_load_campaign_events_empty_shell_when_nothing_stored(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "STORE_DIR", tmp_path / "engagement")
    result = store.load_campaign_events("camp_missing")
    assert result["events"] == []
    assert result["watermark"] is None


def test_merge_and_save_dedups_across_runs(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "STORE_DIR", tmp_path / "engagement")
    e1 = _event("c1", "u1", "open", "2026-09-01T09:00:00+00:00")
    store.merge_and_save("c1", [e1])
    # Second "run" refetches the same event (e.g. re-pulled without since=) plus one new one
    e2 = _event("c1", "u1", "click", "2026-09-01T09:05:00+00:00", url="https://a.com")
    combined = store.merge_and_save("c1", [e1, e2])
    assert len(combined) == 2  # e1 not duplicated


def test_merge_and_save_updates_watermark_to_latest_timestamp(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "STORE_DIR", tmp_path / "engagement")
    store.merge_and_save("c1", [_event("c1", "u1", "open", "2026-09-01T09:00:00+00:00")])
    store.merge_and_save("c1", [_event("c1", "u1", "click", "2026-09-02T10:00:00+00:00", url="https://a.com")])
    assert store.get_watermark("c1") == "2026-09-02T10:00:00+00:00"


def test_get_watermark_none_when_no_timestamps(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "STORE_DIR", tmp_path / "engagement")
    assert store.get_watermark("never_seen") is None


def test_load_all_events_across_multiple_campaigns(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "STORE_DIR", tmp_path / "engagement")
    store.merge_and_save("c1", [_event("c1", "u1", "open", "2026-09-01T09:00:00+00:00")])
    store.merge_and_save("c2", [_event("c2", "u2", "open", "2026-09-02T09:00:00+00:00")])
    all_events = store.load_all_events()
    assert len(all_events) == 2


def test_load_all_events_filters_to_requested_campaigns(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "STORE_DIR", tmp_path / "engagement")
    store.merge_and_save("c1", [_event("c1", "u1", "open", "2026-09-01T09:00:00+00:00")])
    store.merge_and_save("c2", [_event("c2", "u2", "open", "2026-09-02T09:00:00+00:00")])
    filtered = store.load_all_events(campaign_ids=["c1"])
    assert len(filtered) == 1
    assert filtered[0]["campaign_id"] == "c1"


def test_load_all_events_skips_corrupt_files_without_crashing(tmp_path, monkeypatch):
    engagement_dir = tmp_path / "engagement"
    engagement_dir.mkdir(parents=True)
    monkeypatch.setattr(store, "STORE_DIR", engagement_dir)
    (engagement_dir / "broken.json").write_text("{not valid json", encoding="utf-8")
    store.merge_and_save("c1", [_event("c1", "u1", "open", "2026-09-01T09:00:00+00:00")])
    all_events = store.load_all_events()
    assert len(all_events) == 1  # the valid campaign's events still load


def test_save_and_load_link_table_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "STORE_DIR", tmp_path / "engagement")
    link_table = [{"link_id": "l1", "url": "https://a.com", "total_clicks": 5}]
    store.merge_and_save("c1", [], link_table=link_table)
    loaded = store.load_campaign_events("c1")
    assert loaded["link_table"] == link_table


def test_load_all_link_tables(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "STORE_DIR", tmp_path / "engagement")
    store.merge_and_save("c1", [], link_table=[{"link_id": "l1", "url": "https://a.com"}])
    store.merge_and_save("c2", [], link_table=[{"link_id": "l2", "url": "https://b.com"}])
    tables = store.load_all_link_tables()
    assert set(tables.keys()) == {"c1", "c2"}
