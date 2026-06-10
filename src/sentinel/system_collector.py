"""
sentinel/system_collector.py — Live System Metrics Collector

Replaces TrafficSimulator with REAL data from the host Linux system.

Mapping:
  Tenants  → top process groups by name (chrome, python3, gnome-shell, etc.)
  Services → "cpu-monitor"  (CPU-based latency) and "mem-monitor" (RAM-based tokens)

  CPU %  → latency_ms   (cpu% × 40 + 200 base  →  0% = 200ms, 50% = 2200ms, 100% = 4200ms)
  RAM MB → tokens_total (RAM in MB × 2          →  512 MB = 1024 tokens, realistic for LLM)
  CPU > threshold or RAM > 90% → ErrorMetric

Thresholds map (config):
  P95 latency > 2000ms → warning  →  CPU sustained > ~45%
  P99 latency > 4000ms → critical →  CPU sustained > ~95%
  Error rate  > 5%     → warning  →  any process spike
"""

from __future__ import annotations

import time
import uuid
from collections import defaultdict
from typing import Dict, List, Optional

import psutil

from .metrics import ErrorMetric, LatencyMetric, MetricBatch, TokenUsageMetric
from .utils import get_logger, utcnow

log = get_logger(__name__)

# Process names to group under friendly tenant IDs
_PROCESS_GROUPS: Dict[str, List[str]] = {
    "chrome-browser":   ["chrome", "chromium", "chromium-browser", "google-chrome"],
    "python-services":  ["python3", "python", "python3.10", "python3.11", "python3.12"],
    "system-ui":        ["gnome-shell", "Xorg", "xfwm4", "kwin_x11", "plasmashell", "mutter"],
    "dev-tools":        ["code", "code-oss", "node", "npm", "electron", "antigravity", "pyrefly"],
    "system-core":      ["systemd", "dbus-daemon", "NetworkManager", "snapd", "dockerd"],
}

# Services exposed to the dashboard
_CPU_SERVICE  = "cpu-monitor"
_MEM_SERVICE  = "mem-monitor"

# CPU% → latency_ms conversion
_CPU_BASE_MS   = 200.0   # minimum latency even at 0% CPU
_CPU_SCALE     = 40.0    # each 1% CPU adds 40ms  (50% → 2200ms = warning territory)

# Error thresholds (based on real system state)
_CPU_ERROR_PCT = 75.0    # CPU > 75% for a process group → generate error metric
_MEM_ERROR_PCT = 85.0    # RAM overall > 85% → generate error metric


