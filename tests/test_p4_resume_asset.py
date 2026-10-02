"""P4-① —— 统一简历资产入口：``POST /api/v1/resume``。

## 这个文件回答的问题

在此之前，简历有**两套互不相通的存储**，三个写入者各写各的：

```text
POST /api/v1/resume/upload   → data/resumes/{id}.json       （不建立基线）
PUT  /api/v1/resume          → careerly_resumes.text        （不写 profile）
POST /api/v1/analysis        → careerly_resumes.profile     （要跑一次分析）
```

后果：一个走完 Onboarding、还没做过分析的用户，「我的 → 职业资产」永远是空的
—— 于是前端只能继续吃 Mock。

``POST /api/v1/resume`` 把三种**输入方式**（粘贴文本 / 文件 / 截图 OCR）
收进**同一个入口**：它们只差"文本怎么读出来"，之后的解析与落库是同一段代码。
这里断言的重点因此不是"返回 200"，而是：

```text
① 三种输入方式 → 同一张 careerly_resumes 行（不存在第二套 Profile 写入逻辑）
② 上传**不扣**分析额度、**不**建分析任务（CLAUDE.md §5.2：这不是分析）
③ 解析降级时不覆盖已有画像（沿用 POST /analysis 的纪律）
④ 用户之间不串
```

运行：
    ./venv/Scripts/python.exe -m pytest tests/test_p4_resume_asset.py -v
"""

from __future__ import annotations

import io

import pytest
from starlette.testclient import TestClient  # noqa: F401  (fixture 注入)

from app.agents import resume_agent as resume_agent_module

# 复用 P1-5 / P2 / P3 的夹具：临时库 + 已注册用户。
from test_p1_engine_e2e import (  # noqa: F401
    careerly_store,
    client,
    graph_store,
    workdir,
)

_TEXT_RESUME = (
    "张三｜资深后端工程师\n"
    "5 年 Python 后端经验，主导 AI Agent 平台建设。\n"
    "技术栈：Python、FastAPI、PostgreSQL、Docker、Redis。"
)


# ── 离线替身：绝不打真实模型 ────────────────────────────────────────────────


class FakeResumeLLM:
    """`parse_resume` 的返回值**由入参推导**，并记账它读到的文本。

    这样「OCR 出来的文本真的流进了解析器」是可以被断言的事实，
    而不是碰巧对上的常量。
    """

    calls: list[str] = []

    def __init__(self, *args, **kwargs) -> None:
        pass

    @classmethod
    def reset(cls) -> None:
        cls.calls = []

    def parse_resume(self, resume_text: str) -> dict:
        type(self).calls.append(resume_text)
        return {
            "name": "张三",
            "email": "zhangsan@example.com",
            "phone": "13800000000",
            "skills": ["Python", "FastAPI", "PostgreSQL", "Docker"],
            "experience_years": 5.0,
            "roles": ["资深后端工程师"],
            "projects": [{"name": "AI Agent 平台", "description": "主导建设"}],
            "education": [{"degree": "B.S. 计算机", "institution": "某某大学",
                           "year": 2019}],
            "certifications": ["AWS SAA"],
        }


class EmptyResumeLLM(FakeResumeLLM):
    """模拟"模型没返回可用结构"的降级：只有 raw_text，没有 skills。"""

    def parse_resume(self, resume_text: str) -> dict:
        type(self).calls.append(resume_text)
        return {}


@pytest.fixture()
def fake_resume_llm(monkeypatch, workdir):
    """把 ResumeAgent 里的 LLMService 换成离线替身，并把 checkpoint 落临时目录。"""
    FakeResumeLLM.reset()
    monkeypatch.setattr(resume_agent_module, "LLMService", FakeResumeLLM,
                        raising=False)
    # ResumeAgent 会把解析结果写到 app/data/outputs/ —— 测试不该污染仓库。
    monkeypatch.setattr(resume_agent_module, "RESUME_OUTPUT_DIR",
                        workdir / "outputs", raising=False)
    return FakeResumeLLM


@pytest.fixture()
def empty_resume_llm(monkeypatch, workdir):
    EmptyResumeLLM.reset()
    monkeypatch.setattr(resume_agent_module, "LLMService", EmptyResumeLLM,
                        raising=False)
    monkeypatch.setattr(resume_agent_module, "RESUME_OUTPUT_DIR",
                        workdir / "outputs", raising=False)
    return EmptyResumeLLM


# ── 工具 ────────────────────────────────────────────────────────────────────


def _post_text(client: TestClient, headers: dict[str, str], text: str):
    return client.post("/api/v1/resume", data={"text": text}, headers=headers)


