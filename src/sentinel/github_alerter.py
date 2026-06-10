"""
sentinel/github_alerter.py — Auto-raise structured GitHub Issues on breach

Uses PyGithub to create labelled, templated issues with:
  - Severity badge
  - Metric snapshot table
  - Recommended actions
  - Auto-labels (severity, service, auto-raised)

In dry-run mode, prints the issue body to stdout instead.
"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Optional

from .evaluator import BreachEvent
from .utils import get_logger, is_dry_run, load_config, utcnow_iso

log = get_logger(__name__)

_SEVERITY_BADGE = {
    "critical": "![critical](https://img.shields.io/badge/severity-critical-red)",
    "warning": "![warning](https://img.shields.io/badge/severity-warning-orange)",
}

_ISSUE_TEMPLATE = """\
## {severity_badge}

> **Auto-raised by TPOps Sentinel** at `{timestamp}` UTC

---

### 🔍 Description

{description}

---

### 📊 Metric Snapshot

| Metric | Value | Threshold | Status |
|--------|-------|-----------|--------|
| **{metric_name}** | `{metric_value}` | `{threshold}` | {status_icon} **BREACH** |

**Tenant:** `{tenant_id}`
**Service:** `{service_id}`
**Severity:** `{severity}`

---

### 🛠 Recommended Actions

{recommended_action}

---

### 🔗 Context

- Monitor: TPOps Sentinel v1.0.0
- Evaluation window: 60 seconds rolling
- Alert deduplication window: 30 minutes

---

*This issue was automatically generated. If resolved, close with label `resolved`.*
"""


class GitHubAlerter:
    """
    Raises structured GitHub Issues when BreachEvents are received.

    Deduplication is handled upstream by ThresholdEvaluator, so this
    class raises unconditionally for every BreachEvent it receives.
    """

    def __init__(self):
        self._cfg = load_config()
        self._dry_run = is_dry_run()
        self._repo_name = (
            os.getenv("GITHUB_REPO") or self._cfg["alerting"]["github"].get("repo", "")
        )
        self._token = os.getenv("GITHUB_TOKEN", "")
        self._base_labels = self._cfg["alerting"]["github"].get("labels", ["auto-raised"])
        self._repo = None  # lazy-init

        if not self._dry_run:
            self._init_client()

    def _init_client(self) -> None:
        if not self._token:
            log.warning("GITHUB_TOKEN not set — switching to dry-run mode")
            self._dry_run = True
            return
        if not self._repo_name:
            log.warning("GITHUB_REPO not set — switching to dry-run mode")
            self._dry_run = True
            return
        try:
            from github import Github, GithubException
            g = Github(self._token)
            self._repo = g.get_repo(self._repo_name)
            log.info("GitHub client connected — repo: %s", self._repo_name)
        except Exception as exc:
            log.warning("GitHub init failed (%s) — dry-run mode", exc)
            self._dry_run = True

    # ── Public ────────────────────────────────────────────────────────────────

    def raise_issue(self, breach: BreachEvent) -> Optional[str]:
        """
        Raise a GitHub Issue for the given breach.
        Returns the HTML URL of the created issue, or None in dry-run mode.
        """
        title = self._build_title(breach)
        body = self._build_body(breach)
        labels = self._build_labels(breach)

        if self._dry_run:
            self._print_dry_run(title, body, labels)
            return None

        try:
            from github import GithubException
            issue = self._repo.create_issue(title=title, body=body, labels=labels)
            log.info("GitHub issue raised — #%d %s", issue.number, issue.html_url)
            return issue.html_url
        except Exception as exc:
            log.error("Failed to raise GitHub issue: %s", exc)
            return None

    # ── Builders ──────────────────────────────────────────────────────────────

    def _build_title(self, b: BreachEvent) -> str:
        icon = "🔴" if b.severity == "critical" else "🟡"
        return (
            f"{icon} [{b.severity.upper()}] {b.metric_name.replace('_', ' ').title()} "
            f"breach — tenant: {b.tenant_id} | service: {b.service_id}"
        )

    def _build_body(self, b: BreachEvent) -> str:
        metric_val = self._format_metric(b.metric_name, b.metric_value)
        threshold_val = self._format_metric(b.metric_name, b.threshold)
        return _ISSUE_TEMPLATE.format(
            severity_badge=_SEVERITY_BADGE.get(b.severity, ""),
            timestamp=utcnow_iso(),
            description=b.description,
            metric_name=b.metric_name.replace("_", " ").title(),
            metric_value=metric_val,
            threshold=threshold_val,
            status_icon="🚨",
            tenant_id=b.tenant_id,
            service_id=b.service_id,
            severity=b.severity,
            recommended_action=b.recommended_action,
        )

    def _build_labels(self, b: BreachEvent) -> list[str]:
        labels = list(self._base_labels)
        labels.append(f"severity:{b.severity}")
        labels.append(f"service:{b.service_id}")
        labels.append(f"tenant:{b.tenant_id}")
        return labels

    @staticmethod
    def _format_metric(name: str, value: float) -> str:
        if "latency" in name:
            return f"{value:.0f} ms"
        if "rate" in name:
            return f"{value:.1f}%"
        if "token" in name:
            return f"{int(value):,} tokens"
        return f"{value:.2f}"

    def _print_dry_run(self, title: str, body: str, labels: list[str]) -> None:
        separator = "═" * 70
        print(f"\n{separator}")
        print("  🐙 GITHUB ISSUE [DRY-RUN MODE]")
        print(separator)
        print(f"  TITLE  : {title}")
        print(f"  LABELS : {', '.join(labels)}")
        print(f"  REPO   : {self._repo_name or '(not configured)'}")
        print(separator)
        print(body)
        print(separator + "\n")
