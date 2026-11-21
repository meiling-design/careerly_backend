"""P1-03 / P1-04 / P1-05 / P1-06 —— 成长比较、额度预占、任务状态与恢复。

本文件尽量在 **store / growth 层**验证不变式，不依赖真实引擎：

* 成长比较：同一岗位 + 同一 JD + 不同简历才允许生成匹配度事件；
  不同岗位 / 不同 JD / 不同评分口径 → 不生成差值事件。
* 额度：预占 → 确认 / 释放，幂等，并发只有一个赢家，重启恢复不重复扣费。
* 任务状态：取消后完成不降级、不重复扣额；失败不扣费。
* 恢复：启动时把遗留 queued/processing 标为 TASK_INTERRUPTED 并释放额度。

运行：
    ./venv/Scripts/python.exe -m pytest tests/test_p1_growth_quota_recovery.py -v
"""

from __future__ import annotations

import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from app.careerly import growth
from app.careerly.capability import SCORING_VERSION
from app.careerly.engine import build_project
from app.careerly.schemas import AnalysisResult, JobMatch, JobSnapshot
from app.careerly.store import CareerlyStore
from app.careerly import store as careerly_store_module
from app.models.job import JobPosting, JobSource
from app.models.match import MatchResult

_TMP_ROOT = Path(__file__).resolve().parent / ".tmp_p1_growth_quota"


@pytest.fixture()
def workdir() -> Path:
    d = _TMP_ROOT / uuid.uuid4().hex[:12]
    d.mkdir(parents=True, exist_ok=True)
    return d


@pytest.fixture()
def store(workdir, monkeypatch) -> CareerlyStore:
    monkeypatch.setattr(careerly_store_module, "_local", threading.local())
    return CareerlyStore(workdir / "careerly.db")


def _project(
    *,
    title: str = "AI 产品经理",
    raw_jd: str = "JD A",
    score: int = 78,
    capabilities: dict[str, int] | None = None,
):
    job = JobPosting(
        title=title,
        description=raw_jd,
        company="示例公司",
        required_skills=["Python"],
        source=JobSource.MANUAL,
    )
    project = build_project(
        title=title,
        raw_jd=raw_jd,
        match_results=[MatchResult(job=job, score=score / 100, skill_overlap=["Python"])],
        capabilities=[
            {"name": name, "yours": value, "target": 90, "why": None, "actions": []}
            for name, value in (capabilities or {"工程能力": 70}).items()
        ],
    )
    # 与 router 的成功路径一致：把 JD 原文快照挂到结果上。
    project.results[0].jobSnapshots = [
        JobSnapshot(
            jobId=project.jobs[0].id,
            title=title,
            company="示例公司",
            rawJd=raw_jd,
            source="paste",
            importMethod="paste",
            capturedAt=project.createdAt,
        )
    ]
    return project


def _scores(project, resume_text: str) -> dict:
    return growth.build_scores_payload(project, resume_text=resume_text)


# ── P1-03 A：职业能力变化 ──────────────────────────────────────────────────


def test_capability_delta_allowed_for_same_scoring_version():
    prev = _scores(_project(capabilities={"云原生": 70}), "简历 V1")
    new = _scores(_project(capabilities={"云原生": 78}), "简历 V2")
    events = growth.diff_snapshots(
        {"scores": prev}, new, "AI 产品经理", 123.0,
    )
    cap = [e for e in events if e["type"] == "capability"]
    assert cap and cap[0]["title"] == "云原生能力提升"
    assert cap[0]["delta"] == 8


def test_capability_delta_rejected_when_scoring_version_is_incompatible():
    prev = _scores(_project(capabilities={"云原生": 70}), "简历 V1")
    prev_no_version = {k: v for k, v in prev.items() if k != "scoringVersion"}
    new = _scores(_project(capabilities={"云原生": 78}), "简历 V2")

    events = growth.diff_snapshots(
        {"scores": prev_no_version}, new, "AI 产品经理", 123.0,
    )
    assert [e for e in events if e["type"] == "capability"] == [], \
        "旧快照没有评分口径，不得比较能力分"


# ── P1-03 B：目标岗位匹配度变化 ─────────────────────────────────────────────


def test_match_score_event_requires_same_job_same_jd_different_resume():
    prev = _scores(_project(score=78), "简历 V1")
    new = _scores(_project(score=82), "简历 V2")
    events = growth.diff_snapshots({"scores": prev}, new, "AI 产品经理", 1.0)
    ms = [e for e in events if e["type"] == "matchScore"]
    assert len(ms) == 1 and ms[0]["delta"] == 4


