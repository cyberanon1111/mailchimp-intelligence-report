"""
Tests for tools/mailchimp_metrics.py — metric calculations, safe division,
and trend/delta computation. No network calls.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "tools"))

from mailchimp_metrics import (  # noqa: E402
    aggregate_metrics,
    campaign_metrics,
    compute_trends,
    detect_anomalies,
    rate_delta,
    safe_div,
    volume_delta,
)


def _raw_report(**overrides) -> dict:
    base = {
        "id": "abc123",
        "campaign_title": "Test Campaign",
        "subject_line": "Hello",
        "send_time": "2026-09-01T09:00:00+00:00",
        "emails_sent": 1000,
        "abuse_reports": 0,
        "unsubscribed": 5,
        "bounces": {"hard_bounces": 10, "soft_bounces": 5, "syntax_errors": 0},
        "opens": {"opens_total": 500, "unique_opens": 300},
        # unique_clicks (link-aggregate) intentionally higher than
        # unique_subscriber_clicks (people) — this is the normal, expected
        # relationship and is exactly what caused the >100% click-rate bug
        # when unique_clicks was used as the click-rate numerator.
        "clicks": {"clicks_total": 80, "unique_clicks": 65, "unique_subscriber_clicks": 50},
    }
    base.update(overrides)
    return base


# ── safe_div ─────────────────────────────────────────────────────────────

def test_safe_div_normal():
    assert safe_div(10, 4) == 2.5


def test_safe_div_zero_denominator():
    assert safe_div(10, 0) == 0.0


def test_safe_div_zero_numerator():
    assert safe_div(0, 10) == 0.0


# ── campaign_metrics ─────────────────────────────────────────────────────

def test_campaign_metrics_basic_calculation():
    m = campaign_metrics(_raw_report())
    assert m["recipients"] == 1000
    assert m["bounces"] == 15
    assert m["delivered"] == 985
    assert m["unique_opens"] == 300
    assert m["open_rate"] == 300 / 985
    # "unique_clicks" here means people who clicked (unique_subscriber_clicks),
    # NOT Mailchimp's per-link-aggregated unique_clicks field.
    assert m["unique_clicks"] == 50
    assert m["click_rate"] == 50 / 985
    assert m["click_to_open_rate"] == 50 / 300
    assert m["unsubscribe_rate"] == 5 / 985
    assert m["bounce_rate"] == 15 / 1000
    # The per-link-aggregated figure is preserved separately, clearly labeled.
    assert m["unique_link_clicks"] == 65


def test_campaign_metrics_click_rate_never_exceeds_delivered_ratio():
    # Regardless of how large the per-link-aggregated click count gets,
    # click_rate must stay bounded by unique_subscriber_clicks / delivered.
    m = campaign_metrics(_raw_report(
        emails_sent=1000,
        bounces={"hard_bounces": 0, "soft_bounces": 0},
        clicks={"clicks_total": 900, "unique_clicks": 700, "unique_subscriber_clicks": 120},
    ))
    assert m["click_rate"] == 120 / 1000
    assert m["click_rate"] <= 1.0


def test_campaign_metrics_zero_emails_sent_no_crash():
    m = campaign_metrics(_raw_report(
        emails_sent=0,
        bounces={"hard_bounces": 0, "soft_bounces": 0},
        opens={"opens_total": 0, "unique_opens": 0},
        clicks={"clicks_total": 0, "unique_clicks": 0},
        unsubscribed=0,
    ))
    assert m["delivered"] == 0
    assert m["open_rate"] == 0.0
    assert m["click_rate"] == 0.0
    assert m["click_to_open_rate"] == 0.0
    assert m["unsubscribe_rate"] == 0.0
    assert m["bounce_rate"] == 0.0


def test_campaign_metrics_missing_optional_keys():
    # A malformed/partial report object shouldn't raise KeyError
    m = campaign_metrics({"id": "x", "emails_sent": 100})
    assert m["delivered"] == 100
    assert m["open_rate"] == 0.0
    assert m["unsubscribes"] == 0


def test_campaign_metrics_all_opens_bounced_zero_delivered():
    m = campaign_metrics(_raw_report(
        emails_sent=10,
        bounces={"hard_bounces": 10, "soft_bounces": 0},
    ))
    assert m["delivered"] == 0
    assert m["open_rate"] == 0.0  # would be div-by-zero without safe_div


# ── aggregate_metrics ────────────────────────────────────────────────────

def test_aggregate_metrics_empty_list():
    agg = aggregate_metrics([])
    assert agg["campaigns_count"] == 0
    assert agg["open_rate"] == 0.0
    assert agg["recipients"] == 0


def test_aggregate_metrics_sums_not_averages_rates():
    # Two campaigns of very different size — aggregate rate must be computed
    # from summed numerator/denominator, not averaged per-campaign rate.
    c1 = campaign_metrics(_raw_report(emails_sent=100, opens={"opens_total": 0, "unique_opens": 50},
                                       clicks={"clicks_total": 0, "unique_clicks": 0},
                                       bounces={"hard_bounces": 0, "soft_bounces": 0}, unsubscribed=0))
    c2 = campaign_metrics(_raw_report(emails_sent=10000, opens={"opens_total": 0, "unique_opens": 1000},
                                       clicks={"clicks_total": 0, "unique_clicks": 0},
                                       bounces={"hard_bounces": 0, "soft_bounces": 0}, unsubscribed=0))
    agg = aggregate_metrics([c1, c2])
    # naive average of rates would be (0.5 + 0.1) / 2 = 0.30 -- wrong
    # correct: (50 + 1000) / (100 + 10000) = 0.10396...
    assert agg["open_rate"] == safe_div(1050, 10100)
    assert agg["open_rate"] != (0.5 + 0.1) / 2


# ── rate_delta / volume_delta ────────────────────────────────────────────

def test_rate_delta_percentage_points():
    d = rate_delta(0.314, 0.278)
    assert d["change_pct_points"] == 3.6
    assert d["direction"] == "up"


def test_rate_delta_none_previous():
    assert rate_delta(0.3, None) is None


def test_rate_delta_down_direction():
    d = rate_delta(0.10, 0.25)
    assert d["direction"] == "down"
    assert d["change_pct_points"] < 0


def test_volume_delta_percentage_change_not_points():
    d = volume_delta(150, 100)
    assert d["change_pct"] == 50.0
    assert d["direction"] == "up"


def test_volume_delta_zero_previous_no_crash():
    d = volume_delta(100, 0)
    assert d["change_pct"] == 0.0  # safe_div guards this, doesn't raise ZeroDivisionError


# ── compute_trends ───────────────────────────────────────────────────────

def test_compute_trends_no_previous_period():
    current = aggregate_metrics([campaign_metrics(_raw_report())])
    trends = compute_trends(current, None)
    assert trends["has_previous_period"] is False
    assert trends["open_rate"] is None


def test_compute_trends_with_previous_period():
    current = aggregate_metrics([campaign_metrics(_raw_report(opens={"opens_total": 0, "unique_opens": 400}))])
    previous = aggregate_metrics([campaign_metrics(_raw_report(opens={"opens_total": 0, "unique_opens": 300}))])
    trends = compute_trends(current, previous)
    assert trends["has_previous_period"] is True
    assert trends["open_rate"]["direction"] == "up"
    assert trends["emails_sent"]["direction"] == "flat"


# ── Regression test: the reported 346-delivered / 401-click anomaly ─────
# Mailchimp's clicks.unique_clicks is unique-per-LINK, summed across every
# link in the campaign — a small multi-link campaign can easily report more
# "unique clicks" than it had recipients. Using unique_subscriber_clicks
# (the true people-count) as the click-rate numerator makes this impossible.

def test_regression_346_delivered_401_unique_link_clicks_no_invalid_rate():
    m = campaign_metrics(_raw_report(
        emails_sent=346,
        bounces={"hard_bounces": 0, "soft_bounces": 0},
        opens={"opens_total": 600, "unique_opens": 300},
        # 401 is the per-link aggregate (unique_clicks) — the field that
        # produced the invalid 115.9% click rate before the fix. The true
        # people-count (unique_subscriber_clicks) is necessarily <= 346.
        clicks={"clicks_total": 900, "unique_clicks": 401, "unique_subscriber_clicks": 210},
    ))
    assert m["delivered"] == 346
    assert m["unique_link_clicks"] == 401  # preserved, just not used for the rate
    assert m["click_rate"] == 210 / 346
    assert m["click_rate"] < 1.0  # never a >100% click rate
    assert not detect_anomalies(m)  # a correctly-bounded rate is not an anomaly


# ── detect_anomalies ─────────────────────────────────────────────────────

def test_detect_anomalies_none_for_healthy_campaign():
    m = campaign_metrics(_raw_report())
    assert detect_anomalies(m) == []


def test_detect_anomalies_flags_impossible_click_rate():
    # Construct a metrics dict directly (bypassing campaign_metrics) to
    # simulate a data problem detect_anomalies must still catch as a safety
    # net, e.g. if some future/legacy caller ever mis-populates click_rate.
    m = campaign_metrics(_raw_report())
    m["click_rate"] = 1.159
    m["unique_clicks"] = 401
    m["delivered"] = 346
    issues = detect_anomalies(m)
    assert any("click_rate" in i for i in issues)
    assert any("exceeds delivered" in i for i in issues)


def test_detect_anomalies_flags_opens_exceeding_delivered():
    m = campaign_metrics(_raw_report())
    m["unique_opens"] = 2000
    m["delivered"] = 985
    issues = detect_anomalies(m)
    assert any("unique opens" in i and "exceeds delivered" in i for i in issues)


def test_detect_anomalies_does_not_flag_clicks_exceeding_opens_as_impossible():
    # Clicks > opens is plausible (untracked opens from image-blocking clients)
    # so it should be flagged as worth-reviewing, not as an impossible value.
    m = campaign_metrics(_raw_report())
    m["unique_clicks"] = 320
    m["unique_opens"] = 300
    m["delivered"] = 985
    issues = detect_anomalies(m)
    assert any("plausible" in i for i in issues)
