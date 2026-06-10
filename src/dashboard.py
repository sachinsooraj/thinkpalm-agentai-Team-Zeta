"""
dashboard.py — TPOps Sentinel Live Dashboard (Streamlit)

Tabs:
  1. Live Metrics   — real-time latency + error gauges per tenant
  2. Token Usage    — per-tenant token consumption bar charts
  3. Error Analysis — error category breakdown + timeline
  4. Alerts Log     — all breaches raised with severity badges
  5. Daily Digest   — rendered markdown report
"""

from __future__ import annotations

import time
from pathlib import Path

import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import streamlit as st
from streamlit_autorefresh import st_autorefresh

from sentinel.store import MetricStore
from sentinel.digest import generate_markdown_report
from sentinel.utils import load_config, utcnow

# ── Page config ───────────────────────────────────────────────────────────────
st.set_page_config(
    page_title="TPOps Sentinel",
    page_icon="🛡️",
    layout="wide",
    initial_sidebar_state="expanded",
)

# ── Auto-refresh every 5 s ────────────────────────────────────────────────────
st_autorefresh(interval=5000, key="sentinel_refresh")

# ── Custom CSS ────────────────────────────────────────────────────────────────
st.markdown("""
<style>
@import url('https://fonts.googleapis.com/css2?family=Inter:wght@300;400;600;700&display=swap');

html, body, [class*="css"] { font-family: 'Inter', sans-serif; }

.main { background: #0b0f1a; }

.metric-card {
    background: linear-gradient(135deg, #141b2d 0%, #1e2a40 100%);
    border: 1px solid #2a3650;
    border-radius: 12px;
    padding: 20px 24px;
    margin-bottom: 12px;
    box-shadow: 0 4px 20px rgba(0,0,0,0.3);
    transition: transform 0.2s ease, box-shadow 0.2s ease;
}
.metric-card:hover { transform: translateY(-2px); box-shadow: 0 8px 30px rgba(0,0,0,0.4); }

.badge-critical {
    background: linear-gradient(90deg, #ff4b4b, #c0392b);
    color: white; padding: 4px 12px; border-radius: 20px;
    font-weight: 600; font-size: 12px; display: inline-block;
}
.badge-warning {
    background: linear-gradient(90deg, #ffa500, #e67e22);
    color: white; padding: 4px 12px; border-radius: 20px;
    font-weight: 600; font-size: 12px; display: inline-block;
}
.badge-ok {
    background: linear-gradient(90deg, #21c55d, #16a34a);
    color: white; padding: 4px 12px; border-radius: 20px;
    font-weight: 600; font-size: 12px; display: inline-block;
}

.header-gradient {
    background: linear-gradient(135deg, #0f172a 0%, #1e3a5f 50%, #0f172a 100%);
    padding: 24px 32px; border-radius: 16px; margin-bottom: 24px;
    border: 1px solid #2a3650;
}

.stat-number { font-size: 2.4rem; font-weight: 700; color: #60a5fa; }
.stat-label  { font-size: 0.85rem; color: #94a3b8; text-transform: uppercase; letter-spacing: 1px; }

[data-testid="stSidebar"] { background: #0d1525; }
.stTabs [data-baseweb="tab"] { color: #94a3b8; font-weight: 500; }
.stTabs [aria-selected="true"] { color: #60a5fa !important; border-bottom: 2px solid #60a5fa; }

/* Sidebar Premium Styling */
.sidebar-container { padding: 10px 0; }
.sidebar-card {
    background: linear-gradient(145deg, #162032, #0d1525);
    border: 1px solid #2a3650;
    border-radius: 12px;
    padding: 16px;
    margin-bottom: 20px;
    box-shadow: 0 4px 15px rgba(0,0,0,0.2);
}
.sidebar-title {
    color: #60a5fa; font-size: 1.05rem; font-weight: 600;
    margin-bottom: 16px; border-bottom: 1px solid #2a3650; padding-bottom: 8px;
}
.sidebar-item {
    display: flex; justify-content: space-between; align-items: center;
    padding: 8px 0; font-size: 0.9rem; color: #cbd5e1;
    border-bottom: 1px solid rgba(42, 54, 80, 0.4);
}
.sidebar-item:last-child { border-bottom: none; }
.sidebar-value {
    background: rgba(96, 165, 250, 0.1); border: 1px solid rgba(96, 165, 250, 0.2);
    padding: 2px 8px; border-radius: 6px; font-family: monospace; color: #60a5fa; font-weight: 600;
}
.refresh-status {
    text-align: center; color: #64748b; font-size: 0.8rem;
    margin-top: 30px; padding-top: 20px; border-top: 1px solid #2a3650;
}
</style>
""", unsafe_allow_html=True)


