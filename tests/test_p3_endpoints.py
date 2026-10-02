"""P3 —— 聚合读面：career/assets、home/summary、profile。

## 这个文件回答的问题

P1 证明「分析能跑通」，P2 证明「成长被记录」，P3 证明的是：

```text
后端已有的事实，能不能原样喂给「我的」和「首页」，
而**不需要** Flutter 继续吃 Mock、也不需要后端编造数据？
```

三个端点都是只读聚合，因此这里断言的重点不是「端点返回 200」，
而是**每个字段的来源正确、缺失时不伪造、用户之间不串**：

```text
career/assets  ← POST /analysis 落库的简历画像 + 最新能力快照
home/summary   ← 最新分析结果的 Gap actions + 相邻快照的 matchScore diff
profile        ← auth.db users 行（手机号必须脱敏）+ user_quota.is_member
```

复用 `test_p1_engine_e2e` 的假供应商 + 真实 DecisionGraph —— 因为
「简历画像真的从引擎流到了 career/assets」这件事，只有在引擎真跑时才有意义。

运行：
    ./venv/Scripts/python.exe -m pytest tests/test_p3_endpoints.py -v
"""

from __future__ import annotations

from starlette.testclient import TestClient  # noqa: F401  (fixture 注入)

# 复用 P1-5 / P2 的夹具与假供应商：同一套「离线确定性引擎」。
from test_p1_engine_e2e import (  # noqa: F401
    _RAW_JD,
    _RESUME_TEXT,
    _analyze,
    _headers,
    careerly_store,
    client,
    engine_live,
    graph_store,
    workdir,
)
from test_p2_growth import engine_tunable  # noqa: F401  (能力分可编排的 LLM)


def _assets(client: TestClient, headers: dict[str, str]) -> dict:
    r = client.get("/api/v1/career/assets", headers=headers)
    assert r.status_code == 200, r.text
    return r.json()["data"]


def _summary(client: TestClient, headers: dict[str, str]) -> dict:
    r = client.get("/api/v1/home/summary", headers=headers)
    assert r.status_code == 200, r.text
    return r.json()["data"]


def _profile(client: TestClient, headers: dict[str, str]) -> dict:
    r = client.get("/api/v1/profile", headers=headers)
    assert r.status_code == 200, r.text
    return r.json()["data"]


# ── 1. career/assets：真实数据源，而不是空壳 ────────────────────────────────


def test_assets_empty_before_any_analysis(client, careerly_store, graph_store,
                                          engine_live):
    """从未分析 → 空态，不是 404。

    「还没有职业资产」是合法状态：前端据此渲染空态页，
    而 404 会把它渲染成错误页 —— 对一个新用户来说那是错的。
    """
    h = _headers(client, "13900001001")
    data = _assets(client, h)

    assert data["workExperiences"] == []
    assert data["projects"] == []
    assert data["skillGroups"] == []
    assert data["awards"] == []
    assert data["capabilities"] == []
    assert data["spanLabel"] == ""
    assert data["monthlyAdded"] == 0


def test_assets_derive_from_persisted_resume_profile(client, careerly_store,
                                                     graph_store, engine_live):
    """核心断言：引擎解析出的简历画像，真的流到了 career/assets。

    这条覆盖的是 P3-1 最关键的一处改动 —— 在此之前 ``careerly_resumes.profile``
    永远是 ``{}``（router 只写 text），职业资产页只能走 Mock。
    """
    h = _headers(client, "13900001002")
    assert _analyze(client, h).status_code == 201

    data = _assets(client, h)

    # roles[] → workExperiences（title 有，company/period 无来源故为空）
    titles = [w["title"] for w in data["workExperiences"]]
    assert "后端开发工程师" in titles, f"roles 未映射为工作经历：{titles}"
    first = data["workExperiences"][0]
    assert first["company"] == "", "company 无解析来源，不得伪造"
    assert first["period"] == "", "period 无解析来源，不得伪造"

    # projects[] → ProjectExperience
    assert data["projects"], "简历里的项目未映射"
    proj = data["projects"][0]
    assert proj["name"] == "AI Agent 平台"
    assert proj["skills"], "tech_stack 未映射到 skills"
    assert proj["outcome"], "highlights[0] 未映射到 outcome"
    assert proj["year"] == "", "year 无解析来源，不得伪造"

    # skills[] → 单一「专业技能」分组（引擎无更细分类，不编造分类）
    assert data["skillGroups"], "skills 未映射为 skillGroups"
    assert data["skillGroups"][0]["category"] == "专业技能"
    assert "Python" in data["skillGroups"][0]["skills"]

    # spanLabel 来自 experience_years（唯一真实可得的时间信息）
    assert "5" in data["spanLabel"], f"spanLabel 未反映 5 年经验：{data['spanLabel']}"

    # 无来源的字段保持空 —— 这是「不伪造」的可断言形式
    assert data["awards"] == [], "荣誉无数据源，必须为空"
    assert data["monthlyAdded"] == 0, "monthlyAdded 不可推导，必须为 0 而非编造"


