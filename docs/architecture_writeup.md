# TPOps Sentinel — Architecture Write-Up

**ThinkPalm Technologies | AI Observability Platform | v1.0.0**

---

## Overview

TPOps Sentinel is a production-grade, real-time observability platform designed to monitor AI-powered microservices across multiple client tenants. It continuously ingests telemetry (token usage, inference latency, and error events), evaluates live metrics against configurable SLA thresholds, and automatically raises structured alerts via GitHub Issues and Slack — all without human intervention.

---

## System Architecture

The platform is organized into four loosely-coupled layers, each with a single responsibility:

### 1 · Simulation Layer — `TrafficSimulator`
Generates synthetic, multi-tenant AI telemetry for two services (`llm-support` and `iot-anomaly`) across five client tenants. Each tick produces a `MetricBatch` containing token, latency, and error samples with realistic statistical distributions and periodic anomaly injection (latency spikes, error bursts). In production, this layer is replaced by real SDK instrumentation hooks.

### 2 · Core Engine — `MetricStore` · `TenantMetrics` · `ThresholdEvaluator`
The heart of the platform operates as a three-stage pipeline:

| Component | Responsibility |
|-----------|---------------|
| **MetricStore** | Dual-write: persists every metric to SQLite for long-term querying and also writes to an in-memory ring-buffer for sub-second dashboard reads. |
| **TenantMetrics** | Maintains per-tenant, per-service `LatencyWindow` and `ErrorRateWindow` deques. Each window auto-evicts samples older than the configured rolling window (default 60 s), providing always-current P50/P95/P99 percentiles and error-rate percentages via NumPy. |
| **ThresholdEvaluator** | Compares live percentiles against YAML-configured thresholds. Employs a deduplication map keyed on `(tenant, service, metric)` with a cooldown period to suppress alert storms. Emits `BreachEvent` objects consumed by the alerting layer. |

### 3 · Alert Channels — `GitHubAlerter` · `SlackAlerter`
On every `BreachEvent`, both alerters fire in sequence:

- **GitHubAlerter** — Creates a structured Issue via PyGithub with severity-coloured labels (`severity:critical`, `tenant:<id>`, `service:<id>`), a Markdown metric table, and recommended remediation steps. Returns the Issue URL for cross-linking.
- **SlackAlerter** — Posts a Block Kit message with colour-coded severity header, metric summary table, timestamp, and a button deep-linking to the GitHub Issue.

Both operate in **dry-run mode** (payloads printed to stdout, no real API calls) when `--dry-run` is passed or `SENTINEL_DRY_RUN=true` is set.

### 4 · Reporting & Dashboard — `DigestGenerator` · Streamlit
- **DigestGenerator** queries the SQLite store and renders a daily executive summary as both a Markdown file and a PDF (via reportlab). Scheduled at 08:00 UTC; can also be triggered on demand with `python run_monitor.py --digest-now`.
- **Streamlit Dashboard** (`dashboard.py`) provides a premium dark-mode, glassmorphic UI with auto-refresh every 5 seconds. Panels include live latency heat-maps (Plotly), per-tenant token usage rankings, error-category breakdowns, and a full alerts ledger.

---

## Key Design Decisions

| Decision | Rationale |
|----------|-----------|
| **SQLite as the storage layer** | Zero-dependency, file-based persistence. Suitable for single-node deployment; can be swapped for PostgreSQL by changing the connection string in `store.py`. |
| **In-memory ring-buffer alongside SQLite** | Decouples dashboard read latency from SQLite I/O; dashboard always reads from memory, never blocks the monitor loop. |
| **Rolling-window percentiles (not global averages)** | Avoids misleading long-tail suppression; reflects actual current service health rather than historical averages. |
| **Deduplication with cooldown** | Prevents alert fatigue on sustained breaches — a single incident raises one issue, not hundreds. |
| **YAML-driven configuration** | All thresholds, tenants, services, and scheduling parameters are in `config.yaml`; no code changes needed to tune the platform. |
| **Dry-run mode** | Enables safe local demonstration and CI testing without requiring real API credentials. |

---

## Data Flow Summary

```
TrafficSimulator ──ingest()──► MetricStore (SQLite + ring-buffer)
                                     │
                              TenantMetrics (rolling windows)
                                     │ evaluate()
                              ThresholdEvaluator ──► BreachEvent[]
                                     │
                    ┌────────────────┴────────────────┐
               GitHubAlerter                    SlackAlerter
                    └─────────── issue URL ───────────┘
                                     │
                    ┌────────────────┴────────────────┐
               DigestGenerator              Streamlit Dashboard
               (reports/*.md + *.pdf)      (http://localhost:8501)
```

---

## Tech Stack Summary

| Layer | Technology |
|-------|-----------|
| Language | Python 3.11+ |
| Dashboard | Streamlit 1.35+ · Plotly |
| Storage | SQLite (stdlib) |
| Metrics | NumPy · Pandas |
| GitHub | PyGithub |
| Slack | slack_sdk (Block Kit) |
| PDF Reports | reportlab |
| Scheduling | schedule |
| Config | PyYAML · python-dotenv |
| Tests | pytest · pytest-mock |

---

*TPOps Sentinel — ThinkPalm Technologies © 2025*
