"""LangGraph Orchestrator — Decision Graph with Routing Node.

Graph structure:
           ┌────────────┐
           │  CrawlNode  │
           └─────┬───────┘
                 ↓
        ┌─────────────────┐
        │  NormalizeNode   │
        └─────┬───────────┘
              ↓
     ┌──────────────────────┐
     │  ResumeEmbedNode     │
     └─────┬───────────────┘
           ↓
   ┌──────────────────────────┐
   │  RoutingNode (⭐关键)     │
   └─────┬───────────┬────────┘
         ↓           ↓
   MATCH NODE    RECRAWL NODE
         ↓
   RANK NODE
         ↓
   REPORT NODE
         ↓
         END

Key upgrades:
  - Decision routing (skip/recrawl based on data quality)
  - 2-stage matching (embedding filter → LLM rerank)
  - SQLite state persistence
  - Embedding cache across graph nodes
"""

from __future__ import annotations

import asyncio
import json
import time
import traceback
import uuid
from datetime import datetime, timezone
from typing import Any, Callable, Optional

# LangGraph
from langgraph.graph import END, StateGraph

from app.agents import CrawlAgent, MatchingAgent, RankingAgent, ResumeAgent
from app.core.config import AppConfig
from app.core.state import AgentStatus, GraphState
from app.core.state_store import StateStore, restore_session_state
from app.models.job import JobList, JobPosting, JobSource
from app.models.match import MatchResult
from app.models.resume import ResumeProfile
from app.services.embedding_service import EmbeddingService
from app.services.llm_service import LLMService
from app.utils.logger import logger