def test_assets_capabilities_come_from_snapshot(client, careerly_store,
                                                graph_store, engine_tunable):
    """capabilities 取自最新快照；首次分析 delta=0、previousScore=null。"""
    h = _headers(client, "13900001003")
    assert _analyze(client, h).status_code == 201

    caps = _assets(client, h)["capabilities"]
    assert caps, "能力画像未从快照派生"
    by_name = {c["name"]: c for c in caps}
    assert "云原生" in by_name, f"快照里的维度未出现在 assets：{sorted(by_name)}"
    assert by_name["云原生"]["score"] == 40
    # 只有一条快照 → 没有 previous，delta 必须是 0 而不是编一个数
    assert by_name["云原生"]["delta"] == 0
    assert by_name["云原生"]["previousScore"] is None


def test_assets_are_user_scoped(client, careerly_store, graph_store, engine_live):
    """用户 B 看不到用户 A 的职业资产。"""
    ha = _headers(client, "13900001004")
    hb = _headers(client, "13900001005")
    assert _analyze(client, ha).status_code == 201

    b = _assets(client, hb)
    assert b["workExperiences"] == []
    assert b["projects"] == []
    assert b["capabilities"] == [], "B 不得读到 A 的能力快照"


def test_assets_require_auth(client):
    assert client.get("/api/v1/career/assets").status_code == 401


# ── 2. home/summary ─────────────────────────────────────────────────────────


def test_summary_before_analysis_is_neutral_not_fabricated(client, careerly_store,
                                                           graph_store, engine_live):
    """新用户首页：中性引导文案，而不是编造的「建议」。"""
    h = _headers(client, "13900002001")
    data = _summary(client, h)

    assert data["hasBaseline"] is False
    assert data["suggestions"] == [], "没有分析就不得产出建议"
    assert data["matchChanges"] == []
    assert data["recentGrowth"] == []
    assert data["dailyContent"] is None, "无数据来源，必须为 null 而非占位文案"
    assert "职业基线" in data["headline"]


def test_summary_after_first_analysis(client, careerly_store, graph_store,
                                      engine_tunable):
    """首次分析后：headline 反映真实分数，suggestions 来自真实 Gap。

    ⚠️ 这里断言的是「建议确实由 Gap 派生」，而**不是**某个特定字符串。
    早期版本断言建议里出现 "Kubernetes"，但那是假设「最紧急的 Gap 一定是 K8s」——
    实际排序由引擎的 gap 严重度决定，把测试绑死在某个技能名上会随假供应商
    的文案微调而误报。改为断言：建议非空、且必须是某项 Gap 的 action。
    """
    from test_p2_growth import _CAPS_V1

    h = _headers(client, "13900002002")
    assert _analyze(client, h).status_code == 201

    data = _summary(client, h)

    assert data["hasBaseline"] is True
    assert "分" in data["headline"], f"headline 未包含匹配分：{data['headline']}"
    assert data["subheadline"], "subheadline 不应为空"

    # 建议必须来自真实 Gap 的 actions 集合
    all_actions = {a for cap in _CAPS_V1 for a in cap["actions"]}
    assert data["suggestions"], "有 Gap 却没有产出建议"
    for s in data["suggestions"]:
        assert s in all_actions, f"建议「{s}」不是任何 Gap 的 action（可能是编造的）"
    assert len(data["suggestions"]) <= 3, "首页建议应被节制，不是 Gap 的全文搬运"

    # 首次分析 → 一条「建立职业基线」事件，首页应能看见
    assert any("基线" in g for g in data["recentGrowth"]), \
        f"recentGrowth 未包含基线事件：{data['recentGrowth']}"

    # 只有一条快照 → 没有可比的前一条 → 不得产出 matchChange
    assert data["matchChanges"] == [], "首次分析没有对比基准，不得产出变化"