# ── Singleton store (reads SQLite directly on every dashboard refresh) ────────
@st.cache_resource
def get_store() -> MetricStore:
    return MetricStore()

store = get_store()
cfg = load_config()

# ── Header ────────────────────────────────────────────────────────────────────
st.markdown("""
<div class="header-gradient">
  <h1 style="color:#60a5fa;margin:0;font-size:2rem;">🛡️ TPOps Sentinel</h1>
  <p style="color:#94a3b8;margin:4px 0 0 0;font-size:0.95rem;">
    AI Observability Monitor — ThinkPalm Technologies &nbsp;|&nbsp;
    LLM Customer Support Bot &nbsp;+&nbsp; IoT Anomaly Detection API
  </p>
</div>
""", unsafe_allow_html=True)

# ── Sidebar ───────────────────────────────────────────────────────────────────
with st.sidebar:
    thr = cfg["thresholds"]
    st.markdown(f"""
<div class="sidebar-container">
<div style="text-align:center; margin-bottom: 30px;">
<div style="font-size: 3.5rem; text-shadow: 0 0 20px rgba(96,165,250,0.4);">🛰️</div>
<h2 style="color: #f8fafc; margin: 12px 0 0 0; font-size: 1.4rem; letter-spacing: 1px;">Control Node</h2>
<p style="color: #64748b; font-size: 0.85rem; margin: 4px 0 0 0; text-transform: uppercase; letter-spacing: 2px;">TPOps Platform</p>
</div>

<div class="sidebar-card">
<div class="sidebar-title">⚙️ Fleet Configuration</div>
<div class="sidebar-item"><span>Active Tenants</span> <span class="sidebar-value">{len(cfg['tenants'])}</span></div>
<div class="sidebar-item"><span>Monitored Services</span> <span class="sidebar-value">{len(cfg['services'])}</span></div>
</div>

<div class="sidebar-card">
<div class="sidebar-title">🎚️ Global Thresholds</div>
<div class="sidebar-item"><span>P95 Latency</span> <span class="sidebar-value">{thr['latency_p95_ms']} ms</span></div>
<div class="sidebar-item"><span>P99 Latency</span> <span class="sidebar-value">{thr['latency_p99_ms']} ms</span></div>
<div class="sidebar-item"><span>Error Rate Limit</span> <span class="sidebar-value">{thr['error_rate_pct']}%</span></div>
<div class="sidebar-item"><span>Max Tokens/Req</span> <span class="sidebar-value">{thr['tokens_per_request']:,}</span></div>
</div>

<div class="refresh-status">
<div style="color: #21c55d; font-weight: 600; margin-bottom: 8px;">● Telemetry Active</div>
<div>Last sync: {utcnow().strftime('%H:%M:%S')} UTC</div>
<div style="margin-top: 4px;">Auto-refresh interval: 5s</div>
</div>
</div>
""", unsafe_allow_html=True)

# ── Top KPI row ───────────────────────────────────────────────────────────────
latencies = store.get_recent_latencies(500)
errors    = store.get_recent_errors(500)
tokens    = store.get_recent_tokens(500)
alerts    = store.get_recent_alerts(100)

total_req   = len(latencies)
total_tok   = sum(m["tokens_total"] for m in tokens) if tokens else 0
err_count   = len(errors)
err_rate    = round((err_count / max(total_req, 1)) * 100, 1)
alert_count = len(alerts)

c1, c2, c3, c4, c5 = st.columns(5)
kpi_style = "color:#60a5fa;font-size:1.8rem;font-weight:700;"
lbl_style = "color:#94a3b8;font-size:0.8rem;text-transform:uppercase;"

def kpi_card(col, icon, value, label, color="#60a5fa"):
    col.markdown(
        f'<div class="metric-card" style="text-align:center;">'
        f'<div style="font-size:1.8rem;">{icon}</div>'
        f'<div style="color:{color};font-size:1.6rem;font-weight:700;">{value}</div>'
        f'<div style="{lbl_style}">{label}</div>'
        f'</div>',
        unsafe_allow_html=True,
    )

kpi_card(c1, "📡", f"{total_req:,}", "Total Requests")
kpi_card(c2, "🪙", f"{total_tok/1000:.1f}K", "Tokens (window)")
kpi_card(c3, "❌", f"{err_rate}%", "Error Rate",
         color="#ff4b4b" if err_rate > 5 else "#21c55d")
kpi_card(c4, "🚨", str(alert_count), "Alerts Raised",
         color="#ffa500" if alert_count > 0 else "#21c55d")
kpi_card(c5, "✅" if err_rate < 5 else "⚠️", "HEALTHY" if err_rate < 5 else "DEGRADED",
         "System Status", color="#21c55d" if err_rate < 5 else "#ffa500")

