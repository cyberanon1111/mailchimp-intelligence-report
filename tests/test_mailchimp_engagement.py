"""
Tests for tools/mailchimp_engagement.py — the Campaign -> Link -> Contact ->
Activity behavioral model. No network calls; synthetic Mailchimp-shaped data.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "tools"))

from mailchimp_engagement import (  # noqa: E402
    build_activity_events,
    build_cross_campaign_engagement,
    build_contact_activity,
    build_content_performance,
    build_engagement_matrix,
    build_link_clickers,
    build_link_table,
    build_multi_link_behavior,
    build_segmentation,
    canonicalize_url,
    classify_contact_segment,
    dedup_events,
    SEGMENT_MULTI_LINK_ENGAGER,
    SEGMENT_NO_ENGAGEMENT,
    SEGMENT_OPENED_ONLY,
    SEGMENT_REPEAT_CLICKER,
    SEGMENT_SINGLE_CLICKER,
)

PROFILES = {
    "john@abcmaritime.com": {"first_name": "John", "last_name": "Smith", "company": "ABC Maritime", "job_title": "DPA"},
    "jane@xyzshipping.com": {"first_name": "Jane", "last_name": "Doe", "company": "XYZ Shipping", "job_title": ""},
}


def _raw_member(email, email_id, activity):
    return {"campaign_id": "camp1", "email_address": email, "email_id": email_id, "activity": activity}


# ── canonicalize_url ─────────────────────────────────────────────────────

def test_canonicalize_url_strips_utm_params():
    url = "https://example.com/guide?utm_source=x&utm_campaign=y&utm_medium=email&ref=abc"
    result = canonicalize_url(url)
    assert "utm_source" not in result
    assert "utm_campaign" not in result
    assert "ref=abc" in result
    assert result.startswith("https://example.com/guide")


def test_canonicalize_url_same_destination_different_campaigns_matches():
    a = canonicalize_url("https://example.com/x?utm_campaign=campaign1&utm_source=s")
    b = canonicalize_url("https://example.com/x?utm_campaign=campaign2&utm_source=s")
    assert a == b


def test_canonicalize_url_empty():
    assert canonicalize_url(None) == ""
    assert canonicalize_url("") == ""


# ── build_activity_events ────────────────────────────────────────────────

def test_build_activity_events_basic():
    raw = [
        _raw_member("john@abcmaritime.com", "e1", [
            {"action": "open", "timestamp": "2026-09-01T09:00:00+00:00", "ip": "1.2.3.4"},
            {"action": "click", "timestamp": "2026-09-01T09:05:00+00:00", "url": "https://x.com/a"},
        ]),
    ]
    events = build_activity_events("camp1", "September Update", raw, PROFILES)
    assert len(events) == 2
    assert events[0]["event_type"] == "open"
    assert events[0]["name"] == "John Smith"
    assert events[0]["company"] == "ABC Maritime"
    assert events[1]["event_type"] == "click"
    assert events[1]["url"] == "https://x.com/a"


def test_build_activity_events_unknown_contact_no_crash():
    raw = [_raw_member("nobody@nowhere.com", "e9", [{"action": "open", "timestamp": "t"}])]
    events = build_activity_events("camp1", "Title", raw, {})
    assert events[0]["name"] == ""
    assert events[0]["company"] == ""


def test_build_activity_events_ignores_unknown_action_types():
    raw = [_raw_member("john@abcmaritime.com", "e1", [{"action": "spam_complaint", "timestamp": "t"}])]
    events = build_activity_events("camp1", "Title", raw, PROFILES)
    assert events == []


def test_build_activity_events_empty_activity_produces_no_events():
    raw = [_raw_member("john@abcmaritime.com", "e1", [])]
    events = build_activity_events("camp1", "Title", raw, PROFILES)
    assert events == []


# ── dedup_events ─────────────────────────────────────────────────────────

def test_dedup_events_removes_exact_duplicates():
    e = {"campaign_id": "c1", "email_id": "e1", "event_type": "open", "timestamp": "t1", "url": None}
    result = dedup_events([e, dict(e)])
    assert len(result) == 1


def test_dedup_events_keeps_distinct_events():
    e1 = {"campaign_id": "c1", "email_id": "e1", "event_type": "click", "timestamp": "t1", "url": "https://a.com"}
    e2 = {"campaign_id": "c1", "email_id": "e1", "event_type": "click", "timestamp": "t2", "url": "https://a.com"}
    result = dedup_events([e1, e2])
    assert len(result) == 2


# ── build_link_table ─────────────────────────────────────────────────────

def test_build_link_table_ranks_by_total_clicks():
    raw = [
        {"id": "l1", "url": "https://a.com", "total_clicks": 10, "unique_clicks": 8, "click_percentage": 0.4},
        {"id": "l2", "url": "https://b.com", "total_clicks": 30, "unique_clicks": 20, "click_percentage": 0.6},
    ]
    table = build_link_table(raw)
    assert table[0]["url"] == "https://b.com"
    assert table[0]["rank"] == 1
    assert table[1]["rank"] == 2


def test_build_link_table_empty():
    assert build_link_table([]) == []


# ── build_link_clickers (Phase 6: WHO clicked WHAT) ─────────────────────

def _click_events():
    return [
        {"campaign_id": "c1", "campaign_title": "T", "email_id": "e1", "email": "john@abcmaritime.com",
         "name": "John Smith", "company": "ABC Maritime", "event_type": "click",
         "timestamp": "2026-09-01T09:18:00+00:00", "url": "https://x.com/guide"},
        {"campaign_id": "c1", "campaign_title": "T", "email_id": "e1", "email": "john@abcmaritime.com",
         "name": "John Smith", "company": "ABC Maritime", "event_type": "click",
         "timestamp": "2026-09-01T09:22:00+00:00", "url": "https://x.com/guide"},
        {"campaign_id": "c1", "campaign_title": "T", "email_id": "e2", "email": "jane@xyzshipping.com",
         "name": "Jane Doe", "company": "XYZ Shipping", "event_type": "click",
         "timestamp": "2026-09-01T10:00:00+00:00", "url": "https://x.com/guide"},
    ]


def test_build_link_clickers_preserves_per_contact_click_counts():
    clickers = build_link_clickers(_click_events())
    rows = clickers["https://x.com/guide"]
    john = next(r for r in rows if r["email"] == "john@abcmaritime.com")
    assert john["clicks"] == 2  # not collapsed to 1
    assert john["first_click"] == "2026-09-01T09:18:00+00:00"
    assert john["last_click"] == "2026-09-01T09:22:00+00:00"


def test_build_link_clickers_sorted_by_clicks_desc():
    clickers = build_link_clickers(_click_events())
    rows = clickers["https://x.com/guide"]
    assert rows[0]["email"] == "john@abcmaritime.com"  # 2 clicks > jane's 1


# ── build_contact_activity (Phase 7) ─────────────────────────────────────

def test_build_contact_activity_chronological_timeline():
    events = [
        {"campaign_id": "c1", "campaign_title": "T", "email_id": "e1", "email": "john@abcmaritime.com",
         "name": "John Smith", "company": "ABC Maritime", "event_type": "click",
         "timestamp": "2026-09-01T11:00:00+00:00", "url": "https://x.com/b"},
        {"campaign_id": "c1", "campaign_title": "T", "email_id": "e1", "email": "john@abcmaritime.com",
         "name": "John Smith", "company": "ABC Maritime", "event_type": "open",
         "timestamp": "2026-09-01T09:00:00+00:00", "url": None},
    ]
    activity = build_contact_activity(events)
    timeline = activity["e1"]["timeline"]
    assert timeline[0]["event_type"] == "open"  # earlier timestamp first
    assert timeline[1]["event_type"] == "click"


def test_build_contact_activity_missing_timestamp_marked_not_dropped():
    events = [{"campaign_id": "c1", "campaign_title": "T", "email_id": "e1", "email": "x@y.com",
               "name": "", "company": "", "event_type": "open", "timestamp": None, "url": None}]
    activity = build_contact_activity(events)
    assert len(activity["e1"]["timeline"]) == 1
    assert activity["e1"]["timeline"][0]["timestamp_available"] is False


# ── build_multi_link_behavior (Phase 8) ──────────────────────────────────

def test_build_multi_link_behavior_only_includes_multiple_distinct_links():
    events = _click_events()  # john clicks same link twice, jane clicks once
    events.append({
        "campaign_id": "c1", "campaign_title": "T", "email_id": "e1", "email": "john@abcmaritime.com",
        "name": "John Smith", "company": "ABC Maritime", "event_type": "click",
        "timestamp": "2026-09-01T12:00:00+00:00", "url": "https://x.com/other",
    })
    rows = build_multi_link_behavior(events)
    assert len(rows) == 1
    assert rows[0]["email"] == "john@abcmaritime.com"
    assert rows[0]["unique_links_count"] == 2
    assert rows[0]["total_clicks"] == 3


def test_build_multi_link_behavior_single_link_clicker_excluded():
    rows = build_multi_link_behavior(_click_events())  # jane: only 1 distinct link
    assert not any(r["email"] == "jane@xyzshipping.com" for r in rows)


# ── build_cross_campaign_engagement (Phase 9) ────────────────────────────

def test_cross_campaign_engagement_requires_min_campaigns():
    events = [
        {"campaign_id": "c1", "campaign_title": "T1", "email_id": "e1", "email": "john@abcmaritime.com",
         "name": "John Smith", "company": "ABC Maritime", "event_type": "open", "timestamp": "t1", "url": None},
        {"campaign_id": "c2", "campaign_title": "T2", "email_id": "e1", "email": "john@abcmaritime.com",
         "name": "John Smith", "company": "ABC Maritime", "event_type": "click", "timestamp": "t2", "url": "https://a.com"},
        {"campaign_id": "c1", "campaign_title": "T1", "email_id": "e2", "email": "jane@xyzshipping.com",
         "name": "Jane Doe", "company": "XYZ Shipping", "event_type": "open", "timestamp": "t3", "url": None},
    ]
    rows = build_cross_campaign_engagement(events, min_campaigns=2)
    assert len(rows) == 1
    assert rows[0]["email"] == "john@abcmaritime.com"
    assert rows[0]["campaigns_engaged"] == 2


def test_cross_campaign_engagement_does_not_call_them_leads():
    # Structural guarantee: no key named "lead" or "score" anywhere in the output.
    events = [
        {"campaign_id": "c1", "campaign_title": "T1", "email_id": "e1", "email": "a@b.com",
         "name": "A", "company": "C", "event_type": "open", "timestamp": "t1", "url": None},
        {"campaign_id": "c2", "campaign_title": "T2", "email_id": "e1", "email": "a@b.com",
         "name": "A", "company": "C", "event_type": "open", "timestamp": "t2", "url": None},
    ]
    rows = build_cross_campaign_engagement(events)
    keys = set(rows[0].keys())
    assert not any("lead" in k.lower() or "score" in k.lower() for k in keys)


# ── classify_contact_segment / build_segmentation (Phase 10) ────────────

def test_classify_contact_segment_all_buckets():
    assert classify_contact_segment(0, 0, 0) == SEGMENT_NO_ENGAGEMENT
    assert classify_contact_segment(1, 0, 0) == SEGMENT_OPENED_ONLY
    assert classify_contact_segment(1, 1, 1) == SEGMENT_SINGLE_CLICKER
    assert classify_contact_segment(1, 3, 1) == SEGMENT_REPEAT_CLICKER
    assert classify_contact_segment(1, 3, 2) == SEGMENT_MULTI_LINK_ENGAGER


def test_build_segmentation_counts_no_engagement_from_full_recipient_list():
    events = [
        {"campaign_id": "c1", "campaign_title": "T", "email_id": "e1", "email": "a@b.com",
         "name": "A", "company": "", "event_type": "open", "timestamp": "t1", "url": None},
    ]
    # e2 and e3 were delivered to but never opened/clicked (email-activity
    # returns them with an empty activity array in the real API).
    seg = build_segmentation(events, all_recipient_email_ids={"e1", "e2", "e3"})
    assert seg["counts"][SEGMENT_NO_ENGAGEMENT] == 2
    assert seg["counts"][SEGMENT_OPENED_ONLY] == 1


def test_build_segmentation_is_deterministic():
    events = [
        {"campaign_id": "c1", "campaign_title": "T", "email_id": "e1", "email": "a@b.com",
         "name": "A", "company": "", "event_type": "click", "timestamp": "t1", "url": "https://x.com"},
        {"campaign_id": "c1", "campaign_title": "T", "email_id": "e1", "email": "a@b.com",
         "name": "A", "company": "", "event_type": "click", "timestamp": "t2", "url": "https://x.com"},
    ]
    seg1 = build_segmentation(events)
    seg2 = build_segmentation(events)
    assert seg1["counts"] == seg2["counts"]
    assert seg1["counts"][SEGMENT_REPEAT_CLICKER] == 1


# ── build_engagement_matrix (Phase 11) ───────────────────────────────────

def test_build_engagement_matrix_values_are_actual_counts():
    events = _click_events()
    matrix = build_engagement_matrix(events)
    assert "https://x.com/guide" in matrix["links"]
    john_row = next(r for r in matrix["rows"] if r["email"] == "john@abcmaritime.com")
    idx = matrix["links"].index("https://x.com/guide")
    assert john_row["cells"][idx] == 2  # john clicked that link twice


def test_build_engagement_matrix_respects_max_links():
    events = []
    for i in range(20):
        events.append({
            "campaign_id": "c1", "campaign_title": "T", "email_id": "e1", "email": "a@b.com",
            "name": "A", "company": "", "event_type": "click", "timestamp": f"t{i}",
            "url": f"https://x.com/link{i}",
        })
    matrix = build_engagement_matrix(events, max_links=5)
    assert len(matrix["links"]) == 5


# ── build_content_performance (Phase 12) ─────────────────────────────────

def test_build_content_performance_aggregates_across_campaigns():
    link_a_camp1 = {"link_id": "l1", "url": "https://x.com/guide?utm_campaign=c1",
                     "canonical_url": "https://x.com/guide?", "total_clicks": 10, "unique_clicks": 8,
                     "click_share": 0.5, "last_click": None, "rank": 1}
    link_a_camp2 = {"link_id": "l2", "url": "https://x.com/guide?utm_campaign=c2",
                     "canonical_url": "https://x.com/guide?", "total_clicks": 15, "unique_clicks": 12,
                     "click_share": 0.6, "last_click": None, "rank": 1}
    perf = build_content_performance({"c1": [link_a_camp1], "c2": [link_a_camp2]})
    assert len(perf) == 1
    assert perf[0]["total_clicks"] == 25
    assert perf[0]["campaigns_appeared_in"] == 2
