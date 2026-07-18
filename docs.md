# 职位匹配 AI 系统 — 产品需求文档 (PRD)

> 版本: 1.1 | 日期: 2026-07-09 | 状态: ✅ 已实现

---

## 1. 产品概述

### 1.1 产品定位

**职位匹配 AI 系统** 是一个基于 LangGraph 决策图的多智能体系统，能够从招聘 URL 自动抓取职位信息、解析简历、利用语义嵌入和 LLM 深度推理进行两阶段匹配，并生成结构化的匹配报告。

### 1.2 核心价值

- **自动化**：从 URL 抓取到报告导出，全流程自动化
- **混合智能**：语义嵌入快速过滤 + LLM 深度推理重排，兼顾速度与质量
- **成本优化**：2 阶段匹配机制节省约 80% 的 LLM API 调用成本
- **智能路由**：LLM + 规则混合路由决策，5 种动作（match/recrawl/refine_crawl/ask_user/end）
- **可观测性**：实时进度追踪 + SQLite 持久化，支持断点恢复
- **容错设计**：导出失败不阻断结果显示，评分和建议始终可见

### 1.3 目标用户

- 求职者：快速评估自身技能与市场职位匹配度
- 招聘人员：批量筛选候选人
- 职业顾问：生成结构化求职建议报告

---

## 2. 系统架构

### 2.1 整体架构图

```
┌─────────────────────────────────────────────────────────────────────┐
│                     Streamlit UI (前端)                               │
│   侧边栏: 简历上传/URL输入/模式配置       主面板: 流程图+结果展示      │
└──────────────────────────┬──────────────────────────────────────────┘
                           │  async 调用
                           ▼
┌─────────────────────────────────────────────────────────────────────┐
│                  orchestrator.DecisionGraph                            │
│               (LangGraph StateGraph - 8 节点)                         │
│                                                                       │
│   CrawlNode ──► NormalizeNode ──► ResumeEmbedNode ──► RoutingNode ⭐  │
│       ▲                                          │                    │
│       │         ┌─────────────────────────────────┘                    │
│       │         ▼          ▼          ▼          ▼                    │
│   RecrawlNode ◄──    MatchNode    ASK_USER     END                    │
│  (重试+优化重抓)        │                                              │
│                         ▼                                              │
│                    RankNode ──► ReportNode ──► END                     │
│                                  │                                     │
│                          ┌───────┴────────┐                            │
│                          │ 导出(尽力而为)  │                            │
│                          │ 结果(始终展示)   │                            │
│                          └────────────────┘                            │
└─────────────────────────────────────────────────────────────────────┘
```

### 2.2 目录结构

```
job_matching_agent/
├── app/
│   ├── agents/              # 智能体模块（核心业务逻辑）
│   │   ├── adapters/        #   🔌 站点适配器体系
│   │   │   ├── base.py      #     适配器基类
│   │   │   ├── tencent.py   #     腾讯招聘 (API直连)
│   │   │   ├── bytedance.py #     字节跳动 (Playwright)
│   │   │   ├── generic_html.py #  通用HTML (requests+BS4)
│   │   │   ├── generic_spa.py  #  通用SPA (Playwright+LLM)
│   │   │   └── __init__.py  #     注册中心/自动路由
│   │   ├── crawl_agent.py   #   🔍 爬虫智能体（适配器调度器）
│   │   ├── resume_agent.py  #   📄 简历解析智能体
│   │   ├── matching_agent.py#   🧠 匹配智能体（2阶段+批量LLM）
│   │   ├── ranking_agent.py #   📊 排序与报告智能体
│   │   └── job_advisor.py   #   📎 旧版单智能体（保留兼容）
│   ├── api/                 #   🌐 REST API服务
│   │   ├── server.py        #   FastAPI 5端点
│   │   └── middleware/      #   🔒 认证、限流中间件
│   ├── core/                # 核心引擎
│   │   ├── orchestrator.py  #   🎯 LangGraph 决策图编排器
│   │   ├── state.py         #   图状态定义 (GraphState)
│   │   ├── state_store.py   #   💾 SQLite 持久化存储
│   │   └── config.py        #   全局配置 (AppConfig)
│   ├── models/              # 数据模型 (Pydantic)
│   │   ├── job.py           #   JobPosting / JobList
│   │   ├── resume.py        #   ResumeProfile
│   │   └── match.py         #   MatchResult / MatchReport
│   ├── services/            # 基础设施服务
│   │   ├── llm_service.py   #   🤖 LLM 统一接口 (OpenAI兼容)
│   │   ├── embedding_service.py  # 📐 向量嵌入服务
│   │   └── export_service.py     # 📤 多格式导出服务
│   ├── ui/                  # 用户界面
│   │   └── streamlit_app.py #   🖥️ Streamlit 仪表板
│   └── utils/               # 工具
│       ├── logger.py        #   日志
│       └── helpers.py       #   URL验证、文本清洗
├── main.py                  # CLI 入口
├── config.yaml              # 运行时配置
├── requirements.txt         # 依赖清单
├── Dockerfile               # 🐳 容器化部署
├── docker-compose.yml       # 🐳 编排配置
└── .env.example             # 环境变量模板
```

