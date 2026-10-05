# 🧠 Job Matching AI Agent

多模块协同的任务型 Agent 系统，基于 **LangGraph Decision Graph** 构建，支持职位爬取、简历解析、智能匹配和报告生成。

## 📐 架构

```
                          ┌────────────┐
                          │  CrawlNode  │  🔍 适配器自动路由
                          └─────┬───────┘
                               │
                          ┌─────▼───────┐
                          │ NormalizeNode│  ⚙️  LLM字段富化
                          └─────┬───────┘
                               │
                          ┌─────▼───────────┐
                          │ ResumeEmbedNode  │  📄 简历解析 + 嵌入
                          └─────┬───────────┘
                               │
                    ┌───────────▼──────────────┐
                    │  RoutingNode (⭐关键)     │  🧭 LLM+规则混合路由
                    └─────┬──────────┬─────────┘
                         ╱            │        │
                        ▼             ▼        ▼
                  MATCH NODE     RECRAWL    END/ASK_USER
                  (2阶段匹配)    (重试循环)
                        │
                        ▼
                  RANK → REPORT → END
```

### 🧩 站点适配器架构

```
URL ──→ Adapter Registry ──→ TencentAdapter      careers.tencent.com   (API直连)
                          ├── ByteDanceAdapter    jobs.bytedance.com   (Playwright+API)
                          ├── GenericSPAAdapter   未知SPA站点          (Playwright+LLM)
                          └── GenericHTMLAdapter   传统HTML页面         (requests+BS4)
                                          
            新的招聘站点只需新增一个 Adapter 文件，一行注册即可
```

## ✨ 核心特性

| 特性 | 说明 |
|------|------|
| **决策图** | LangGraph 8节点 StateGraph，带 LLM+规则混合路由 |
| **路由节点 ⭐** | LLM + 规则混合路由，5 种决策：match/recrawl/refine_crawl/ask_user/end |
| **2-Stage Matching** | Embedding 快速过滤 → LLM 深度评分，成本 ↓80% |
| **Batch LLM 匹配** | 批量调用 LLM 评估多职位，节省 API 调用次数 |
| **站点适配器** | 腾讯/字节跳动/通用SPA/通用HTML 四层回落自适应爬虫 |
| **SQLite State Store** | 全流程状态持久化，支持断点恢复 `--session` |
| **动态爬虫** | requests → Playwright 自动降级，SPA 站点 JS 渲染 |
| **多格式简历** | PDF / DOCX / TXT，LLM 结构化提取 |
| **多格式导出** | Excel / CSV / Markdown / JSON |
| **REST API** | FastAPI 5个端点，支持远程调用 |
| **容错报告** | 导出失败不阻断结果显示，评分和建议保证 UI 可见 |

## 🚀 快速开始

```bash
# 1. 安装依赖
pip install -r requirements.txt

# 2. 安装 Playwright 浏览器（动态页面爬取用）
playwright install chromium

# 3. 配置密钥
cp .env.example .env
# 编辑 .env 填入 DASHSCOPE_API_KEY 或其他 LLM API Key

# 4. CLI 运行
python main.py --urls "https://careers.tencent.com/jobdesc.html?postId=XXX" --resume "resume.pdf"

# 5. 或启动 Web UI
streamlit run app/ui/streamlit_app.py

# 6. 或启动 API 服务
uvicorn app.api.server:app --port 8000
```

### 配置示例

```bash
# .env — DashScope (阿里通义千问)
DASHSCOPE_API_KEY=sk-xxxxx        # 替换为真实密钥
DASHSCOPE_BASE_URL=https://dashscope.aliyuncs.com/compatible-mode/v1
LLM_MODEL=qwen3.5-plus

# 或切换到 OpenAI
# LLM_API_KEY=sk-xxxxx
# LLM_BASE_URL=https://api.openai.com/v1
# LLM_MODEL=gpt-4o
```

## 📦 项目结构

