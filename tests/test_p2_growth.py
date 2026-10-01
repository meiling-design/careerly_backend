"""P2 —— 成长时间轴：快照落库 + diff 派生事件 + 只读端点。

## 这个文件回答的问题

P1-5 证明了「分析能跑通」；本文件证明「两次分析之间，成长被看见了」：

```text
分析 #1 → capability_snapshots #1 + 「建立职业基线」事件
分析 #2 → capability_snapshots #2 + diff(#1,#2) 派生的 capability/matchScore 事件
                ↓
GET /api/v1/growth/events | /growth/timeline 按 user 作用域可读
```

## 边界决策（2026-10-01 用户裁决）

* 首次分析 → 一条 ``experience``「建立职业基线」事件（不让新用户足迹为空）
* ``rank`` 事件不生成（一次分析一个岗位，无排名变化语义）
* ``experience`` / ``skill`` 仅基线事件使用；其余来源（职业资产）仍 Mock，不伪造
* 上一快照有、本次消失的维度**静默**（能力维度随 JD 波动，消失 ≠ 退步）

模型供应商复用 test_p1_engine_e2e 的假实现；唯一可调的是
``TunableLLM.caps`` —— 让「两次分析能力分不同」成为可编排的事实。

运行：
    ./venv/Scripts/python.exe -m pytest tests/test_p2_growth.py -v
"""

from __future__ import annotations

import sqlite3

import pytest

from app.careerly import router as careerly_router

# 复用 P1-5 的夹具与假供应商 —— 它们本来就是「离线确定性引擎」的公共件。
from test_p1_engine_e2e import (  # noqa: F401  (fixture 以导入方式注入本模块)
    _RAW_JD,
    _RESUME_TEXT,
    _analyze,
    _headers,
    OfflineEmbeddingService,
    RecordingLLMService,
    careerly_store,
    client,
    graph_store,
    workdir,
)
from app.core import orchestrator as orchestrator_module
from app.services import embedding_service as embedding_module
from app.services import llm_service as llm_module


# ── 可调能力分的假 LLM ──────────────────────────────────────────────────────

_CAPS_V1 = [
    {"name": "工程能力", "yours": 72, "target": 85,
     "why": "岗位要求更强的分布式工程能力", "actions": ["做一次压测复盘"]},
    {"name": "云原生", "yours": 40, "target": 70,
     "why": "简历缺 Kubernetes 生产经验", "actions": ["补一个 K8s 部署项目"]},
    {"name": "AI 工程", "yours": 68, "target": 80,
     "why": "有 LangGraph 编排经验但未成体系", "actions": ["沉淀一套 Agent 编排范式"]},
]


class TunableLLM(RecordingLLMService):
    """``score_capabilities`` 的返回值由类属性 ``caps`` 决定 —— 测试可改。"""

    caps: list[dict] = []

    @classmethod
    def reset(cls) -> None:
        super().reset()
        cls.caps = [dict(c) for c in _CAPS_V1]

    def score_capabilities(self, resume_profile: dict, job_text: str) -> dict:
        self.calls.append(("score_capabilities", (job_text,)))
        return {"capabilities": [dict(c) for c in self.caps]}


@pytest.fixture()
def engine_tunable(monkeypatch):
    """同 P1-5 的 engine_live，但 LLM 换成能力分可调的 TunableLLM。"""
    TunableLLM.reset()
    for module in (llm_module, orchestrator_module):
        monkeypatch.setattr(module, "LLMService", TunableLLM, raising=False)
    for module in (embedding_module, orchestrator_module):
        monkeypatch.setattr(module, "EmbeddingService", OfflineEmbeddingService,
                            raising=False)


def _snapshots(db_path: str) -> list[sqlite3.Row]:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        return conn.execute(
            "SELECT * FROM capability_snapshots ORDER BY created_at, id"
        ).fetchall()
    finally:
        conn.close()


def _events(client, headers, **params):
    return client.get("/api/v1/growth/events", headers=headers, params=params)


# ── 1. 首次分析：基线快照 + 基线事件 ────────────────────────────────────────


def test_first_analysis_writes_baseline_snapshot_and_event(
    client, careerly_store, graph_store, engine_tunable
):
    h = _headers(client, "13900002001")
    assert _analyze(client, h).status_code == 201

    snaps = _snapshots(careerly_store.db_path)
    assert len(snaps) == 1, "每次成功分析必须写一条快照"
    import json
    scores = json.loads(snaps[0]["scores"])
    assert scores["capabilities"], "快照应包含能力分"
    assert scores["capabilities"]["云原生"] == 40
    assert isinstance(scores["matchScore"], int) and scores["matchScore"] > 0
    assert snaps[0]["source"] == "analysis" and snaps[0]["source_id"]

    r = _events(client, h)
    assert r.status_code == 200, r.text
    events = r.json()["data"]
    assert len(events) == 1, "首次分析只产生基线事件（没有上一次可 diff）"
    ev = events[0]
    assert ev["type"] == "experience"
    assert ev["title"] == "建立职业基线"
    assert ev["delta"] is None
    assert ev["occurredAt"], "occurredAt 必须是 ISO 字符串（DateTime.tryParse 要能解析）"


# ── 2. 第二次分析：diff 派生事件 ────────────────────────────────────────────


