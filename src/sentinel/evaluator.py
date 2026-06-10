"""
sentinel/evaluator.py — Threshold breach evaluator

Evaluates rolling metric windows against config-defined thresholds.
Returns structured BreachEvent objects consumed by the alerting layer.
Tracks recently-raised alerts to prevent duplicate firing.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Dict, List, Optional

from .metrics import MetricBatch, TenantMetrics, TokenUsageMetric
from .utils import get_logger, load_config, utcnow

log = get_logger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# Breach event
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class BreachEvent:
    """Represents a single threshold breach that should trigger an alert."""
    tenant_id: str
    service_id: str
    metric_name: str          # e.g. "latency_p95", "error_rate", "tokens_per_request"
    metric_value: float
    threshold: float
    severity: str             # "warning" | "critical"
    description: str
    recommended_action: str
    timestamp: datetime = field(default_factory=utcnow)

    @property
    def dedup_key(self) -> str:
        return f"{self.tenant_id}:{self.service_id}:{self.metric_name}"

    def to_dict(self) -> dict:
        return {
            "tenant_id": self.tenant_id,
            "service_id": self.service_id,
            "metric_name": self.metric_name,
            "metric_value": round(self.metric_value, 2),
            "threshold": self.threshold,
            "severity": self.severity,
            "description": self.description,
            "recommended_action": self.recommended_action,
            "timestamp": self.timestamp.isoformat(),
        }


# ─────────────────────────────────────────────────────────────────────────────
# Evaluator
# ─────────────────────────────────────────────────────────────────────────────

class ThresholdEvaluator:
    """
    Evaluates a TenantMetrics snapshot against configured thresholds.

    Deduplication: won't fire the same (tenant, service, metric) breach
    more than once within `dedup_window_minutes` minutes.
    """

    def __init__(self):
        self._cfg = load_config()
        self._thresholds = self._cfg["thresholds"]
        dedup_min = self._cfg["alerting"]["github"].get("dedup_window_minutes", 30)
        self._dedup_window = timedelta(minutes=dedup_min)
        # dedup_key → last breach datetime
        self._last_breach: Dict[str, datetime] = {}

    # ── Public ────────────────────────────────────────────────────────────────

    def evaluate(
        self, tenant_metrics: Dict[str, TenantMetrics], latest_batch: MetricBatch
    ) -> List[BreachEvent]:
        """
        Evaluate all tenant metrics + latest batch for threshold breaches.
        Returns deduplicated list of BreachEvent objects.
        """
        breaches: List[BreachEvent] = []

        for tenant_id, tm in tenant_metrics.items():
            # Latency percentiles per service
            for service_id, percs in tm.get_all_percentiles().items():
                breaches.extend(
                    self._check_latency(tenant_id, service_id, percs)
                )
            # Error rates per service
            for service_id in tm._error_rate:
                err_rate = tm.get_error_rate(service_id)
                breach = self._check_error_rate(tenant_id, service_id, err_rate)
                if breach:
                    breaches.append(breach)

        # Per-request token checks from latest batch
        for m in latest_batch.token_metrics:
            breach = self._check_token_usage(m)
            if breach:
                breaches.append(breach)

        # Deduplicate
        return self._deduplicate(breaches)

    # ── Latency checks ────────────────────────────────────────────────────────

    def _check_latency(
        self, tenant_id: str, service_id: str, percs: dict
    ) -> List[BreachEvent]:
        events = []
        p95 = percs.get("p95", 0)
        p99 = percs.get("p99", 0)
        thr_p95 = self._thresholds["latency_p95_ms"]
        thr_p99 = self._thresholds["latency_p99_ms"]

        if p99 > thr_p99:
            events.append(BreachEvent(
                tenant_id=tenant_id,
                service_id=service_id,
                metric_name="latency_p99",
                metric_value=p99,
                threshold=thr_p99,
                severity="critical",
                description=(
                    f"P99 latency {p99:.0f}ms exceeds critical threshold {thr_p99}ms "
                    f"for tenant **{tenant_id}** on service **{service_id}**."
                ),
                recommended_action=(
                    "1. Check model inference cluster health\n"
                    "2. Review recent deployments for regressions\n"
                    "3. Consider scaling out inference workers\n"
                    "4. Enable request queuing with bounded timeout"
                ),
            ))
        elif p95 > thr_p95:
            events.append(BreachEvent(
                tenant_id=tenant_id,
                service_id=service_id,
                metric_name="latency_p95",
                metric_value=p95,
                threshold=thr_p95,
                severity="warning",
                description=(
                    f"P95 latency {p95:.0f}ms exceeds warning threshold {thr_p95}ms "
                    f"for tenant **{tenant_id}** on service **{service_id}**."
                ),
                recommended_action=(
                    "1. Review slow request traces in logs\n"
                    "2. Check for noisy-neighbour tenants\n"
                    "3. Consider enabling caching for repeated prompts"
                ),
            ))
        return events

    # ── Error rate check ──────────────────────────────────────────────────────

    def _check_error_rate(
        self, tenant_id: str, service_id: str, error_rate: float
    ) -> Optional[BreachEvent]:
        threshold = self._thresholds["error_rate_pct"]
        if error_rate < threshold:
            return None
        severity = "critical" if error_rate > threshold * 2 else "warning"
        return BreachEvent(
            tenant_id=tenant_id,
            service_id=service_id,
            metric_name="error_rate",
            metric_value=error_rate,
            threshold=threshold,
            severity=severity,
            description=(
                f"Error rate {error_rate:.1f}% exceeds threshold {threshold}% "
                f"for tenant **{tenant_id}** on service **{service_id}** "
                f"(60-second rolling window)."
            ),
            recommended_action=(
                "1. Inspect recent error logs for dominant error category\n"
                "2. Check upstream model API status page\n"
                "3. Trigger circuit breaker if error rate > 20%\n"
                "4. Notify tenant SLA team if sustained > 5 minutes"
            ),
        )

    # ── Token check ───────────────────────────────────────────────────────────

    def _check_token_usage(self, m: TokenUsageMetric) -> Optional[BreachEvent]:
        threshold = self._thresholds["tokens_per_request"]
        if m.tokens_total <= threshold:
            return None
        return BreachEvent(
            tenant_id=m.tenant_id,
            service_id=m.service_id,
            metric_name="tokens_per_request",
            metric_value=float(m.tokens_total),
            threshold=float(threshold),
            severity="warning",
            description=(
                f"Single request consumed {m.tokens_total:,} tokens "
                f"(threshold {threshold:,}) for tenant **{m.tenant_id}**. "
                f"Request ID: `{m.request_id}`"
            ),
            recommended_action=(
                "1. Review prompt engineering to reduce context bloat\n"
                "2. Enable conversation summarisation for long threads\n"
                "3. Apply per-tenant hard token cap in the gateway"
            ),
        )

    # ── Deduplication ─────────────────────────────────────────────────────────

    def _deduplicate(self, breaches: List[BreachEvent]) -> List[BreachEvent]:
        now = utcnow()
        result = []
        for b in breaches:
            key = b.dedup_key
            last = self._last_breach.get(key)
            if last is None or (now - last) > self._dedup_window:
                self._last_breach[key] = now
                result.append(b)
                log.warning(
                    "BREACH [%s] %s/%s = %.2f (threshold %.2f)",
                    b.severity.upper(), b.tenant_id, b.metric_name,
                    b.metric_value, b.threshold,
                )
        return result
