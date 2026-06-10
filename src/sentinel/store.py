"""
sentinel/store.py — SQLite persistence + in-memory ring buffer for live streaming
"""

from __future__ import annotations

import json
import sqlite3
import threading
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Deque, Dict, List, Optional

from .metrics import ErrorMetric, LatencyMetric, MetricBatch, TokenUsageMetric
from .utils import get_logger, load_config, utcnow

log = get_logger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# MetricStore — single source of truth
# ─────────────────────────────────────────────────────────────────────────────

class MetricStore:
    """
    Thread-safe store combining:
      - SQLite for durable persistence (survives restarts)
      - In-memory deque ring buffer for live dashboard streaming
    """

    _DDL = """
    CREATE TABLE IF NOT EXISTS token_usage (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        tenant_id   TEXT NOT NULL,
        service_id  TEXT NOT NULL,
        request_id  TEXT NOT NULL,
        tokens_input  INTEGER NOT NULL,
        tokens_output INTEGER NOT NULL,
        tokens_total  INTEGER NOT NULL,
        model       TEXT,
        ts          TEXT NOT NULL
    );

    CREATE TABLE IF NOT EXISTS latency_events (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        tenant_id   TEXT NOT NULL,
        service_id  TEXT NOT NULL,
        request_id  TEXT NOT NULL,
        latency_ms  REAL NOT NULL,
        status_code INTEGER NOT NULL,
        ts          TEXT NOT NULL
    );

    CREATE TABLE IF NOT EXISTS error_events (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        tenant_id       TEXT NOT NULL,
        service_id      TEXT NOT NULL,
        request_id      TEXT NOT NULL,
        error_category  TEXT NOT NULL,
        error_message   TEXT NOT NULL,
        severity        TEXT NOT NULL,
        ts              TEXT NOT NULL
    );

    CREATE TABLE IF NOT EXISTS alerts_raised (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        alert_type  TEXT NOT NULL,
        tenant_id   TEXT NOT NULL,
        service_id  TEXT NOT NULL,
        severity    TEXT NOT NULL,
        metric_name TEXT NOT NULL,
        metric_value REAL NOT NULL,
        threshold   REAL NOT NULL,
        github_issue_url TEXT,
        slack_sent  INTEGER DEFAULT 0,
        ts          TEXT NOT NULL
    );

    CREATE INDEX IF NOT EXISTS idx_token_ts ON token_usage(ts);
    CREATE INDEX IF NOT EXISTS idx_latency_ts ON latency_events(ts);
    CREATE INDEX IF NOT EXISTS idx_error_ts ON error_events(ts);
    CREATE INDEX IF NOT EXISTS idx_alerts_ts ON alerts_raised(ts);
    """

    def __init__(self, db_path: Optional[str] = None, max_memory: int = 5000):
        cfg = load_config()
        self._db_path = db_path or cfg["database"]["path"]
        self._max_memory = max_memory or cfg["database"]["max_events_in_memory"]
        self._lock = threading.Lock()

        # In-memory ring buffers (dicts for dashboard consumption)
        self._tokens: Deque[dict] = deque(maxlen=self._max_memory)
        self._latencies: Deque[dict] = deque(maxlen=self._max_memory)
        self._errors: Deque[dict] = deque(maxlen=self._max_memory)
        self._alerts: Deque[dict] = deque(maxlen=1000)

        self._init_db()
        self._load_from_db()          # hydrate ring buffers from SQLite on startup
        log.info("MetricStore initialised — db: %s", self._db_path)

    # ── DB setup ──────────────────────────────────────────────────────────────

    def _get_conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self) -> None:
        with self._get_conn() as conn:
            conn.executescript(self._DDL)

    def _load_from_db(self) -> None:
        """Pre-load recent rows from SQLite into in-memory ring buffers.
        Ensures the dashboard shows historical data immediately on startup,
        even when no monitor process is currently running."""
        try:
            with self._get_conn() as conn:
                # Latencies
                rows = conn.execute(
                    "SELECT tenant_id, service_id, request_id, latency_ms, "
                    "status_code, ts AS timestamp FROM latency_events "
                    "ORDER BY id DESC LIMIT 5000"
                ).fetchall()
                for r in reversed(rows):
                    self._latencies.append(dict(r))

                # Tokens
                rows = conn.execute(
                    "SELECT tenant_id, service_id, request_id, tokens_input, "
                    "tokens_output, tokens_total, model, ts AS timestamp "
                    "FROM token_usage ORDER BY id DESC LIMIT 5000"
                ).fetchall()
                for r in reversed(rows):
                    self._tokens.append(dict(r))

                # Errors
                rows = conn.execute(
                    "SELECT tenant_id, service_id, request_id, error_category, "
                    "error_message, severity, ts AS timestamp FROM error_events "
                    "ORDER BY id DESC LIMIT 5000"
                ).fetchall()
                for r in reversed(rows):
                    self._errors.append(dict(r))

                # Alerts
                rows = conn.execute(
                    "SELECT alert_type, tenant_id, service_id, severity, "
                    "metric_name, metric_value, threshold, github_issue_url, "
                    "slack_sent, ts FROM alerts_raised "
                    "ORDER BY id DESC LIMIT 1000"
                ).fetchall()
                for r in reversed(rows):
                    self._alerts.append(dict(r))

            log.info(
                "Ring buffers hydrated — %d latencies, %d tokens, %d errors, %d alerts",
                len(self._latencies), len(self._tokens),
                len(self._errors), len(self._alerts),
            )
        except Exception as exc:
            log.warning("Could not pre-load from DB: %s", exc)

    # ── Write ─────────────────────────────────────────────────────────────────

    def ingest(self, batch: MetricBatch) -> None:
        """Persist a full MetricBatch atomically."""
        with self._lock:
            with self._get_conn() as conn:
                for m in batch.token_metrics:
                    self._insert_token(conn, m)
                for m in batch.latency_metrics:
                    self._insert_latency(conn, m)
                for m in batch.error_metrics:
                    self._insert_error(conn, m)

    def _insert_token(self, conn: sqlite3.Connection, m: TokenUsageMetric) -> None:
        conn.execute(
            "INSERT INTO token_usage "
            "(tenant_id, service_id, request_id, tokens_input, tokens_output, "
            "tokens_total, model, ts) VALUES (?,?,?,?,?,?,?,?)",
            (m.tenant_id, m.service_id, m.request_id, m.tokens_input,
             m.tokens_output, m.tokens_total, m.model, m.timestamp.isoformat()),
        )
        self._tokens.append(m.to_dict())

    def _insert_latency(self, conn: sqlite3.Connection, m: LatencyMetric) -> None:
        conn.execute(
            "INSERT INTO latency_events "
            "(tenant_id, service_id, request_id, latency_ms, status_code, ts) "
            "VALUES (?,?,?,?,?,?)",
            (m.tenant_id, m.service_id, m.request_id, m.latency_ms,
             m.status_code, m.timestamp.isoformat()),
        )
        self._latencies.append(m.to_dict())

    def _insert_error(self, conn: sqlite3.Connection, m: ErrorMetric) -> None:
        conn.execute(
            "INSERT INTO error_events "
            "(tenant_id, service_id, request_id, error_category, "
            "error_message, severity, ts) VALUES (?,?,?,?,?,?,?)",
            (m.tenant_id, m.service_id, m.request_id, m.error_category,
             m.error_message, m.severity, m.timestamp.isoformat()),
        )
        self._errors.append(m.to_dict())

    def record_alert(
        self,
        alert_type: str,
        tenant_id: str,
        service_id: str,
        severity: str,
        metric_name: str,
        metric_value: float,
        threshold: float,
        github_issue_url: str = "",
        slack_sent: bool = False,
    ) -> None:
        ts = utcnow().isoformat()
        row = {
            "alert_type": alert_type,
            "tenant_id": tenant_id,
            "service_id": service_id,
            "severity": severity,
            "metric_name": metric_name,
            "metric_value": metric_value,
            "threshold": threshold,
            "github_issue_url": github_issue_url,
            "slack_sent": int(slack_sent),
            "ts": ts,
        }
        with self._lock:
            with self._get_conn() as conn:
                conn.execute(
                    "INSERT INTO alerts_raised "
                    "(alert_type, tenant_id, service_id, severity, metric_name, "
                    "metric_value, threshold, github_issue_url, slack_sent, ts) "
                    "VALUES (:alert_type,:tenant_id,:service_id,:severity,"
                    ":metric_name,:metric_value,:threshold,:github_issue_url,"
                    ":slack_sent,:ts)",
                    row,
                )
            self._alerts.append(row)

    # ── Read — dashboard helpers (always query SQLite for live cross-process data) ──

    def get_recent_latencies(self, limit: int = 500) -> list[dict]:
        """Query SQLite directly — always reflects latest data from any process."""
        rows = self.query(
            "SELECT tenant_id, service_id, request_id, latency_ms, "
            "status_code, ts AS timestamp "
            "FROM latency_events ORDER BY id DESC LIMIT ?",
            (limit,),
        )
        return list(reversed(rows))

    def get_recent_errors(self, limit: int = 500) -> list[dict]:
        """Query SQLite directly — always reflects latest data from any process."""
        rows = self.query(
            "SELECT tenant_id, service_id, request_id, error_category, "
            "error_message, severity, ts AS timestamp "
            "FROM error_events ORDER BY id DESC LIMIT ?",
            (limit,),
        )
        return list(reversed(rows))

    def get_recent_tokens(self, limit: int = 500) -> list[dict]:
        """Query SQLite directly — always reflects latest data from any process."""
        rows = self.query(
            "SELECT tenant_id, service_id, request_id, tokens_input, "
            "tokens_output, tokens_total, model, ts AS timestamp "
            "FROM token_usage ORDER BY id DESC LIMIT ?",
            (limit,),
        )
        return list(reversed(rows))

    def get_recent_alerts(self, limit: int = 100) -> list[dict]:
        """Query SQLite directly — always reflects latest data from any process."""
        rows = self.query(
            "SELECT alert_type, tenant_id, service_id, severity, metric_name, "
            "metric_value, threshold, github_issue_url, slack_sent, ts "
            "FROM alerts_raised ORDER BY id DESC LIMIT ?",
            (limit,),
        )
        return list(reversed(rows))


    # ── Read — digest helpers ─────────────────────────────────────────────────

    def query(self, sql: str, params: tuple = ()) -> list[dict]:
        """Execute arbitrary SELECT and return list of dicts."""
        with self._get_conn() as conn:
            rows = conn.execute(sql, params).fetchall()
            return [dict(r) for r in rows]

    def token_summary_today(self) -> list[dict]:
        today = datetime.now(tz=timezone.utc).strftime("%Y-%m-%d")
        return self.query(
            "SELECT tenant_id, service_id, "
            "SUM(tokens_total) AS total_tokens, "
            "COUNT(*) AS request_count, "
            "AVG(tokens_total) AS avg_tokens "
            "FROM token_usage WHERE ts >= ? "
            "GROUP BY tenant_id, service_id "
            "ORDER BY total_tokens DESC",
            (today,),
        )

    def error_summary_today(self) -> list[dict]:
        today = datetime.now(tz=timezone.utc).strftime("%Y-%m-%d")
        return self.query(
            "SELECT tenant_id, service_id, error_category, "
            "COUNT(*) AS error_count "
            "FROM error_events WHERE ts >= ? "
            "GROUP BY tenant_id, service_id, error_category "
            "ORDER BY error_count DESC",
            (today,),
        )

    def latency_summary_today(self) -> list[dict]:
        today = datetime.now(tz=timezone.utc).strftime("%Y-%m-%d")
        return self.query(
            "SELECT tenant_id, service_id, "
            "AVG(latency_ms) AS avg_ms, "
            "MIN(latency_ms) AS min_ms, "
            "MAX(latency_ms) AS max_ms, "
            "COUNT(*) AS request_count "
            "FROM latency_events WHERE ts >= ? "
            "GROUP BY tenant_id, service_id",
            (today,),
        )

    def alerts_today(self) -> list[dict]:
        today = datetime.now(tz=timezone.utc).strftime("%Y-%m-%d")
        return self.query(
            "SELECT * FROM alerts_raised WHERE ts >= ? ORDER BY ts DESC",
            (today,),
        )