---

## 3. 核心节点详解

### 3.1 Node 1: 🔍 CrawlNode（爬取节点）

**职责**：从用户提供的 URL 列表中抓取职位信息。

**输入**：`state.job_urls`（URL 列表）

**输出**：`state.crawler_output`（JobList 对象）

**逻辑**：
1. 验证 URL 合法性
2. **适配器路由**：自动选择最佳站点适配器（腾讯API / 字节Playwright / 通用HTML / 通用SPA）
3. **静态抓取**：优先使用 `requests` + `BeautifulSoup` 获取 HTML
4. **动态回退**：静态内容不足时自动降级到 Playwright（支持 JS 渲染页面）
5. **文本提取**：使用 `trafilatura` 库提取纯净文本，回退到 BeautifulSoup
6. **LLM 解析**（可选）：调用 LLM 从文本中提取结构化字段（技能、职责等）
7. **启发式解析**（无 LLM 时）：正则表达式提取职位标题和公司名
8. **去重**：按 URL 去重
9. **重试机制**：失败时最多重试 2 次，指数退避

### 3.2 Node 2: ⚙️ NormalizeNode（标准化节点）

**职责**：对原始职位数据进行 LLM 字段富化。

**输入**：`state.crawler_output`

**输出**：`state.normalized_jobs`

**逻辑**：
1. 遍历所有职位
2. 已有技能字段的职位直接通过
3. 缺少结构化字段的，调用 LLM 提取：skills、experience、responsibilities 等
4. LLM 失败时保留原始数据作为降级

### 3.3 Node 3: 📄 ResumeEmbedNode（简历+嵌入节点）

**职责**：解析简历文件并计算向量嵌入。

**输入**：`state.resume_file_path` 或 `state.resume_text`

**输出**：`state.resume_output` + `state.embedding_cache["resume"]`

**逻辑**：
1. **文件解析**：
   - PDF → `pdfplumber` 库提取文字
   - DOCX → `python-docx` 库提取文字
   - TXT → 直接读取
   - OCR 降级：`pdf2image` + `pytesseract`（图像型 PDF 回退）
2. **LLM 结构化提取**：调用 LLM 从简历文本中提取姓名、技能、项目、教育等
3. **嵌入计算**：计算简历文本的语义嵌入向量并缓存
4. **SQLite 持久化**：简历数据和嵌入向量存入数据库

### 3.4 Node 4: 🧭 RoutingNode（路由节点）⭐ 关键

**职责**：根据当前数据质量决定下一步操作路径，采用 **LLM + 规则混合策略**。

**输入**：`state.normalized_jobs` + `state.resume_output`

**输出**：`state.routing_decision`（match / recrawl / refine_crawl / ask_user / end）

#### 决策流程

```
1. 收集数据质量信号（职位数、描述长度、技能数、错误等）
2. 尝试 LLM Router → 获取决策+置信度
3. 置信度 ≥ 0.6 且不违反关键规则 → 采用 LLM 决策
4. 置信度 < 0.6 或 LLM 不可用 → 规则回退
5. 规则覆盖：无简历→end、无职位+有重试→recrawl、一切就绪→match
```

#### 决策矩阵

