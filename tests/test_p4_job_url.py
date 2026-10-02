"""③ 岗位 URL —— ``POST /api/v1/analysis/jobs/parse-url``（抓取回填）。

## 这个端点回答的问题

用户手里常常只有一条**岗位链接**，没有 JD 文本。改造前 Flutter 那条
「粘贴岗位链接」路径把 URL 原样塞进 JD 文本框，再当 ``rawJd`` 发给
``/analysis`` —— 等于把一条网址当招聘描述去解析。

产品侧 2026-10-02 裁决的落点是**抓取回填**，不是让 ``/analysis`` 收 URL：

```text
粘贴链接 → 抓取 → 回填 JD 文本框 + 岗位名 → 用户看得到、改得动 → 原分析链路
```

因此这里断言的重点不是"返回 200"，而是：

```text
① 抓到的 title / company / description 正确落到回填字段
② 链接无效 → 422，且**根本不去抓**（不浪费一次爬取）
③ 抓不到 → 502 显式报错，**不是**静默返回空（D108 的"失败无痕"反面）
④ 只抓到标题、没抓到正文 → 同样报错（否则回填后 JD 框仍是空的）
⑤ 不扣额度、不建分析任务（Baseline §5：这不是分析）
⑥ 未鉴权 → 401
```

运行：
    ./venv/Scripts/python.exe -m pytest tests/test_p4_job_url.py -v
"""

from __future__ import annotations

import pytest

from app.careerly import job_import as job_import_module
from app.models.job import JobList, JobPosting

# 复用 P1-5 / P2 / P3 的夹具：临时库 + 已注册用户。
from test_p1_engine_e2e import (  # noqa: F401
    careerly_store,
    client,
    graph_store,
    workdir,
)

_URL = "https://www.zhipin.com/job/detail/abc123"

_JD_BODY = (
    "高级 Python 后端工程师\n"
    "岗位职责：负责 AI Agent 平台的服务端设计与开发。\n"
    "任职要求：5 年以上 Python 经验，熟悉 FastAPI、PostgreSQL、Docker。"
)


# ── 离线替身：绝不发真实网络请求 ────────────────────────────────────────────


class FakeCrawlAgent:
    """替换 ``job_import.CrawlAgent``。行为由构造时的 ``jobs`` / ``raises`` 决定。"""

    calls: list[list[str]] = []

    def __init__(self, jobs=None, raises: Exception | None = None, **_kwargs):
        self._jobs = jobs if jobs is not None else []
        self._raises = raises

    async def run(self, urls, **_kwargs) -> JobList:
        FakeCrawlAgent.calls.append(list(urls))
        if self._raises is not None:
            raise self._raises
        out = JobList()
        for job in self._jobs:
            out.add(job)
        return out


@pytest.fixture(autouse=True)
def _reset_calls():
    FakeCrawlAgent.calls = []
    yield
    FakeCrawlAgent.calls = []


def _patch_crawler(monkeypatch, *, jobs=None, raises=None) -> None:
    def factory(*_args, **_kwargs):
        return FakeCrawlAgent(jobs=jobs, raises=raises)

    monkeypatch.setattr(job_import_module, "CrawlAgent", factory)


def _register(client) -> dict[str, str]:
    r = client.post(
        "/api/v1/auth/register",
        json={"name": "测试用户", "phone": "13900000001"},
    )
    assert r.status_code == 201, r.text
    return {"Authorization": f"Bearer {r.json()['data']['access_token']}"}


def _user_id(client, headers) -> int:
    r = client.get("/api/v1/auth/me", headers=headers)
    assert r.status_code == 200, r.text
    return int(r.json()["data"]["user"]["id"])


def _parse(client, headers, url: str = _URL):
    return client.post(
        "/api/v1/analysis/jobs/parse-url",
        json={"url": url},
        headers=headers,
    )


# ── 1. 抓取结果落到回填字段 ────────────────────────────────────────────────


