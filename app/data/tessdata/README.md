# tessdata —— 中文 OCR 语言包

`ResumeAgent` 用 Tesseract 读**图片 / 扫描件**简历。Tesseract 的 Windows 官方
安装包（UB-Mannheim）**只带 `eng` + `osd`**，没有中文；而 Careerly 的简历是中文的。
因此本项目**自带**一份 `chi_sim`，放在这里。

## 需要什么

| 文件 | 来源 | 大小 |
|---|---|---|
| `chi_sim.traineddata` | [tesseract-ocr/tessdata_fast](https://github.com/tesseract-ocr/tessdata_fast) | ~2.5 MB |

选 `tessdata_fast` 而不是 `tessdata_best`：OCR 在**上传时同步执行**，用户要等；
`screenshots` 通常清晰，`fast` 的识别质量足够，且下游还有 LLM 结构化解析兜底。
若要换更高精度版本，直接替换同名文件即可，代码不用改。

## 怎么装

```bash
# 直接用镜像（GitHub raw 在国内常被重置）
curl -sSL -o app/data/tessdata/chi_sim.traineddata \
  https://cdn.jsdelivr.net/gh/tesseract-ocr/tessdata_fast@main/chi_sim.traineddata
```

**不需要管理员权限** —— 不写进 `C:\Program Files\Tesseract-OCR\tessdata\`，
而是通过 `--tessdata-dir` 指过来（见 `resume_agent.py` 的 `_tessdata_config`）。

⚠️ 这个文件**不进 git**（二进制，且可随时从上游取回）。

## 还需要系统装 Tesseract

语言包只是词典，还得有引擎：

```bash
winget install --id UB-Mannheim.TesseractOCR -e
```

`resume_agent.py` 会依次找：`CAREERLY_TESSERACT_CMD` 环境变量 → `PATH` →
`C:\Program Files\Tesseract-OCR\tesseract.exe`。都找不到时**明确报错**
（`OCR_UNAVAILABLE`），不会静默返回空文本。

## 部署（Docker）

镜像里应装系统包，并把 `chi_sim` 放进 tessdata：

```dockerfile
RUN apt-get update && apt-get install -y --no-install-recommends \
      tesseract-ocr tesseract-ocr-chi-sim poppler-utils
```

`poppler-utils` 是 `pdf2image` 渲扫描件 PDF 用的。

## 降级行为

没有 `chi_sim.traineddata` 时，`_ocr_language()` 回落到 `eng` ——
混合中英的简历仍能读出拉丁字符（Python、SQL 等技术词），
但中文段落会丢失。**不会**因为缺语言包而整体失败。