def _post_file(client: TestClient, headers: dict[str, str],
               filename: str, content: bytes, mime: str = "application/octet-stream"):
    return client.post(
        "/api/v1/resume",
        files={"file": (filename, io.BytesIO(content), mime)},
        headers=headers,
    )


def _baseline(client: TestClient, headers: dict[str, str]) -> dict:
    r = client.get("/api/v1/resume", headers=headers)
    assert r.status_code == 200, r.text
    return r.json()["data"]


def _quota(client: TestClient, headers: dict[str, str]) -> dict:
    r = client.get("/api/v1/quota", headers=headers)
    assert r.status_code == 200, r.text
    return r.json()["data"]


def _tesseract_ready() -> bool:
    """OCR 需要系统装 tesseract **且**自带 chi_sim —— 缺任一就跳过图片用例。"""
    from app.agents.resume_agent import _LOCAL_TESSDATA, _configure_tesseract

    if not (_LOCAL_TESSDATA / "chi_sim.traineddata").exists():
        return False
    try:
        _configure_tesseract()
    except Exception:
        return False
    return True


# ── 1. 文本路径：建立基线 + 画像 ────────────────────────────────────────────


def test_text_establishes_baseline_and_profile(client, careerly_store,
                                               fake_resume_llm):
    """粘贴文本 → 一次调用同时落 text 与 profile。"""
    h = client.post("/api/v1/auth/register",
                    json={"name": "测试用户", "phone": "13900002001"}).json()
    headers = {"Authorization": f"Bearer {h['data']['access_token']}"}

    r = _post_text(client, headers, _TEXT_RESUME)
    assert r.status_code == 200, r.text
    data = r.json()["data"]

    # 返回体与 GET /resume 同形，客户端不必再发一次 GET。
    assert data["profile"]["skills"] == ["Python", "FastAPI", "PostgreSQL",
                                         "Docker"]
    assert data["profile"]["roles"] == ["资深后端工程师"]
    assert data["updatedAt"]

    # 真的落进了 careerly_resumes —— 而且是同一行。
    saved = _baseline(client, headers)
    assert saved["profile"]["skills"] == data["profile"]["skills"]
    assert "Python" in saved["text"]

    # 解析器读到的就是提交的文本。
    assert _TEXT_RESUME in fake_resume_llm.calls[0]


def test_text_is_trimmed_and_stored_verbatim(client, careerly_store,
                                             fake_resume_llm):
    headers = _register(client, "13900002002")

    _post_text(client, headers, f"\n\n  {_TEXT_RESUME}  \n")

    saved = _baseline(client, headers)
    assert saved["text"] == _TEXT_RESUME


# ── 2. 文件路径：与文本进同一张表 ───────────────────────────────────────────


def test_txt_file_lands_in_the_same_store(client, careerly_store,
                                          fake_resume_llm):
    """文件与文本**不是**两套存储 —— 这是本次改造的核心。"""
    headers = _register(client, "13900002003")

    r = _post_file(client, headers, "resume.txt",
                   _TEXT_RESUME.encode("utf-8"), "text/plain")
    assert r.status_code == 200, r.text

    saved = _baseline(client, headers)
    assert saved["profile"]["skills"] == ["Python", "FastAPI", "PostgreSQL",
                                          "Docker"]
    assert "AI Agent 平台建设" in saved["text"]