st.markdown("<br>", unsafe_allow_html=True)

# ── Tabs ──────────────────────────────────────────────────────────────────────
tabs = st.tabs([
    "📈 Live Metrics",
    "🪙 Token Usage",
    "❌ Error Analysis",
    "🚨 Alerts Log",
    "📄 Daily Digest",
])


# ════════════════════════════════════════════════════════════════════════════
# TAB 1 — Live Metrics
# ════════════════════════════════════════════════════════════════════════════
with tabs[0]:
    st.markdown("#### Latency Over Time — All Tenants")

    if latencies:
        df_lat = pd.DataFrame(latencies)
        df_lat["timestamp"] = pd.to_datetime(df_lat["timestamp"])
        df_lat = df_lat.sort_values("timestamp")

        fig = px.line(
            df_lat.tail(300), x="timestamp", y="latency_ms",
            color="tenant_id", line_group="service_id",
            template="plotly_dark",
            labels={"latency_ms": "Latency (ms)", "timestamp": "Time", "tenant_id": "Tenant"},
            title="Request Latency Stream",
        )
        fig.add_hline(y=thr["latency_p95_ms"], line_dash="dash",
                      line_color="#ffa500", annotation_text="P95 Threshold")
        fig.add_hline(y=thr["latency_p99_ms"], line_dash="dash",
                      line_color="#ff4b4b", annotation_text="P99 Threshold")
        fig.update_layout(
            paper_bgcolor="#0b0f1a", plot_bgcolor="#0d1525",
            legend=dict(orientation="h", y=-0.2),
            height=380,
        )
        st.plotly_chart(fig, use_container_width=True)

        # Per-tenant latency percentile table
        st.markdown("#### Per-Tenant Latency Percentiles")
        tenant_stats = (
            df_lat.groupby("tenant_id")["latency_ms"]
            .quantile([0.5, 0.95, 0.99])
            .unstack()
            .rename(columns={0.5: "P50 (ms)", 0.95: "P95 (ms)", 0.99: "P99 (ms)"})
            .round(0)
            .reset_index()
        )

        def color_p99(val):
            if isinstance(val, float):
                if val > thr["latency_p99_ms"]:
                    return "background-color:#3d1515;color:#ff4b4b"
                if val > thr["latency_p95_ms"]:
                    return "background-color:#3d2d00;color:#ffa500"
            return ""

        st.dataframe(
            tenant_stats.style.map(color_p99, subset=["P95 (ms)", "P99 (ms)"]),
            use_container_width=True,
            hide_index=True,
        )
    else:
        st.info("⏳ Waiting for metrics — start `run_monitor.py` to generate traffic.")


# ════════════════════════════════════════════════════════════════════════════
# TAB 2 — Token Usage
# ════════════════════════════════════════════════════════════════════════════
with tabs[1]:
    if tokens:
        df_tok = pd.DataFrame(tokens)
        df_tok["timestamp"] = pd.to_datetime(df_tok["timestamp"])

        col1, col2 = st.columns(2)

        with col1:
            st.markdown("#### Total Tokens by Tenant")
            tenant_tok = (
                df_tok.groupby("tenant_id")["tokens_total"].sum().reset_index()
                .sort_values("tokens_total", ascending=True)
            )
            fig = px.bar(
                tenant_tok, x="tokens_total", y="tenant_id",
                orientation="h", template="plotly_dark",
                color="tokens_total",
                color_continuous_scale=["#1e3a5f", "#60a5fa", "#a78bfa"],
                labels={"tokens_total": "Total Tokens", "tenant_id": "Tenant"},
            )
            fig.update_layout(
                paper_bgcolor="#0b0f1a", plot_bgcolor="#0d1525",
                coloraxis_showscale=False, height=300,
            )
            st.plotly_chart(fig, use_container_width=True)

        with col2:
            st.markdown("#### Token Split — Input vs Output")
            totals = {"Input": df_tok["tokens_input"].sum(),
                      "Output": df_tok["tokens_output"].sum()}
            fig = px.pie(
                values=list(totals.values()), names=list(totals.keys()),
                template="plotly_dark",
                color_discrete_sequence=["#60a5fa", "#a78bfa"],
                hole=0.55,
            )
            fig.update_layout(paper_bgcolor="#0b0f1a", height=300)
            st.plotly_chart(fig, use_container_width=True)

        st.markdown("#### Token Usage Timeline by Service")
        df_tok_time = df_tok.sort_values("timestamp")
        fig = px.area(
            df_tok_time.tail(400), x="timestamp", y="tokens_total",
            color="service_id", template="plotly_dark",
            labels={"tokens_total": "Tokens", "service_id": "Service"},
        )
        fig.update_layout(
            paper_bgcolor="#0b0f1a", plot_bgcolor="#0d1525", height=300,
        )
        st.plotly_chart(fig, use_container_width=True)
    else:
        st.info("⏳ No token data yet.")


