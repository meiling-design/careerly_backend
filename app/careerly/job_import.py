"""③ 岗位 URL —— 抓取回填端点（``POST /api/v1/analysis/jobs/parse-url``）。

## 这个端点做什么

用户粘贴一条岗位链接 → 抓取 → 返回 ``{url, title, company, description}``
→ 前端把 ``description`` 填进 JD 文本框、``title`` 填进岗位名
→ **用户看得到、改得动** → 再走原有 ``POST /analysis``。

## 为什么不让 ``POST /analysis`` 直接收 URL（产品侧 2026-10-02 裁决）

```text
① 用户看不到抓到的 JD —— 抓错了无法校正，只会拿到一个莫名其妙的结果；
② 爬取要 30s+（SPA 站点还要起 Playwright），叠加在本已 ~210s 的同步分析上；
③ 契约 §2.1 明文：「后端必须走 GraphState.manual_job_texts 分支构造
   JobPosting，不得要求 URL」。
```

因此 URL 只影响「JD 文本从哪来」，**不改变分析链路**：
``engine.run_analysis`` 的 ``job_urls=[]`` 保持不动。

## 额度与任务

- **不扣额度**（Baseline §5：只有「开始分析」扣）；
- **不建分析任务**（它不是分析 —— 与 ``POST /resume`` 同类）。

## 复用而非重写

抓取能力全部复用 ``app/agents/adapters`` + ``CrawlAgent``，本模块只做两件事：
把 URL 交给 CrawlAgent，把 ``JobPosting`` 压成回填所需的三个字段。
"""

from __future__ import annotations

from app.agents.crawl_agent import CrawlAgent
from app.careerly.schemas import ParsedJob
from app.utils.helpers import validate_url
from app.utils.logger import logger


class JobImportError(Exception):
    """Domain error surfaced to the client as an error envelope."""

    def __init__(self, code: str, message: str, status_code: int = 400):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code


async def fetch_job_from_url(url: str) -> ParsedJob:
    """抓取一条岗位链接，压成回填 JD 文本框所需的最小字段。

    ⚠️ 这是本项目里**唯一**一处「CrawlAgent 返回空 = 失败」的调用点。

    ``CrawlAgent.run`` 会吞掉每个适配器的异常并 ``continue``
    （``crawl_agent.py:123-129``），返回一个空 ``JobList``。对批量爬取那是
    合理的容错；但对「用户贴了一条链接、在等一个结果」的场景，静默返回空
    就是 D108 点名的「失败无痕」——用户会看到一个空表单，不知道发生了什么。

    因此这里把「一个岗位都没抓到」显式升格为错误。代价是无法区分
    「适配器崩了」与「页面本来就没有岗位数据」——从用户视角两者都是
    「这条链接抓不到」，同一条提示即可，且提示里给了可执行的下一步。
    """
    candidate = (url or "").strip()
    normalized = validate_url(candidate) if candidate else None
    if not normalized:
        raise JobImportError(
            "INVALID_URL",
            "请提供以 http:// 或 https:// 开头的有效岗位链接",
            422,
        )

    agent = CrawlAgent()
    job_list = await agent.run(urls=[normalized])

    if job_list.total_count == 0:
        raise JobImportError(
            "JOB_FETCH_FAILED",
            "未能从该链接抓取到岗位信息。请确认链接可公开访问，或直接粘贴 JD 文本",
            502,
        )

    job = job_list.jobs[0]
    description = (job.description or "").strip()
    if not description:
        # 抓到标题却没有正文 —— 回填后用户拿到的 JD 框仍是空的，
        # 点「开始分析」只会撞上「岗位 JD 不能为空」。不把半成品当成功。
        raise JobImportError(
            "JOB_FETCH_FAILED",
            "该链接只抓到了岗位名称、没有正文。请直接粘贴 JD 文本",
            502,
        )

    logger.info(
        "Careerly parse-job-url OK | adapter_jobs=%d | title=%s",
        job_list.total_count, (job.title or "")[:40],
    )
    return ParsedJob(
        url=normalized,
        title=(job.title or "").strip(),
        company=(job.company or "").strip(),
        description=description,
    )
