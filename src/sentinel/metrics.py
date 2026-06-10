"""
sentinel/metrics.py — Metric data models and rolling-window percentile calculator
"""

from __future__ import annotations

import math
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Deque, List, Optional
import numpy as np

from .utils import utcnow


# ─────────────────────────────────────────────────────────────────────────────
# Data models
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class TokenUsageMetric:
    tenant_id: str
    service_id: str
    request_id: str
    tokens_input: int
    tokens_output: int
    tokens_total: int
    timestamp: datetime = field(default_factory=utcnow)
    model: str = ""

    def to_dict(self) -> dict:
        return {
            "tenant_id": self.tenant_id,
            "service_id": self.service_id,
            "request_id": self.request_id,
            "tokens_input": self.tokens_input,
            "tokens_output": self.tokens_output,
            "tokens_total": self.tokens_total,
            "model": self.model,
            "timestamp": self.timestamp.isoformat(),
        }


@dataclass
class LatencyMetric:
    tenant_id: str
    service_id: str
    request_id: str
    latency_ms: float
    timestamp: datetime = field(default_factory=utcnow)
    status_code: int = 200

    def to_dict(self) -> dict:
        return {
            "tenant_id": self.tenant_id,
            "service_id": self.service_id,
            "request_id": self.request_id,
            "latency_ms": self.latency_ms,
            "status_code": self.status_code,
            "timestamp": self.timestamp.isoformat(),
        }


@dataclass
class ErrorMetric:
    tenant_id: str
    service_id: str
    request_id: str
    error_category: str
    error_message: str
    timestamp: datetime = field(default_factory=utcnow)
    severity: str = "warning"   # warning | critical

    def to_dict(self) -> dict:
        return {
            "tenant_id": self.tenant_id,
            "service_id": self.service_id,
            "request_id": self.request_id,
            "error_category": self.error_category,
            "error_message": self.error_message,
            "severity": self.severity,
            "timestamp": self.timestamp.isoformat(),
        }


@dataclass
class MetricBatch:
    """A bundle of metrics produced by one simulator tick."""
    token_metrics: List[TokenUsageMetric] = field(default_factory=list)
    latency_metrics: List[LatencyMetric] = field(default_factory=list)
    error_metrics: List[ErrorMetric] = field(default_factory=list)

    def __len__(self) -> int:
        return (
            len(self.token_metrics)
            + len(self.latency_metrics)
            + len(self.error_metrics)
        )


# ─────────────────────────────────────────────────────────────────────────────
# Rolling window percentile calculator
# ─────────────────────────────────────────────────────────────────────────────

class LatencyWindow:
    """
    Thread-safe rolling window of latency samples for one (tenant, service) pair.
    Evicts samples older than `window_seconds`.
    """

    def __init__(self, window_seconds: int = 60):
        self._window = window_seconds
        # Deque of (timestamp_epoch_float, latency_ms)
        self._samples: Deque[tuple[float, float]] = deque()

    def add(self, latency_ms: float, ts: Optional[float] = None) -> None:
        now = ts or time.time()
        self._samples.append((now, latency_ms))
        self._evict(now)

    def _evict(self, now: float) -> None:
        cutoff = now - self._window
        while self._samples and self._samples[0][0] < cutoff:
            self._samples.popleft()

    def percentiles(self) -> dict[str, float]:
        """Return p50, p95, p99 over the current window. Returns zeros if empty."""
        self._evict(time.time())
        if not self._samples:
            return {"p50": 0.0, "p95": 0.0, "p99": 0.0}
        values = np.array([s[1] for s in self._samples])
        return {
            "p50": float(np.percentile(values, 50)),
            "p95": float(np.percentile(values, 95)),
            "p99": float(np.percentile(values, 99)),
        }

    @property
    def count(self) -> int:
        self._evict(time.time())
        return len(self._samples)


class ErrorRateWindow:
    """
    Tracks request count and error count in a rolling window to compute error rate %.
    """

    def __init__(self, window_seconds: int = 60):
        self._window = window_seconds
        self._requests: Deque[float] = deque()   # timestamps of all requests
        self._errors: Deque[float] = deque()     # timestamps of errored requests

    def record_request(self, ts: Optional[float] = None) -> None:
        now = ts or time.time()
        self._requests.append(now)
        self._evict_deque(self._requests, now)

    def record_error(self, ts: Optional[float] = None) -> None:
        now = ts or time.time()
        self._errors.append(now)
        self._evict_deque(self._errors, now)

    def _evict_deque(self, dq: Deque[float], now: float) -> None:
        cutoff = now - self._window
        while dq and dq[0] < cutoff:
            dq.popleft()

    def error_rate_pct(self) -> float:
        now = time.time()
        self._evict_deque(self._requests, now)
        self._evict_deque(self._errors, now)
        if not self._requests:
            return 0.0
        return (len(self._errors) / len(self._requests)) * 100.0

    @property
    def request_count(self) -> int:
        self._evict_deque(self._requests, time.time())
        return len(self._requests)

    @property
    def error_count(self) -> int:
        self._evict_deque(self._errors, time.time())
        return len(self._errors)


# ─────────────────────────────────────────────────────────────────────────────
# Per-tenant aggregator
# ─────────────────────────────────────────────────────────────────────────────

class TenantMetrics:
    """Live rolling metrics for a single tenant across all services."""

    def __init__(self, tenant_id: str, window_seconds: int = 60):
        self.tenant_id = tenant_id
        self._latency: dict[str, LatencyWindow] = {}
        self._error_rate: dict[str, ErrorRateWindow] = {}
        self._window = window_seconds
        self.total_tokens: int = 0
        self.request_count: int = 0

    def _lat_win(self, service_id: str) -> LatencyWindow:
        if service_id not in self._latency:
            self._latency[service_id] = LatencyWindow(self._window)
        return self._latency[service_id]

    def _err_win(self, service_id: str) -> ErrorRateWindow:
        if service_id not in self._error_rate:
            self._error_rate[service_id] = ErrorRateWindow(self._window)
        return self._error_rate[service_id]

    def record_token(self, metric: TokenUsageMetric) -> None:
        self.total_tokens += metric.tokens_total
        self.request_count += 1

    def record_latency(self, metric: LatencyMetric) -> None:
        self._lat_win(metric.service_id).add(metric.latency_ms)
        self._err_win(metric.service_id).record_request()

    def record_error(self, metric: ErrorMetric) -> None:
        self._err_win(metric.service_id).record_error()

    def get_latency_percentiles(self, service_id: str) -> dict[str, float]:
        return self._lat_win(service_id).percentiles()

    def get_error_rate(self, service_id: str) -> float:
        return self._err_win(service_id).error_rate_pct()

    def get_all_percentiles(self) -> dict[str, dict[str, float]]:
        return {svc: win.percentiles() for svc, win in self._latency.items()}

    def snapshot(self) -> dict:
        return {
            "tenant_id": self.tenant_id,
            "total_tokens": self.total_tokens,
            "request_count": self.request_count,
            "latency": self.get_all_percentiles(),
            "error_rates": {
                svc: win.error_rate_pct() for svc, win in self._error_rate.items()
            },
        }