```
repo
├── app/
│   ├── agents/                    # 智能体模块
│   │   ├── adapters/              # 站点适配器（新增）
│   │   │   ├── base.py            适配器基类
│   │   │   ├── tencent.py         腾讯招聘 (API直连)
│   │   │   ├── bytedance.py       字节跳动 (Playwright)
│   │   │   ├── generic_html.py    通用HTML (requests+BS4)
│   │   │   ├── generic_spa.py     通用SPA (Playwright+LLM)
│   │   │   └── __init__.py        注册中心/自动路由
│   │   ├── crawl_agent.py         通用爬虫（适配器调度器）
│   │   ├── resume_agent.py        简历解析
│   │   ├── matching_agent.py      匹配引擎（2阶段+批量LLM）
│   │   ├── ranking_agent.py       排序+导出
│   │   └── job_advisor.py         旧版单智能体（保留兼容）
│   ├── api/                       # REST API
│   │   ├── server.py              FastAPI 服务
│   │   └── middleware/            认证、限流中间件
│   ├── core/                      # LangGraph 调度层
│   │   ├── orchestrator.py        决策图引擎
│   │   ├── state.py               状态定义
│   │   ├── state_store.py         SQLite 持久化+断点恢复
│   │   └── config.py              config.yaml + .env 双加载
│   ├── models/                    Pydantic 数据模型
│   │   ├── job.py                 JobPosting / JobList
│   │   ├── resume.py              ResumeProfile
│   │   └── match.py               MatchResult / MatchReport
│   ├── services/                  共享服务
│   │   ├── llm_service.py         LLM 统一接口（含超时）
│   │   ├── embedding_service.py   向量嵌入
│   │   └── export_service.py      多格式导出
│   ├── ui/                        Streamlit 前端
│   │   └── streamlit_app.py       实时流程图+结果展示
│   └── utils/                     工具函数
│       ├── logger.py              日志
│       └── helpers.py             URL验证、文本清洗
├── main.py                        CLI 入口
├── config.yaml                    运行时配置
├── requirements.txt
└── .env.example                   环境变量模板
```

## 🛣️ 工作流程

```
 ┌──── 输入 ────┐
 │ 职位URLs + 简历 │
 └──────┬───────┘
        ▼
┌────────────────────────────────────────────────────────────────┐
│ 1. CrawlNode     适配器自动识别URL → API/Playwright/BS4 → JobList │
│ 2. NormalizeNode  LLM字段提取（技能、经验、职责）                  │
│ 3. ResumeEmbed    PDF/DOCX/TXT → LLM结构化 → 嵌入向量           │
│ 4. RoutingNode ⭐  LLM+规则混合决策                               │
│    ├─ match         → 进入匹配阶段                               │
│    ├─ recrawl       → 重试抓取（相同策略）                       │
│    ├─ refine_crawl  → 优化重抓（不同策略，如 Playwright）        │
│    ├─ ask_user      → 数据不足，请求人工介入                     │
│    └─ end           → 无法恢复，终止流程                         │
│ 5. MatchNode      嵌入过滤(Top20%) → LLM深度评分 → 差距分析     │
│ 6. RankNode       综合分数排序                                   │
│ 7. ReportNode     生成报告（导出失败仍可显示评分和建议）         │
└────────────────────────────────────────────────────────────────┘
        ▼
 ┌──── 输出 ────┐
 │ 排名+评分+建议 │
 └──────────────┘
```

## ⚙️ 配置

编辑 `config.yaml`（优先级低于 `.env`）：

```yaml
matching:
  two_stage: true          # 启用 2-Stage 匹配
  stage1_top_k: 10         # Embedding 过滤 top 10
  stage1_top_ratio: 0.2    # 或 top 20%
  semantic_weight: 0.3     # 语义权重
  llm_weight: 0.4          # LLM 推理权重
  overlap_weight: 0.3      # 关键词重叠权重

routing:
  resume_min_skills: 3     # 简历最少技能数
  jobs_min_count: 1        # 最少职位数
  max_recrawl: 1           # 最多重试次数
  use_llm_router: true     # 启用 LLM 混合路由
  llm_confidence_threshold: 0.6  # LLM 决策置信度阈值
  enable_refine_crawl: true      # 允许优化重抓
  enable_ask_user: true          # 允许请求人工介入
```

## 📊 评分公式