class DecisionGraph:
    """LangGraph-based decision graph for the job matching pipeline.

    Usage:
        graph = DecisionGraph()
        final_state = await graph.run(job_urls=[...], resume_file_path=...)
    """

    def __init__(self, config: Optional[AppConfig] = None):
        self.config = config or AppConfig()

        # Shared services (single instances reused across all agents)
        self.llm_service = LLMService(
            model=self.config.llm.model,
            api_key=self.config.llm.api_key,
            base_url=self.config.llm.base_url,
            temperature=self.config.llm.temperature,
            max_tokens=self.config.llm.max_tokens,
        )
        self.embedding_service = EmbeddingService(
            provider=self.config.embedding.provider,
            model=self.config.embedding.model,
            api_key=self.config.embedding.api_key,
            base_url=self.config.embedding.base_url,
        )

        # Agents (shared services injected)
        self.crawl_agent = CrawlAgent(
            llm=self.llm_service,
            use_playwright=self.config.crawl.use_playwright,
            timeout=self.config.crawl.timeout,
        )
        self.resume_agent = ResumeAgent(
            llm=self.llm_service,
        )
        self.matching_agent = MatchingAgent(
            llm=self.llm_service,
            embedder=self.embedding_service,
            semantic_weight=self.config.matching.semantic_weight,
            llm_weight=self.config.matching.llm_weight,
            overlap_weight=self.config.matching.overlap_weight,
            stage1_top_k=self.config.matching.stage1_top_k,
            stage1_top_ratio=self.config.matching.stage1_top_ratio,
            stage1_min_score=self.config.matching.stage1_min_score,
        )
        self.ranking_agent = RankingAgent()

        # State store
        self.store = StateStore()

        # Callbacks
        self.on_progress: Optional[Callable[[str], None]] = None
        self.on_status_change: Optional[Callable[[str, str], None]] = None

        # Current graph state (set at start of each node for _emit logging)
        self._current_state: Optional[GraphState] = None

        # Progress tracking for the /tasks endpoint
        self._completed_nodes: set[str] = set()

        # Compiled graph (built lazily)
        self._graph: Optional[Any] = None

        logger.info("DecisionGraph initialized")

    # ═════════════════════════════════════════════════════════════════════
    #  Graph Construction
    # ═════════════════════════════════════════════════════════════════════

    def _build_graph(self) -> StateGraph:
        """Build the LangGraph StateGraph with all nodes and edges."""
        graph = StateGraph(GraphState)

        # ── Nodes ──────────────────────────────────────────────────────
        graph.add_node("crawl", self._node_crawl)
        graph.add_node("normalize", self._node_normalize)
        graph.add_node("resume_embed", self._node_resume_embed)
        graph.add_node("routing", self._node_routing)
        graph.add_node("match", self._node_match)
        graph.add_node("recrawl", self._node_recrawl)
        graph.add_node("rank", self._node_rank)
        graph.add_node("report", self._node_report)

        # ── Edges ──────────────────────────────────────────────────────
        graph.set_entry_point("crawl")
        graph.add_edge("crawl", "normalize")
        graph.add_edge("normalize", "resume_embed")
        graph.add_edge("resume_embed", "routing")

        # Conditional edges from Routing Node
        graph.add_conditional_edges(
            "routing",
            self._router,
            {
                "match": "match",
                "recrawl": "recrawl",
                "refine_crawl": "recrawl",    # ⭐ 新增: 用不同策略重抓
                "ask_user": END,               # ⭐ 新增: 需要人工介入
                "end": END,
            },
        )

        graph.add_edge("recrawl", "normalize")  # loop back

        graph.add_edge("match", "rank")
        graph.add_edge("rank", "report")
        graph.add_edge("report", END)

        logger.info("LangGraph StateGraph built: 8 nodes, conditional routing")
        return graph

    def _router(self, state: GraphState) -> str:
        """Router function — reads routing_decision from state."""
        decision = state.routing_decision or "match"
        logger.debug("Router decision: %s", decision)
        return decision

    # ═════════════════════════════════════════════════════════════════════
    #  Graph Nodes
    # ═════════════════════════════════════════════════════════════════════

    async def _node_crawl(self, state: GraphState) -> dict:
        """CrawlNode: Process manual job texts + crawl URLs.

        Two sources of job data:
          1. manual_job_texts — user-pasted JD text (Boss直聘等受限站点)
          2. job_urls — crawlable URLs (腾讯、字节等)
        Results are merged into a single JobList.
        """
        self._current_state = state
        self._update_status(state, "crawl", "running")

        has_manual = bool(state.manual_job_texts)
        has_urls = bool(state.job_urls)
        sources = []
        if has_manual:
            sources.append("%d manual" % len(state.manual_job_texts))
        if has_urls:
            sources.append("%d URLs" % len(state.job_urls))

        source_label = " + ".join(sources) if sources else "nothing"
        self._emit("🔍 Node 1/8: Processing %s..." % source_label)

        job_list = JobList(session_id=state.session_id)

        # ── Step 1: Process manually pasted job descriptions ────────────
        if has_manual:
            self._emit("📝 Processing %d manually pasted JD(s)..." % len(state.manual_job_texts))
            for i, text in enumerate(state.manual_job_texts):
                text = text.strip()
                if not text:
                    continue
                job = JobPosting(
                    title="[手动输入] 职位 #%d" % (i + 1),
                    description=text,
                    source=JobSource.MANUAL,
                    url=None,
                    company=None,
                    crawled_at=time.strftime("%Y-%m-%dT%H:%M:%S"),
                )
                job_list.add(job)
            self._emit("✅ Added %d manual JD(s)" % job_list.total_count)

        # ── Step 2: Crawl job URLs ─────────────────────────────────────
        if has_urls:
            max_retries = 2
            for attempt in range(1, max_retries + 1):
                try:
                    crawled = await self.crawl_agent.run(
                        urls=state.job_urls,
                        use_llm=state.use_llm,
                        session_id=state.session_id,
                    )
                    # Merge crawled jobs into job_list
                    for j in crawled.jobs:
                        job_list.add(j)
                    self._emit("✅ Crawl: %d jobs from %d URLs" % (crawled.total_count, len(state.job_urls)))
                    break
                except Exception as e:
                    logger.warning("Crawl attempt %d/%d failed: %s", attempt, max_retries, e)
                    if attempt < max_retries:
                        await asyncio.sleep(2 ** attempt)
                        self._emit("🔄 Retry crawl (%d/%d)..." % (attempt, max_retries))
                    else:
                        self._emit("⚠️ URL crawl exhausted after %d attempts" % max_retries)
                        # Non-fatal: manual JDs may still exist

        # ── Finalize ────────────────────────────────────────────────────
        if job_list.total_count == 0:
            self._emit("⚠️ No jobs from either manual input or URL crawl")
            self._update_status(state, "crawl", "completed")
            # Return a valid empty JobList so routing can decide recrawl/end
            self.store.save_state(state.session_id, jobs=job_list)
            return {"crawler_output": job_list}

        job_list.deduplicate()
        self._update_status(state, "crawl", "completed")
        self._emit("✅ CrawlNode complete: %d total jobs (%s)" % (job_list.total_count, source_label))

        # Persist to SQLite
        self.store.save_state(state.session_id, jobs=job_list)

        return {"crawler_output": job_list}

    async def _node_normalize(self, state: GraphState) -> dict:
        """NormalizeNode: Enhance raw job data with LLM parsing (skills, etc.)."""
        self._current_state = state
        self._update_status(state, "normalize", "running")
        self._emit("⚙️  Node 2/8: Normalizing job data...")

        job_list = state.crawler_output
        if not job_list or not job_list.jobs:
            self._emit("⚠️ No jobs to normalize")
            self._update_status(state, "normalize", "skipped")
            return {"normalized_jobs": job_list}

        # If already parsed by LLM during crawl, just pass through
        # Otherwise, run LLM parsing here
        normalized = JobList(session_id=job_list.session_id)

        for job in job_list.jobs:
            # If the job already has skills extracted, keep as-is
            if job.required_skills:
                normalized.add(job)
            elif state.use_llm and job.description:
                # Run LLM parse to enrich
                try:
                    parsed = self.crawl_agent.llm.parse_job(job.description[:4000])
                    enriched = JobPosting(
                        title=parsed.get("title") or job.title,
                        description=job.description,
                        url=job.url,
                        company=parsed.get("company") or job.company,
                        location=job.location,
                        source=job.source,
                        crawled_at=job.crawled_at,
                        required_skills=parsed.get("required_skills", []),
                        preferred_skills=parsed.get("preferred_skills", []),
                        required_experience=parsed.get("required_experience"),
                        responsibilities=parsed.get("responsibilities", []),
                        soft_skills=parsed.get("soft_skills", []),
                    )
                    normalized.add(enriched)
                except Exception as e:
                    logger.debug("Normalize LLM parse failed for %s: %s", job.title, e)
                    normalized.add(job)
            else:
                normalized.add(job)

        self._update_status(state, "normalize", "completed")
        self._emit("✅ Normalized %d jobs" % normalized.total_count)
        return {"normalized_jobs": normalized}

    async def _node_resume_embed(self, state: GraphState) -> dict:
        """ResumeEmbedNode: Parse resume + compute embedding for stage 1 matching.

        If resume is not yet parsed, parse it first.
        Then compute and cache the resume embedding.
        """
        self._current_state = state
        updates: dict[str, Any] = {}
        self._update_status(state, "resume_embed", "running")
        self._emit("📄 Node 3/8: Parsing resume & computing embeddings...")

        # Parse resume if not already done
        resume = state.resume_output
        resume_raw = state.resume_raw_text

        if not resume and (state.resume_file_path or state.resume_text):
            try:
                parsed = await self.resume_agent.run(
                    file_path=state.resume_file_path,
                    text=state.resume_text,
                    use_llm=state.use_llm,
                    session_id=state.session_id,
                )
                resume = parsed
                # Store raw text for embedding and LLM downstream use
                updates["resume_raw_text"] = parsed.raw_text or state.resume_text or ""
                updates["resume_output"] = parsed
                updates["resume_raw_text"] = resume_raw
                self.store.save_state(state.session_id, resume=parsed)
                self._emit("✅ Resume parsed: %s (%d skills)" % (parsed.name or "Unknown", len(parsed.skills)))
            except Exception as e:
                self._update_status(state, "resume_embed", "failed", str(e))
                self._emit("❌ Resume parsing failed: %s" % e)
                # Continue without embedding
                updates["errors"] = state.errors + [str(e)]
                self._update_status(state, "resume_embed", "completed")
                return updates

        if not resume:
            self._emit("⚠️ No resume data — cannot compute embedding")
            self._update_status(state, "resume_embed", "skipped")
            return updates

        # Compute resume embedding for stage 1 matching
        resume_text = MatchingAgent._resume_for_prompt(resume)
        cache_key = "resume"

        # Use cache if available
        if cache_key in state.embedding_cache:
            self._emit("✅ Resume embedding loaded from cache")
            self._update_status(state, "resume_embed", "completed")
            return updates

        try:
            resume_emb = self.matching_agent.embedder.embed(resume_text)
            updates["embedding_cache"] = {**state.embedding_cache, cache_key: resume_emb}
            self.store.save_resume_embedding(state.session_id, resume_emb)
            self._emit("✅ Resume embedding computed (dim=%d)" % len(resume_emb))
        except Exception as e:
            logger.warning("Resume embedding failed (non-fatal): %s", e)
            self._emit("⚠️ Embedding computation failed, proceeding without")

        self._update_status(state, "resume_embed", "completed")
        return updates

    async def _node_routing(self, state: GraphState) -> dict:
        """RoutingNode (⭐关键): LLM + Rule Hybrid Router.

        Step 1: Collect data quality signals.
        Step 2: If LLM router enabled, ask LLM for decision with confidence.
        Step 3: If LLM unavailable or confidence < threshold → fall back to rules.
        Step 4: Return decision with reason and (optionally) refine strategy.
        """
        self._current_state = state
        self._update_status(state, "routing", "running")
        self._emit("🧭 Node 4/8: Hybrid routing decision...")

        jobs = state.normalized_jobs or state.crawler_output
        resume = state.resume_output

        # ═══ Step 1: Collect quality signals ═══════════════════════════════
        total_jobs = jobs.total_count if jobs else 0
        total_urls = len(state.job_urls) if state.job_urls else 0
        resume_exists = resume is not None
        resume_skills = len(resume.skills) if resume else 0
        resume_has_raw = bool(resume and resume.raw_text)
        errors_list = state.errors or []
        fatal_errors = [e for e in errors_list if "fatal" in e.lower() or "critical" in e.lower()]

        # Compute job quality signals
        avg_job_desc_length = 0
        avg_skills_per_job = 0
        if jobs and jobs.jobs:
            descs = [len(j.description or "") for j in jobs.jobs]
            avg_job_desc_length = int(sum(descs) / len(descs)) if descs else 0
            avg_skills_per_job = int(sum(len(j.required_skills or []) for j in jobs.jobs) / len(jobs.jobs)) if jobs.jobs else 0

        quality_signals = {
            "total_urls": total_urls,
            "total_jobs": total_jobs,
            "avg_job_desc_length": avg_job_desc_length,
            "avg_skills_per_job": avg_skills_per_job,
            "resume_exists": resume_exists,
            "resume_skills_count": resume_skills,
            "resume_has_raw_text": resume_has_raw,
            "crawl_error_count": len(errors_list),
            "fatal_errors": len(fatal_errors) > 0,
            "recrawl_remaining": max(0, state.max_recrawl - state.recrawl_count),
            "use_llm": state.use_llm,
        }

        # ═══ Step 2: Try LLM Router ═══════════════════════════════════════
        llm_decision = None
        if self.config.routing.use_llm_router and state.use_llm:
            try:
                llm_decision = self.matching_agent.llm.route(quality_signals)
                self._emit("🤖 LLM Router → %s (conf=%.2f)" % (
                    llm_decision.get("next_action", "?"),
                    llm_decision.get("confidence", 0),
                ))
            except Exception as e:
                logger.warning("LLM Router failed, falling back to rules: %s", e)
                llm_decision = None

        # ═══ Step 3: Validate LLM decision vs rules ═══════════════════════
        decision = "match"
        reason = ""
        confidence = 0.0
        used_llm = False

        if llm_decision:
            llm_action = llm_decision.get("next_action", "match")
            llm_conf = float(llm_decision.get("confidence", 0))
            threshold = self.config.routing.llm_confidence_threshold

            # Cross-check with basic rules
            rule_says = self._rule_based_decision(
                resume_exists=resume_exists,
                resume_skills=resume_skills,
                total_jobs=total_jobs,
                fatal_errors=bool(fatal_errors),
                recrawl_remaining=quality_signals["recrawl_remaining"],
                max_recrawl=state.max_recrawl,
            )

            # Accept LLM if confidence >= threshold AND doesn't violate critical rules
            if llm_conf >= threshold:
                # Critical rule override: no resume → end
                if llm_action != "end" and not resume_exists:
                    decision = "end"
                    reason = "LLM suggested %s but no resume exists — forced end" % llm_action
                    confidence = 1.0
                    used_llm = False
                # Critical rule override: fatal errors + !end → end
                elif llm_action != "end" and fatal_errors:
                    decision = "end"
                    reason = "LLM suggested %s but fatal errors exist — forced end" % llm_action
                    confidence = 1.0
                    used_llm = False
                else:
                    decision = llm_action
                    reason = llm_decision.get("reason", "LLM Router decision")
                    confidence = llm_conf
                    used_llm = True
                    self._emit("✅ LLM Router accepted: %s (conf=%.2f)" % (decision, confidence))
            else:
                # LLM confidence too low → use rule fallback
                self._emit("⚠️ LLM Router confidence too low (%.2f < %.2f), using rules" % (llm_conf, threshold))
                decision = rule_says["decision"]
                reason = rule_says["reason"]
                confidence = 0.5
                used_llm = False
        else:
            # ═══ Step 4: Pure rule fallback ════════════════════════════════
            rule_result = self._rule_based_decision(
                resume_exists=resume_exists,
                resume_skills=resume_skills,
                total_jobs=total_jobs,
                fatal_errors=bool(fatal_errors),
                recrawl_remaining=quality_signals["recrawl_remaining"],
                max_recrawl=state.max_recrawl,
            )
            decision = rule_result["decision"]
            reason = rule_result["reason"]
            confidence = 1.0
            used_llm = False

        # ═══ Emit result ═══════════════════════════════════════════════════
        decision_cn = {"match": "匹配", "recrawl": "重抓", "refine_crawl": "优化重抓", "ask_user": "人工介入", "end": "结束"}.get(decision, decision)
        self._emit("🧭 路由: %s (%s)" % (decision_cn, reason))

        logger.info("Routing decision: %s | conf=%.2f | llm=%s | %s", decision, confidence, used_llm, reason)
        self._update_status(state, "routing", "completed")
        self.store.append_log(state.session_id, "Routing: %s — %s (conf=%.2f, llm=%s)" % (decision, reason, confidence, used_llm))

        return {
            "routing_decision": decision,
            "routing_reason": reason,
            "routing_confidence": confidence,
            "router_llm_used": used_llm,
        }

    def _rule_based_decision(
        self,
        *,
        resume_exists: bool,
        resume_skills: int,
        total_jobs: int,
        fatal_errors: bool,
        recrawl_remaining: int,
        max_recrawl: int = 1,
    ) -> dict:
        """Pure rule-based routing fallback (original if/else logic)."""
        # Case 1: Fatal errors
        if fatal_errors:
            return {"decision": "end", "reason": "存在严重错误 — 终止流程"}

        # Case 2: No resume
        if not resume_exists:
            return {"decision": "end", "reason": "无简历数据 — 无法匹配"}

        # Case 3: No jobs with retries remaining
        if total_jobs == 0 and recrawl_remaining > 0:
            return {"decision": "recrawl", "reason": "未找到职位 — 尝试重抓"}

        # Case 4: No jobs, no retries
        if total_jobs == 0 and recrawl_remaining <= 0:
            return {"decision": "end", "reason": "重试 %d 次后仍无职位 — 终止" % max_recrawl}

        # Case 5: Resume very few skills
        if resume_skills < 3:
            return {"decision": "match", "reason": "简历技能仅 %d 项 — 使用原始文本匹配" % resume_skills}

        # Case 6: Everything ready
        return {"decision": "match", "reason": "%d 个职位 × %d 项技能 — 进入匹配" % (total_jobs, resume_skills)}

    async def _node_recrawl(self, state: GraphState) -> dict:
        """RecrawlNode: Retry failed URLs with different strategy.

        Supports two modes:
          - "recrawl": Standard retry (same settings, different session).
          - "refine_crawl": ⭐ Different strategy (Playwright, alternate parsing).
        """
        self._current_state = state
        is_refine = state.routing_decision == "refine_crawl"
        mode_label = "优化重抓" if is_refine else "重试"
        self._update_status(state, "recrawl", "running")
        self._emit("🔄 Node 5/8: %s (attempt %d/%d)..." % (
            mode_label, state.recrawl_count + 1, state.max_recrawl,
        ))

        urls = state.job_urls
        if not urls:
            self._emit("⚠️ No URLs to recrawl")
            self._update_status(state, "recrawl", "skipped")
            return {"routing_decision": "end"}

        try:
            # ⭐ Refine mode: use Playwright + full LLM parsing
            job_list = await self.crawl_agent.run(
                urls=urls,
                use_llm=True,          # always use LLM for refine
                session_id=state.session_id + ("-refine" if is_refine else "-recrawl"),
            )
            self.store.save_state(state.session_id, jobs=job_list)
            self._emit("✅ %s found %d jobs" % (mode_label, job_list.total_count))
            self._update_status(state, "recrawl", "completed")
            return {
                "crawler_output": job_list,
                "recrawl_count": state.recrawl_count + 1,
            }
        except Exception as e:
            self._update_status(state, "recrawl", "failed", str(e))
            self._emit("❌ Recrawl failed: %s" % e)
            return {
                "errors": state.errors + ["Recrawl failed: %s" % e],
                "recrawl_count": state.recrawl_count + 1,
                "routing_decision": "end",
            }

    async def _node_match(self, state: GraphState) -> dict:
        """MatchNode: 2-stage matching — embedding filter → LLM rerank."""
        self._current_state = state
        self._update_status(state, "match", "running")

        jobs = (state.normalized_jobs or state.crawler_output)
        resume = state.resume_output

        if not resume or not jobs or not jobs.jobs:
            self._emit("⚠️ Cannot match: missing resume or jobs")
            self._update_status(state, "match", "skipped")
            return {}

        job_count = jobs.total_count
        two_stage = self.config.matching.two_stage and job_count > self.config.matching.stage1_top_k
        use_batch = two_stage and job_count >= 3
        stage = "batch-2-stage" if use_batch else ("2-stage" if two_stage else "full-pass")

        self._emit("🧠 Node 6/8: Matching (%s) — %d jobs" % (stage, job_count))

        # Load cached embeddings from state store
        resume_emb_cache = state.embedding_cache.get("resume")
        job_emb_cache: Optional[list[list[float]]] = None

        if state.embedding_cache:
            stored = self.store.load_all_job_embeddings(state.session_id)
            if stored:
                job_emb_cache = [stored.get(i, []) for i in range(job_count)]

        try:
            results = await self.matching_agent.run(
                resume=resume,
                jobs=jobs,
                use_llm=state.use_llm,
                use_embedding=state.use_embedding,
                two_stage=two_stage,
                session_id=state.session_id,
                resume_embedding=resume_emb_cache,
                job_embeddings=job_emb_cache,
                use_batch_llm=True,  # ⭐ 启用批量 LLM 评分
            )

            # Compute stage1 scores for state tracking
            stage1_scores = []
            top_k_indices = []
            for r in results[:self.config.matching.stage1_top_k]:
                stage1_scores.append({
                    "job_index": jobs.jobs.index(r.job) if r.job in jobs.jobs else -1,
                    "title": r.job.title,
                    "semantic_score": r.semantic_score or 0.0,
                })
                top_k_indices.append(jobs.jobs.index(r.job) if r.job in jobs.jobs else -1)

            self._update_status(state, "match", "completed")
            top = results[0] if results else None
            self._emit("✅ Matching complete: %d results | Top: %s (%.1f%%)" % (
                len(results), top.job.title if top else "N/A", top.score * 100 if top else 0,
            ))

            # Persist
            self.store.save_match_results(state.session_id, results)

            return {
                "match_results": results,
                "stage1_scores": stage1_scores,
                "top_k_indices": top_k_indices,
            }

        except Exception as e:
            self._update_status(state, "match", "failed", str(e))
            self._emit("❌ Matching failed: %s" % e)
            return {"errors": state.errors + ["Matching failed: %s" % e]}

    async def _node_rank(self, state: GraphState) -> dict:
        """RankNode: Sort and score all match results."""
        self._current_state = state
        self._update_status(state, "rank", "running")
        self._emit("📊 Node 7/8: Ranking %d results..." % len(state.match_results))

        if not state.match_results:
            self._emit("⚠️ No results to rank")
            self._update_status(state, "rank", "skipped")
            return {}

        try:
            # Sort by score descending
            sorted_results = sorted(state.match_results, key=lambda r: r.score, reverse=True)
            avg_score = round(sum(r.score for r in sorted_results) / len(sorted_results), 4) if sorted_results else 0.0

            self._update_status(state, "rank", "completed")
            self._emit("✅ Ranking done: avg=%.1f%%, top=%s (%.1f%%)" % (
                avg_score * 100,
                sorted_results[0].job.title if sorted_results else "N/A",
                sorted_results[0].score * 100 if sorted_results else 0,
            ))

            return {"match_results": sorted_results}

        except Exception as e:
            self._update_status(state, "rank", "failed", str(e))
            self._emit("❌ Ranking failed: %s" % e)
            return {"errors": state.errors + ["Ranking failed: %s" % e]}

    async def _node_report(self, state: GraphState) -> dict:
        """ReportNode: Generate final report and export files."""
        self._current_state = state
        self._update_status(state, "report", "running")
        self._emit("📋 Node 8/8: Generating reports & exports...")

        resume = state.resume_output
        jobs = state.normalized_jobs or state.crawler_output

        if not state.match_results or not resume:
            self._emit("⚠️ No results to report")
            self._update_status(state, "report", "skipped")
            return {}

        if not jobs:
            jobs = JobList()

        # Always build a report object — export is best-effort
        report: Optional[MatchReport] = None
        try:
            report = await self.ranking_agent.run(
                results=state.match_results,
                resume=resume,
                jobs=jobs,
                session_id=state.session_id,
            )
            self._update_status(state, "report", "completed")
            self._emit("✅ Report generated: avg=%.1f%% | Top: %s" % (
                (report.average_score or 0) * 100,
                report.top_n(1)[0].job.title if report.results else "N/A",
            ))

            # Print summary
            print("\n" + "=" * 60)
            print(report.summary())
            print("=" * 60 + "\n")

            # Persist final status
            self.store.update_session_status(state.session_id, "completed")

        except Exception as e:
            self._emit("⚠️ Export failed (non-fatal): %s" % e)
            logger.warning("Report export failed, building in-memory report: %s", e)
            # Build a minimal report from match_results so UI can still display scores
            if not report:
                from app.models.match import MatchReport
                report = MatchReport(
                    resume=resume,
                    results=sorted(state.match_results, key=lambda r: r.score, reverse=True),
                    total_jobs=len(state.match_results),
                    average_score=round(
                        sum(r.score for r in state.match_results) / len(state.match_results), 4
                    ) if state.match_results else 0.0,
                )
            self._update_status(state, "report", "completed")

        return {"report": report}

    # ═════════════════════════════════════════════════════════════════════
    #  Public entry point
    # ═════════════════════════════════════════════════════════════════════

    async def run(
        self,
        job_urls: list[str],
        resume_file_path: Optional[str] = None,
        resume_text: Optional[str] = None,
        manual_job_texts: Optional[list[str]] = None,
        session_id: Optional[str] = None,
        use_llm: bool = True,
        use_embedding: bool = True,
    ) -> GraphState:
        """Execute the full decision graph pipeline.

        If session_id points to an existing session with persisted data,
        the pipeline resumes from where it left off (crawler_output,
        resume, match_results are restored from the store).

        Args:
            job_urls: Job posting URLs to analyze.
            resume_file_path: Path to resume file.
            resume_text: Raw resume text (alternative).
            manual_job_texts: Manually pasted job descriptions (for sites
                              like Boss直聘 that block crawlers).
            session_id: Optional traceability ID.
            use_llm: Enable LLM-based matching.
            use_embedding: Enable embedding-based matching.

        Returns:
            Final GraphState with all results and logs.
        """
        sid = session_id or self.store.create_session()

        # Reset progress tracking for fresh run
        self._completed_nodes.clear()

        # ── Session resume: restore state from SQLite if available ─────────
        session_exists = self.store.session_exists(sid)
        restored: dict[str, Any] = {}

        if session_exists and session_id:
            restored = restore_session_state(self.store, sid)
            if restored:
                logger.info("Session restored | id=%s | keys=%s", sid, list(restored.keys()))
        elif not session_exists:
            # ⚠️ 必须带上 ``session_id=sid``：调用方显式传入 id 时，
            # 若这里让它自动生成一个新的，``sessions`` 表里就**没有 sid 这一行**，
            # 后续每个节点的 ``update_session_progress(sid, ...)`` 会因查不到行
            # 而静默 return —— 表现为「任务一直是 queued，进度永远不动」。
            self.store.create_session({
                "job_urls": len(job_urls),
                "use_llm": use_llm,
                "use_embedding": use_embedding,
            }, session_id=sid)

        # Build initial state — merge restored data with fresh run params
        initial_state = GraphState(
            job_urls=job_urls,
            resume_file_path=resume_file_path,
            resume_text=resume_text,
            manual_job_texts=manual_job_texts or [],
            session_id=sid,
            use_llm=use_llm,
            use_embedding=use_embedding,
            debug=self.config.debug,
            stage1_top_k=self.config.matching.stage1_top_k,
            stage1_top_ratio=self.config.matching.stage1_top_ratio,
            max_recrawl=self.config.routing.max_recrawl,
        )

        # Apply restored fields (they take precedence over blank defaults)
        for key, val in restored.items():
            if key in ("progress_log",) and val:
                initial_state.progress_log = list(val)
            elif key == "errors" and val:
                initial_state.errors = list(val)
            elif key == "crawler_output" and val:
                initial_state.crawler_output = val
            elif key == "normalized_jobs" and val:
                initial_state.normalized_jobs = val
            elif key == "resume_output" and val:
                initial_state.resume_output = val
            elif key == "match_results" and val:
                initial_state.match_results = val
            elif key == "embedding_cache" and val:
                initial_state.embedding_cache = dict(val)

        logger.info("=" * 60)
        logger.info("DecisionGraph RUN | session=%s | urls=%d | manual=%d | resume=%s | 2-stage=%s | restored=%s",
                      sid, len(job_urls), len(manual_job_texts or []),
                      resume_file_path or "(inline text)",
                      self.config.matching.two_stage, bool(restored))
        logger.info("=" * 60)

        # Build & compile graph
        graph = self._build_graph()
        compiled = graph.compile()

        try:
            # Run the graph
            raw_state = await compiled.ainvoke(initial_state)

            # LangGraph may return a dict (early END via conditional edge)
            # or a GraphState object (full pipeline). Normalize to GraphState.
            if isinstance(raw_state, dict):
                # Merge dict back into a fresh GraphState on top of initial_state
                for key, val in raw_state.items():
                    if hasattr(initial_state, key):
                        setattr(initial_state, key, val)
                final_state = initial_state
            else:
                final_state = raw_state

            logger.info("DecisionGraph DONE | session=%s | statuses=%s",
                          sid, {k: v.status for k, v in final_state.statuses.items()})
            return final_state

        except Exception as e:
            error_msg = "Graph execution failed: %s\n%s" % (e, traceback.format_exc())
            logger.error(error_msg)
            initial_state.errors.append(error_msg)
            self.store.append_error(sid, error_msg)
            self.store.update_session_status(sid, "failed")
            return initial_state

    # ═════════════════════════════════════════════════════════════════════
    #  Sync runner (for thread-pool offloading)
    # ═════════════════════════════════════════════════════════════════════

    def run_sync(
        self,
        job_urls: list[str],
        resume_file_path: Optional[str] = None,
        resume_text: Optional[str] = None,
        manual_job_texts: Optional[list[str]] = None,
        session_id: Optional[str] = None,
        use_llm: bool = True,
        use_embedding: bool = True,
    ) -> GraphState:
        """Synchronous wrapper for ``run()`` — runs in a thread executor.

        Uses ``asyncio.run()`` internally to drive the async pipeline.
        Intended to be called via ``loop.run_in_executor()`` from an async
        handler so the event loop is not blocked by synchronous I/O.

        See ``_run_match_background`` in ``router_v1.py`` for the primary
        call site.
        """
        import asyncio as _asyncio
        return _asyncio.run(self.run(
            job_urls=job_urls,
            resume_file_path=resume_file_path,
            resume_text=resume_text,
            manual_job_texts=manual_job_texts,
            session_id=session_id,
            use_llm=use_llm,
            use_embedding=use_embedding,
        ))

    # ═════════════════════════════════════════════════════════════════════
    #  Status & Progress helpers
    # ═════════════════════════════════════════════════════════════════════

    def _update_status(
        self,
        state: GraphState,
        node_name: str,
        status: str,
        error: Optional[str] = None,
    ) -> None:
        """Update the status of a graph node and push progress to state store."""
        timestamp = datetime.now(timezone.utc).isoformat()
        state.statuses[node_name] = AgentStatus(
            name=node_name,
            status=status,
            error=error,
            started_at=timestamp if status == "running" else state.statuses.get(
                node_name, AgentStatus(name=node_name)
            ).started_at,
            completed_at=timestamp if status in ("completed", "failed") else None,
        )
        if self.on_status_change:
            self.on_status_change(node_name, status)

        # ── Fire-and-forget: push progress to store for the /tasks endpoint ──
        completed = None
        if status in ("completed", "failed", "skipped"):
            self._completed_nodes.add(node_name)
            completed = len(self._completed_nodes)
        try:
            kwargs: dict[str, Any] = {"current_node": node_name, "node_status": status}
            if completed is not None:
                kwargs["completed_count"] = completed
            self.store.update_session_progress(state.session_id, **kwargs)
        except RuntimeError:
            pass  # no running event loop (testing / CLI context)

    def _emit(self, message: str) -> None:
        """Emit a progress message and log to current graph state."""
        logger.info("[Graph] %s", message)
        if self.on_progress:
            self.on_progress(message)
        if self._current_state is not None:
            self._current_state.log(message)

    def get_graph_status(self, state: GraphState) -> dict[str, Any]:
        """Return a snapshot of the current graph status."""
        return {
            "session_id": state.session_id,
            "nodes": {
                name: {"status": s.status, "error": s.error}
                for name, s in state.statuses.items()
            },
            "routing": {
                "decision": state.routing_decision,
                "reason": state.routing_reason,
            },
            "jobs_found": state.crawler_output.total_count if state.crawler_output else 0,
            "jobs_normalized": state.normalized_jobs.total_count if state.normalized_jobs else 0,
            "jobs_matched": len(state.match_results),
            "stage1_passed": len(state.top_k_indices),
            "top_score": state.match_results[0].score if state.match_results else None,
            "embeddings_cached": len(state.embedding_cache),
            "recrawl_count": state.recrawl_count,
            "errors": state.errors,
            "progress": state.progress_log[-15:] if state.progress_log else [],
        }


# ── Backward-compatible alias ──────────────────────────────────────────────

class JobMatchingOrchestrator(DecisionGraph):
    """Legacy alias for DecisionGraph (backward compatible)."""
    pass
