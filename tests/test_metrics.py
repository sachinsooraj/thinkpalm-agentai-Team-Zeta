"""tests/test_metrics.py — Unit tests for metric models and rolling windows"""
import time
import pytest
from sentinel.metrics import (
    LatencyWindow, ErrorRateWindow, TenantMetrics,
    TokenUsageMetric, LatencyMetric, ErrorMetric,
)
from sentinel.utils import utcnow


def test_latency_window_empty():
    win = LatencyWindow(window_seconds=60)
    p = win.percentiles()
    assert p == {"p50": 0.0, "p95": 0.0, "p99": 0.0}


def test_latency_window_basic():
    win = LatencyWindow(window_seconds=60)
    for v in [100, 200, 300, 400, 500]:
        win.add(v)
    p = win.percentiles()
    assert p["p50"] == pytest.approx(300.0, rel=0.05)
    assert p["p95"] >= 450.0
    assert p["p99"] >= 480.0


def test_latency_window_eviction():
    win = LatencyWindow(window_seconds=1)
    now = time.time()
    # Add old samples (2 seconds ago)
    for v in [9999, 8888, 7777]:
        win.add(v, ts=now - 2.0)
    # Add fresh samples
    win.add(100.0, ts=now)
    win.add(200.0, ts=now)
    # Should only see fresh samples
    assert win.count == 2


def test_error_rate_zero():
    win = ErrorRateWindow(window_seconds=60)
    win.record_request()
    win.record_request()
    assert win.error_rate_pct() == 0.0


def test_error_rate_calculation():
    win = ErrorRateWindow(window_seconds=60)
    for _ in range(10):
        win.record_request()
    for _ in range(2):
        win.record_error()
    assert win.error_rate_pct() == pytest.approx(20.0, rel=0.01)


def test_token_metric_to_dict():
    m = TokenUsageMetric(
        tenant_id="acme-corp", service_id="llm-support",
        request_id="req-1", tokens_input=100, tokens_output=50,
        tokens_total=150, model="gpt-4o",
    )
    d = m.to_dict()
    assert d["tokens_total"] == 150
    assert d["tenant_id"] == "acme-corp"
    assert "timestamp" in d


def test_tenant_metrics_accumulates():
    tm = TenantMetrics("acme-corp", window_seconds=60)
    for i in range(5):
        m = TokenUsageMetric(
            tenant_id="acme-corp", service_id="llm-support",
            request_id=f"r{i}", tokens_input=100, tokens_output=50,
            tokens_total=150,
        )
        tm.record_token(m)
    assert tm.total_tokens == 750
    assert tm.request_count == 5


def test_latency_metric_to_dict():
    m = LatencyMetric(
        tenant_id="globex", service_id="iot-anomaly",
        request_id="r1", latency_ms=250.5, status_code=200,
    )
    d = m.to_dict()
    assert d["latency_ms"] == 250.5
    assert d["status_code"] == 200