# ════════════════════════════════════════════════════════════════════════════
# TAB 3 — Error Analysis
# ════════════════════════════════════════════════════════════════════════════
with tabs[2]:
    if errors:
        df_err = pd.DataFrame(errors)
        df_err["timestamp"] = pd.to_datetime(df_err["timestamp"])

        col1, col2 = st.columns(2)

        with col1:
            st.markdown("#### Error Category Distribution")
            cat_counts = df_err["error_category"].value_counts().reset_index()
            cat_counts.columns = ["category", "count"]
            fig = px.pie(
                cat_counts, values="count", names="category",
                template="plotly_dark", hole=0.45,
                color_discrete_sequence=px.colors.sequential.Plasma_r,
            )
            fig.update_layout(paper_bgcolor="#0b0f1a", height=320)
            st.plotly_chart(fig, use_container_width=True)

        with col2:
            st.markdown("#### Errors per Tenant")
            tenant_err = df_err.groupby("tenant_id").size().reset_index(name="errors")
            fig = px.bar(
                tenant_err, x="tenant_id", y="errors",
                template="plotly_dark",
                color="errors",
                color_continuous_scale=["#1e3a5f", "#ff4b4b"],
            )
            fig.update_layout(
                paper_bgcolor="#0b0f1a", plot_bgcolor="#0d1525",
                coloraxis_showscale=False, height=320,
            )
            st.plotly_chart(fig, use_container_width=True)

        st.markdown("#### Error Timeline")
        df_err_time = df_err.sort_values("timestamp")
        fig = px.scatter(
            df_err_time.tail(300), x="timestamp", y="tenant_id",
            color="error_category", symbol="severity",
            template="plotly_dark", size_max=12,
            labels={"tenant_id": "Tenant", "error_category": "Category"},
        )
        fig.update_layout(
            paper_bgcolor="#0b0f1a", plot_bgcolor="#0d1525", height=320,
        )
        st.plotly_chart(fig, use_container_width=True)
    else:
        st.info("✅ No errors recorded in current window.")


# ════════════════════════════════════════════════════════════════════════════
# TAB 4 — Alerts Log
# ════════════════════════════════════════════════════════════════════════════
with tabs[3]:
    st.markdown("#### 🚨 Threshold Breach Alerts")
    if alerts:
        for a in reversed(alerts):
            sev = a.get("severity", "warning")
            badge_cls = "badge-critical" if sev == "critical" else "badge-warning"
            issue_link = ""
            if a.get("github_issue_url"):
                issue_link = (
                    f' &nbsp; <a href="{a["github_issue_url"]}" target="_blank" '
                    f'style="color:#60a5fa;text-decoration:none;">🐙 GitHub Issue</a>'
                )
            slack_badge = (
                '<span style="color:#21c55d;">💬 Slack sent</span>'
                if a.get("slack_sent") else ""
            )
            st.markdown(
                f'<div class="metric-card">'
                f'<span class="{badge_cls}">{sev.upper()}</span>&nbsp;&nbsp;'
                f'<strong>{a["metric_name"].replace("_", " ").title()}</strong>'
                f'<span style="color:#94a3b8;font-size:0.85rem;"> — '
                f'{a["tenant_id"]} / {a["service_id"]}</span>'
                f'{issue_link} &nbsp; {slack_badge}'
                f'<br><span style="color:#60a5fa;font-size:1.1rem;">'
                f'{a["metric_value"]:.2f}</span>'
                f'<span style="color:#94a3b8;"> / threshold {a["threshold"]:.2f}</span>'
                f'<br><small style="color:#64748b;">{a["ts"]}</small>'
                f'</div>',
                unsafe_allow_html=True,
            )
    else:
        st.success("✅ No alerts raised in this session. All metrics within thresholds.")


# ════════════════════════════════════════════════════════════════════════════
# TAB 5 — Daily Digest
# ════════════════════════════════════════════════════════════════════════════
with tabs[4]:
    col1, col2 = st.columns([3, 1])
    with col2:
        if st.button("🔄 Regenerate Report", use_container_width=True):
            st.rerun()

    md_content = generate_markdown_report(store, utcnow())
    st.markdown(md_content)

    reports_dir = Path(cfg["digest"]["output_dir"])
    existing = sorted(reports_dir.glob("digest_*.md"), reverse=True)
    if existing:
        st.markdown("---")
        st.markdown("#### 📁 Saved Reports")
        for p in existing[:5]:
            with open(p) as f:
                content = f.read()
            st.download_button(
                label=f"⬇️ {p.name}",
                data=content,
                file_name=p.name,
                mime="text/markdown",
            )