def test_no_match_score_event_for_different_job():
    """P1-03 的核心反例：AI 产品经理 78 → AI 产品运营 82 不是 +4。"""
    prev = _scores(_project(title="AI 产品经理", score=78), "简历 V1")
    new = _scores(_project(title="AI 产品运营", score=82), "简历 V2")
    events = growth.diff_snapshots({"scores": prev}, new, "AI 产品运营", 1.0)
    assert [e for e in events if e["type"] == "matchScore"] == []


def test_no_match_score_event_for_different_jd_content():
    prev = _scores(_project(raw_jd="JD 版本 A", score=78), "简历 V1")
    new = _scores(_project(raw_jd="JD 版本 B", score=82), "简历 V2")
    events = growth.diff_snapshots({"scores": prev}, new, "AI 产品经理", 1.0)
    assert [e for e in events if e["type"] == "matchScore"] == []


def test_no_match_score_event_without_resume_change():
    prev = _scores(_project(score=78), "同一份简历")
    new = _scores(_project(score=82), "同一份简历")
    events = growth.diff_snapshots({"scores": prev}, new, "AI 产品经理", 1.0)
    assert [e for e in events if e["type"] == "matchScore"] == [], \
        "简历没换，分数波动不应当成用户成长"


def test_no_match_score_event_for_legacy_snapshot():
    prev = _scores(_project(score=78), "简历 V1")
    legacy_prev = {"capabilities": prev["capabilities"], "matchScore": prev["matchScore"]}
    new = _scores(_project(score=82), "简历 V2")
    events = growth.diff_snapshots({"scores": legacy_prev}, new, "AI 产品经理", 1.0)
    assert events == [], "没有可追溯元数据的旧快照不生成任何差值事件"


def test_first_analysis_still_creates_baseline_event():
    new = _scores(_project(), "简历 V1")
    events = growth.diff_snapshots(None, new, "AI 产品经理", 1.0)
    assert len(events) == 1 and events[0]["type"] == "experience"


# ── P1-01：结果页「较上次」只由后端确认可比（annotate_match_previous_scores）──


def _result(
    *,
    version: int,
    score: int,
    resume_fp: str | None,
    scoring: str | None = SCORING_VERSION,
    title: str = "AI 产品经理",
    company: str = "示例公司",
    raw_jd: str = "JD A",
    job_id: str | None = None,
):
    """一条分析结果 + 一个岗位 + 它的 JD 快照（与 store 读取形态一致）。"""
    job_id = job_id or f"job-{version}"
    return AnalysisResult(
        id=f"res-{version}",
        projectId="project-1",
        version=version,
        matches=[JobMatch(jobId=job_id, score=score, rank=1)],
        jobSnapshots=[
            JobSnapshot(jobId=job_id, title=title, company=company, rawJd=raw_jd)
        ],
        scoringVersion=scoring,
        resumeFingerprint=resume_fp,
    )


def test_previous_score_filled_for_same_job_same_jd_different_resume():
    prev = _result(version=1, score=78, resume_fp=growth.fingerprint("简历 V1"))
    new = _result(version=2, score=82, resume_fp=growth.fingerprint("简历 V2"))
    results = [prev, new]
    growth.annotate_match_previous_scores(results)
    assert prev.matches[0].previousScore is None
    assert new.matches[0].previousScore == 78


def test_previous_score_hidden_for_different_job():
    """跨岗位：AI 产品经理 78 → AI 产品运营 82 不是可比的「较上次」。"""
    prev = _result(
        version=1, score=78, resume_fp=growth.fingerprint("简历 V1"),
        title="AI 产品经理",
    )
    new = _result(
        version=2, score=82, resume_fp=growth.fingerprint("简历 V2"),
        title="AI 产品运营",
    )
    growth.annotate_match_previous_scores([prev, new])
    assert new.matches[0].previousScore is None


def test_previous_score_hidden_for_different_jd_content():
    prev = _result(
        version=1, score=78, resume_fp=growth.fingerprint("简历 V1"),
        raw_jd="JD 版本 A",
    )
    new = _result(
        version=2, score=82, resume_fp=growth.fingerprint("简历 V2"),
        raw_jd="JD 版本 B",
    )
    growth.annotate_match_previous_scores([prev, new])
    assert new.matches[0].previousScore is None