def test_parse_url_backfills_title_and_description(client, monkeypatch):
    _patch_crawler(
        monkeypatch,
        jobs=[
            JobPosting(
                title="高级 Python 后端工程师",
                company="某某科技",
                description=_JD_BODY,
                url=_URL,
            )
        ],
    )
    headers = _register(client)

    r = _parse(client, headers)

    assert r.status_code == 200, r.text
    body = r.json()
    assert body["success"] is True
    data = body["data"]
    assert data["url"] == _URL
    assert data["title"] == "高级 Python 后端工程师"
    assert data["company"] == "某某科技"
    assert data["description"] == _JD_BODY
    # 适配器收到的正是规范化后的 URL
    assert FakeCrawlAgent.calls == [[_URL]]


def test_parse_url_normalizes_a_scheme_less_url(client, monkeypatch):
    """``zhipin.com/job/x`` 这类省略协议的输入应被补成 https 后交给爬虫。"""
    _patch_crawler(
        monkeypatch,
        jobs=[JobPosting(title="后端", company="C", description=_JD_BODY)],
    )
    headers = _register(client)

    r = _parse(client, headers, url="www.zhipin.com/job/detail/abc123")

    assert r.status_code == 200, r.text
    assert FakeCrawlAgent.calls[0][0].startswith("https://")


# ── 2. 无效链接：422，且不去抓 ──────────────────────────────────────────────


def test_invalid_url_is_rejected_without_crawling(client, monkeypatch):
    _patch_crawler(monkeypatch, jobs=[])
    headers = _register(client)

    r = _parse(client, headers, url="这不是一个链接")

    assert r.status_code == 422, r.text
    assert r.json()["error"]["code"] == "INVALID_URL"
    # 关键：一次爬取都不该发生
    assert FakeCrawlAgent.calls == []


# ── 3/4. 抓不到 → 显式报错，不是静默空 ──────────────────────────────────────


def test_empty_crawl_is_an_error_not_a_silent_empty(client, monkeypatch):
    """CrawlAgent 对「一个岗位都没抓到」是静默返回空 —— 本端点必须把它升格为错误。"""
    _patch_crawler(monkeypatch, jobs=[])
    headers = _register(client)

    r = _parse(client, headers)

    assert r.status_code == 502, r.text
    assert r.json()["error"]["code"] == "JOB_FETCH_FAILED"


def test_title_only_result_is_an_error(client, monkeypatch):
    """只抓到标题没有正文 → 回填后 JD 框仍空，点分析必失败，故不算成功。"""
    _patch_crawler(
        monkeypatch,
        jobs=[JobPosting(title="后端工程师", company="C", description="")],
    )
    headers = _register(client)

    r = _parse(client, headers)

    assert r.status_code == 502, r.text
    assert r.json()["error"]["code"] == "JOB_FETCH_FAILED"


def test_crawler_crash_becomes_an_error_envelope(client, monkeypatch):
    _patch_crawler(monkeypatch, raises=RuntimeError("playwright 启动失败"))
    headers = _register(client)

    r = _parse(client, headers)

    assert r.status_code == 502, r.text
    assert r.json()["error"]["code"] == "JOB_FETCH_FAILED"
    assert r.json()["success"] is False


# ── 5. 不扣额度、不建任务 ───────────────────────────────────────────────────


def test_parse_url_consumes_no_quota_and_creates_no_task(
    client, careerly_store, monkeypatch
):
    _patch_crawler(
        monkeypatch,
        jobs=[JobPosting(title="后端", company="C", description=_JD_BODY)],
    )
    headers = _register(client)
    user_id = _user_id(client, headers)

    before = careerly_store.get_quota(user_id)
    r = _parse(client, headers)
    after = careerly_store.get_quota(user_id)

    assert r.status_code == 200, r.text
    assert (after.used, after.total) == (before.used, before.total)
    assert careerly_store.list_tasks(user_id) == []


# ── 6. 鉴权 ─────────────────────────────────────────────────────────────────


def test_requires_auth(client, monkeypatch):
    _patch_crawler(
        monkeypatch,
        jobs=[JobPosting(title="后端", company="C", description=_JD_BODY)],
    )
    r = client.post("/api/v1/analysis/jobs/parse-url", json={"url": _URL})
    assert r.status_code == 401, r.text
    assert FakeCrawlAgent.calls == []
