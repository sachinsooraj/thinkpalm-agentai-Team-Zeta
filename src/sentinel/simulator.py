"""
sentinel/simulator.py — Realistic multi-tenant AI traffic generator

Generates metric batches simulating:
  - LLM Customer Support Bot  (high token variance, latency spikes)
  - IoT Anomaly Detection API (low latency, occasional burst errors)

Five client tenants across enterprise / standard / starter tiers.
Supports configurable anomaly injection for testing threshold breach flows.
"""

from __future__ import annotations

import random
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Dict, List, Optional

import numpy as np

from .metrics import ErrorMetric, LatencyMetric, MetricBatch, TokenUsageMetric
from .utils import get_logger, load_config, utcnow

log = get_logger(__name__)

_ERROR_MESSAGES = {
    "timeout": [
        "Request timed out after 30s waiting for model response",
        "Gateway timeout: upstream model endpoint unreachable",
        "Connection timeout: LLM inference cluster unresponsive",
    ],
    "rate_limit": [
        "Rate limit exceeded: 60 req/min cap for this tenant tier",
        "Quota exhausted: daily token limit reached",
        "Too many requests: throttled by upstream provider",
    ],
    "context_overflow": [
        "Input context length 8192 tokens exceeds model limit",
        "Conversation history truncation failed; context too large",
        "Prompt + completion exceeds maximum context window",
    ],
    "model_unavailable": [
        "Model endpoint returned 503: service temporarily unavailable",
        "Inference worker crashed; request could not be routed",
        "Health check failed: all model replicas unhealthy",
    ],
    "validation_error": [
        "Invalid JSON payload: missing required field 'messages'",
        "Malformed request: temperature must be in [0, 2]",
        "Schema validation failed: unknown parameter 'stream_mode'",
    ],
    "upstream_failure": [
        "Upstream API returned 500: internal provider error",
        "Third-party embeddings service unavailable",
        "Vector store connection refused",
    ],
}


@dataclass
class AnomalyState:
    """Tracks an active burst anomaly for a (tenant, service) pair."""
    tenant_id: str
    service_id: str
    ticks_remaining: int
    anomaly_type: str  # latency_spike | error_burst | token_surge


