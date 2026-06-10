"""
run_monitor.py — TPOps Sentinel CLI entrypoint

Orchestrates the full observability pipeline:
  1. Traffic Simulator → generates metric batches each tick
  2. MetricStore    → persists metrics to SQLite + in-memory buffer
  3. TenantMetrics  → maintains rolling windows for live percentile calc
  4. ThresholdEvaluator → detects breaches
  5. GitHubAlerter + SlackAlerter → fire structured alerts

Usage:
  python run_monitor.py                  # Normal mode (reads .env for credentials)
  python run_monitor.py --dry-run        # Print GitHub/Slack payloads, no real calls
  python run_monitor.py --duration 120   # Run for N seconds then exit
  python run_monitor.py --digest-now     # Generate digest report and exit
"""

from __future__ import annotations

import argparse
import signal
import sys
import time
import threading
from typing import Dict

from sentinel.evaluator import ThresholdEvaluator
from sentinel.github_alerter import GitHubAlerter
from sentinel.metrics import TenantMetrics
from sentinel.simulator import TrafficSimulator
from sentinel.system_collector import SystemCollector
from sentinel.slack_alerter import SlackAlerter
from sentinel.store import MetricStore
from sentinel.digest import DigestGenerator
from sentinel.utils import get_logger, load_config

log = get_logger("sentinel.monitor")

_BANNER = """
╔══════════════════════════════════════════════════════════╗
║      🛡️  TPOps Sentinel — AI Observability Monitor       ║
║         ThinkPalm Technologies  •  v1.0.0                ║
╚══════════════════════════════════════════════════════════╝
"""


# ─────────────────────────────────────────────────────────────────────────────
# Monitor loop
# ─────────────────────────────────────────────────────────────────────────────

class SentinelMonitor:
    def __init__(self, dry_run: bool = False, live_system: bool = False):
        cfg = load_config()
        if dry_run:
            cfg["monitor"]["dry_run"] = True

        self._tick_interval = cfg["monitor"]["tick_interval_seconds"]
        self._window_seconds = cfg["monitor"]["window_seconds"]
        self._live_system = live_system

        self._store = MetricStore()
        if live_system:
            self._simulator = SystemCollector()
            log.info("Mode: LIVE SYSTEM — collecting real host metrics via psutil")
        else:
            self._simulator = TrafficSimulator()
            log.info("Mode: SIMULATED — generating synthetic AI traffic")
        self._evaluator = ThresholdEvaluator()
        self._github = GitHubAlerter()
        self._slack = SlackAlerter()
        self._digest = DigestGenerator(self._store)

        self._tenant_metrics: Dict[str, TenantMetrics] = {
            t["id"]: TenantMetrics(t["id"], window_seconds=self._window_seconds)
            for t in cfg["tenants"]
        }

        self._running = False
        self._tick_count = 0
        self._total_alerts = 0

    def start(self, duration: int | None = None) -> None:
        """Start the monitor loop. Runs indefinitely unless duration is set."""
        self._running = True
        log.info("Monitor started — tick every %ds", self._tick_interval)

        start_time = time.time()
        try:
            while self._running:
                self._tick()
                self._tick_count += 1

                if duration and (time.time() - start_time) >= duration:
                    log.info("Duration %ds elapsed — shutting down", duration)
                    break

                time.sleep(self._tick_interval)
        except KeyboardInterrupt:
            log.info("Interrupted by user")
        finally:
            self._print_summary()

    def stop(self) -> None:
        self._running = False

    # ── Tick ──────────────────────────────────────────────────────────────────

    def _tick(self) -> None:
        # 1. Generate traffic (simulated or live system)
        batch = self._simulator.generate_batch()

        # 2. Persist to store
        self._store.ingest(batch)

        # 3. Auto-register any new tenant IDs (e.g. from SystemCollector)
        all_tenant_ids = (
            {m.tenant_id for m in batch.token_metrics}
            | {m.tenant_id for m in batch.latency_metrics}
            | {m.tenant_id for m in batch.error_metrics}
        )
        for tid in all_tenant_ids:
            if tid not in self._tenant_metrics:
                self._tenant_metrics[tid] = TenantMetrics(
                    tid, window_seconds=self._window_seconds
                )

        # 4. Update rolling windows
        for m in batch.token_metrics:
            self._tenant_metrics[m.tenant_id].record_token(m)
        for m in batch.latency_metrics:
            self._tenant_metrics[m.tenant_id].record_latency(m)
        for m in batch.error_metrics:
            self._tenant_metrics[m.tenant_id].record_error(m)

        # 5. Evaluate thresholds
        breaches = self._evaluator.evaluate(self._tenant_metrics, batch)

        # 6. Fire alerts for each breach
        for breach in breaches:
            github_url = self._github.raise_issue(breach)
            self._slack.send_alert(breach, github_issue_url=github_url)
            self._store.record_alert(
                alert_type="threshold_breach",
                tenant_id=breach.tenant_id,
                service_id=breach.service_id,
                severity=breach.severity,
                metric_name=breach.metric_name,
                metric_value=breach.metric_value,
                threshold=breach.threshold,
                github_issue_url=github_url or "",
                slack_sent=True,
            )
            self._total_alerts += 1

        # 7. Console status line
        self._print_tick_status(batch, len(breaches))

    # ── Output ────────────────────────────────────────────────────────────────

    def _print_tick_status(self, batch, breach_count: int) -> None:
        tokens = sum(m.tokens_total for m in batch.token_metrics)
        errors = len(batch.error_metrics)
        total = len(batch.latency_metrics)
        err_rate = (errors / max(total, 1)) * 100

        alert_indicator = f"  ⚠️  {breach_count} BREACH(ES)!" if breach_count else ""
        print(
            f"  Tick {self._tick_count:>4} │ "
            f"Reqs: {total:>4} │ "
            f"Tokens: {tokens:>7,} │ "
            f"Errors: {errors:>3} ({err_rate:>5.1f}%) │ "
            f"Alerts: {self._total_alerts:>3}"
            f"{alert_indicator}",
            flush=True,
        )

    def _print_summary(self) -> None:
        print("\n" + "─" * 60)
        print(f"  ✅ Monitor stopped after {self._tick_count} ticks")
        print(f"  📊 Total alerts raised: {self._total_alerts}")
        print("─" * 60 + "\n")


