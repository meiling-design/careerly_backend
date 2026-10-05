#! /usr/bin/env python3
"""Job Matching AI Agent — Decision Graph entry point.

Supports:
  CLI:     python main.py --urls URL1 URL2 --resume path/to/resume.pdf
  UI:      streamlit run app/ui/streamlit_app.py
  API:     uvicorn app.api.server:app --host 0.0.0.0 --port 8000

⚠️ 本文件是 **CLI 入口**，不是 API 入口 —— 它没有 `app` 对象，
`uvicorn main:app` 会直接导入失败。FastAPI 应用在 ``app/api/server.py``
（Careerly 契约面全部挂在 ``/api/v1``，见 ``app/careerly/router.py``）。
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from app.core.orchestrator import DecisionGraph, GraphState
from app.utils.logger import logger


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="🧠 Job Matching AI Agent — LangGraph Decision Graph",
    )
    parser.add_argument("--urls", "-u", nargs="+", required=True,
                        help="Job posting URLs to analyze")
    parser.add_argument("--resume", "-r", type=str,
                        help="Path to resume file (PDF/DOCX/TXT)")
    parser.add_argument("--text", "-t", type=str,
                        help="Resume text (alternative to --resume)")
    parser.add_argument("--no-llm", action="store_true",
                        help="Disable LLM-based matching")
    parser.add_argument("--no-embedding", action="store_true",
                        help="Disable embedding-based matching")
    parser.add_argument("--no-two-stage", action="store_true",
                        help="Disable 2-stage matching (use full-pass)")
    parser.add_argument("--verbose", "-v", action="store_true",
                        help="Enable verbose logging")
    parser.add_argument("--session", "-s", type=str,
                        help="Resume a prior session ID from SQLite store")
    return parser.parse_args()


async def main() -> None:
    args = parse_args()

    if not args.resume and not args.text:
        print("❌ Please provide a resume file (--resume) or text (--text)")
        sys.exit(1)

    graph = DecisionGraph()

    print("=" * 60)
    print("🧠 Job Matching AI Agent — Decision Graph")
    print("=" * 60)
    print(f"URLs:       {len(args.urls)}")
    print(f"Resume:     {args.resume or '(inline text)'}")
    print(f"LLM:        {'ON' if not args.no_llm else 'OFF'}")
    print(f"Embedding:  {'ON' if not args.no_embedding else 'OFF'}")
    print(f"2-Stage:    {'ON' if not args.no_two_stage else 'OFF'}")
    if args.session:
        print(f"Session:    {args.session}")
    print("=" * 60)

    state = await graph.run(
        job_urls=args.urls,
        resume_file_path=args.resume,
        resume_text=args.text,
        session_id=args.session,
        use_llm=not args.no_llm,
        use_embedding=not args.no_embedding,
    )

    # Print graph status
    status = graph.get_graph_status(state)
    print("\n📊 Graph Status:")
    print(f"  Session:    {state.session_id}")
    print(f"  Routing:    {state.routing_decision or 'N/A'} — {state.routing_reason or ''}")
    print(f"  Jobs found: {status['jobs_found']}")
    print(f"  Matched:    {status['jobs_matched']}")
    if state.stage1_scores:
        print(f"  Stage 1 → 2: {len(state.top_k_indices)} / {len(state.stage1_scores)} passed embedding filter")
    if state.match_results:
        print(f"  Top score:  {state.match_results[0].score:.1%} — {state.match_results[0].job.title}")

    if state.has_errors:
        print(f"\n⚠️  Pipeline completed with {len(state.errors)} error(s):")
        for err in state.errors:
            print(f"  • {err}")

    if state.report:
        print(f"\n📋 Final Report:")
        print(state.report.summary())
    else:
        print("\n❌ No report was generated.")


if __name__ == "__main__":
    asyncio.run(main())
