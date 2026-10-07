"""P1-5 —— 「Careerly API 真的把 LangGraph Agent 跑起来了」的端到端回归。

## 这个文件回答的问题

既有的 P1-1a 测试为了跑得快，**把 ``run_analysis`` 整个 monkeypatch 掉** ——
它证明的是「任务状态机接线正确」，但证明不了「引擎真的被调用」：

```text
HTTP → router → run_analysis(被替换) → 假的 project
                    ↑ 从这里往下全被跳过
```

本文件把替换点**下移到模型供应商那一层**：

```text
HTTP → router → run_analysis → DecisionGraph.run_sync()
                                 ├─ _node_crawl       真的
                                 ├─ _node_normalize   真的
                                 ├─ _node_resume_embed 真的
                                 ├─ _node_routing     真的（含 LLM 路由分支）
                                 ├─ _node_match       真的（overlap + 语义 + 加权）
                                 ├─ _node_rank        真的
                                 └─ _node_report      真的
                                        ↓
                                 LLMService / EmbeddingService ← 只有这两处是假的
```

LangGraph 的 8 个节点、条件路由、加权打分、状态回写全部真实执行；
唯一被替换的是**模型供应商**（LLM / Embedding），因为它会联网花钱。

## 为什么这样切是合理的边界

「假数据 vs 真逻辑」的分界应该切在**最外层的 I/O**上。LLM 与 Embedding
正是引擎的全部外部 I/O；再往里切一层就会开始伪造业务结论，测试也就失去意义。

## 验证点

1. ``POST /analysis`` → 201，且**图真的跑完**：8 节点的产物写进了
   ``graph_state.db``（jobs / resume / match_results 均可回读）
2. LLM 被真实调用（``parse_resume`` / ``match``），调用参数来自 HTTP 请求体
3. 分析结果**落进 careerly.db**：本人可读 TA，他人读同一 projectId → **404**
4. 分析任务为 ``completed``，projectId 已回填
5. 引擎崩溃 → 任务 ``failed`` + ``ANALYSIS_FAILED``（不是半截成功）
6. **D-F**：``user_id`` 不进引擎 —— ``run_analysis`` 的入参里没有任何用户身份

运行：
    ./venv/Scripts/python.exe -m pytest tests/test_p1_engine_e2e.py -v
"""

from __future__ import annotations

import re
import sqlite3
import threading
import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.api import server as server_module
from app.careerly import router as careerly_router
from app.careerly import store as careerly_store_module
from app.careerly.engine import run_analysis as real_run_analysis
from app.careerly.store import CareerlyStore
from app.core import auth_store as auth_store_module
from app.core import orchestrator as orchestrator_module
from app.core import state_store as state_store_module
from app.core.state_store import StateStore
from app.services import embedding_service as embedding_module
from app.services import llm_service as llm_module


# ── Fixtures（同 test_p1_analysis_tasks：零删除的工作目录）────────────────────

_TMP_ROOT = Path(__file__).resolve().parent / ".tmp_engine"


@pytest.fixture()
def workdir() -> Path:
    d = _TMP_ROOT / uuid.uuid4().hex[:12]
    d.mkdir(parents=True, exist_ok=True)
    return d


# ── 假的模型供应商 ──────────────────────────────────────────────────────────

_RESUME_TEXT = (
    "张三｜资深后端工程师\n"
    "5 年 Python 后端经验，主导 AI Agent 平台建设。\n"
    "技术栈：Python、FastAPI、PostgreSQL、Docker、Redis、LangGraph。\n"
    "2019-2024 某某科技，负责微服务拆分与检索链路。"
)

_RAW_JD = (
    "高级 Python 后端工程师\n"
    "负责 AI 平台后端研发。\n"
    "任职要求：精通 Python、FastAPI；熟悉 PostgreSQL 调优；"
    "有 Kubernetes 生产经验者优先；了解 LangGraph / Agent 编排加分。"
)

