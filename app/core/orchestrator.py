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
from app.core.state_store import StateStore
from app.models.job import JobList, JobPosting, JobSource
from app.models.match import MatchResult
from app.models.resume import ResumeProfile
from app.utils.logger import logger


class DecisionGraph:
    """LangGraph-based decision graph for the job matching pipeline.

    Usage:
        graph = DecisionGraph()
        final_state = await graph.run(job_urls=[...], resume_file_path=...)
    """

    def __init__(self, config: Optional[AppConfig] = None):
        self.config = config or AppConfig()

        # Agents
        self.crawl_agent = CrawlAgent(
            use_playwright=self.config.crawl.use_playwright,
            timeout=self.config.crawl.timeout,
        )
        self.resume_agent = ResumeAgent()
        self.matching_agent = MatchingAgent(
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
        """CrawlNode: Fetch and parse job listings from URLs."""
        updates: dict[str, Any] = {}
        self._update_status(state, "crawl", "running")
        self._emit("🔍 Node 1/8: Crawling %d URLs..." % len(state.job_urls))

        if not state.job_urls:
            self._emit("⚠️ No job URLs, skipping crawl")
            self._update_status(state, "crawl", "skipped")
            return {"crawler_output": None}

        max_retries = 2
        for attempt in range(1, max_retries + 1):
            try:
                job_list = await self.crawl_agent.run(
                    urls=state.job_urls,
                    use_llm=state.use_llm,
                    session_id=state.session_id,
                )
                self._update_status(state, "crawl", "completed")
                self._emit("✅ Crawl complete: %d jobs from %d URLs" % (job_list.total_count, len(state.job_urls)))

                # Persist to SQLite
                self.store.save_state(state.session_id, jobs=job_list)

                return {"crawler_output": job_list}
            except Exception as e:
                logger.warning("Crawl attempt %d/%d failed: %s", attempt, max_retries, e)
                if attempt < max_retries:
                    await asyncio.sleep(2 ** attempt)
                    self._emit("🔄 Retry crawl (%d/%d)..." % (attempt, max_retries))
                else:
                    self._update_status(state, "crawl", "failed", str(e))
                    self._emit("❌ Crawl failed: %s" % e)
                    return {"errors": state.errors + [str(e)], "crawler_output": None}

        return {}

    async def _node_normalize(self, state: GraphState) -> dict:
        """NormalizeNode: Enhance raw job data with LLM parsing (skills, etc.)."""
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
                        title=job.title,
                        description=job.description,
                        url=job.url,
                        company=job.company,
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
                resume_raw = parsed.raw_text or state.resume_text or ""
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
        """RoutingNode (⭐关键): Decide whether to match, recrawl, or end.

        Decision logic:
          - No jobs at all              → "end"
          - No resume                   → "end" (can't match without profile)
          - Jobs empty after crawl      → "recrawl" (if retries remain)
          - Resume has too few skills   → "match" anyway (use raw text)
          - Jobs + resume ready         → "match"
        """
        self._update_status(state, "routing", "running")
        self._emit("🧭 Node 4/8: Routing decision...")

        decision = "match"
        reason = ""

        jobs = state.normalized_jobs or state.crawler_output
        resume = state.resume_output

        # Case 1: No resume
        if not resume:
            decision = "end"
            reason = "No resume data available — cannot match"
            self._emit("❌ " + reason)

        # Case 2: No jobs at all
        elif not jobs or not jobs.jobs:
            if state.recrawl_count < state.max_recrawl:
                decision = "recrawl"
                reason = "No jobs found — attempting recrawl"
                self._emit("🔄 " + reason)
            else:
                decision = "end"
                reason = "No jobs found after %d recrawl(s)" % state.recrawl_count
                self._emit("❌ " + reason)

        # Case 3: Resume has very few skills (low quality)
        elif resume and len(resume.skills) < self.config.routing.resume_min_skills:
            reason = "Resume has only %d skills — will match using raw text" % len(resume.skills)
            self._emit("⚠️ " + reason)
            decision = "match"  # still proceed, just warn

        # Case 4: Everything ready
        else:
            job_count = jobs.total_count
            skill_count = len(resume.skills) if resume else 0
            reason = "%d jobs × %d skills — proceeding to matching" % (job_count, skill_count)
            self._emit("✅ " + reason)
            decision = "match"

        logger.info("Routing decision: %s | %s", decision, reason)
        self._update_status(state, "routing", "completed")
        self.store.append_log(state.session_id, "Routing: %s — %s" % (decision, reason))

        return {
            "routing_decision": decision,
            "routing_reason": reason,
        }

    async def _node_recrawl(self, state: GraphState) -> dict:
        """RecrawlNode: Retry failed URLs or try alternative parsing."""
        self._update_status(state, "recrawl", "running")
        self._emit("🔄 Node 5/8: Recrawling (attempt %d/%d)..." % (
            state.recrawl_count + 1, state.max_recrawl,
        ))

        # Try with Playwright if we didn't before, or with different settings
        urls = state.job_urls
        if not urls:
            self._emit("⚠️ No URLs to recrawl")
            self._update_status(state, "recrawl", "skipped")
            return {"routing_decision": "end"}

        try:
            job_list = await self.crawl_agent.run(
                urls=urls,
                use_llm=state.use_llm,
                session_id=state.session_id + "-recrawl",
            )
            self.store.save_state(state.session_id, jobs=job_list)
            self._emit("✅ Recrawl found %d jobs" % job_list.total_count)
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
        self._update_status(state, "match", "running")

        jobs = (state.normalized_jobs or state.crawler_output)
        resume = state.resume_output

        if not resume or not jobs or not jobs.jobs:
            self._emit("⚠️ Cannot match: missing resume or jobs")
            self._update_status(state, "match", "skipped")
            return {}

        job_count = jobs.total_count
        two_stage = self.config.matching.two_stage and job_count > self.config.matching.stage1_top_k
        stage = "2-stage" if two_stage else "full-pass"

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

            return {"report": report}

        except Exception as e:
            self._update_status(state, "report", "failed", str(e))
            self._emit("❌ Report generation failed: %s" % e)
            return {"errors": state.errors + ["Report failed: %s" % e]}

    # ═════════════════════════════════════════════════════════════════════
    #  Public entry point
    # ═════════════════════════════════════════════════════════════════════

    async def run(
        self,
        job_urls: list[str],
        resume_file_path: Optional[str] = None,
        resume_text: Optional[str] = None,
        session_id: Optional[str] = None,
        use_llm: bool = True,
        use_embedding: bool = True,
    ) -> GraphState:
        """Execute the full decision graph pipeline.

        Args:
            job_urls: Job posting URLs to analyze.
            resume_file_path: Path to resume file.
            resume_text: Raw resume text (alternative).
            session_id: Optional traceability ID.
            use_llm: Enable LLM-based matching.
            use_embedding: Enable embedding-based matching.

        Returns:
            Final GraphState with all results and logs.
        """
        sid = session_id or self.store.create_session()

        # Ensure session exists in store
        if not self.store.session_exists(sid):
            self.store.create_session({
                "job_urls": len(job_urls),
                "use_llm": use_llm,
                "use_embedding": use_embedding,
            })

        initial_state = GraphState(
            job_urls=job_urls,
            resume_file_path=resume_file_path,
            resume_text=resume_text,
            session_id=sid,
            use_llm=use_llm,
            use_embedding=use_embedding,
            debug=self.config.debug,
            stage1_top_k=self.config.matching.stage1_top_k,
            stage1_top_ratio=self.config.matching.stage1_top_ratio,
            max_recrawl=self.config.routing.max_recrawl,
        )

        logger.info("=" * 60)
        logger.info("DecisionGraph RUN | session=%s | urls=%d | resume=%s | 2-stage=%s",
                      sid, len(job_urls), resume_file_path or "(inline text)",
                      self.config.matching.two_stage)
        logger.info("=" * 60)

        # Build & compile graph
        graph = self._build_graph()
        compiled = graph.compile()

        try:
            # Run the graph
            final_state = await compiled.ainvoke(initial_state)
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
    #  Status & Progress helpers
    # ═════════════════════════════════════════════════════════════════════

    def _update_status(
        self,
        state: GraphState,
        node_name: str,
        status: str,
        error: Optional[str] = None,
    ) -> None:
        """Update the status of a graph node."""
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

    def _emit(self, message: str) -> None:
        """Emit a progress message."""
        logger.info("[Graph] %s", message)
        if self.on_progress:
            self.on_progress(message)

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