class SystemCollector:
    """
    Collects live system telemetry using psutil and converts it to MetricBatch.
    Drop-in replacement for TrafficSimulator — same public API: generate_batch().
    """

    def __init__(self) -> None:
        self._tick_count = 0
        # Prime psutil CPU counters — first call always returns 0.0
        psutil.cpu_percent(interval=None)
        for p in psutil.process_iter(["pid", "name", "cpu_percent"]):
            try:
                p.cpu_percent(interval=None)
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass
        time.sleep(0.5)   # let counters settle
        log.info("SystemCollector ready — collecting live host metrics via psutil")

    # ── Public API (same as TrafficSimulator) ─────────────────────────────────

    def generate_batch(self) -> MetricBatch:
        """Read live system metrics and return a MetricBatch."""
        self._tick_count += 1
        batch = MetricBatch()
        now = utcnow()

        # ── System-wide snapshot ───────────────────────────────────────────────
        mem = psutil.virtual_memory()
        swap = psutil.swap_memory()
        system_cpu = psutil.cpu_percent(interval=None)   # non-blocking

        # ── Per-process group metrics ──────────────────────────────────────────
        group_cpu:    Dict[str, float] = defaultdict(float)
        group_rss_mb: Dict[str, float] = defaultdict(float)
        group_count:  Dict[str, int]   = defaultdict(int)

        for proc in psutil.process_iter(["name", "cpu_percent", "memory_info", "status"]):
            try:
                pname = proc.info["name"] or ""
                pcpu  = proc.info["cpu_percent"] or 0.0
                pmem  = proc.info["memory_info"]
                prss  = (pmem.rss / 1024 / 1024) if pmem else 0.0  # bytes → MB

                tenant_id = self._group(pname)
                if tenant_id:
                    group_cpu[tenant_id]    += pcpu
                    group_rss_mb[tenant_id] += prss
                    group_count[tenant_id]  += 1

            except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
                continue

        # Ensure all groups exist (even with zero readings)
        for tid in _PROCESS_GROUPS:
            group_cpu.setdefault(tid, 0.0)
            group_rss_mb.setdefault(tid, 0.0)
            group_count.setdefault(tid, 0)

        # ── Build MetricBatch from group readings ──────────────────────────────
        for tenant_id in _PROCESS_GROUPS:
            cpu_pct  = min(group_cpu[tenant_id], 100.0)
            rss_mb   = group_rss_mb[tenant_id]
            req_id   = str(uuid.uuid4())

            # — CPU → Latency metric ———————————————————————————————————————————
            latency_ms = _CPU_BASE_MS + cpu_pct * _CPU_SCALE

            # Determine status code: high CPU = degraded responses
            if cpu_pct > _CPU_ERROR_PCT:
                status_code = 503
            elif cpu_pct > 50:
                status_code = 429
            else:
                status_code = 200

            batch.latency_metrics.append(
                LatencyMetric(
                    tenant_id=tenant_id,
                    service_id=_CPU_SERVICE,
                    request_id=req_id,
                    latency_ms=latency_ms,
                    status_code=status_code,
                    timestamp=now,
                )
            )

            # — RAM → Token usage metric ———————————————————————————————————————
            if rss_mb > 0:
                tokens_total  = max(10, int(rss_mb * 2))    # 512 MB → 1024 tokens
                tokens_input  = int(tokens_total * 0.67)
                tokens_output = tokens_total - tokens_input

                batch.token_metrics.append(
                    TokenUsageMetric(
                        tenant_id=tenant_id,
                        service_id=_MEM_SERVICE,
                        request_id=req_id,
                        tokens_input=tokens_input,
                        tokens_output=tokens_output,
                        tokens_total=tokens_total,
                        model="system-monitor",
                        timestamp=now,
                    )
                )

            # — Error metric: CPU spike or high system memory ——————————————————
            if cpu_pct > _CPU_ERROR_PCT:
                batch.error_metrics.append(
                    ErrorMetric(
                        tenant_id=tenant_id,
                        service_id=_CPU_SERVICE,
                        request_id=req_id,
                        error_category="cpu_overload",
                        error_message=(
                            f"Process group '{tenant_id}' CPU at {cpu_pct:.1f}% "
                            f"— inference degraded (threshold: {_CPU_ERROR_PCT}%)"
                        ),
                        severity="critical" if cpu_pct > 90 else "warning",
                        timestamp=now,
                    )
                )

        # ── System-wide memory error ───────────────────────────────────────────
        if mem.percent > _MEM_ERROR_PCT:
            batch.error_metrics.append(
                ErrorMetric(
                    tenant_id="system-core",
                    service_id=_MEM_SERVICE,
                    request_id=str(uuid.uuid4()),
                    error_category="memory_pressure",
                    error_message=(
                        f"Host RAM at {mem.percent:.1f}% "
                        f"({mem.used//1024//1024}/{mem.total//1024//1024} MB) "
                        f"— risk of OOM-kill on inference workers"
                    ),
                    severity="critical" if mem.percent > 95 else "warning",
                    timestamp=now,
                )
            )

        log.debug(
            "Tick %d [LIVE] — CPU: %.1f%%  RAM: %.1f%%  "
            "%d latencies  %d tokens  %d errors",
            self._tick_count, system_cpu, mem.percent,
            len(batch.latency_metrics),
            len(batch.token_metrics),
            len(batch.error_metrics),
        )
        return batch

    # ── Helpers ───────────────────────────────────────────────────────────────

    @staticmethod
    def _group(process_name: str) -> Optional[str]:
        """Map a raw process name to a tenant group ID."""
        name_lower = process_name.lower()
        for tenant_id, names in _PROCESS_GROUPS.items():
            if any(n in name_lower for n in names):
                return tenant_id
        return None

    @property
    def tenant_ids(self) -> List[str]:
        """Return list of all tenant group IDs."""
        return list(_PROCESS_GROUPS.keys())