class TrafficSimulator:
    """
    Generates realistic metric batches on each tick.

    Design decisions:
      - Uses numpy for vectorised latency sampling (log-normal distribution
        gives realistic heavy tails matching real LLM response times)
      - Token counts follow a Gaussian distribution truncated at 0
      - Anomaly injection is Poisson-process-like (probability per tick)
      - Each request is assigned a UUID so the store can correlate metrics
    """

    def __init__(self):
        self._cfg = load_config()
        self._tenants = self._cfg["tenants"]
        self._services = {s["id"]: s for s in self._cfg["services"]}
        self._error_cats = self._cfg["error_categories"]
        self._sim_cfg = self._cfg["simulator"]
        self._reqs_per_tick = self._sim_cfg["requests_per_tick"]
        self._anomaly_prob = self._sim_cfg["anomaly_probability"]
        self._burst_ticks = self._sim_cfg["burst_duration_ticks"]
        self._active_anomalies: Dict[str, AnomalyState] = {}
        self._tick_count = 0
        log.info(
            "Simulator ready — %d tenants, %d services, %d req/tick",
            len(self._tenants),
            len(self._services),
            self._reqs_per_tick,
        )

    # ── Public API ────────────────────────────────────────────────────────────

    def generate_batch(self) -> MetricBatch:
        """Generate one tick worth of metrics across all tenants/services."""
        self._tick_count += 1
        batch = MetricBatch()
        self._maybe_inject_anomaly()

        for tenant in self._tenants:
            for service_id, service in self._services.items():
                n = max(1, self._reqs_per_tick // (len(self._tenants) * len(self._services)))
                for _ in range(n):
                    self._generate_request(batch, tenant, service, service_id)

        log.debug(
            "Tick %d — %d tokens, %d latencies, %d errors",
            self._tick_count,
            len(batch.token_metrics),
            len(batch.latency_metrics),
            len(batch.error_metrics),
        )
        return batch

    # ── Anomaly injection ─────────────────────────────────────────────────────

    def _maybe_inject_anomaly(self) -> None:
        if random.random() < self._anomaly_prob:
            tenant = random.choice(self._tenants)
            service_id = random.choice(list(self._services.keys()))
            key = f"{tenant['id']}:{service_id}"
            if key not in self._active_anomalies:
                anomaly_type = random.choice(["latency_spike", "error_burst", "token_surge"])
                self._active_anomalies[key] = AnomalyState(
                    tenant_id=tenant["id"],
                    service_id=service_id,
                    ticks_remaining=self._burst_ticks,
                    anomaly_type=anomaly_type,
                )
                log.info(
                    "🔴 ANOMALY INJECTED — tenant=%s service=%s type=%s",
                    tenant["id"], service_id, anomaly_type,
                )

        # Decrement ticks
        expired = [k for k, v in self._active_anomalies.items() if v.ticks_remaining <= 0]
        for k in expired:
            del self._active_anomalies[k]
        for k in self._active_anomalies:
            self._active_anomalies[k].ticks_remaining -= 1

    def _get_anomaly(self, tenant_id: str, service_id: str) -> Optional[AnomalyState]:
        return self._active_anomalies.get(f"{tenant_id}:{service_id}")

    # ── Request generation ────────────────────────────────────────────────────

    def _generate_request(
        self,
        batch: MetricBatch,
        tenant: dict,
        service: dict,
        service_id: str,
    ) -> None:
        request_id = str(uuid.uuid4())
        now = utcnow()
        anomaly = self._get_anomaly(tenant["id"], service_id)

        # ── Latency ───────────────────────────────────────────────────────────
        base_mean = service["avg_latency_ms"]
        base_std = service["latency_std_ms"]

        if anomaly and anomaly.anomaly_type == "latency_spike":
            # Log-normal spike: multiply mean by 5–15x
            multiplier = random.uniform(5, 15)
            latency_ms = float(
                np.random.lognormal(
                    mean=np.log(base_mean * multiplier),
                    sigma=0.4,
                )
            )
        else:
            latency_ms = float(
                max(10, np.random.lognormal(
                    mean=np.log(max(1, base_mean)),
                    sigma=base_std / base_mean if base_mean > 0 else 0.5,
                ))
            )

        # ── Error / success decision ───────────────────────────────────────────
        base_error_rate = 0.03  # 3% baseline
        if anomaly and anomaly.anomaly_type == "error_burst":
            base_error_rate = 0.60  # 60% during burst

        is_error = random.random() < base_error_rate
        status_code = random.choice([429, 500, 503, 504]) if is_error else 200

        # ── Token usage ───────────────────────────────────────────────────────
        if not is_error:
            base_total_mean = service["avg_tokens_input"] + service["avg_tokens_output"]
            std = service["avg_tokens_std"]

            if anomaly and anomaly.anomaly_type == "token_surge":
                scale = random.uniform(3.0, 4.5)
            else:
                scale = 1.0

            tokens_total = max(
                10,
                int(np.random.normal(base_total_mean * scale, std)),
            )
            # Split input/output ~2:1
            tokens_input = int(tokens_total * 0.67)
            tokens_output = tokens_total - tokens_input

            batch.token_metrics.append(
                TokenUsageMetric(
                    tenant_id=tenant["id"],
                    service_id=service_id,
                    request_id=request_id,
                    tokens_input=tokens_input,
                    tokens_output=tokens_output,
                    tokens_total=tokens_total,
                    model=service["model"],
                    timestamp=now,
                )
            )

        # ── Latency metric (always recorded) ─────────────────────────────────
        batch.latency_metrics.append(
            LatencyMetric(
                tenant_id=tenant["id"],
                service_id=service_id,
                request_id=request_id,
                latency_ms=latency_ms,
                status_code=status_code,
                timestamp=now,
            )
        )

        # ── Error metric ──────────────────────────────────────────────────────
        if is_error:
            category = random.choice(self._error_cats)
            messages = _ERROR_MESSAGES.get(category, ["Unknown error"])
            severity = "critical" if status_code in (503, 504) else "warning"
            batch.error_metrics.append(
                ErrorMetric(
                    tenant_id=tenant["id"],
                    service_id=service_id,
                    request_id=request_id,
                    error_category=category,
                    error_message=random.choice(messages),
                    severity=severity,
                    timestamp=now,
                )
            )