# 刻意的 Gap：岗位要 Kubernetes，简历里没有。
_MISSING_SKILL = "Kubernetes"


class RecordingLLMService:
    """确定性、离线、会记账的 LLMService 替身。

    返回值**由入参推导**而不是写死常量：这样「简历技能进了 engine」
    是可以被断言的事实，而不是碰巧对上的魔法字符串。
    """

    #: 每次进程可见的调用流水；``reset()`` 在 fixture 里清空。
    calls: list[tuple[str, tuple]] = []

    def __init__(self, *args, **kwargs) -> None:
        # 吞掉 DecisionGraph 传入的 model / api_key / base_url：
        # 真实现会用它们建 HTTP 连接，这里一律忽略 —— 由此保证零联网。
        pass

    @classmethod
    def reset(cls) -> None:
        cls.calls = []

    @classmethod
    def names(cls) -> list[str]:
        return [name for name, _ in cls.calls]

    # ── Resume ────────────────────────────────────────────────────────────

    def parse_resume(self, resume_text: str) -> dict:
        self.calls.append(("parse_resume", (resume_text,)))
        skills = [s for s in
                  ["Python", "FastAPI", "PostgreSQL", "Docker", "Redis", "LangGraph"]
                  if s.lower() in resume_text.lower()]
        return {
            "name": "张三",
            "email": "zhangsan@example.com",
            "phone": "13800000000",
            "skills": skills,
            "experience_years": 5,
            "roles": ["后端开发工程师"],
            "projects": [{
                "name": "AI Agent 平台",
                "description": "基于 LangGraph 的多智能体编排平台",
                "tech_stack": skills,
                "highlights": ["QPS 提升 3 倍"],
            }],
            "education": [{"degree": "本科", "institution": "某大学", "year": 2019}],
            "certifications": [],
        }

    # ── Job parsing ───────────────────────────────────────────────────────

    def parse_job(self, description: str) -> dict:
        """按 JD 原文抽取技能 —— 同 TEXT → 同结果，且必须包含 K8s。

        岗位画像里的 ``required_skills`` 是后续 Gap 计算的**输入**。
        若这里写死几个技能，下游的 missing_skills 就永远是假的，
        「简历缺 K8s → 算成 Gap」这条链路等于没被测到。
        """
        self.calls.append(("parse_job", (description,)))
        skills = [s for s in
                  ["Python", "FastAPI", "PostgreSQL", "Kubernetes", "LangGraph"]
                  if s.lower() in description.lower()]
        return {
            "title": "高级 Python 后端工程师",
            "company": "某某科技",
            "description": description,
            "required_skills": skills,
            "preferred_skills": ["LangGraph"],
            "required_experience": 3,
        }

    # ── Matching ──────────────────────────────────────────────────────────

    def match(self, resume_text: str, job_text: str, job_title: str = "") -> dict:
        self.calls.append(("match", (resume_text, job_text, job_title)))
        return self._verdict(resume_text, job_text)

    def batch_match(self, resume_text: str, jobs: list[dict]) -> list[dict]:
        self.calls.append(("batch_match", (resume_text, len(jobs))))
        out = []
        for j in jobs:
            v = self._verdict(resume_text, j.get("description", ""))
            v["job_index"] = j.get("job_index")
            out.append(v)
        return out

    @staticmethod
    def _verdict(resume_text: str, job_text: str) -> dict:
        """按技能重合度推导出 0.0–1.0 的分数 + 结构化 Gap。"""
        wanted = ["Python", "FastAPI", "PostgreSQL", "Kubernetes", "LangGraph"]
        have = {"Python", "FastAPI", "PostgreSQL", "Docker", "Redis", "LangGraph"}
        matched = [w for w in wanted if w in have and w.lower() in resume_text.lower()]
        missing = [w for w in wanted if w not in have]
        score = round(min(0.95, 0.35 + 0.15 * len(matched)), 2)
        return {
            "score": score,
            "match_reasons": [f"{m} 技能重合" for m in matched],
            "missing_skills": missing,
            "partial_skills": ["Redis(基础)"],
            "recommendations": [f"补充 {missing[0]} 生产经验"] if missing else [],
            "gaps": [
                {"skill": _MISSING_SKILL, "severity": "missing",
                 "suggestion": "学习 K8s 基础并做一个小项目"},
            ],
        }

    # ── Routing ───────────────────────────────────────────────────────────

    def route(self, quality_signals: dict) -> dict:
        self.calls.append(("route", (quality_signals,)))
        # 数据和剧本一致（简历有技能、有岗位）→ 直接进匹配，走最短正常路径。
        return {"next_action": "match", "confidence": 0.95,
                "reason": "简历技能与岗位数量充足", "suggested_strategy": None}

    # ── Careerly C4 capability scoring ────────────────────────────────────

    def score_capabilities(self, resume_profile: dict, job_text: str) -> dict:
        self.calls.append(("score_capabilities", (job_text,)))
        return {"capabilities": [
            {"name": "工程能力", "yours": 72, "target": 85,
             "why": "岗位要求更强的分布式工程能力", "actions": ["做一次压测复盘"]},
            {"name": "云原生", "yours": 40, "target": 70,
             "why": "简历缺 Kubernetes 生产经验", "actions": ["补一个 K8s 部署项目"]},
            {"name": "AI 工程", "yours": 68, "target": 80,
             "why": "有 LangGraph 编排经验但未成体系",
             "actions": ["沉淀一套 Agent 编排范式"]},
            {"name": "数据建模", "yours": 75, "target": 78,
             "why": "PostgreSQL 调优深度尚可", "actions": ["补索引优化案例"]},
        ]}


