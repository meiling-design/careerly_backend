"""Resume Agent — parse PDF / DOCX / TXT / image resumes into structured profiles.

Supports:
- PDF via pdfplumber
- DOCX via python-docx
- Plain text
- Images (screenshots) via pytesseract OCR
- Scanned PDFs via pdf2image + pytesseract (fallback)
- LLM-enhanced structured extraction

## 为什么图片走同一条链路

「文件上传」「粘贴文本」「截图 OCR」是三种**输入方式**，但都只做一件事：
**产出一段简历文本**。文本之后的解析（`_parse_with_llm` → `ResumeProfile`）
是**同一段代码**，不存在第二套画像逻辑。文本提取器是唯一的差异点。
"""

from __future__ import annotations

import json
import os
import shutil
import uuid
from pathlib import Path
from typing import Optional

from app.models.resume import Education, Project, ResumeProfile
from app.services.llm_service import LLMService
from app.utils.logger import logger
from app.utils.paths import APP_DATA_DIR as DATA_DIR, TESSDATA_DIR
from app.utils.redact import mask_name

RESUME_OUTPUT_DIR = DATA_DIR / "outputs"

# ── OCR 环境 ────────────────────────────────────────────────────────────────
#
# 图片 / 扫描件**只能**靠 OCR 读出文本。缺依赖时**不能**静默返回 ""——
# 那会让用户拿到一份空基线却看不到任何错误（"失败无痕"）。
# 因此这里统一抛 [ResumeExtractionError]，由 API 层映射为四态的 Error。

_IMAGE_SUFFIXES = frozenset({".png", ".jpg", ".jpeg", ".webp", ".bmp", ".tif", ".tiff"})

#: Windows 官方安装包（UB-Mannheim）的默认落点。`which` 找不到时依次探测。
_TESSERACT_CANDIDATES = (
    r"C:\Program Files\Tesseract-OCR\tesseract.exe",
    r"C:\Program Files (x86)\Tesseract-OCR\tesseract.exe",
)

#: 随仓库携带的 tessdata（`chi_sim` 不在 stock 安装包里，见 app/data/tessdata/README.md）。
#:
#: ⚠️ 注意这一行**不**跟随上面的 DATA_DIR（本文件里唯一如此的地方）：
#: tessdata 是随代码仓发布的**只读资源**，不是用户数据。若跟着 DATA_DIR 落到
#: 运行时才挂载、初始为空的数据盘上，下面 _apply_tessdata() 的存在性检查会
#: **静默失败**、不设 TESSDATA_PREFIX，OCR 悄悄退回默认语言包 —— 正是不远处的
#: 注释明令禁止的"失败无痕"。
_LOCAL_TESSDATA = TESSDATA_DIR


class ResumeExtractionError(RuntimeError):
    """简历文本**无法提取**（区别于"提取到了但是空的"）。

    `code` 供 API 层原样透出，Dart 侧据此渲染四态的 Error 分支，例如
    ``OCR_UNAVAILABLE`` / ``UNSUPPORTED_FORMAT``。
    """

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def _configure_tesseract() -> None:
    """把 pytesseract 指向 tesseract 二进制。幂等，可重复调用。"""
    import pytesseract

    cmd = os.getenv("CAREERLY_TESSERACT_CMD") or shutil.which("tesseract")
    if not cmd:
        for candidate in _TESSERACT_CANDIDATES:
            if Path(candidate).exists():
                cmd = candidate
                break
    if not cmd:
        raise ResumeExtractionError(
            "OCR_UNAVAILABLE",
            "OCR 引擎不可用：未找到 tesseract 可执行文件"
            "（可设 CAREERLY_TESSERACT_CMD，或安装 Tesseract-OCR）",
        )
    pytesseract.pytesseract.tesseract_cmd = cmd


def _apply_tessdata() -> None:
    """自带 tessdata 时把 ``TESSDATA_PREFIX`` 指过去；否则用 Tesseract 默认目录。

    ⚠️ **不用 ``--tessdata-dir``**：pytesseract 把 config 按空白切分，
    含空格的路径会被切碎，而且引号不会被剥掉（实测报
    ``Error opening data file "...tessdata"/chi_sim.traineddata``）。
    ``TESSDATA_PREFIX`` 走环境变量，没有这个限制。

    ⚠️ Tesseract 5 要求它指向 **tessdata 目录本身**，不是它的父目录
    （指父目录会去找 ``<prefix>/chi_sim.traineddata`` 而失败）。
    """
    if (_LOCAL_TESSDATA / "chi_sim.traineddata").exists():
        os.environ["TESSDATA_PREFIX"] = str(_LOCAL_TESSDATA)


def _ocr_language() -> str:
    """中文优先。

    ⚠️ 只在**自带** tessdata 时返回 ``chi_sim``：stock 安装包**没有**这个
    语言包，写死会让整次提取直接抛 TesseractError。``chi_sim`` 模型本身
    能识别拉丁字符，因此简历里的 Python / SQL 等技术词同样读得出来，
    不需要 ``+eng`` 拼串（拼了反而要求两个语言包同时在同一个 tessdata 目录下）。
    """
    if (_LOCAL_TESSDATA / "chi_sim.traineddata").exists():
        return "chi_sim"
    return "eng"


