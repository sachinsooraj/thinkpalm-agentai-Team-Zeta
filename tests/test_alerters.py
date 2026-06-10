"""tests/test_alerters.py — Unit tests for GitHub and Slack alerters (dry-run)"""
import json
import pytest
from unittest.mock import patch, MagicMock
from sentinel.evaluator import BreachEvent
from sentinel.github_alerter import GitHubAlerter
from sentinel.slack_alerter import SlackAlerter
from sentinel.utils import reset_config_cache


@pytest.fixture(autouse=True)
def reset_cache():
    reset_config_cache()
    yield
    reset_config_cache()


def _make_breach(severity="critical", metric="latency_p99", value=5000.0) -> BreachEvent:
    return BreachEvent(
        tenant_id="acme-corp",
        service_id="llm-support",
        metric_name=metric,
        metric_value=value,
        threshold=4000.0,
        severity=severity,
        description="P99 latency exceeded threshold",
        recommended_action="Check inference cluster health",
    )


# ── GitHub Alerter ────────────────────────────────────────────────────────────

class TestGitHubAlerter:
    def test_dry_run_returns_none(self, capsys):
        with patch("sentinel.utils.load_config") as mock_cfg:
            mock_cfg.return_value = {
                "monitor": {"dry_run": True},
                "alerting": {"github": {"repo": "", "labels": ["auto-raised"],
                                        "dedup_window_minutes": 30}},
                "database": {"path": ":memory:", "max_events_in_memory": 100},
                "digest": {"output_dir": "/tmp/reports", "formats": ["markdown"]},
            }
            alerter = GitHubAlerter()
            breach = _make_breach()
            result = alerter.raise_issue(breach)
            assert result is None

    def test_title_contains_severity_and_tenant(self):
        alerter = GitHubAlerter.__new__(GitHubAlerter)
        alerter._dry_run = True
        breach = _make_breach(severity="critical")
        title = alerter._build_title(breach)
        assert "CRITICAL" in title
        assert "acme-corp" in title

    def test_body_contains_metric_table(self):
        alerter = GitHubAlerter.__new__(GitHubAlerter)
        alerter._dry_run = True
        breach = _make_breach()
        body = alerter._build_body(breach)
        assert "Metric Snapshot" in body
        assert "acme-corp" in body
        assert "P99 latency exceeded" in body

    def test_labels_include_severity_and_service(self):
        alerter = GitHubAlerter.__new__(GitHubAlerter)
        alerter._dry_run = True
        alerter._base_labels = ["auto-raised"]
        breach = _make_breach()
        labels = alerter._build_labels(breach)
        assert "severity:critical" in labels
        assert "service:llm-support" in labels
        assert "tenant:acme-corp" in labels

    def test_format_metric_latency(self):
        result = GitHubAlerter._format_metric("latency_p99", 4500.0)
        assert "ms" in result

    def test_format_metric_error_rate(self):
        result = GitHubAlerter._format_metric("error_rate", 12.5)
        assert "%" in result

    def test_format_metric_tokens(self):
        result = GitHubAlerter._format_metric("tokens_per_request", 3500.0)
        assert "tokens" in result


# ── Slack Alerter ─────────────────────────────────────────────────────────────

class TestSlackAlerter:
    def test_dry_run_returns_true(self, capsys):
        with patch.dict("os.environ", {"SENTINEL_DRY_RUN": "true"}):
            with patch("sentinel.utils.load_config") as mock_cfg:
                mock_cfg.return_value = {
                    "monitor": {"dry_run": True},
                    "alerting": {"slack": {"channel": "#test"}},
                    "database": {"path": ":memory:", "max_events_in_memory": 100},
                    "digest": {"output_dir": "/tmp", "formats": []},
                }
                alerter = SlackAlerter()
                breach = _make_breach()
                result = alerter.send_alert(breach)
                assert result is True

    def test_payload_has_blocks(self):
        alerter = SlackAlerter.__new__(SlackAlerter)
        alerter._cfg = {"alerting": {"slack": {"channel": "#test"}}}
        alerter._dry_run = True
        alerter._channel = "#test"
        breach = _make_breach()
        payload = alerter._build_payload(breach, issue_url=None)
        assert "blocks" in payload
        assert len(payload["blocks"]) >= 4

    def test_payload_includes_github_button(self):
        alerter = SlackAlerter.__new__(SlackAlerter)
        alerter._cfg = {"alerting": {"slack": {"channel": "#test"}}}
        alerter._dry_run = True
        alerter._channel = "#test"
        breach = _make_breach()
        payload = alerter._build_payload(breach, issue_url="https://github.com/test/issues/1")
        block_types = [b.get("type") for b in payload["blocks"]]
        assert "actions" in block_types

    def test_fallback_text_format(self):
        alerter = SlackAlerter.__new__(SlackAlerter)
        breach = _make_breach(severity="warning", metric="error_rate", value=12.5)
        text = alerter._build_fallback_text(breach)
        assert "WARNING" in text
        assert "acme-corp" in text
        assert "error_rate" in text