def test_previous_score_hidden_for_different_scoring_version():
    prev = _result(
        version=1, score=78, resume_fp=growth.fingerprint("简历 V1"),
        scoring="c4-cap-v0",
    )
    new = _result(version=2, score=82, resume_fp=growth.fingerprint("简历 V2"))
    growth.annotate_match_previous_scores([prev, new])
    assert new.matches[0].previousScore is None


def test_previous_score_hidden_without_resume_change():
    """同一份简历的分数波动不是成长 —— 不展示「较上次」。"""
    same = growth.fingerprint("同一份简历")
    prev = _result(version=1, score=78, resume_fp=same)
    new = _result(version=2, score=82, resume_fp=same)
    growth.annotate_match_previous_scores([prev, new])
    assert new.matches[0].previousScore is None


def test_previous_score_hidden_for_legacy_results():
    """迁移前的历史结果没有评分口径 / 简历指纹 → 不可比较。"""
    prev = _result(version=1, score=78, resume_fp=None, scoring=None)
    new = _result(version=2, score=82, resume_fp=None, scoring=None)
    growth.annotate_match_previous_scores([prev, new])
    assert new.matches[0].previousScore is None


def test_previous_score_ignores_job_row_id_and_uses_job_key_and_jd_hash():
    """岗位行 id 每次分析都会变，比较必须认 jobKey + jdHash。"""
    prev = _result(
        version=1, score=78, resume_fp=growth.fingerprint("简历 V1"),
        job_id="job-old",
    )
    new = _result(
        version=2, score=82, resume_fp=growth.fingerprint("简历 V2"),
        job_id="job-new",
    )
    growth.annotate_match_previous_scores([prev, new])
    assert new.matches[0].previousScore == 78


def test_previous_score_skips_incomparable_intermediate_result():
    """跳过不同 JD / 同简历的中间结果，取最近一条**可比**的。"""
    first = _result(version=1, score=70, resume_fp=growth.fingerprint("简历 V1"))
    middle = _result(
        version=2, score=60, resume_fp=growth.fingerprint("简历 V2"),
        raw_jd="JD 改版",
    )
    last = _result(version=3, score=80, resume_fp=growth.fingerprint("简历 V3"))
    growth.annotate_match_previous_scores([first, middle, last])
    assert last.matches[0].previousScore == 70
    assert middle.matches[0].previousScore is None


def test_previous_score_per_job_in_multi_job_result():
    """多岗位结果：只有可比的那个岗位拿到 previousScore。"""
    prev = AnalysisResult(
        id="res-1", projectId="project-1", version=1,
        matches=[
            JobMatch(jobId="a1", score=78, rank=1),
            JobMatch(jobId="b1", score=60, rank=2),
        ],
        jobSnapshots=[
            JobSnapshot(jobId="a1", title="岗位 A", company="公司", rawJd="JD A"),
            JobSnapshot(jobId="b1", title="岗位 B", company="公司", rawJd="JD B"),
        ],
        scoringVersion=SCORING_VERSION,
        resumeFingerprint=growth.fingerprint("简历 V1"),
    )
    new = AnalysisResult(
        id="res-2", projectId="project-1", version=2,
        matches=[
            JobMatch(jobId="a2", score=82, rank=1),
            JobMatch(jobId="b2", score=55, rank=2),
        ],
        jobSnapshots=[
            JobSnapshot(jobId="a2", title="岗位 A", company="公司", rawJd="JD A"),
            # 岗位 B 的 JD 改写了 → 不可比较。
            JobSnapshot(jobId="b2", title="岗位 B", company="公司", rawJd="JD B v2"),
        ],
        scoringVersion=SCORING_VERSION,
        resumeFingerprint=growth.fingerprint("简历 V2"),
    )
    growth.annotate_match_previous_scores([prev, new])
    by_job = {m.jobId: m.previousScore for m in new.matches}
    assert by_job["a2"] == 78
    assert by_job["b2"] is None


# ── P1-04 额度预占 ─────────────────────────────────────────────────────────


def _new_task(store: CareerlyStore, uid: int, title: str = "T") -> str:
    return store.create_task(uid, title)


def test_reserve_commit_consumes_exactly_once(store):
    uid = 1
    task = _new_task(store, uid)
    assert store.reserve_analysis_quota(uid, task) == "reserved"
    assert store.get_quota(uid).reserved == 1
    assert store.get_quota(uid).used == 0

    assert store.complete_task(task, "proj_1") is True
    quota = store.get_quota(uid)
    assert quota.used == 1 and quota.reserved == 0

    # 重复确认：不重复扣减。
    assert store.commit_analysis_quota(task) is False
    assert store.get_quota(uid).used == 1