# ─────────────────────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────────────────────

def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="TPOps Sentinel — AI Observability Monitor"
    )
    p.add_argument(
        "--live-system",
        action="store_true",
        default=False,
        help="Collect REAL host metrics via psutil instead of the traffic simulator",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        default=False,
        help="Print GitHub/Slack payloads instead of making real API calls",
    )
    p.add_argument(
        "--duration",
        type=int,
        default=None,
        metavar="SECONDS",
        help="Run for N seconds then exit (useful for smoke tests)",
    )
    p.add_argument(
        "--digest-now",
        action="store_true",
        default=False,
        help="Generate today's digest report and exit",
    )
    p.add_argument(
        "--no-simulator",
        action="store_true",
        default=False,
        help="Disable traffic simulator (use real metrics only)",
    )
    return p.parse_args()


def main() -> None:
    args = parse_args()
    print(_BANNER)

    cfg = load_config()
    dry_run = args.dry_run or cfg["monitor"].get("dry_run", False)

    if dry_run:
        print("  ⚠️  DRY-RUN MODE — GitHub/Slack payloads will be printed, not sent\n")
    else:
        print("  🔴 LIVE MODE — Real GitHub Issues and Slack alerts will be raised\n")

    if args.digest_now:
        store = MetricStore()
        gen = DigestGenerator(store)
        written = gen.run()
        print(f"\n  📄 Digest reports written:")
        for p in written:
            print(f"     → {p}")
        return

    monitor = SentinelMonitor(dry_run=dry_run, live_system=args.live_system)

    if args.live_system:
        print("  🖥️  LIVE SYSTEM MODE — monitoring real host processes via psutil\n")
    else:
        print("  🤖 SIMULATOR MODE — generating synthetic AI traffic\n")

    # Graceful shutdown on SIGTERM
    signal.signal(signal.SIGTERM, lambda *_: monitor.stop())

    print(
        f"  {'─'*56}\n"
        f"  {'Tick':>6} │ {'Requests':>8} │ {'Tokens':>10} │ "
        f"{'Errors':>10} │ {'Total Alerts':>12}\n"
        f"  {'─'*56}"
    )
    monitor.start(duration=args.duration)


if __name__ == "__main__":
    main()