| 条件 | 决策 | 原因 |
|------|------|------|
| 无简历数据 | `end` | 无法匹配 |
| 无职位 + 有重试次数 | `recrawl` | 回退重抓 |
| 无职位 + 无重试次数 | `end` | 已达最大重试 |
| 职位质量差（描述短/技能少） | `refine_crawl` | 换策略重抓 |
| 简历技能几乎为空 | `ask_user` 或 `match` | 质量差，尝试或求助 |
| 一切就绪 | `match` | 正常进入匹配阶段 |

### 3.5 Node 5: 🔄 RecrawlNode（重抓节点）

**职责**：重新抓取失败的 URL，支持两种重抓模式。

**输入**：`state.job_urls`

**输出**：`state.crawler_output` + `state.recrawl_count++`

**逻辑**：
1. **标准重试**（recrawl）：相同设置，不同会话
2. **优化重抓**（refine_crawl）：使用 Playwright + 完整 LLM 解析
3. 重试计数递增
4. 再次失败则设置 `routing_decision = "end"`

### 3.6 Node 6: 🧠 MatchNode（匹配节点）⭐ 核心

**职责**：执行 2 阶段匹配——嵌入过滤 + LLM 重排。

**输入**：resume + jobs + 缓存嵌入

**输出**：`state.match_results` + `state.stage1_scores` + `state.top_k_indices`

#### 3.6.1 阶段 1: 嵌入相似度过滤

```
对所有 N 个职位:
  1. 计算职位描述的语义嵌入向量
  2. 计算与简历嵌入的余弦相似度
  3. 取 Top K（max(10, N × 20%)，且 score ≥ 0.3）
  4. 传递这些候选到阶段 2

效果: 仅 ~20% 的职位进入 LLM 阶段，节省 ~80% API 成本
```

#### 3.6.2 阶段 2: LLM 深度重排

```
对每个候选职位:
  1. 技能重合评分（词汇匹配 + 部分匹配加分）
  2. LLM 综合评估（调用 LLM，给出 0-1 评分）
  3. 批量模式: 一次 LLM 调用评估多个职位（当候选 ≥ 3 时）
  4. 权重综合:
     最终分 = 技能重合×0.3 + 语义分×0.3 + LLM 分×0.4
  5. 识别: 匹配理由 / 缺失技能 / 差距分析 / 改进建议
```

### 3.7 Node 7: 📊 RankNode（排序节点）

**职责**：对所有匹配结果按最终分数降序排列，计算平均分。

**输入**：`state.match_results`

**输出**：排序后的 `state.match_results`

### 3.8 Node 8: 📋 ReportNode（报告节点）

**职责**：生成最终报告（评分和建议），导出多格式文件（尽力而为）。

**输入**：排序后的 match_results + resume + jobs

**输出**：`state.report`（MatchReport 对象）

**容错设计**：
- 文件导出为 **best-effort**，每个格式独立 try-catch
- 即使所有导出都失败，仍从 `match_results` 构建内存版 `MatchReport`
- 节点状态标记为 `completed`，不丢失结果
- **UI 降级**：当 report 存在 → 正常渲染；report 不存在但 match_results 存在 → 直接从 match_results 渲染评分和建议

**导出格式**：
| 格式 | 依赖 | 特点 |
|------|------|------|
| 📝 Markdown | 无 | 结构化可读报告 |
| 📄 CSV | 无 | 导入电子表格 |
| 📊 Excel | openpyxl | 带颜色样式（等级着色） |
| 📦 JSON | 无 | 原始数据导出 |

---

## 4. 评分体系

### 4.1 综合评分公式

```
final_score = (skill_overlap × 0.3) 
            + (semantic_score × 0.3) 
            + (llm_score × 0.4)
```

- **技能重合权重 (0.3)**：词汇级别匹配，字符串归一化后比对
- **语义权重 (0.3)**：向量嵌入余弦相似度（捕捉语义相关性）
- **LLM 权重 (0.4)**：大模型深度理解（权重最高，最准确）

### 4.2 等级划分

| 等级 | 分数区间 | 含义 |
|------|----------|------|
| 🟢 A | ≥ 0.85 | 完美匹配 |
| 🔵 B | 0.70 — 0.84 | 良好匹配 |
| 🟡 C | 0.50 — 0.69 | 部分匹配 |
| 🔴 D | < 0.50 | 弱匹配 |