def test_image_runs_ocr_into_the_same_pipeline(client, careerly_store,
                                               fake_resume_llm):
    """截图 OCR → 同一条解析链路，不新建第二套 Profile 逻辑。"""
    if not _tesseract_ready():
        pytest.skip("tesseract / chi_sim 不可用")

    from PIL import Image, ImageDraw, ImageFont

    img = Image.new("RGB", (1100, 200), "white")
    draw = ImageDraw.Draw(img)
    font = ImageFont.truetype(r"C:\Windows\Fonts\msyh.ttc" if __import__("os").name == "nt"
                              else "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 40)
    draw.text((30, 40), "王五 数据分析师 Python SQL", font=font, fill="black")
    draw.text((30, 110), "用户增长 指标体系", font=font, fill="black")

    buf = io.BytesIO()
    img.save(buf, format="PNG")

    headers = _register(client, "13900002004")
    r = _post_file(client, headers, "screenshot.png", buf.getvalue(), "image/png")
    assert r.status_code == 200, r.text

    # OCR 的产物是**文本**，并且真的喂给了同一个解析器。
    seen = fake_resume_llm.calls[0]
    assert "Python" in seen, f"OCR 文本没流进解析器：{seen!r}"

    saved = _baseline(client, headers)
    assert saved["profile"]["skills"] == ["Python", "FastAPI", "PostgreSQL",
                                          "Docker"]
    assert "Python" in saved["text"]


# ── 3. 这不是分析：不扣额度、不建任务 ───────────────────────────────────────


def test_upload_does_not_consume_quota(client, careerly_store, fake_resume_llm):
    """CLAUDE.md §5.2：上传简历**不扣**额度，只有「开始分析」才扣。"""
    headers = _register(client, "13900002005")

    before = _quota(client, headers)
    _post_text(client, headers, _TEXT_RESUME)
    after = _quota(client, headers)

    assert before["used"] == 0
    assert after["used"] == 0
    assert after["total"] == 3


def test_upload_creates_no_analysis_task(client, careerly_store,
                                         fake_resume_llm):
    """上传留痕不该出现在「分析任务」列表里 —— 它压根不是分析。"""
    headers = _register(client, "13900002006")

    _post_text(client, headers, _TEXT_RESUME)

    r = client.get("/api/v1/analysis/tasks", headers=headers)
    assert r.status_code == 200, r.text
    assert r.json()["data"] == []


# ── 4. 降级纪律：不拿空壳覆盖已有画像 ───────────────────────────────────────


def test_degraded_parse_keeps_previous_profile(client, careerly_store,
                                               fake_resume_llm):
    """解析偶尔会降级成"只有 raw_text"。此时**不得**清空职业资产。"""
    headers = _register(client, "13900002007")
    _post_text(client, headers, _TEXT_RESUME)
    good = _baseline(client, headers)["profile"]["skills"]
    assert good

    # 第二个用户换成"模型不返回结构"的替身（同一进程内切模块全局）。
    import app.agents.resume_agent as mod

    original = mod.LLMService
    mod.LLMService = EmptyResumeLLM
    try:
        r = _post_text(client, headers, "一份模型读不出结构的简历")
        assert r.status_code == 200, r.text
    finally:
        mod.LLMService = original

    saved = _baseline(client, headers)
    # 文本更新了（它是权威），但画像保住了旧的。
    assert saved["text"] == "一份模型读不出结构的简历"
    assert saved["profile"]["skills"] == good, "空壳画像不得覆盖已有职业资产"


# ── 5. 校验与错误 ───────────────────────────────────────────────────────────


def test_no_payload_is_rejected(client, careerly_store, fake_resume_llm):
    headers = _register(client, "13900002008")

    r = client.post("/api/v1/resume", data={}, headers=headers)

    assert r.status_code == 422
    assert r.json()["error"]["code"] == "VALIDATION_ERROR"


def test_blank_text_is_rejected(client, careerly_store, fake_resume_llm):
    headers = _register(client, "13900002009")

    r = _post_text(client, headers, "   \n  ")

    assert r.status_code == 422
    assert r.json()["error"]["code"] == "VALIDATION_ERROR"


def test_unsupported_format_is_rejected(client, careerly_store,
                                        fake_resume_llm):
    """`.doc` 是旧的二进制格式，python-docx 读不了 —— 明确拒绝，不静默失败。"""
    headers = _register(client, "13900002010")

    r = _post_file(client, headers, "resume.doc", b"\xd0\xcf\x11\xe0\x00\x00")

    assert r.status_code == 422
    assert r.json()["error"]["code"] == "UNSUPPORTED_FORMAT"


def test_empty_file_is_rejected(client, careerly_store, fake_resume_llm):
    headers = _register(client, "13900002011")

    r = _post_file(client, headers, "resume.txt", b"")

    assert r.status_code == 422
    assert r.json()["error"]["code"] == "VALIDATION_ERROR"


def test_oversized_file_is_rejected(client, careerly_store, fake_resume_llm):
    headers = _register(client, "13900002012")

    r = _post_file(client, headers, "resume.txt", b"x" * (10 * 1024 * 1024 + 1))

    assert r.status_code == 413
    assert r.json()["error"]["code"] == "FILE_TOO_LARGE"


# ── 6. 用户隔离 ─────────────────────────────────────────────────────────────


def test_baselines_are_per_user(client, careerly_store, fake_resume_llm):
    alice = _register(client, "13900002013")
    bob = _register(client, "13900002014")

    _post_text(client, alice, _TEXT_RESUME)

    assert _baseline(client, alice)["text"] == _TEXT_RESUME
    assert _baseline(client, bob) is None, "新用户不该看到别人的基线"


def _register(client: TestClient, phone: str) -> dict[str, str]:
    r = client.post("/api/v1/auth/register",
                    json={"name": "测试用户", "phone": phone})
    assert r.status_code == 201, r.text
    return {"Authorization": f"Bearer {r.json()['data']['access_token']}"}
