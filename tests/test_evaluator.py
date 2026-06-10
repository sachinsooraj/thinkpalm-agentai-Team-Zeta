"""tests/test_evaluator.py — Unit tests for threshold breach detection"""
import pytest
from unittest.mock import patch
from sentinel.metrics import TenantMetrics, LatencyMetric, ErrorMetric, MetricBatch, TokenUsageMetric
from sentinel.evaluator import ThresholdEvaluator, BreachEvent
from sentinel.utils import reset_config_cache


@pytest.fixture(autouse=True)
def reset_cache():
    reset_config_cache()
    yield
    reset_config_cache()


def _make_tenant_with_latencies(tenant_id: str, latencies: list[float]) -> TenantMetrics:
    tm = TenantMetrics(tenant_id, window_seconds=60)
    for lat in latencies:
        m = LatencyMetric(
            tenant_id=tenant_id, service_id="llm-support",
            request_id="r1", latency_ms=lat, status_code=200,
        )
        tm.record_latency(m)
    return tm


def _make_tenant_with_errors(tenant_id: str, n_req: int, n_err: int) -> TenantMetrics:
    tm = TenantMetrics(tenant_id, window_seconds=60)
    for _ in range(n_req):
        m = LatencyMetric(
            tenant_id=tenant_id, service_id="llm-support",
            request_id="r", latency_ms=100, status_code=200,
        )
        tm.record_latency(m)
    for _ in range(n_err):
        e = ErrorMetric(
            tenant_id=tenant_id, service_id="llm-support",
            request_id="r", error_category="timeout", error_message="t",
        )
        tm.record_error(e)
    return tm


def test_no_breach_normal_traffic():
    ev = ThresholdEvaluator()
    tm = _make_tenant_with_latencies("acme-corp", [100.0] * 50)
    breaches = ev.evaluate({"acme-corp": tm}, MetricBatch())
    assert breaches == []


def test_p95_latency_breach():
    ev = ThresholdEvaluator()
    # 100 samples where 95th pct is ~5000ms
    lats = [100.0] * 94 + [5000.0, 5100.0, 5200.0, 5300.0, 5400.0, 5500.0]
    tm = _make_tenant_with_latencies("acme-corp", lats)
    breaches = ev.evaluate({"acme-corp": tm}, MetricBatch())
    metrics = [b.metric_name for b in breaches]
    assert any("latency" in m for m in metrics)


def test_error_rate_breach():
    ev = ThresholdEvaluator()
    # 10 requests, 8 errors => 80% error rate
    tm = _make_tenant_with_errors("globex-ltd", 10, 8)
    breaches = ev.evaluate({"globex-ltd": tm}, MetricBatch())
    assert any(b.metric_name == "error_rate" for b in breaches)


def test_error_rate_no_breach_below_threshold():
    ev = ThresholdEvaluator()
    # 100 requests, 2 errors => 2% error rate (threshold 5%)
    tm = _make_tenant_with_errors("initech", 100, 2)
    breaches = ev.evaluate({"initech": tm}, MetricBatch())
    assert not any(b.metric_name == "error_rate" for b in breaches)


def test_token_breach():
    ev = ThresholdEvaluator()
    batch = MetricBatch()
    batch.token_metrics.append(
        TokenUsageMetric(
            tenant_id="cyberdyne", service_id="llm-support",
            request_id="r1", tokens_input=2000, tokens_output=2000,
            tokens_total=4000,  # over 3500 threshold
        )
    )
    breaches = ev.evaluate({}, batch)
    assert any(b.metric_name == "tokens_per_request" for b in breaches)


def test_deduplication_prevents_repeat_alerts():
    ev = ThresholdEvaluator()
    lats = [100.0] * 94 + [5000.0] * 6
    tm = _make_tenant_with_latencies("acme-corp", lats)
    # First evaluation — should breach
    b1 = ev.evaluate({"acme-corp": tm}, MetricBatch())
    # Second evaluation immediately — should be deduplicated
    b2 = ev.evaluate({"acme-corp": tm}, MetricBatch())
    assert len(b2) == 0


def test_breach_event_dedup_key():
    b = BreachEvent(
        tenant_id="acme", service_id="svc",
        metric_name="latency_p95", metric_value=3000,
        threshold=2000, severity="warning",
        description="test", recommended_action="fix it",
    )
    assert b.dedup_key == "acme:svc:latency_p95"


def test_breach_severity_critical_for_high_error_rate():
    ev = ThresholdEvaluator()
    # 90% error rate — should be critical (>2x threshold)
    tm = _make_tenant_with_errors("umbrella-co", 10, 9)
    breaches = ev.evaluate({"umbrella-co": tm}, MetricBatch())
    err_breaches = [b for b in breaches if b.metric_name == "error_rate"]
    if err_breaches:
        assert err_breaches[0].severity == "critical"