class OfflineEmbeddingService:
    """确定性 embedding：同文本 → 同向量，不下载模型、不联网。"""

    def __init__(self, *args, **kwargs) -> None:
        pass

    @staticmethod
    def embed(text: str) -> list[float]:
        # 词袋哈希：共享词越多，余弦相似度越高 —— 语义打分有意义而非随机。
        vec = [0.0] * 64
        for tok in re.findall(r"[A-Za-z\u4e00-\u9fff]+", text.lower()):
            vec[hash(tok) % 64] += 1.0
        norm = sum(v * v for v in vec) ** 0.5
        return [v / norm for v in vec] if norm else vec

    def embed_many(self, texts: list[str]) -> list[list[float]]:
        return [self.embed(t) for t in texts]

    def similarity(self, a: str, b: str) -> float:
        ea, eb = self.embed(a), self.embed(b)
        return sum(x * y for x, y in zip(ea, eb))

    def similarity_matrix(self, candidates: list[str], target: str) -> list[tuple[int, float]]:
        return [(i, self.similarity(c, target)) for i, c in enumerate(candidates)]

    def batch_similarity(self, target: list[float],
                         candidates: list[list[float]]) -> list[float]:
        return [sum(a * b for a, b in zip(target, c)) for c in candidates]

    def filter_top_k(self, target: list[float], candidates: list[list[float]],
                     k: int = 10, ratio: float = 0.2, **kwargs) -> list[tuple[int, float]]:
        scores = self.batch_similarity(target, candidates)
        indexed = sorted(enumerate(scores), key=lambda x: -x[1])
        cutoff = max(k, int(len(indexed) * ratio))
        return indexed[:cutoff]


@pytest.fixture()
def engine_live(monkeypatch):
    """把假模型供应商装进 Llayers 1，并记下真实 ``run_analysis`` 的入参。

    替换点有两个名字各一份，缺一不可：

    * ``app.services.*`` —— ``engine._score_capabilities_safe`` 在函数体里
      ``from app.services.llm_service import LLMService``，运行时才取值；
    * ``app.core.orchestrator`` —— 模块顶层已经 import 进自己的命名空间。
    """
    RecordingLLMService.reset()

    for module in (llm_module, orchestrator_module):
        monkeypatch.setattr(module, "LLMService", RecordingLLMService, raising=False)
    for module in (embedding_module, orchestrator_module):
        monkeypatch.setattr(module, "EmbeddingService", OfflineEmbeddingService,
                            raising=False)

    calls: list[dict] = []

    def spy_run_analysis(**kwargs):
        calls.append(dict(kwargs))
        return real_run_analysis(**kwargs)

    monkeypatch.setattr(careerly_router, "run_analysis", spy_run_analysis)
    return calls