def test_summary_match_change_after_second_analysis(client, careerly_store,
                                                    graph_store, engine_live):
    """「改简历 → 再分析」后：matchScore 变化必须出现在 matchChanges 里，且带 jobTitle。

    引擎的合成匹配分由简历文本决定（假供应商按技能重合度打分），
    所以让分数真实变化的唯一办法是**换一份重合度不同的简历** ——
    这恰好就是产品的核心场景「用户修改简历后再次分析」。
    """
    from test_p1_engine_e2e import _RESUME_TEXT

    h = _headers(client, "13900002003")
    assert _analyze(client, h).status_code == 201

    # 第二份简历：拿掉 LangGraph → 假供应商的技能重合度下降 → 匹配分必变。
    weaker_resume = _RESUME_TEXT.replace("、LangGraph", "")
    assert weaker_resume != _RESUME_TEXT
    assert _analyze(client, h, title="云原生后端工程师",
                    resumeText=weaker_resume).status_code == 201

    data = _summary(client, h)
    changes = data["matchChanges"]
    assert changes, "两次分析之间分数变了，matchChanges 不应为空"
    ch = changes[0]
    assert ch["jobTitle"], "matchChange 必须带岗位名，否则用户不知道是哪个岗位"
    assert isinstance(ch["delta"], int) and ch["delta"] != 0, \
        f"delta 应是非零整数：{ch}"
    assert ch["score"] > 0

    # 成长也会有非基线事件（能力分随简历变化）
    assert data["recentGrowth"], "第二次分析后应有成长事件"

    # 成长也会有非基线事件
    assert data["recentGrowth"], "第二次分析后应有成长事件"


def test_summary_is_user_scoped(client, careerly_store, graph_store, engine_live):
    ha = _headers(client, "13900002004")
    hb = _headers(client, "13900002005")
    assert _analyze(client, ha).status_code == 201

    b = _summary(client, hb)
    assert b["hasBaseline"] is False
    assert b["recentGrowth"] == [], "B 不得读到 A 的成长事件"


def test_summary_requires_auth(client):
    assert client.get("/api/v1/home/summary").status_code == 401


# ── 3. profile ──────────────────────────────────────────────────────────────


def test_profile_reads_registration_name(client):
    h = _headers(client, "13900003001", name="李四")
    data = _profile(client, h)

    assert data["displayName"] == "李四"
    assert data["membershipLabel"] == "", "非会员不显示徽章（空串，不是编造的标签）"


def test_profile_masks_phone(client):
    """手机号必须脱敏 —— 这个字段会渲染在屏幕上。"""
    h = _headers(client, "13900003002")
    data = _profile(client, h)

    assert data["handle"] == "139****3002", f"未脱敏：{data['handle']}"
    assert "13900003002" not in data["handle"], "完整手机号不得下发"


def test_mask_phone_passes_through_unexpected_shapes():
    """非 11 位数字原样返回，不猜测格式（否则会产出 ``****``）。"""
    from app.careerly.identity import mask_phone

    assert mask_phone("+8613900000000") == "+8613900000000"
    assert mask_phone("123") == "123"
    assert mask_phone(None) == ""
    assert mask_phone("") == ""
    assert mask_phone("13900003003") == "139****3003"


def test_profile_requires_auth(client):
    assert client.get("/api/v1/profile").status_code == 401


# ── 4. 防御性：新端点不得回退 P1/P2 的既有保证 ──────────────────────────────


def test_saving_resume_text_does_not_wipe_profile(client, careerly_store,
                                                  graph_store, engine_live):
    """P3-1 的一个连带修复：``PUT /resume`` 不得把已解析的画像清空。

    改简历文本是高频动作；如果它顺带清掉画像，「我的 → 职业资产」
    会在用户每次粘简历后变空 —— 看起来像丢数据。
    """
    h = _headers(client, "13900004001")
    assert _analyze(client, h).status_code == 201
    assert _assets(client, h)["projects"], "前置条件：画像已落库"

    r = client.put("/api/v1/resume",
                   json={"text": "更新后的简历文本，仍然是 5 年 Python 经验"},
                   headers=h)
    assert r.status_code == 200, r.text

    # 画像必须还在
    assert _assets(client, h)["projects"], "PUT /resume 把已解析画像清空了"
    assert _assets(client, h)["skillGroups"], "skills 也应保留"


def test_growth_endpoints_still_work(client, careerly_store, graph_store,
                                     engine_live):
    """回归：P3 改动不得影响 P2 的成长闭环。"""
    h = _headers(client, "13900004002")
    assert _analyze(client, h).status_code == 201

    r = client.get("/api/v1/growth/events", headers=h)
    assert r.status_code == 200, r.text
    events = r.json()["data"]
    assert len(events) == 1 and events[0]["type"] == "experience"