def test_second_analysis_derives_capability_events(
    client, careerly_store, graph_store, engine_tunable
):
    h = _headers(client, "13900002002")
    _analyze(client, h)

    # 用户成长了：云原生 40 → 48；新掌握了「稳定性工程」
    TunableLLM.caps = [
        dict(_CAPS_V1[0]),
        {**_CAPS_V1[1], "yours": 48},
        dict(_CAPS_V1[2]),
        {"name": "稳定性工程", "yours": 55, "target": 75,
         "why": "新补齐的维度", "actions": ["做一次故障演练"]},
    ]
    assert _analyze(client, h).status_code == 201

    assert len(_snapshots(careerly_store.db_path)) == 2

    events = _events(client, h).json()["data"]
    capability = [e for e in events if e["type"] == "capability"]
    titles = {e["title"] for e in capability}

    assert "云原生能力提升" in titles
    up = next(e for e in capability if e["title"] == "云原生能力提升")
    assert up["delta"] == 8
    assert up["description"] == "40 → 48"

    assert "稳定性工程能力纳入画像" in titles, "新维度应产生「纳入画像」事件"
    new_dim = next(e for e in capability if e["title"] == "稳定性工程能力纳入画像")
    assert new_dim["delta"] is None

    # 分数没变的维度（工程能力 / AI 工程）不得产生事件 —— 成长足迹不是噪音流。
    # 用集合精确判等：「稳定性工程能力纳入画像」里含子串「工程能力」，朴素
    # 的 substring 断言会误伤。
    assert titles == {"云原生能力提升", "稳定性工程能力纳入画像"}


def test_match_score_drop_derives_matchscore_event(
    client, careerly_store, graph_store, engine_tunable
):
    h = _headers(client, "13900002003")
    first = _analyze(client, h).json()["data"]
    score_1 = first["results"][0]["matches"][0]["score"]

    # 第二次分析用了一份更单薄的简历（技能变少 → 匹配分下降）
    thin_resume = "李四，会 Python 和 FastAPI。"
    second = _analyze(client, h, resumeText=thin_resume).json()["data"]
    score_2 = second["results"][0]["matches"][0]["score"]
    assert score_2 != score_1, "测试前提：两份简历的匹配分必须不同"

    events = _events(client, h).json()["data"]
    ms = [e for e in events if e["type"] == "matchScore"]
    assert len(ms) == 1, f"匹配分变化应且只应产生一条事件，实际 {ms}"
    assert ms[0]["delta"] == score_2 - score_1
    assert ms[0]["description"] == f"{score_1} → {score_2}"


# ── 3. 端点行为 ─────────────────────────────────────────────────────────────


def test_type_filter_and_invalid_type(client, careerly_store, graph_store, engine_tunable):
    h = _headers(client, "13900002004")
    _analyze(client, h)
    # ⚠️ capability.normalize 会拒绝少于 3 个维度的评分（C4 校验下限），
    # 所以这里保留 3 个维度、只把云原生 40 → 50。
    TunableLLM.caps = [
        dict(_CAPS_V1[0]),
        {**_CAPS_V1[1], "yours": 50},
        dict(_CAPS_V1[2]),
    ]
    _analyze(client, h)

    only_cap = _events(client, h, type="capability").json()["data"]
    assert only_cap and all(e["type"] == "capability" for e in only_cap)

    bogus = _events(client, h, type="bogus")
    assert bogus.status_code == 422
    assert bogus.json()["error"]["code"] == "INVALID_TYPE", \
        "非法 type 必须显式 422，静默忽略会让前端误以为「没有这类事件」"


def test_timeline_range_filter(client, careerly_store, graph_store, engine_tunable):
    h = _headers(client, "13900002005")
    _analyze(client, h)

    ok_range = client.get("/api/v1/growth/timeline", headers=h, params={
        "from": "2020-01-01T00:00:00Z", "to": "2100-01-01T00:00:00Z",
    })
    assert ok_range.status_code == 200
    assert len(ok_range.json()["data"]) == 1

    future = client.get("/api/v1/growth/timeline", headers=h, params={
        "from": "2100-01-01T00:00:00Z",
    })
    assert future.json()["data"] == []

    bad = client.get("/api/v1/growth/timeline", headers=h, params={"from": "不是日期"})
    assert bad.status_code == 422 and bad.json()["error"]["code"] == "INVALID_RANGE"

    inverted = client.get("/api/v1/growth/timeline", headers=h, params={
        "from": "2100-01-01T00:00:00Z", "to": "2020-01-01T00:00:00Z",
    })
    assert inverted.status_code == 422


def test_growth_events_are_user_scoped(client, careerly_store, graph_store, engine_tunable):
    a = _headers(client, "13900002006", "A")
    b = _headers(client, "13900002007", "B")
    _analyze(client, a)

    assert _events(client, a).json()["data"], "A 应看到自己的基线事件"
    assert _events(client, b).json()["data"] == [], "B 的成长足迹必须为空"
    tl = client.get("/api/v1/growth/timeline", headers=b)
    assert tl.json()["data"] == []


# ── 4. 防御性 ───────────────────────────────────────────────────────────────


def test_failed_analysis_writes_no_snapshot(
    client, careerly_store, graph_store, engine_tunable
):
    h = _headers(client, "13900002008")
    r = client.post("/api/v1/analysis", headers=h,
                    json={"title": "x", "rawJd": _RAW_JD, "resumeText": ""})
    assert r.status_code == 422
    assert _snapshots(careerly_store.db_path) == []
    assert _events(client, h).json()["data"] == []


def test_growth_recording_failure_never_breaks_analysis(
    client, careerly_store, graph_store, engine_tunable, monkeypatch
):
    """成长记录挂了，分析本身必须照样 201 —— C4 `state.resume` 事故的教训。"""
    def boom(*a, **kw):
        raise RuntimeError("快照写入炸了")

    monkeypatch.setattr(careerly_store, "get_latest_snapshot", boom)
    r = _analyze(client, _headers(client, "13900002009"))
    assert r.status_code == 201, r.text
