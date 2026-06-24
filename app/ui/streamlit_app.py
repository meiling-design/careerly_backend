"""Streamlit UI — Decision Graph Dashboard for Job Matching AI.

Shows:
- 8-node LangGraph flow visualization
- Routing decisions
- 2-stage matching progress (embedding filter → LLM rerank)
- Export all formats

Run with:
    streamlit run app/ui/streamlit_app.py
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from typing import Any, Optional

import streamlit as st

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from app.core.orchestrator import DecisionGraph
from app.models.match import MatchReport, MatchResult
from app.utils.logger import logger

# ── Page config ─────────────────────────────────────────────────────────

st.set_page_config(
    page_title="Job Matching AI Agent — Decision Graph",
    page_icon="🧠",
    layout="wide",
    initial_sidebar_state="expanded",
)

# ── Session state ───────────────────────────────────────────────────────

if "graph" not in st.session_state:
    st.session_state.graph = DecisionGraph()
if "state" not in st.session_state:
    st.session_state.state = None
if "running" not in st.session_state:
    st.session_state.running = False
if "progress" not in st.session_state:
    st.session_state.progress = []
if "graph_log" not in st.session_state:
    st.session_state.graph_log = []

# ── Graph node definitions (for flow visualization) ──────────────────────

GRAPH_NODES = [
    ("crawl", "🔍 Crawl", "Fetch job listings from URLs"),
    ("normalize", "⚙️ Normalize", "LLM-enrich job fields"),
    ("resume_embed", "📄 Resume+Embed", "Parse resume & compute embedding"),
    ("routing", "🧭 Routing", "Decide: match / recrawl / end"),
    ("match", "🧠 Match", "2-stage embedding→LLM scoring"),
    ("rank", "📊 Rank", "Sort by composite score"),
    ("report", "📋 Report", "Generate exports"),
]

# ── Sidebar ──────────────────────────────────────────────────────────────

def render_sidebar() -> dict:
    """Render sidebar controls."""
    with st.sidebar:
        st.title("🧠 Job Matching AI")
        st.markdown("**Decision Graph** · LangGraph · 8 nodes")
        st.markdown("---")

        with st.expander("⚙️ Matching Mode", expanded=True):
            use_llm = st.checkbox("LLM Deep Analysis", value=True)
            use_embedding = st.checkbox("Semantic Embedding", value=True)
            two_stage = st.checkbox("🚀 2-Stage Matching (embedding filter → LLM rerank)", value=True,
                                     help="Stage 1: fast embedding filter (top 20%). Stage 2: LLM rerank. Saves ~80% cost.")

        st.markdown("---")
        st.subheader("📄 Resume")
        uploaded_file = st.file_uploader(
            "Upload (PDF/DOCX/TXT)", type=["pdf", "docx", "txt"],
        )
        resume_text = st.text_area(
            "Or paste text", height=120,
            placeholder="Paste resume...",
            disabled=uploaded_file is not None,
        )

        st.markdown("---")
        st.subheader("🔗 Job URLs")
        url_input = st.text_area(
            "One per line", height=120,
            placeholder="https://example.com/job/1",
        )

        st.markdown("---")
        if st.session_state.state:
            state = st.session_state.state
            st.caption(f"Session: `{state.session_id or 'N/A'[:16]}...`")
            if state.report:
                st.metric("Avg Score", f"{state.report.average_score:.1%}" if state.report.average_score else "N/A")
            if state.routing_decision:
                st.info(f"🧭 Route: **{state.routing_decision}**")
            if state.top_k_indices:
                st.metric("Stage 1 Passed", len(state.top_k_indices))

        st.markdown("---")
        st.caption("Built with LangGraph + LLM + Embedding")

    return {
        "use_llm": use_llm,
        "use_embedding": use_embedding,
        "two_stage": two_stage,
        "uploaded_file": uploaded_file,
        "resume_text": resume_text,
        "urls": [u.strip() for u in url_input.strip().split("\n") if u.strip()],
    }


# ── Graph Flow Visualization ─────────────────────────────────────────────

def render_graph_flow(state_any: Any) -> None:
    """Render the 8-node graph flow as a visual pipeline."""
    statuses: dict[str, str] = {}
    if state_any and hasattr(state_any, "statuses"):
        statuses = {k: v.status for k, v in state_any.statuses.items()}

    cols = st.columns(len(GRAPH_NODES))
    for i, (node_key, label, desc) in enumerate(GRAPH_NODES):
        s = statuses.get(node_key, "pending" if not st.session_state.running else "waiting")
        emoji_map = {
            "completed": "✅", "running": "🔄", "failed": "❌",
            "skipped": "⏭️", "pending": "⏳", "waiting": "⚪",
        }
        color_map = {
            "completed": "#1b5e20", "running": "#e65100", "failed": "#b71c1c",
            "skipped": "#4a148c", "pending": "#555", "waiting": "#333",
        }
        emoji = emoji_map.get(s, "⚪")
        bg = color_map.get(s, "#333")

        with cols[i]:
            st.markdown(
                f"<div style='text-align:center; padding:8px 2px; "
                f"background:{bg}22; border-radius:8px; border:1px solid {bg}44;'>"
                f"<div style='font-size:1.5rem'>{emoji}</div>"
                f"<div style='font-weight:600; font-size:0.85rem'>{label}</div>"
                f"<div style='font-size:0.65rem; opacity:0.7'>{s}</div>"
                f"</div>",
                unsafe_allow_html=True,
            )
            if i < len(GRAPH_NODES) - 1:
                st.markdown(
                    f"<div style='text-align:center; color:{bg}88; font-size:1.2rem'>↓</div>",
                    unsafe_allow_html=True,
                )

    # Conditional routing display
    if state_any and hasattr(state_any, "routing_decision") and state_any.routing_decision:
        decision = state_any.routing_decision
        reason = getattr(state_any, "routing_reason", "")
        color = {"match": "#2e7d32", "recrawl": "#e65100", "end": "#b71c1c"}.get(decision, "#555")
        st.markdown(
            f"<div style='text-align:center; padding:4px; "
            f"background:{color}22; border-radius:6px; border-left:4px solid {color};'>"
            f"🧭 Routing Decision: <strong>{decision.upper()}</strong> — {reason}"
            f"</div>",
            unsafe_allow_html=True,
        )


# ── Main Panel ───────────────────────────────────────────────────────────

def render_main(config: dict) -> None:
    """Render the main content area."""
    st.title("🧠 AI Job Matching · Decision Graph")
    st.markdown(
        "LangGraph 8-node pipeline: Crawl → Normalize → Embed → **Route** → Match → Rank → Report"
    )

    # Graph flow visualization (always visible)
    render_graph_flow(st.session_state.state)

    st.markdown("---")

    # Progress
    progress_placeholder = st.empty()
    log_placeholder = st.empty()

    # Action
    col1, col2, col3 = st.columns([2, 1, 2])
    with col2:
        can_run = (
            not st.session_state.running
            and (config["uploaded_file"] or config["resume_text"])
            and config["urls"]
        )
        run_btn = st.button(
            "🚀 Run Graph Pipeline",
            type="primary",
            use_container_width=True,
            disabled=not can_run,
        )

    st.markdown("---")

    # ── Run ──────────────────────────────────────────────────────────
    if run_btn:
        st.session_state.running = True
        st.session_state.progress = []
        st.session_state.graph_log = []
        st.session_state.state = None

        file_path = None
        resume_text_arg = None
        if config["uploaded_file"]:
            upload_dir = Path(__file__).resolve().parent.parent / "data" / "uploads"
            upload_dir.mkdir(parents=True, exist_ok=True)
            file_path = str(upload_dir / config["uploaded_file"].name)
            with open(file_path, "wb") as f:
                f.write(config["uploaded_file"].getbuffer())
        else:
            resume_text_arg = config["resume_text"]

        async def run_graph():
            graph = st.session_state.graph
            graph.on_progress = lambda msg: (
                st.session_state.progress.append(msg)
            )
            state = await graph.run(
                job_urls=config["urls"],
                resume_file_path=file_path,
                resume_text=resume_text_arg,
                use_llm=config["use_llm"],
                use_embedding=config["use_embedding"],
            )
            st.session_state.state = state
            st.session_state.running = False

        with progress_placeholder.container():
            progress_bar = st.progress(0, text="⏳ Starting graph pipeline...")
            status_placeholder = st.empty()
            latest_logs = st.empty()

            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)

            async def run_and_poll():
                task = asyncio.create_task(run_graph())
                node_progress = {
                    "crawl": (1, 0.12), "normalize": (2, 0.22),
                    "resume_embed": (3, 0.35), "routing": (4, 0.40),
                    "match": (5, 0.70), "rank": (6, 0.82),
                    "report": (7, 0.95),
                }
                while not task.done():
                    await asyncio.sleep(0.25)
                    state = st.session_state.state
                    status_text = "⏳ Running..."
                    if state:
                        statuses = {k: v.status for k, v in state.statuses.items()}
                        for node_key, (_, pct) in node_progress.items():
                            if statuses.get(node_key) == "completed":
                                progress_bar.progress(pct, text=f"✅ {node_key} done")
                            elif statuses.get(node_key) == "running":
                                progress_bar.progress(pct, text=f"🔄 {node_key}...")
                            elif statuses.get(node_key) == "failed":
                                progress_bar.progress(pct, text=f"❌ {node_key} failed")

                        if statuses.get("report") == "completed":
                            progress_bar.progress(1.0, text="✅ Pipeline complete!")

                    logs = st.session_state.progress[-6:]
                    latest_logs.markdown("\n".join(f"- {l}" for l in logs))
                    await asyncio.sleep(0)

                await task

            loop.run_until_complete(run_and_poll())

        st.rerun()

    # ── Results ──────────────────────────────────────────────────────
    if st.session_state.state and st.session_state.state.report:
        render_results(
            st.session_state.state.report,
            st.session_state.state.match_results,
            st.session_state.state,
        )
    elif st.session_state.state and not st.session_state.running:
        # Show errors if no report
        state = st.session_state.state
        if state.errors:
            for err in state.errors:
                st.error(err)
        if not state.report and not state.running:
            st.warning("⚠️ Pipeline finished but no report was generated.")


def render_results(report: MatchReport, results: list[MatchResult], graph_state: Any) -> None:
    """Render match results with 2-stage info."""
    st.markdown("## 📊 Matching Results")
    st.markdown("---")

    # Summary row
    c1, c2, c3, c4, c5 = st.columns(5)
    with c1:
        st.metric("Jobs Evaluated", report.total_jobs)
    with c2:
        st.metric("Avg Score", f"{report.average_score:.1%}" if report.average_score else "N/A")
    with c3:
        top_grade = report.results[0].grade() if report.results else "N/A"
        st.metric("Best Grade", top_grade)
    with c4:
        name = report.resume.name or "Unknown"
        st.metric("Candidate", name)
    with c5:
        passed = len(graph_state.top_k_indices) if hasattr(graph_state, "top_k_indices") else 0
        if passed:
            st.metric("Stage 1 → Stage 2", f"{passed} / {report.total_jobs}")
        else:
            st.metric("Stage 2 (LLM)", f"{report.total_jobs} jobs")

    # Routing decision
    if hasattr(graph_state, "routing_decision") and graph_state.routing_decision:
        st.info(f"🧭 Routing: **{graph_state.routing_decision.upper()}** — {graph_state.routing_reason}")

    st.markdown("---")

    # Stage 1 info (if 2-stage)
    if graph_state.stage1_scores:
        with st.expander("📈 Stage 1 — Embedding Filter Results", expanded=False):
            st.caption(f"Top {len(graph_state.stage1_scores)} candidates by semantic similarity")
            for s in graph_state.stage1_scores[:15]:
                st.markdown(f"- `{s.get('title', '?')[:50]}` — semantic: {s.get('semantic_score', 0):.2%}")

    # Top matches
    st.subheader(f"🏆 Top {min(len(report.results), 10)} Matches (Stage 2: LLM Rerank)")

    for i, r in enumerate(report.results[:10]):
        grade_color = {"A": "🟢", "B": "🔵", "C": "🟡", "D": "🔴"}.get(r.grade(), "⚪")

        with st.expander(
            f"{grade_color} **#{i+1}** {r.job.title} @ {r.job.company or 'Unknown'} "
            f"— Score: **{r.score:.1%}**",
            expanded=i == 0,
        ):
            cols = st.columns([3, 2])

            with cols[0]:
                st.markdown(f"**📍 Location:** {r.job.location or 'Remote'}")
                st.markdown(f"**🎯 Grade:** {r.grade()}")
                if r.match_reasons:
                    st.markdown("**✅ Match Reasons:**")
                    for reason in r.match_reasons:
                        st.markdown(f"- {reason}")
                if r.skill_overlap:
                    st.markdown(f"**🧩 Skills Overlap:** {', '.join(r.skill_overlap)}")

            with cols[1]:
                if r.missing_skills:
                    st.markdown("**❌ Missing Skills:**")
                    for s in r.missing_skills:
                        st.markdown(f"- `{s}`")
                if r.gaps:
                    st.markdown("**📈 Gaps:**")
                    for g in r.gaps[:3]:
                        st.markdown(f"- `{g.skill}` ({g.severity})")
                if r.recommendations:
                    st.markdown("**💡 Recommendations:**")
                    for rec in r.recommendations[:3]:
                        st.markdown(f"- {rec}")

            # Score breakdown (2-stage)
            sem = f"{r.semantic_score:.1%}" if r.semantic_score is not None else "N/A"
            llm = f"{r.llm_score:.1%}" if r.llm_score is not None else "N/A"
            st.caption(f"Stage 1 (Semantic): {sem} | Stage 2 (LLM): {llm}")

    st.markdown("---")

    # Export section
    st.subheader("📥 Export Results")

    col1, col2 = st.columns(2)
    export_path = Path(__file__).resolve().parent.parent / "data" / "outputs"
    session_id = st.session_state.state.session_id or "latest"
    report_file = export_path / f"report_{session_id}"

    with col1:
        for fmt, label, mime in [
            (".csv", "📄 Download CSV", "text/csv"),
            (".xlsx", "📊 Download Excel", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"),
        ]:
            p = report_file.with_suffix(fmt)
            if p.exists():
                with open(p, "rb") as f:
                    st.download_button(
                        label, data=f,
                        file_name=f"job_match_report_{session_id}{fmt}",
                        mime=mime, use_container_width=True,
                    )

    with col2:
        for fmt, label, mime in [
            (".md", "📝 Download Markdown", "text/markdown"),
            (".json", "📦 Download JSON", "application/json"),
        ]:
            p = report_file.with_suffix(fmt)
            if p.exists():
                data = p.read_bytes() if fmt == ".json" else p.read_text(encoding="utf-8")
                st.download_button(
                    label, data=data,
                    file_name=f"job_match_report_{session_id}{fmt}",
                    mime=mime, use_container_width=True,
                )

    # Logs
    if st.session_state.progress:
        with st.expander("📜 Pipeline Log"):
            for msg in st.session_state.progress:
                st.text(msg)

    # Errors
    if graph_state.errors:
        with st.expander("⚠️ Errors"):
            for err in graph_state.errors:
                st.error(err)

    # Full table
    with st.expander("📋 Full Comparison Table"):
        import pandas as pd
        table_data = [{
            "Rank": i + 1,
            "Grade": r.grade(),
            "Job Title": r.job.title,
            "Company": r.job.company or "",
            "Location": r.job.location or "",
            "Score": f"{r.score:.1%}",
            "Semantic (S1)": f"{r.semantic_score:.1%}" if r.semantic_score else "N/A",
            "LLM (S2)": f"{r.llm_score:.1%}" if r.llm_score else "N/A",
            "Skills Match": len(r.skill_overlap),
            "Gaps": len(r.missing_skills),
        } for i, r in enumerate(report.results)]
        st.dataframe(pd.DataFrame(table_data), use_container_width=True, hide_index=True)


# ── Entry ────────────────────────────────────────────────────────────────

def main():
    config = render_sidebar()
    render_main(config)


if __name__ == "__main__":
    main()