---

## 5. 数据模型

### 5.1 GraphState（图状态）

LangGraph 的有状态数据流，包含：
- **输入**：`job_urls`, `resume_file_path`, `resume_text`, `session_id`
- **爬取**：`crawler_output` (JobList)
- **标准化**：`normalized_jobs` (JobList)
- **简历**：`resume_output` (ResumeProfile), `resume_raw_text`
- **嵌入缓存**：`embedding_cache` (dict, key="resume" / "job_{index}")
- **路由**：`routing_decision`, `routing_reason`, `routing_confidence`, `router_llm_used`
- **匹配**：`stage1_scores`, `top_k_indices`, `match_results`
- **报告**：`report` (MatchReport)
- **控制**：`recrawl_count`, `max_recrawl`, `errors`, `statuses`, `progress_log`

### 5.2 JobPosting（职位）

| 字段 | 类型 | 说明 |
|------|------|------|
| title | str | 职位名称 |
| description | str | 完整职位描述 |
| company | str? | 公司名称 |
| location | str? | 工作地点 |
| url | str? | 来源 URL |
| required_skills | list[str] | 必备技能 |
| preferred_skills | list[str] | 加分技能 |
| required_experience | int? | 经验年限要求 |
| responsibilities | list[str] | 工作职责 |
| soft_skills | list[str] | 软技能要求 |
| source | JobSource | 来源（crawl/manual/api/file） |

### 5.3 ResumeProfile（简历）

| 字段 | 类型 | 说明 |
|------|------|------|
| name | str? | 候选人姓名 |
| email | str? | 邮箱 |
| phone | str? | 电话 |
| skills | list[str] | 技能列表 |
| experience_years | float? | 工作经验年数 |
| roles | list[str] | 曾担任/目标职位 |
| projects | list[Project] | 项目经历 |
| education | list[Education] | 教育背景 |
| certifications | list[str] | 证书 |
| raw_text | str? | 原始文本（LLM 回退用） |

### 5.4 MatchResult（匹配结果）

| 字段 | 类型 | 说明 |
|------|------|------|
| job | JobPosting | 匹配的职位 |
| score | float [0,1] | 综合匹配分 |
| skill_overlap | list[str] | 匹配的技能 |
| missing_skills | list[str] | 缺失的技能 |
| partial_skills | list[str] | 部分满足的技能 |
| match_reasons | list[str] | 匹配理由 |
| gaps | list[SkillGap] | 差距分析 |
| semantic_score | float? | 语义相似度分 |
| llm_score | float? | LLM 评分 |
| recommendations | list[str] | 改进建议 |

---

## 6. 基础设施

### 6.1 LLM 服务 (`LLMService`)

- **协议**：OpenAI API 兼容
- **默认模型**：`qwen3.5-plus`（通义千问）
- **系统提示词**：内置 4 套专业提示词
  - `JOB_PARSE_PROMPT`：职位解析（技能/经验提取）
  - `RESUME_PARSE_PROMPT`：简历解析（结构化转换）
  - `MATCH_PROMPT`：单职位匹配评估
  - `BATCH_MATCH_PROMPT`：⭐ 批量多职位匹配评估（JSON 数组格式）
  - `ROUTING_PROMPT`：⭐ LLM 路由决策（含质量信号评估）
- **JSON 输出**：所有 LLM 调用返回 JSON，含自动 Markdown 围栏解析修复
- **配置**：基于 `.env` 文件的 `LLM_MODEL` / `LLM_API_KEY` / `LLM_BASE_URL`

### 6.2 嵌入服务 (`EmbeddingService`)

- **双提供商支持**：
  - `openai`（默认）：OpenAI 兼容 API，默认 `text-embedding-v3`
  - `sentence-transformers`：本地模型（默认 `all-MiniLM-L6-v2`）
- **核心功能**：
  - 单文本嵌入 / 批量嵌入
  - 余弦相似度计算
  - Top-K 过滤（支持 k + ratio + min_score 三参数）
  - 预嵌入向量批量相似度计算

### 6.3 持久化存储 (`StateStore`)