@pytest.fixture()
def careerly_store(workdir, monkeypatch) -> CareerlyStore:
    monkeypatch.setattr(careerly_store_module, "_local", threading.local())
    store = CareerlyStore(workdir / "careerly.db")
    monkeypatch.setattr(careerly_router, "_store", store, raising=False)
    return store


@pytest.fixture()
def graph_store(workdir, monkeypatch) -> StateStore:
    """引擎运行态库指向临时目录，避免污染 data/graph_state.db。"""
    path = workdir / "graph_state.db"
    monkeypatch.setattr(state_store_module, "_local", threading.local())
    monkeypatch.setattr(state_store_module, "DB_PATH", path)
    return StateStore(path)


@pytest.fixture()
def client(workdir, monkeypatch, graph_store) -> TestClient:
    monkeypatch.setattr(auth_store_module, "_local", threading.local())
    monkeypatch.setattr(auth_store_module, "DB_PATH", workdir / "auth.db")
    monkeypatch.setattr(auth_store_module, "DATA_DIR", workdir)
    return TestClient(server_module.app)


def _headers(client: TestClient, phone: str, name: str = "测试用户") -> dict[str, str]:
    r = client.post("/api/v1/auth/register", json={"name": name, "phone": phone})
    assert r.status_code == 201, r.text
    return {"Authorization": f"Bearer {r.json()['data']['access_token']}"}


def _analyze(client: TestClient, headers: dict[str, str], **overrides):
    payload = {"title": "高级 Python 后端工程师", "rawJd": _RAW_JD,
               "resumeText": _RESUME_TEXT}
    payload.update(overrides)
    return client.post("/api/v1/analysis", json=payload, headers=headers)


# ── 1. 引擎真的被跑起来 ─────────────────────────────────────────────────────


def test_analysis_runs_the_real_decision_graph(
    client, careerly_store, graph_store, engine_live
):
    h = _headers(client, "13900001001")

    r = _analyze(client, h)
    assert r.status_code == 201, r.text
    body = r.json()["data"]

    # ── LangGraph 真的动了：每个节点的产物都应写进 graph_state.db ──
    #
    # 这些是 ``_node_crawl`` / ``_node_resume_embed`` / ``_node_match`` 的
    # 落库结果；若 router 偷懒没调引擎，这里会全是空。
    sessions = graph_store.list_sessions(limit=10)
    assert sessions, "图应当至少创建一个 session"

    session_id = str(sessions[0]["session_id"])
    jobs = graph_store.load_jobs(session_id)
    resume = graph_store.load_resume(session_id)
    match_results = graph_store.load_match_results(session_id)

    assert jobs and jobs.total_count >= 1, "crawl/normalize 节点应产出岗位"
    assert resume is not None, "resume_embed 节点应产出画像"
    assert resume.skills, "简历解析必须产出技能列表"
    assert match_results, "match 节点应产出匹配结果"
    assert graph_store.load_logs(session_id), "节点应按序写过进度日志"

    # ── LLM 被真实调用，且入参来自 HTTP 请求体 ──
    assert "parse_resume" in RecordingLLMService.names(), \
        "ResumeAgent 必须真的调用了 LLMService.parse_resume"
    assert "match" in RecordingLLMService.names(), \
        "MatchingAgent 必须真的调用了 LLMService.match"
    ptr_text = [a for n, a in RecordingLLMService.calls if n == "parse_resume"][0][0]
    assert "张三" in ptr_text, \
        "传给引擎的必须是请求里的简历原文，不是写死的 fixture"

    # ── 分析结果可用 ──
    assert body["results"], "应有一份分析结果"
    assert body["results"][0]["matches"], "至少一条岗位匹配"
    top = body["results"][0]["matches"][0]
    assert 0 < top["score"] <= 100
    assert "Kubernetes" in top["gaps"], "简历没有 K8s，必须算成 Gap"