def _ocr_images(images: list) -> str:
    """对一组 PIL 图片做 OCR。依赖缺失 / 引擎故障都抛 [ResumeExtractionError]。"""
    try:
        import pytesseract
    except ImportError as exc:
        raise ResumeExtractionError(
            "OCR_UNAVAILABLE",
            "OCR 依赖未安装：请执行 pip install pytesseract pdf2image Pillow",
        ) from exc

    _configure_tesseract()
    _apply_tessdata()
    lang = _ocr_language()

    try:
        return "\n".join(
            pytesseract.image_to_string(img, lang=lang) for img in images
        )
    except pytesseract.TesseractError as exc:
        raise ResumeExtractionError(
            "OCR_FAILED", f"OCR 识别失败：{exc}"
        ) from exc


class ResumeAgent:
    """Parse and structure resume documents."""

    def __init__(self, llm: Optional[LLMService] = None):
        self.llm = llm or LLMService()
        self.session_id: str = ""
        #: 最近一次 [run] 提取到的**完整**文本。
        #:
        #: ⚠️ 与 ``profile.raw_text`` 不同 —— 后者截断到 5000 字（喂 LLM 用），
        #: 而职业基线要的是完整文本（它决定后续每次分析读到什么）。
        self.raw_text: str = ""
        RESUME_OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    # ── Main entry point ──────────────────────────────────────────────────

    async def run(
        self,
        file_path: Optional[str] = None,
        text: Optional[str] = None,
        use_llm: bool = True,
        session_id: Optional[str] = None,
    ) -> ResumeProfile:
        """Parse a resume from file path or raw text.

        Args:
            file_path: Path to a PDF / DOCX / TXT / image resume file.
            text: Raw resume text (alternative to file_path).
            use_llm: Whether to use LLM for structured extraction.
            session_id: Optional session ID for traceability.

        Returns:
            ResumeProfile with extracted fields.

        Raises:
            ResumeExtractionError: 文本**完全读不出来**（格式不支持、
                OCR 引擎不可用）。调用方必须把它当作 Error 态，而不是当空数据。
        """
        self.session_id = session_id or f"resume-{uuid.uuid4().hex[:8]}"

        # Extract raw text
        raw_text = text or ""
        if file_path:
            raw_text = self._extract_text(file_path)
            logger.info("ResumeAgent loaded file | path=%s | chars=%d", file_path, len(raw_text))

        # 完整文本先落到实例上：调用方（`POST /resume`）要拿它当职业基线，
        # 而 `profile.raw_text` 是截断版。下面任何 early return 都保留这份。
        self.raw_text = raw_text

        if not raw_text or not raw_text.strip():
            # 给了文件却一个字都读不出来 —— 这是**提取失败**，不是"简历很短"。
            # 若在这里静默返回空 profile，用户会得到一份空基线且看不到任何错误。
            if file_path:
                raise ResumeExtractionError(
                    "EMPTY_EXTRACTION",
                    "未能从文件中读取到任何文字："
                    "请确认内容清晰可辨（扫描件 / 截图请提高清晰度后重试）",
                )
            return ResumeProfile(raw_text=raw_text or "")

        if len(raw_text.strip()) < 20:
            logger.warning("Resume text too short (%d chars), returning empty profile", len(raw_text))
            return ResumeProfile(raw_text=raw_text)

        if use_llm:
            profile = self._parse_with_llm(raw_text)
        else:
            profile = ResumeProfile(raw_text=raw_text)

        profile.raw_text = raw_text[:5000]  # keep raw text for reference
        self._save_checkpoint(profile)
        # ⚠️ 姓名必须脱敏（TODO K21）：此前这里是完整明文 INFO 日志。
        # 保留首字符 —— 排查时 "name=(none)"（没解析出姓名）
        # 与 "name=张****"（解析到了）是两个完全不同的结论。
        logger.info("ResumeAgent done | name=%s | skills=%d | exp=%s",
                      mask_name(profile.name), len(profile.skills), profile.experience_years)
        return profile

    # ── Text Extraction ───────────────────────────────────────────────────

    def _extract_text(self, file_path: str) -> str:
        """Extract text from PDF, DOCX, TXT, or an image.

        ⚠️ **所有输入方式在这里汇合** —— 图片与 PDF/DOCX 只是不同的读取器，
        产出的都是「一段简历文本」，之后的解析链路完全一致。
        """
        path = Path(file_path)
        if not path.exists():
            raise FileNotFoundError(f"Resume file not found: {file_path}")

        suffix = path.suffix.lower()

        if suffix == ".pdf":
            return self._extract_pdf(path)
        elif suffix == ".docx":
            return self._extract_docx(path)
        elif suffix == ".txt":
            return path.read_text(encoding="utf-8", errors="replace")
        elif suffix in _IMAGE_SUFFIXES:
            return self._extract_image(path)
        else:
            raise ResumeExtractionError(
                "UNSUPPORTED_FORMAT",
                f"不支持的文件类型 {suffix}："
                f"支持 PDF / DOCX / TXT / 图片（{'/'.join(sorted(_IMAGE_SUFFIXES))}）",
            )

    def _extract_pdf(self, path: Path) -> str:
        """Extract text from PDF using pdfplumber."""
        try:
            import pdfplumber  # type: ignore[import-untyped]
            text_parts: list[str] = []
            with pdfplumber.open(path) as pdf:
                for page in pdf.pages:
                    page_text = page.extract_text()
                    if page_text:
                        text_parts.append(page_text)
            result = "\n".join(text_parts)
            if not result.strip():
                logger.warning("pdfplumber returned empty, trying OCR fallback")
                return self._ocr_extract(path)
            return result
        except ImportError:
            logger.warning("pdfplumber not installed, trying OCR fallback")
            return self._ocr_extract(path)

    def _extract_docx(self, path: Path) -> str:
        """Extract text from DOCX using python-docx."""
        try:
            from docx import Document  # type: ignore[import-untyped]
            doc = Document(str(path))
            return "\n".join(p.text for p in doc.paragraphs if p.text.strip())
        except ImportError:
            logger.warning("python-docx not installed")
            raise ImportError("Install python-docx for .docx support: pip install python-docx")

    def _ocr_extract(self, path: Path) -> str:
        """扫描件 PDF 的 OCR 回退（pdf2image 先把每页渲成图，再交给 tesseract）。

        ⚠️ 依赖缺失时**抛错而不是返回 ""** —— 见 [ResumeExtractionError]。
        """
        try:
            import pdf2image  # type: ignore[import-untyped]
        except ImportError as exc:
            raise ResumeExtractionError(
                "OCR_UNAVAILABLE",
                "扫描件 PDF 需要 OCR：请执行 pip install pdf2image pytesseract Pillow",
            ) from exc

        try:
            images = list(pdf2image.convert_from_path(str(path)))
        except Exception as exc:  # pdf2image 依赖 poppler，缺失时抛的是普通异常
            raise ResumeExtractionError(
                "OCR_UNAVAILABLE",
                f"扫描件 PDF 渲染失败（通常缺 poppler）：{exc}",
            ) from exc

        return _ocr_images(images)

    def _extract_image(self, path: Path) -> str:
        """图片（截图 / 拍照）→ 文本。简历的第三种输入方式。"""
        try:
            from PIL import Image  # type: ignore[import-untyped]
        except ImportError as exc:
            raise ResumeExtractionError(
                "OCR_UNAVAILABLE",
                "图片解析需要 Pillow：请执行 pip install Pillow",
            ) from exc

        try:
            with Image.open(path) as img:
                # OCR 全程只需要像素，转 RGB 可避免调色板 / 透明通道报错。
                return _ocr_images([img.convert("RGB")])
        except ResumeExtractionError:
            raise
        except OSError as exc:
            raise ResumeExtractionError(
                "UNSUPPORTED_FORMAT", f"图片无法打开（可能已损坏）：{exc}"
            ) from exc

    # ── LLM Parsing ───────────────────────────────────────────────────────

    def _parse_with_llm(self, raw_text: str) -> ResumeProfile:
        """Use LLM to extract structured fields from resume text."""
        try:
            result = self.llm.parse_resume(raw_text[:8000])
            if not result:
                logger.warning("LLM returned empty result for resume")
                return ResumeProfile(raw_text=raw_text)

            projects = [
                Project(
                    name=p.get("name", "Unknown"),
                    description=p.get("description"),
                    tech_stack=p.get("tech_stack", []),
                    highlights=p.get("highlights", []),
                )
                for p in (result.get("projects") or [])
                if p.get("name")
            ]

            education = [
                Education(
                    degree=e.get("degree", ""),
                    institution=e.get("institution", ""),
                    year=e.get("year"),
                )
                for e in (result.get("education") or [])
                if e.get("degree")
            ]

            return ResumeProfile(
                name=result.get("name"),
                email=result.get("email"),
                phone=result.get("phone"),
                skills=result.get("skills", []),
                experience_years=result.get("experience_years"),
                roles=result.get("roles", []),
                projects=projects,
                education=education,
                certifications=result.get("certifications", []),
            )
        except Exception as e:
            logger.error("LLM resume parsing failed: %s", e)
            return ResumeProfile(raw_text=raw_text)

    # ── Persistence ───────────────────────────────────────────────────────

    def _save_checkpoint(self, profile: ResumeProfile) -> None:
        """Save parsed resume to disk."""
        path = RESUME_OUTPUT_DIR / f"resume_parsed_{self.session_id}.json"
        path.write_text(
            json.dumps(profile.model_dump(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        logger.info("Checkpoint saved → %s", path)