- **数据库**：SQLite，WAL 模式 + `busy_timeout=5000`
- **表结构**：
  - `sessions`：会话生命周期（状态/配置/错误）
  - `jobs`：职位数据 + 嵌入向量（BLOB binary）
  - `resume`：简历数据 + 嵌入向量
  - `match_results`：匹配结果（JSON）
  - `progress_logs`：进度日志（序号递增）
- **线程安全**：`threading.local()` 连接管理
- **断点恢复**：`restore_session_state()` 重建 GraphState

### 6.4 导出服务 (`ExportService`)

| 格式 | 依赖 | 特点 |
|------|------|------|
| Markdown | 无 | 结构化可读报告 |
| CSV | 无 | 导入电子表格 |
| Excel | openpyxl | 带颜色样式（等级着色） |
| JSON | 无 | 原始数据导出 |

---

## 7. 前端界面 (Streamlit)

### 7.1 页面布局

- **左侧边栏**：简历上传/粘贴 + URL 输入 + 匹配模式配置（LLM/嵌入/2阶段）
- **主面板**：流程图可视化 + 运行按钮 + 结果展示

### 7.2 功能特性

- 实时流程图状态更新（每个节点的完成/运行/失败/跳过状态以彩色卡片展示，含箭头连接）
- 路由决策可视化（带颜色编码和原因说明）
- 2 阶段匹配进度（阶段 1 过滤数量 / 阶段 2 LLM 评估）
- 结果展开/折叠详情（匹配理由、技能重合、缺失技能、差距分析、建议）
- 分数构成展示（语义分 vs LLM 分）
- 完整对比表格（含所有维度）
- 多格式报告下载（Markdown/CSV/Excel/JSON）
- 管线日志展示
- **容错展示**：导出失败时直接从 `match_results` 渲染评分和建议

### 7.3 匹配模式配置

| 配置项 | 默认值 | 说明 |
|--------|--------|------|
| LLM 深度分析 | ✅ | 启用 LLM 综合评分 |
| 语义嵌入 | ✅ | 启用向量相似度计算 |
| 2 阶段匹配 | ✅ | 嵌入过滤 → LLM 重排 |

---

## 8. 配置体系

### 8.1 环境变量 (.env)

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `LLM_MODEL` | qwen3.5-plus | LLM 模型名 |
| `LLM_API_KEY` | — | LLM API 密钥 |
| `LLM_BASE_URL` | — | LLM API 地址 |
| `EMBEDDING_MODEL` | text-embedding-v3 | 嵌入模型名 |
| `EMBEDDING_API_KEY` | — | 嵌入 API 密钥 |
| `EMBEDDING_BASE_URL` | — | 嵌入 API 地址 |
| `DASHSCOPE_API_KEY` | — | 通义千问密钥（兼容） |
| `DASHSCOPE_BASE_URL` | — | 通义千问地址（兼容） |
| `TAVILY_API_KEY` | — | 搜索工具密钥（旧版兼容） |
| `DEBUG` | false | 调试模式 |

### 8.2 AppConfig 结构

```
AppConfig
├── LLMConfig       (model, api_key, base_url, temperature, max_tokens)
├── EmbeddingConfig (provider, model, api_key, base_url)
├── CrawlConfig     (use_playwright, timeout, max_urls)
├── MatchingConfig
│   ├── weights:    semantic=0.3, llm=0.4, overlap=0.3
│   ├── two_stage:  true
│   ├── stage1_top_k: 10
│   ├── stage1_top_ratio: 0.2
│   └── stage1_min_score: 0.3
├── RoutingConfig ⭐
│   ├── resume_min_skills: 3
│   ├── jobs_min_count: 1
│   ├── max_recrawl: 1
│   ├── use_llm_router: true
│   ├── llm_confidence_threshold: 0.6
│   ├── enable_refine_crawl: true
│   └── enable_ask_user: true
└── debug: false
```

---

## 9. 技术栈