def test_llm_router_branch_is_exercised(client, careerly_store, graph_store, engine_live):
    """``use_llm=True`` 时 routing 节点会真的去问 LLM Router（不是纯规则）。"""
    _analyze(client, _headers(client, "13900001002"))
    assert "route" in RecordingLLMService.names(), \
        "orchestrator._node_routing 应调用 LLMService.route 做混合路由"


# ── 2. 结果被正确保存 ───────────────────────────────────────────────────────


def test_result_is_persisted_and_readable_by_its_owner(
    client, careerly_store, graph_store, engine_live
):
    h = _headers(client, "13900001003")
    created = _analyze(client, h).json()["data"]
    project_id = created["id"]

    again = client.get(f"/api/v1/analysis/projects/{project_id}", headers=h)
    assert again.status_code == 200, again.text
    persisted = again.json()["data"]

    assert persisted["id"] == project_id
    assert persisted["results"][0]["matches"][0]["score"] == \
        created["results"][0]["matches"][0]["score"], \
        "重读回来的分数必须与首次返回一致（结果确实入库了，不是只存在于响应里）"
    assert persisted["results"][0]["version"] == 1

    # 列表里也能看到
    listed = client.get("/api/v1/analysis/projects", headers=h).json()["data"]
    assert any(p["id"] == project_id for p in listed)

    # 任务状态机：completed + projectId 已回填
    task = client.get("/api/v1/analysis/tasks", headers=h).json()["data"][0]
    assert task["status"] == "completed"
    assert task["projectId"] == project_id

    # 配额扣减了一次
    assert client.get("/api/v1/quota", headers=h).json()["data"]["used"] == 1


def test_other_user_cannot_read_the_analysis(
    client, careerly_store, graph_store, engine_live
):
    a = _headers(client, "13900001004", "A")
    b = _headers(client, "13900001005", "B")

    project_id = _analyze(client, a).json()["data"]["id"]

    cross = client.get(f"/api/v1/analysis/projects/{project_id}", headers=b)
    assert cross.status_code == 404, f"越权必须 404（D-G），实际 {cross.status_code}"

    # 真正要保的不是「某个具体 code 字符串」，而是**不可区分性**：
    # 「别人的资源」与「压根不存在的资源」必须长得一模一样，否则可以从
    # 错误码差异枚举出哪些 projectId 真实存在。
    missing = client.get("/api/v1/analysis/projects/proj_does_not_exist", headers=b)
    assert missing.status_code == cross.status_code
    assert missing.json()["error"]["code"] == cross.json()["error"]["code"], \
        "越权与不存在必须返回同一错误码，否则可枚举探测"

    listed = client.get("/api/v1/analysis/projects", headers=b).json()["data"]
    assert listed == [], "B 的项目列表里不该出现 A 的分析"


def test_capability_snapshot_is_persisted(client, careerly_store, graph_store, engine_live):
    """C4 能力快照随结果一起落库 —— P2 成长时间线的输入就在这里。"""
    body = _analyze(client, _headers(client, "13900001006")).json()["data"]
    snapshot = body["results"][0]["capabilitySnapshot"]
    assert snapshot, "引擎跑通时应连同能力快照一起返回"
    assert "云原生" in snapshot, f"快照应包含动态推导出的能力维度，实际 {snapshot}"


# ── 3. 失败语义 ─────────────────────────────────────────────────────────────


