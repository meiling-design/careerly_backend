"""Streamlit 界面 — 职位匹配 AI 决策图仪表板。

功能：
- 8 节点 LangGraph 流程图可视化
- 路由决策展示
- 2 阶段匹配进度（嵌入过滤 → LLM 重排）
- 多格式导出

启动：
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
from app.models.job import JobSource
from app.models.match import MatchReport, MatchResult
from app.utils.logger import logger

# ── 页面配置 ─────────────────────────────────────────────────────────────

st.set_page_config(
    page_title="职位匹配 AI 系统 — 决策图",
    page_icon="🧠",
    layout="wide",
    initial_sidebar_state="expanded",
)

# ── 会话状态 ─────────────────────────────────────────────────────────────

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

# ── 图节点定义（流程图展示）────────────────────────────────────────────────

GRAPH_NODES = [
    ("crawl", "🔍 抓取", "从 URL 抓取职位信息"),
    ("normalize", "⚙️ 标准化", "LLM 字段富化"),
    ("resume_embed", "📄 简历+嵌入", "解析简历并计算向量"),
    ("routing", "🧭 路由", "决策：匹配 / 重抓 / 结束"),
    ("match", "🧠 匹配", "2 阶段：嵌入→LLM 打分"),
    ("rank", "📊 排序", "按综合分数排序"),
    ("report", "📋 报告", "生成导出文件"),
]

# ── 状态映射（中文）──────────────────────────────────────────────────────

STATUS_CN = {
    "completed": "已完成",
    "running": "运行中",
    "failed": "失败",
    "skipped": "已跳过",
    "pending": "等待中",
    "waiting": "排队中",
}

# ── 侧边栏 ───────────────────────────────────────────────────────────────

def render_sidebar() -> dict:
    """渲染侧边栏控件。"""
    with st.sidebar:
        st.title("🧠 职位匹配 AI")
        st.markdown("**决策图** · LangGraph · 8 节点")
        st.markdown("---")

        with st.expander("⚙️ 匹配模式", expanded=True):
            use_llm = st.checkbox("LLM 深度分析", value=True, help="启用大模型进行深度推理")
            use_embedding = st.checkbox("语义嵌入", value=True, help="启用向量相似度计算")
            two_stage = st.checkbox("🚀 2 阶段匹配（嵌入过滤 → LLM 重排）", value=True,
                                     help="阶段 1：快速嵌入过滤（Top 20%）。阶段 2：LLM 深度重排。节省约 80% 成本。")

        st.markdown("---")
        st.subheader("📄 简历")
        uploaded_file = st.file_uploader(
            "上传文件（PDF/DOCX/TXT）", type=["pdf", "docx", "txt"],
            help="支持 PDF、DOCX、TXT 格式",
        )
        resume_text = st.text_area(
            "或粘贴简历文本", height=120,
            placeholder="在此粘贴简历内容...",
            disabled=uploaded_file is not None,
        )

        st.markdown("---")
        st.subheader("🔗 职位链接")
        url_input = st.text_area(
            "每行一个链接", height=120,
            placeholder="https://example.com/job/1",
            help="请输入招聘职位的 URL，每行一个",
        )

        st.markdown("---")
        st.subheader("📝 手动输入职位描述")
        manual_jd_text = st.text_area(
            "粘贴职位内容（支持 Boss直聘等受限站点）",
            height=180,
            placeholder="从 Boss直聘 / 猎聘 / 拉勾等站点复制职位描述粘贴到这里...\n\n多段 JD 请用 --- 分隔",
            help="适用于无法自动抓取的站点：直接复制 JD 文本粘贴到此。\n多段职位描述请用 ---（三个减号）分隔",
        )

        # Parse manual JDs
        manual_job_texts_raw = manual_jd_text.strip()
        manual_job_texts = []
        if manual_job_texts_raw:
            import re
            parts = re.split(r"\n-{3,}\s*\n|\n{3,}", manual_job_texts_raw)
            for p in parts:
                p = p.strip()
                if p and len(p) > 20:
                    manual_job_texts.append(p)

        # Show manual JD count
        if manual_job_texts:
            st.caption(f"📎 已识别 {len(manual_job_texts)} 条职位描述")

        st.markdown("---")
        if st.session_state.state:
            state = st.session_state.state
            sid = (state.session_id or "N/A")[:16]
            st.caption(f"会话: `{sid}...`")
            if state.report:
                st.metric("平均分", f"{state.report.average_score:.1%}" if state.report.average_score else "N/A")
            if state.routing_decision:
                decision_cn = {"match": "匹配", "recrawl": "重抓", "end": "结束"}.get(state.routing_decision, state.routing_decision)
                st.info(f"🧭 路由: **{decision_cn}**")
            if state.top_k_indices:
                st.metric("阶段 1 通过", len(state.top_k_indices))

        st.markdown("---")
        st.caption("基于 LangGraph + LLM + 嵌入构建")

    return {
        "use_llm": use_llm,
        "use_embedding": use_embedding,
        "two_stage": two_stage,
        "uploaded_file": uploaded_file,
        "resume_text": resume_text,
        "urls": [u.strip() for u in url_input.strip().split("\n") if u.strip()],
        "manual_job_texts": manual_job_texts,
    }


# ── 流程图可视化 ─────────────────────────────────────────────────────────

def render_graph_flow(state_any: Any) -> None:
    """渲染 8 节点流程图。"""
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
        status_cn = STATUS_CN.get(s, s)

        with cols[i]:
            st.markdown(
                f"<div style='text-align:center; padding:8px 2px; "
                f"background:{bg}22; border-radius:8px; border:1px solid {bg}44;'>"
                f"<div style='font-size:1.5rem'>{emoji}</div>"
                f"<div style='font-weight:600; font-size:0.85rem'>{label}</div>"
                f"<div style='font-size:0.65rem; opacity:0.7'>{status_cn}</div>"
                f"<div style='font-size:0.6rem; opacity:0.5'>{desc}</div>"
                f"</div>",
                unsafe_allow_html=True,
            )
            if i < len(GRAPH_NODES) - 1:
                st.markdown(
                    f"<div style='text-align:center; color:{bg}88; font-size:1.2rem'>↓</div>",
                    unsafe_allow_html=True,
                )

    # 条件路由展示
    if state_any and hasattr(state_any, "routing_decision") and state_any.routing_decision:
        decision = state_any.routing_decision
        reason = getattr(state_any, "routing_reason", "")
        decision_cn = {"match": "匹配", "recrawl": "重抓", "end": "结束"}.get(decision, decision)
        color = {"match": "#2e7d32", "recrawl": "#e65100", "end": "#b71c1c"}.get(decision, "#555")
        st.markdown(
            f"<div style='text-align:center; padding:4px; "
            f"background:{color}22; border-radius:6px; border-left:4px solid {color};'>"
            f"🧭 路由决策: <strong>{decision_cn}</strong> — {reason}"
            f"</div>",
            unsafe_allow_html=True,
        )


# ── 主面板 ───────────────────────────────────────────────────────────────

def render_main(config: dict) -> None:
    """渲染主内容区域。"""
    st.title("🧠 职位匹配 AI · 决策图")
    st.markdown(
        "LangGraph 8 节点管线：抓取 → 标准化 → 简历+嵌入 → **路由** → 匹配 → 排序 → 报告"
    )

    # 流程图（始终可见）
    render_graph_flow(st.session_state.state)

    st.markdown("---")

    # 进度
    progress_placeholder = st.empty()

    # 操作按钮
    col1, col2, col3 = st.columns([2, 1, 2])
    with col2:
        can_run = (
            not st.session_state.running
            and (config["uploaded_file"] or config["resume_text"])
            and (config["urls"] or config["manual_job_texts"])
        )
        run_btn = st.button(
            "🚀 运行匹配管线",
            type="primary",
            use_container_width=True,
            disabled=not can_run,
        )

    st.markdown("---")

    # ── 运行 ──────────────────────────────────────────────────────────
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
                manual_job_texts=config["manual_job_texts"],
                use_llm=config["use_llm"],
                use_embedding=config["use_embedding"],
            )
            st.session_state.state = state
            st.session_state.running = False

        with progress_placeholder.container():
            progress_bar = st.progress(0, text="⏳ 启动管线...")
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
                node_cn = {
                    "crawl": "抓取", "normalize": "标准化", "resume_embed": "简历+嵌入",
                    "routing": "路由", "match": "匹配", "rank": "排序", "report": "报告",
                }
                while not task.done():
                    await asyncio.sleep(0.25)
                    state = st.session_state.state
                    if state:
                        statuses = {k: v.status for k, v in state.statuses.items()}
                        for node_key, (_, pct) in node_progress.items():
                            cn = node_cn.get(node_key, node_key)
                            if statuses.get(node_key) == "completed":
                                progress_bar.progress(pct, text=f"✅ {cn} 完成")
                            elif statuses.get(node_key) == "running":
                                progress_bar.progress(pct, text=f"🔄 {cn} 进行中...")
                            elif statuses.get(node_key) == "failed":
                                progress_bar.progress(pct, text=f"❌ {cn} 失败")

                        if statuses.get("report") == "completed":
                            progress_bar.progress(1.0, text="✅ 管线运行完毕！")

                    logs = st.session_state.progress[-6:]
                    latest_logs.markdown("\n".join(f"- {l}" for l in logs))
                    await asyncio.sleep(0)

                await task

            loop.run_until_complete(run_and_poll())

        st.rerun()

    # ── 结果显示 ──────────────────────────────────────────────────────
    if st.session_state.state and not st.session_state.running:
        state = st.session_state.state
        match_results = state.match_results

        if state.report:
            # 有完整报告 → 正常渲染
            render_results(
                state.report,
                state,
            )
        elif match_results:
            # 无报告但匹配结果存在 → 直接从 match_results 渲染
            render_results_from_matches(match_results, state)
        else:
            if state.errors:
                for err in state.errors:
                    st.error(err)
            if not match_results:
                st.warning("⚠️ 管线执行完毕，但未生成匹配结果。")


def render_results(report: MatchReport, graph_state: Any) -> None:
    """渲染匹配结果（使用 MatchReport 对象）。"""
    st.markdown("## 📊 匹配结果")
    st.markdown("---")

    # 摘要行
    total_jobs = report.total_jobs or len(report.results)
    avg_score = report.average_score
    top_result = report.results[0] if report.results else None

    c1, c2, c3, c4, c5 = st.columns(5)
    with c1:
        st.metric("职位数量", total_jobs)
    with c2:
        st.metric("平均分", f"{avg_score:.1%}" if avg_score else "N/A")
    with c3:
        top_grade = top_result.grade() if top_result else "N/A"
        grade_cn = {"A": "A 级", "B": "B 级", "C": "C 级", "D": "D 级"}.get(top_grade, top_grade)
        st.metric("最高等级", grade_cn)
    with c4:
        name = report.resume.name or "未知"
        st.metric("候选人", name)
    with c5:
        passed = len(graph_state.top_k_indices) if hasattr(graph_state, "top_k_indices") else 0
        if passed:
            st.metric("阶段 1 → 阶段 2", f"{passed} / {total_jobs}")
        else:
            st.metric("阶段 2（LLM）", f"{total_jobs} 个职位")

    # 路由决策
    if hasattr(graph_state, "routing_decision") and graph_state.routing_decision:
        decision = graph_state.routing_decision
        decision_cn = {"match": "匹配", "recrawl": "重抓", "end": "结束"}.get(decision, decision)
        st.info(f"🧭 路由: **{decision_cn}** — {graph_state.routing_reason}")

    st.markdown("---")

    # 阶段 1 信息
    if graph_state.stage1_scores:
        with st.expander("📈 阶段 1 — 嵌入过滤结果", expanded=False):
            st.caption(f"按语义相似度排名前 {len(graph_state.stage1_scores)} 的候选职位")
            for s in graph_state.stage1_scores[:15]:
                st.markdown(f"- `{s.get('title', '?')[:50]}` — 语义分: {s.get('semantic_score', 0):.2%}")

    # 最佳匹配
    st.subheader(f"🏆 前 {min(len(report.results), 10)} 名（阶段 2：LLM 重排）")

    for i, r in enumerate(report.results[:10]):
        grade_color = {"A": "🟢", "B": "🔵", "C": "🟡", "D": "🔴"}.get(r.grade(), "⚪")
        grade_cn = {"A": "A 级", "B": "B 级", "C": "C 级", "D": "D 级"}.get(r.grade(), r.grade())
        source_badge = "📝 手动" if r.job.source == JobSource.MANUAL else "🔗 爬取"

        with st.expander(
            f"{grade_color} **#{i+1}** {r.job.title} @ {r.job.company or '未知'} "
            f"— 得分: **{r.score:.1%}** | {source_badge}",
            expanded=i == 0,
        ):
            cols = st.columns([3, 2])

            with cols[0]:
                st.markdown(f"**📍 地点:** {r.job.location or '远程'}")
                st.markdown(f"**🎯 等级:** {grade_cn}")
                if r.match_reasons:
                    st.markdown("**✅ 匹配理由:**")
                    for reason in r.match_reasons:
                        st.markdown(f"- {reason}")
                if r.skill_overlap:
                    st.markdown(f"**🧩 技能重合:** {', '.join(r.skill_overlap)}")

            with cols[1]:
                if r.missing_skills:
                    st.markdown("**❌ 缺失技能:**")
                    for s in r.missing_skills:
                        st.markdown(f"- `{s}`")
                if r.gaps:
                    st.markdown("**📈 差距分析:**")
                    for g in r.gaps[:3]:
                        severity_cn = {"missing": "缺失", "weak": "薄弱", "gap": "差距"}.get(g.severity, g.severity)
                        st.markdown(f"- `{g.skill}`（{severity_cn}）")
                if r.recommendations:
                    st.markdown("**💡 建议:**")
                    for rec in r.recommendations[:3]:
                        st.markdown(f"- {rec}")

            # 分数构成
            sem = f"{r.semantic_score:.1%}" if r.semantic_score is not None else "N/A"
            llm = f"{r.llm_score:.1%}" if r.llm_score is not None else "N/A"
            st.caption(f"阶段 1（语义）: {sem} | 阶段 2（LLM）: {llm}")

    st.markdown("---")

    # 导出区
    st.subheader("📥 导出结果")

    col1, col2 = st.columns(2)
    export_path = Path(__file__).resolve().parent.parent / "data" / "outputs"
    session_id = st.session_state.state.session_id or "latest"
    report_file = export_path / f"report_{session_id}"

    with col1:
        for fmt, label, mime in [
            (".csv", "📄 下载 CSV", "text/csv"),
            (".xlsx", "📊 下载 Excel", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"),
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
            (".md", "📝 下载 Markdown", "text/markdown"),
            (".json", "📦 下载 JSON", "application/json"),
        ]:
            p = report_file.with_suffix(fmt)
            if p.exists():
                data = p.read_bytes() if fmt == ".json" else p.read_text(encoding="utf-8")
                st.download_button(
                    label, data=data,
                    file_name=f"job_match_report_{session_id}{fmt}",
                    mime=mime, use_container_width=True,
                )

    # 日志
    if st.session_state.progress:
        with st.expander("📜 管线日志"):
            for msg in st.session_state.progress:
                st.text(msg)

    # 错误
    if graph_state.errors:
        with st.expander("⚠️ 错误信息"):
            for err in graph_state.errors:
                st.error(err)

    # 完整表格
    with st.expander("📋 完整对比表"):
        import pandas as pd
        table_data = [{
            "排名": i + 1,
            "等级": r.grade(),
            "职位": r.job.title,
            "公司": r.job.company or "",
            "地点": r.job.location or "",
            "来源": "📝 手动" if r.job.source == JobSource.MANUAL else "🔗 爬取",
            "综合分": f"{r.score:.1%}",
            "语义分（阶段1）": f"{r.semantic_score:.1%}" if r.semantic_score else "N/A",
            "LLM 分（阶段2）": f"{r.llm_score:.1%}" if r.llm_score else "N/A",
            "技能匹配数": len(r.skill_overlap),
            "差距数": len(r.missing_skills),
        } for i, r in enumerate(report.results)]
        st.dataframe(pd.DataFrame(table_data), use_container_width=True, hide_index=True)


def render_results_from_matches(results: list[MatchResult], graph_state: Any) -> None:
    """直接从 match_results 渲染评分和建议（无 report 对象时的降级方案）。"""
    if not results:
        st.warning("⚠️ 无匹配结果可显示")
        return

    st.markdown("## 📊 匹配结果")
    st.markdown("---")

    # 排序
    sorted_results = sorted(results, key=lambda r: r.score, reverse=True)
    avg_score = sum(r.score for r in sorted_results) / len(sorted_results)
    top = sorted_results[0]

    # 摘要行
    c1, c2, c3, c4, c5 = st.columns(5)
    with c1:
        st.metric("职位数量", len(sorted_results))
    with c2:
        st.metric("平均分", f"{avg_score:.1%}")
    with c3:
        top_grade = top.grade()
        grade_cn = {"A": "A 级", "B": "B 级", "C": "C 级", "D": "D 级"}.get(top_grade, top_grade)
        st.metric("最高等级", grade_cn)
    with c4:
        name = graph_state.resume_output.name if graph_state.resume_output else "未知"
        st.metric("候选人", name)
    with c5:
        passed = len(graph_state.top_k_indices) if hasattr(graph_state, "top_k_indices") else 0
        if passed:
            st.metric("阶段 1 → 阶段 2", f"{passed} / {len(sorted_results)}")
        else:
            st.metric("阶段 2（LLM）", f"{len(sorted_results)} 个职位")

    # 路由决策
    if hasattr(graph_state, "routing_decision") and graph_state.routing_decision:
        decision = graph_state.routing_decision
        decision_cn = {"match": "匹配", "recrawl": "重抓", "end": "结束"}.get(decision, decision)
        st.info(f"🧭 路由: **{decision_cn}** — {graph_state.routing_reason}")

    st.markdown("---")

    # 阶段 1 信息（如有）
    if graph_state.stage1_scores:
        with st.expander("📈 阶段 1 — 嵌入过滤结果", expanded=False):
            st.caption(f"按语义相似度排名前 {len(graph_state.stage1_scores)} 的候选职位")
            for s in graph_state.stage1_scores[:15]:
                st.markdown(f"- `{s.get('title', '?')[:50]}` — 语义分: {s.get('semantic_score', 0):.2%}")

    # 每个匹配结果
    st.subheader(f"🏆 前 {min(len(sorted_results), 10)} 名排名")

    for i, r in enumerate(sorted_results[:10]):
        grade_color = {"A": "🟢", "B": "🔵", "C": "🟡", "D": "🔴"}.get(r.grade(), "⚪")
        grade_cn = {"A": "A 级", "B": "B 级", "C": "C 级", "D": "D 级"}.get(r.grade(), r.grade())
        source_badge = "📝 手动" if r.job.source == JobSource.MANUAL else "🔗 爬取"

        with st.expander(
            f"{grade_color} **#{i+1}** {r.job.title} @ {r.job.company or '未知'} "
            f"— 得分: **{r.score:.1%}** | {source_badge}",
            expanded=i == 0,
        ):
            cols = st.columns([3, 2])

            with cols[0]:
                st.markdown(f"**📍 地点:** {r.job.location or '远程'}")
                st.markdown(f"**🎯 等级:** {grade_cn}")
                if r.match_reasons:
                    st.markdown("**✅ 匹配理由:**")
                    for reason in r.match_reasons:
                        st.markdown(f"- {reason}")
                if r.skill_overlap:
                    st.markdown(f"**🧩 技能重合:** {', '.join(r.skill_overlap)}")

            with cols[1]:
                if r.missing_skills:
                    st.markdown("**❌ 缺失技能:**")
                    for s in r.missing_skills:
                        st.markdown(f"- `{s}`")
                if r.gaps:
                    st.markdown("**📈 差距分析:**")
                    for g in r.gaps[:3]:
                        severity_cn = {"missing": "缺失", "weak": "薄弱", "gap": "差距"}.get(g.severity, g.severity)
                        st.markdown(f"- `{g.skill}`（{severity_cn}）")
                if r.recommendations:
                    st.markdown("**💡 建议:**")
                    for rec in r.recommendations[:3]:
                        st.markdown(f"- {rec}")

            # 分数构成
            sem = f"{r.semantic_score:.1%}" if r.semantic_score is not None else "N/A"
            llm = f"{r.llm_score:.1%}" if r.llm_score is not None else "N/A"
            st.caption(f"阶段 1（语义）: {sem} | 阶段 2（LLM）: {llm}")

    st.markdown("---")

    # 完整表格
    with st.expander("📋 完整对比表"):
        import pandas as pd
        table_data = [{
            "排名": i + 1,
            "等级": r.grade(),
            "职位": r.job.title,
            "公司": r.job.company or "",
            "地点": r.job.location or "",
            "来源": "📝 手动" if r.job.source == JobSource.MANUAL else "🔗 爬取",
            "综合分": f"{r.score:.1%}",
            "语义分（阶段1）": f"{r.semantic_score:.1%}" if r.semantic_score else "N/A",
            "LLM 分（阶段2）": f"{r.llm_score:.1%}" if r.llm_score else "N/A",
            "技能匹配数": len(r.skill_overlap),
            "差距数": len(r.missing_skills),
        } for i, r in enumerate(sorted_results)]
        st.dataframe(pd.DataFrame(table_data), use_container_width=True, hide_index=True)

    # 错误
    if graph_state.errors:
        with st.expander("⚠️ 错误信息"):
            for err in graph_state.errors:
                st.error(err)

    # 日志
    if st.session_state.progress:
        with st.expander("📜 管线日志"):
            for msg in st.session_state.progress:
                st.text(msg)


# ── 入口 ─────────────────────────────────────────────────────────────────

def main():
    config = render_sidebar()
    render_main(config)


if __name__ == "__main__":
    main()
