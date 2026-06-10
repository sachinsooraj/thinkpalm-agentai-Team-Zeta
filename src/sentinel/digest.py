"""
sentinel/digest.py — Daily digest report generator

Produces a Markdown + PDF daily summary report covering:
  - Executive summary
  - Per-tenant token consumption
  - Latency statistics table
  - Error breakdown by category
  - Alerts raised log
  - SLA compliance summary

Can be triggered manually or scheduled via the `schedule` library.
"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING

from .utils import get_logger, load_config, utcnow

if TYPE_CHECKING:
    from .store import MetricStore

log = get_logger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# Markdown report builder
# ─────────────────────────────────────────────────────────────────────────────

def _md_table(headers: list[str], rows: list[list]) -> str:
    """Build a markdown table string."""
    header_row = "| " + " | ".join(headers) + " |"
    sep_row = "| " + " | ".join(["---"] * len(headers)) + " |"
    body_rows = "\n".join(
        "| " + " | ".join(str(cell) for cell in row) + " |" for row in rows
    )
    return f"{header_row}\n{sep_row}\n{body_rows}"


def generate_markdown_report(store: "MetricStore", report_date: datetime) -> str:
    """Generate the full daily digest as a Markdown string."""
    date_str = report_date.strftime("%Y-%m-%d")
    ts_str = report_date.strftime("%Y-%m-%d %H:%M UTC")

    token_summary = store.token_summary_today()
    error_summary = store.error_summary_today()
    latency_summary = store.latency_summary_today()
    alerts = store.alerts_today()

    # ── Totals for executive summary ──────────────────────────────────────────
    total_requests = sum(r["request_count"] for r in token_summary)
    total_tokens = sum(r["total_tokens"] for r in token_summary)
    total_errors = sum(r["error_count"] for r in error_summary)
    total_alerts = len(alerts)
    error_rate = (total_errors / max(total_requests, 1)) * 100

    lines = [
        f"# 📊 TPOps Sentinel — Daily Digest",
        f"**Report Date:** {date_str}  |  **Generated:** {ts_str}",
        f"",
        "---",
        "",
        "## 🎯 Executive Summary",
        "",
        f"| Metric | Value |",
        f"|--------|-------|",
        f"| Total Requests (today) | **{total_requests:,}** |",
        f"| Total Tokens Consumed  | **{total_tokens:,}** |",
        f"| Total Errors           | **{total_errors:,}** |",
        f"| Overall Error Rate     | **{error_rate:.2f}%** |",
        f"| Alerts Raised          | **{total_alerts}** |",
        "",
        "---",
        "",
        "## 🏢 Per-Tenant Token Consumption",
        "",
    ]

    if token_summary:
        rows = [
            [
                r["tenant_id"],
                r["service_id"],
                f"{r['total_tokens']:,}",
                f"{r['request_count']:,}",
                f"{r['avg_tokens']:.0f}",
            ]
            for r in token_summary
        ]
        lines.append(
            _md_table(
                ["Tenant", "Service", "Total Tokens", "Requests", "Avg Tokens/Req"],
                rows,
            )
        )
    else:
        lines.append("*No token data recorded today.*")

    lines += [
        "",
        "---",
        "",
        "## ⚡ Latency Statistics",
        "",
    ]

    if latency_summary:
        rows = [
            [
                r["tenant_id"],
                r["service_id"],
                f"{r['avg_ms']:.0f} ms",
                f"{r['min_ms']:.0f} ms",
                f"{r['max_ms']:.0f} ms",
                f"{r['request_count']:,}",
            ]
            for r in latency_summary
        ]
        lines.append(
            _md_table(
                ["Tenant", "Service", "Avg Latency", "Min", "Max", "Requests"],
                rows,
            )
        )
    else:
        lines.append("*No latency data recorded today.*")

    lines += [
        "",
        "---",
        "",
        "## ❌ Error Breakdown",
        "",
    ]

    if error_summary:
        rows = [
            [
                r["tenant_id"],
                r["service_id"],
                r["error_category"],
                f"{r['error_count']:,}",
            ]
            for r in error_summary
        ]
        lines.append(
            _md_table(["Tenant", "Service", "Error Category", "Count"], rows)
        )
    else:
        lines.append("*No errors recorded today.* ✅")

    lines += [
        "",
        "---",
        "",
        "## 🚨 Alerts Raised",
        "",
    ]

    if alerts:
        rows = [
            [
                a["ts"][:19],
                a["severity"].upper(),
                a["tenant_id"],
                a["service_id"],
                a["metric_name"],
                f"{a['metric_value']:.2f}",
                f"{a['threshold']:.2f}",
                f"[Issue]({a['github_issue_url']})" if a.get("github_issue_url") else "—",
            ]
            for a in alerts
        ]
        lines.append(
            _md_table(
                ["Time", "Severity", "Tenant", "Service", "Metric", "Value", "Threshold", "Issue"],
                rows,
            )
        )
    else:
        lines.append("*No alerts raised today.* ✅")

    lines += [
        "",
        "---",
        "",
        "## ✅ SLA Compliance",
        "",
        "| SLA Target | Threshold | Status |",
        "|------------|-----------|--------|",
        f"| P95 Latency < 2000 ms | 2000 ms | {'✅ Pass' if not any(a['metric_name'] == 'latency_p95' for a in alerts) else '❌ Breach'} |",
        f"| P99 Latency < 4000 ms | 4000 ms | {'✅ Pass' if not any(a['metric_name'] == 'latency_p99' for a in alerts) else '❌ Breach'} |",
        f"| Error Rate < 5%       | 5.0%    | {'✅ Pass' if error_rate < 5.0 else '❌ Breach'} |",
        "",
        "---",
        "",
        "*Report auto-generated by TPOps Sentinel v1.0.0*",
    ]

    return "\n".join(lines)


# ─────────────────────────────────────────────────────────────────────────────
# PDF generation (optional — requires reportlab)
# ─────────────────────────────────────────────────────────────────────────────

def generate_pdf_report(markdown_content: str, output_path: Path) -> bool:
    """Convert markdown report to PDF using reportlab. Returns success bool."""
    try:
        from reportlab.lib.pagesizes import A4
        from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
        from reportlab.lib.units import mm
        from reportlab.lib import colors
        from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle
        from reportlab.lib.enums import TA_LEFT, TA_CENTER

        doc = SimpleDocTemplate(
            str(output_path),
            pagesize=A4,
            rightMargin=20 * mm,
            leftMargin=20 * mm,
            topMargin=20 * mm,
            bottomMargin=20 * mm,
        )
        styles = getSampleStyleSheet()
        story = []

        # Simple line-by-line parsing for section headers and content
        for line in markdown_content.split("\n"):
            if line.startswith("# "):
                style = ParagraphStyle(
                    "h1", parent=styles["Heading1"],
                    fontSize=18, textColor=colors.HexColor("#1e3a5f"),
                )
                story.append(Paragraph(line[2:], style))
                story.append(Spacer(1, 6))
            elif line.startswith("## "):
                style = ParagraphStyle(
                    "h2", parent=styles["Heading2"],
                    fontSize=13, textColor=colors.HexColor("#2e6da4"),
                )
                story.append(Paragraph(line[3:], style))
                story.append(Spacer(1, 4))
            elif line.startswith("---"):
                story.append(Spacer(1, 8))
            elif line.strip():
                story.append(Paragraph(line.replace("|", " | "), styles["Normal"]))
            else:
                story.append(Spacer(1, 4))

        doc.build(story)
        log.info("PDF report written: %s", output_path)
        return True
    except ImportError:
        log.warning("reportlab not available — skipping PDF generation")
        return False
    except Exception as exc:
        log.error("PDF generation failed: %s", exc)
        return False


# ─────────────────────────────────────────────────────────────────────────────
# DigestGenerator — orchestrates report creation and scheduling
# ─────────────────────────────────────────────────────────────────────────────

class DigestGenerator:
    """Generates and saves daily digest reports."""

    def __init__(self, store: "MetricStore"):
        self._store = store
        self._cfg = load_config()
        self._output_dir = Path(self._cfg["digest"]["output_dir"])
        self._formats = self._cfg["digest"].get("formats", ["markdown"])

    def run(self, report_date: datetime | None = None) -> list[Path]:
        """Generate report for the given date (defaults to today). Returns written paths."""
        report_date = report_date or utcnow()
        date_str = report_date.strftime("%Y-%m-%d")
        self._output_dir.mkdir(parents=True, exist_ok=True)

        md_content = generate_markdown_report(self._store, report_date)
        written: list[Path] = []

        if "markdown" in self._formats:
            md_path = self._output_dir / f"digest_{date_str}.md"
            md_path.write_text(md_content, encoding="utf-8")
            log.info("Markdown digest written: %s", md_path)
            written.append(md_path)

        if "pdf" in self._formats:
            pdf_path = self._output_dir / f"digest_{date_str}.pdf"
            if generate_pdf_report(md_content, pdf_path):
                written.append(pdf_path)

        return written

    def schedule_daily(self, time_str: str = "08:00") -> None:
        """Block-schedule the digest at `time_str` (HH:MM) using the schedule library."""
        import schedule
        import time

        schedule.every().day.at(time_str).do(self.run)
        log.info("Daily digest scheduled at %s", time_str)

        while True:
            schedule.run_pending()
            time.sleep(30)