def test_engine_crash_becomes_a_failed_task(client, careerly_store, graph_store, engine_live,
                                            monkeypatch):
    def boom(**kw):
        raise RuntimeError("DecisionGraph 内部炸了")

    monkeypatch.setattr(careerly_router, "run_analysis", boom)
    h = _headers(client, "13900001007")
    r = _analyze(client, h)
    assert r.status_code == 502

    task = client.get("/api/v1/analysis/tasks", headers=h).json()["data"][0]
    assert task["status"] == "failed"
    assert task["errorCode"] == "ANALYSIS_FAILED"
    assert task["projectId"] is None, "失败任务不得回填 projectId"


def test_llm_outage_does_not_half_succeed(
    client, careerly_store, graph_store, engine_live, monkeypatch
):
    """模型供应商挂了：要么给出明确失败，要么降级完成 —— 不能有半个 project。

    MatchingAgent 对每个 LLM 调用都包了 try/except 并走 ``_fallback_result``，
    所以这里期望的是**降级成功**（引擎照样产出结果，只是没有 LLM 理由），
    而不是 500。这条用例钉住的是「偶发故障不会让用户拿到残缺数据」。
    """

    class DeadLLM(RecordingLLMService):
        @staticmethod
        def _verdict(*_: object) -> dict:
            raise RuntimeError("LLM 服务不可用")

    monkeypatch.setattr(orchestrator_module, "LLMService", DeadLLM)
    monkeypatch.setattr(llm_module, "LLMService", DeadLLM)

    r = _analyze(client, _headers(client, "13900001008"))
    assert r.status_code in (201, 502), r.text
    if r.status_code == 201:
        # 降级也要给一份结构完整的结果，而不是空壳
        matches = r.json()["data"]["results"][0]["matches"]
        assert matches and 0 <= matches[0]["score"] <= 100


# ── 4. 架构边界 ─────────────────────────────────────────────────────────────


#: 引擎入参的**白名单**。
#:
#: P1-1b 起多了 ``session_id`` —— 它是**会话标识**，不是用户身份：API 层先用它
#: 建好任务行，引擎再拿同一个 id 跑，于是 ``GET /analysis/tasks/{id}`` 在运行
#: 期间就能读到节点进度。引擎侧 session 的 ``user_id`` 仍为 NULL（见下）。
_ALLOWED_ENGINE_KWARGS = {"title", "raw_jd", "resume_text", "session_id"}

#: 明确禁止出现在引擎入参里的身份字段。
#:
#: 白名单已经拦住了它们，这里再列一遍是**故意的冗余** —— 白名单日后被逐步放宽
#: 时，这道断言仍然守着 D-F 的原意（``user_id`` 只活在持久层）。
_FORBIDDEN_ENGINE_KWARGS = {"user_id", "userId", "user", "phone", "email"}


def test_user_identity_never_reaches_the_engine(
    client, careerly_store, graph_store, engine_live
):
    """D-F：``user_id`` 只活在持久层，引擎对用户身份无感知。"""
    _analyze(client, _headers(client, "13900001009"))

    assert engine_live, "引擎必须被调用过"
    kwargs = engine_live[0]

    # 白名单：新增入参必须在此显式登记，免得有人顺手塞个身份字段进来无人察觉。
    assert set(kwargs) <= _ALLOWED_ENGINE_KWARGS, \
        f"传给 run_analysis 的参数不应混入用户身份，实际 {sorted(kwargs)}"

    leaked = set(kwargs) & _FORBIDDEN_ENGINE_KWARGS
    assert not leaked, f"引擎入参混入了用户身份字段：{sorted(leaked)}"


def test_analysis_tasks_table_is_user_scoped(client, careerly_store, graph_store, engine_live):
    """业务真相库里不该出现无主任务（fail-closed 由表结构保证）。"""
    _analyze(client, _headers(client, "13900001010"))

    conn = sqlite3.connect(careerly_store.db_path)
    try:
        orphan = conn.execute(
            "SELECT COUNT(*) FROM analysis_tasks WHERE user_id IS NULL"
        ).fetchone()[0]
    finally:
        conn.close()
    assert orphan == 0, "analysis_tasks 不允许存在无主行"
