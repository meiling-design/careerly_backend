#!/usr/bin/env python3
"""Test the full matching pipeline with manual job descriptions."""
import asyncio
import sys

# Fix Windows console encoding
if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8')

from app.core.orchestrator import DecisionGraph

async def test():
    graph = DecisionGraph()
    print("=" * 60)
    print("Testing full pipeline with manual JD...")
    print("=" * 60)

    manual_job = """
公司: 阿里巴巴集团
职位: 高级Python后端工程师
地点: 杭州
职责: 负责高并发分布式后端系统设计与开发，使用Python/FastAPI构建微服务，参与AI Agent系统架构设计
要求: 5年以上Python经验，精通FastAPI/Django，熟悉Docker/K8s，有AI/LLM开发经验
"""

    resume_text = """
姓名: 张三
技能: Python, FastAPI, Django, PostgreSQL, Redis, Docker, Kubernetes, AWS, LangChain, LangGraph
经验: 5年后端开发经验，主导过K8s迁移，构建过AI Agent流水线
"""

    state = await graph.run(
        job_urls=[],
        resume_text=resume_text.strip(),
        manual_job_texts=[manual_job.strip()],
        use_llm=True,
        use_embedding=True,
    )

    status = graph.get_graph_status(state)
    print(f"\nJobs found: {status['jobs_found']}")
    print(f"Routing: {state.routing_decision} -- {state.routing_reason or ''}")
    print(f"Matched: {status['jobs_matched']}")

    if state.match_results:
        for i, r in enumerate(state.match_results[:3]):
            print(f"  {i+1}. {r.job.title} | {r.score:.1%} | {r.grade()}")
    if state.errors:
        for e in state.errors:
            print(f"  Error: {e}")
    print("DONE")

if __name__ == "__main__":
    asyncio.run(test())