```
final_score = overlap_score × 0.3 + semantic_score × 0.3 + llm_score × 0.4
```

| 等级 | 分数 | 含义 |
|------|------|------|
| 🟢 A | ≥ 0.85 | 完美匹配 |
| 🔵 B | 0.70–0.84 | 良好匹配 |
| 🟡 C | 0.50–0.69 | 部分匹配 |
| 🔴 D | < 0.50 | 弱匹配 |

## 🛠 技术栈

- **框架**: LangGraph (StateGraph), FastAPI
- **LLM**: Qwen (DashScope) / OpenAI / Anthropic / DeepSeek
- **嵌入**: text-embedding-v3 / sentence-transformers
- **爬虫**: requests + BeautifulSoup + Playwright + 站点API
- **解析**: pdfplumber + python-docx + trafilatura
- **持久化**: SQLite (WAL模式)
- **前端**: Streamlit
- **Python**: 3.14+

## 🔧 三种运行方式

```bash
# CLI
python main.py -u "URL" -r "resume.pdf"

# Web UI (浏览器)
streamlit run app/ui/streamlit_app.py

# REST API
uvicorn app.api.server:app --port 8000
curl http://localhost:8000/health
curl -X POST http://localhost:8000/match \
  -H "Content-Type: application/json" \
  -d '{"job_urls":["URL"],"resume_text":"..."}'
```

## 🔌 支持抓取的招聘站点

| 平台 | 适配器 | 方式 | 状态 |
|------|--------|------|------|
| 腾讯招聘 `careers.tencent.com` | TencentAdapter | 公开 API 直连 | ✅ |
| 字节跳动 `jobs.bytedance.com` | ByteDanceAdapter | Playwright + API | ✅ |
| 阿里 `talent.alibaba.com` | GenericSPAAdapter | Playwright 渲染 + LLM | ✅ |
| Boss直聘/拉勾/猎聘等 | GenericSPAAdapter | Playwright + LLM | ✅ |
| 传统HTML招聘页面 | GenericHTMLAdapter | requests + BS4 + LLM | ✅ |
| 其他任意页面 | 自动路由 | 按优先级探测 | ✅ |

> 扩展新平台：只需写一个继承 `BaseSiteAdapter` 的类，实现 `can_handle()` 和 `fetch_job()` 两个方法，在 `adapters/__init__.py` 注册即可。

## 🧠 路由节点详解 (⭐ 关键升级)

路由节点是系统的决策核心，采用 **LLM + 规则混合策略**：

### 决策流程

```

数据质量信号 → LLM Router → 置信度≥0.6? → 是 → LLM决策
                                        → 否 → 规则回退
```

### 5 种决策

| 决策 | 触发条件 | 说明 |
|------|----------|------|
| `match` | 数据充足，进入匹配 | 正常流程 |
| `recrawl` | 无职位但有重试次数 | 相同策略重试 |
| `refine_crawl` | 数据质量差（描述短/技能少） | 切换抓取策略（如启用 Playwright） |
| `ask_user` | 关键数据缺失（如简历质量差） | 暂停等待人工输入 |
| `end` | 无法恢复的错误 | 终止流程 |

## 💡 容错机制

| 故障场景 | 处理方式 |
|----------|----------|
| **导出文件生成失败** | 不影响 `MatchReport` 生成和 UI 展示，评分和建议始终可见 |
| **LLM API 不可用** | 降级到规则匹配（技能重合 + 嵌入相似度） |
| **嵌入 API 不可用** | 跳过阶段 1，使用全量 LLM 匹配 |
| **爬虫解析失败** | requests → Playwright → LLM 解析 逐级降级 |
| **SQLite 写入失败** | 日志警告，不影响当前运行 |

## 📡 API 端点

| 端点 | 方法 | 说明 |
|------|------|------|
| `/health` | GET | 健康检查 |
| `/match` | POST | 执行全流程匹配 |
| `/sessions` | GET | 列出最近会话 |
| `/sessions/{id}` | GET | 获取会话详情 |
| `/sessions/{id}/report` | GET | 下载报告（支持 csv/xlsx/md/json） |