| 层次 | 技术 | 用途 |
|------|------|------|
| 框架 | LangGraph | 状态图编排 |
| 语言 | Python 3.14 | 运行时 |
| API | OpenAI SDK | LLM / Embedding 调用 |
| 前端 | Streamlit | 交互式仪表板 |
| 持久化 | SQLite | 会话状态存储 |
| 爬虫 | requests + BeautifulSoup + Playwright | 静态/动态页面抓取 |
| 文本提取 | trafilatura + pdfplumber + python-docx | HTML/PDF/DOCX 解析 |
| 嵌入 | sentence-transformers / API | 语义向量计算 |
| 导出 | openpyxl + csv | Excel / CSV 生成 |
| 模型 | Pydantic v2 | 数据验证与序列化 |
| 配置 | python-dotenv | 环境变量管理 |
| API | FastAPI + Uvicorn | REST 服务 |
| 部署 | Docker / docker-compose | 容器化 |

---

## 10. 数据流示例

```
用户输入:
  URL: ["https://example.com/jobs/senior-python-dev"]
  简历: [上传 PDF]

流程:
  1. CrawlNode:     适配器路由 → API/Playwright/BS4 → 文本提取 → LLM 解析
  2. NormalizeNode: LLM 字段增强（技能/经验/职责）
  3. ResumeEmbedNode: PDF 解析 → LLM 提取技能/项目/教育 → 嵌入计算 → 缓存
  4. RoutingNode:   LLM+规则混合决策 → "match"
  5. MatchNode:     
     ┌─ Stage 1:    嵌入相似度 → Top 20% 候选
     └─ Stage 2:    批量 LLM 评估 → 综合评分 + 差距分析 + 建议
  6. RankNode:      按分数排列
  7. ReportNode:    构建报告（导出失败仍可展示评分）→ 导出CSV/Excel/MD/JSON

最终输出:
  - 排名列表（含A/B/C/D等级）
  - 每个职位的匹配理由、缺失技能、改进建议
  - 分数构成（语义分 vs LLM 分）
  - CSV / Excel / Markdown / JSON 导出（尽力而为）
  - 即使导出失败，评分和建议仍在 UI 展示
```

---

## 11. 容错设计

| 场景 | 处理方式 |
|------|----------|
| **报告导出失败** | 每个格式独立 try-catch；构建内存版 MatchReport；UI 直接从 match_results 渲染评分和建议 |
| **LLM API 不可用** | 降级到规则匹配（技能重合 + 嵌入相似度） |
| **嵌入 API 不可用** | 跳过阶段 1，使用全量 LLM 匹配 |
| **爬虫解析失败** | requests → Playwright → LLM 解析 逐级降级 |
| **LLM JSON 输出格式错误** | `_safe_json()` 自动去除 Markdown 围栏并修复 |
| **无有效的职位 URL** | 返回空 JobList，路由节点 → "end" |
| **简历文件解析失败** | 尝试 OCR 降级，返回带原始文本的空 Profile |
| **SQLite 写入失败** | 日志警告，不影响当前运行 |
| **多线程访问** | SQLite 使用 `threading.local()` + WAL 模式 |

---

## 12. API 接口

### 12.1 端点列表

| 端点 | 方法 | 说明 |
|------|------|------|
| `/health` | GET | 健康检查（无认证） |
| `/match` | POST | 执行全流程匹配 |
| `/sessions` | GET | 列出最近会话 |
| `/sessions/{id}` | GET | 获取会话详情 |
| `/sessions/{id}/report` | GET | 下载报告（支持 csv/xlsx/md/json） |

### 12.2 调用示例

```bash
curl -X POST http://localhost:8000/match \
  -H "Content-Type: application/json" \
  -d '{
    "job_urls": ["https://careers.tencent.com/jobdesc.html?postId=XXX"],
    "resume_text": "5年Python开发经验，精通FastAPI、Kubernetes...",
    "use_llm": true,
    "use_embedding": true
  }'

curl http://localhost:8000/health
```

---

## 13. 未来扩展方向

- [ ] **持续学习**：保存历史匹配反馈，优化评分权重
- [ ] **多简历支持**：一次运行匹配多个候选人与多个职位
- [ ] **异步队列**：使用 Celery / Redis 队列处理大规模批次
- [ ] **Webhook 通知**：匹配完成后回调通知
- [ ] **报告模板**：用户可自定义报告输出格式和品牌样式
- [ ] **缓存层**：使用 Redis 替代内存字典作为嵌入缓存
- [ ] **更多源支持**：直接集成 LinkedIn、Boss 直聘等 API
- [ ] **生产部署**：认证、限流、HTTPS、PostgreSQL、K8s