def test_reserve_release_does_not_charge(store):
    uid = 2
    task = _new_task(store, uid)
    assert store.reserve_analysis_quota(uid, task) == "reserved"
    assert store.fail_task(task, "ANALYSIS_FAILED", "炸了") is True
    quota = store.get_quota(uid)
    assert quota.used == 0 and quota.reserved == 0

    # 重复释放：幂等。
    assert store.release_analysis_quota(task) is False
    assert store.get_quota(uid).reserved == 0


def test_concurrent_reservations_never_exceed_free_total(store):
    """5 个并发请求抢 3 次免费额度：只能有 3 个成功。"""
    uid = 3
    tasks = [_new_task(store, uid, f"T{i}") for i in range(5)]

    def reserve(task_id: str) -> str:
        return store.reserve_analysis_quota(uid, task_id)

    with ThreadPoolExecutor(max_workers=5) as pool:
        results = list(pool.map(reserve, tasks))

    assert results.count("reserved") == CareerlyStore.FREE_ANALYSIS_QUOTA
    assert results.count("exhausted") == 2
    assert store.get_quota(uid).reserved == CareerlyStore.FREE_ANALYSIS_QUOTA
    assert store.get_quota(uid).used == 0


def test_cancel_and_fail_never_downgrade_completed_task(store):
    uid = 4
    task = _new_task(store, uid)
    store.reserve_analysis_quota(uid, task)
    assert store.complete_task(task, "proj_x") is True

    store.cancel_task(task)
    store.fail_task(task, "ANALYSIS_FAILED", "迟到的失败")
    assert store.get_task(uid, task)["status"] == "completed"
    assert store.get_quota(uid).used == 1
    assert store.get_quota(uid).reserved == 0


def test_cancel_releases_reservation(store):
    uid = 5
    task = _new_task(store, uid)
    store.reserve_analysis_quota(uid, task)
    store.cancel_task(task)
    assert store.get_task(uid, task)["status"] == "cancelled"
    assert store.get_quota(uid).reserved == 0
    assert store.get_quota(uid).used == 0


# ── P1-06 启动恢复 ─────────────────────────────────────────────────────────


def test_recovery_marks_interrupted_tasks_failed_and_releases_quota(store):
    uid = 6
    queued = _new_task(store, uid, "queued 遗留")
    processing = _new_task(store, uid, "processing 遗留")
    store.reserve_analysis_quota(uid, queued)
    store.reserve_analysis_quota(uid, processing)
    store.start_task(processing)

    recovered = store.recover_interrupted_tasks()
    assert recovered == 2

    for task_id in (queued, processing):
        task = store.get_task(uid, task_id)
        assert task["status"] == "failed"
        assert task["errorCode"] == "TASK_INTERRUPTED"
        assert task["errorMessage"]
        assert task["finishedAt"]

    quota = store.get_quota(uid)
    assert quota.reserved == 0 and quota.used == 0


def test_recovery_does_not_rerun_successful_tasks(store):
    uid = 7
    task = _new_task(store, uid)
    store.reserve_analysis_quota(uid, task)
    store.complete_task(task, "proj_done")

    assert store.recover_interrupted_tasks() == 0
    assert store.get_task(uid, task)["status"] == "completed"
    assert store.get_quota(uid).used == 1


def test_router_startup_recovery_uses_the_store(store, monkeypatch):
    """启动钩子调用的就是这条路径，不是另一套逻辑。"""
    from app.careerly import router as careerly_router

    monkeypatch.setattr(careerly_router, "_store", store, raising=False)
    task = _new_task(store, 9)
    store.reserve_analysis_quota(9, task)

    assert careerly_router.recover_interrupted_tasks() == 1
    assert store.get_task(9, task)["errorCode"] == "TASK_INTERRUPTED"
    assert store.get_quota(9).reserved == 0


def test_recovery_settles_terminal_task_with_dangling_reservation(store):
    """进程在「任务完成」与「额度确认」之间被杀：恢复时补确认，不退费。"""
    uid = 8
    task = _new_task(store, uid)
    store.reserve_analysis_quota(uid, task)
    # 直接写 terminal 状态，模拟没来得及 commit quota 的崩溃窗口。
    import sqlite3
    conn = sqlite3.connect(store.db_path)
    conn.execute(
        "UPDATE analysis_tasks SET status = 'completed', project_id = 'proj_x' WHERE id = ?",
        (task,),
    )
    conn.commit()
    conn.close()

    store.recover_interrupted_tasks()
    quota = store.get_quota(uid)
    assert quota.used == 1 and quota.reserved == 0
