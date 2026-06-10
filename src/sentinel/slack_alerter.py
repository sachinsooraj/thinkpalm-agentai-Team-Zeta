"""
sentinel/slack_alerter.py — Fire Block Kit Slack alerts on threshold breach

Sends richly-formatted Slack messages to the delivery channel via
Incoming Webhook. Includes color-coded severity, metric table,
GitHub issue deep-link, and recommended actions.

In dry-run mode, prints the JSON payload to stdout.
"""

from __future__ import annotations

import json
import os
from typing import Optional

from .evaluator import BreachEvent
from .utils import get_logger, is_dry_run, load_config, severity_color, utcnow_iso

log = get_logger(__name__)


class SlackAlerter:
    """Sends Block Kit Slack messages on BreachEvent."""

    def __init__(self):
        self._cfg = load_config()
        self._dry_run = is_dry_run()
        self._webhook_url = os.getenv("SLACK_WEBHOOK_URL", "")
        self._channel = self._cfg["alerting"]["slack"].get("channel", "#delivery-alerts")

        if not self._dry_run and not self._webhook_url:
            log.warning("SLACK_WEBHOOK_URL not set — switching to dry-run mode")
            self._dry_run = True

    # ── Public ────────────────────────────────────────────────────────────────

    def send_alert(
        self, breach: BreachEvent, github_issue_url: Optional[str] = None
    ) -> bool:
        """
        Send a Slack alert for the breach.
        Returns True on success (or dry-run).
        """
        payload = self._build_payload(breach, github_issue_url)

        if self._dry_run:
            self._print_dry_run(payload)
            return True

        try:
            from slack_sdk.webhook import WebhookClient
            client = WebhookClient(self._webhook_url)
            response = client.send(
                text=self._build_fallback_text(breach),
                blocks=payload["blocks"],
                attachments=payload.get("attachments"),
            )
            if response.status_code == 200:
                log.info("Slack alert sent for %s/%s", breach.tenant_id, breach.metric_name)
                return True
            else:
                log.error("Slack returned %d: %s", response.status_code, response.body)
                return False
        except Exception as exc:
            log.error("Slack send failed: %s", exc)
            return False

    # ── Payload builders ──────────────────────────────────────────────────────

    def _build_payload(self, b: BreachEvent, issue_url: Optional[str]) -> dict:
        color = severity_color(b.severity)
        icon = "🔴" if b.severity == "critical" else "🟡"
        metric_display = self._format_metric(b.metric_name, b.metric_value)
        threshold_display = self._format_metric(b.metric_name, b.threshold)

        blocks = [
            {
                "type": "header",
                "text": {
                    "type": "plain_text",
                    "text": f"{icon} TPOps Sentinel Alert — {b.severity.upper()}",
                    "emoji": True,
                },
            },
            {"type": "divider"},
            {
                "type": "section",
                "fields": [
                    {"type": "mrkdwn", "text": f"*Tenant:*\n`{b.tenant_id}`"},
                    {"type": "mrkdwn", "text": f"*Service:*\n`{b.service_id}`"},
                    {"type": "mrkdwn", "text": f"*Metric:*\n`{b.metric_name}`"},
                    {"type": "mrkdwn", "text": f"*Severity:*\n`{b.severity}`"},
                    {"type": "mrkdwn", "text": f"*Value:*\n`{metric_display}`"},
                    {"type": "mrkdwn", "text": f"*Threshold:*\n`{threshold_display}`"},
                ],
            },
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": f"*Description:*\n{b.description}",
                },
            },
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": f"*Recommended Actions:*\n{b.recommended_action}",
                },
            },
        ]

        if issue_url:
            blocks.append({
                "type": "actions",
                "elements": [
                    {
                        "type": "button",
                        "text": {"type": "plain_text", "text": "View GitHub Issue 🐙"},
                        "url": issue_url,
                        "style": "danger" if b.severity == "critical" else "primary",
                    }
                ],
            })

        blocks.append({
            "type": "context",
            "elements": [
                {
                    "type": "mrkdwn",
                    "text": (
                        f"🕐 {utcnow_iso()} UTC  |  "
                        f"Channel: {self._channel}  |  "
                        f"TPOps Sentinel v1.0.0"
                    ),
                }
            ],
        })

        return {
            "attachments": [
                {
                    "color": color,
                    "blocks": blocks,
                }
            ],
            "blocks": blocks,
        }

    @staticmethod
    def _build_fallback_text(b: BreachEvent) -> str:
        return (
            f"[{b.severity.upper()}] {b.metric_name} breach for {b.tenant_id}/{b.service_id} "
            f"— value: {b.metric_value:.2f} (threshold: {b.threshold:.2f})"
        )

    @staticmethod
    def _format_metric(name: str, value: float) -> str:
        if "latency" in name:
            return f"{value:.0f} ms"
        if "rate" in name:
            return f"{value:.1f}%"
        if "token" in name:
            return f"{int(value):,} tokens"
        return f"{value:.2f}"

    def _print_dry_run(self, payload: dict) -> None:
        separator = "═" * 70
        print(f"\n{separator}")
        print("  💬 SLACK ALERT [DRY-RUN MODE]")
        print(separator)
        print(json.dumps(payload, indent=2))
        print(separator + "\n")
