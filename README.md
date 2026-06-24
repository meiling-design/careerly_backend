# 🧠 Job Matching AI Agent

多模块协同的任务型 Agent 系统，基于 **LangGraph Decision Graph** 构建，支持职位爬取、简历解析、智能匹配和报告生成。

## 📐 架构

```
           ┌────────────┐
           │  CrawlNode  │  🔍 抓取职位URL
           └─────┬───────┘
                 ↓
        ┌─────────────────┐
        │  NormalizeNode   │  ⚙️  LLM字段富化
        └─────┬───────────┘
              ↓
     ┌──────────────────────┐
     │  ResumeEmbedNode     │  📄 简历解析 + 嵌入
     └─────┬───────────────┘
           ↓
   ┌──────────────────────────┐
   │  RoutingNode (⭐关键)     │  🧭 条件路由
   └─────┬───────────┬────────┘
         ↓           ↓
   MATCH NODE    RECRAWL NODE  🔄 重试循环
         ↓
   RANK → REPORT → END
```

## ✨ 核心特性

| 特性 | 说明 |
|------|------|
| **决策图** | LangGraph 8节点 StateGraph，带条件路由 |
| **2-Stage Matching** | Embedding 快速过滤 → LLM 深度评分，成本 ↓80% |
| **SQLite State Store** | 全流程状态持久化，支持断点恢复 |
| **动态爬虫** | requests → Playwright 自动降级 |
| **多格式简历** | PDF / DOCX / TXT，LLM 结构化提取 |
| **多格式导出** | Excel / CSV / Markdown / JSON |

## 🚀 快速开始

```bash
# 1. 安装依赖
pip install -r requirements.txt

# 2. 配置密钥
cp .env.example .env
# 编辑 .env 填入 DASHSCOPE_API_KEY

# 3. CLI 运行
python main.py --urls "https://example.com/job/1" --resume "resume.pdf"

# 4. 或启动 Web UI
streamlit run app/ui/streamlit_app.py
```

## 📦 项目结构

```
repo
├── app/
│   ├── agents/         # 4 个核心 Agent
│   │   ├── crawl_agent.py
│   │   ├── resume_agent.py
│   │   ├── matching_agent.py
│   │   └── ranking_agent.py
│   ├── core/           # LangGraph 调度层
│   │   ├── orchestrator.py   # 决策图引擎
│   │   ├── state.py          # 状态定义
│   │   ├── state_store.py    # SQLite 持久化
│   │   └── config.py         # 全局配置
│   ├── models/         # Pydantic 数据模型
│   ├── services/       # 共享服务 (LLM / Embedding / Export)
│   ├── ui/             # Streamlit 前端
│   └── utils/          # 工具函数
├── main.py             # CLI 入口
├── config.yaml         # 运行时配置
├── requirements.txt
├── .env.example        # 环境变量模板
└── README.md
```

## ⚙️ 配置

编辑 `config.yaml` 或 `.env`：

```yaml
matching:
  two_stage: true          # 启用 2-Stage 匹配
  stage1_top_k: 10         # Embedding 过滤 top 10
  stage1_top_ratio: 0.2    # 或 top 20%
  semantic_weight: 0.3     # 语义权重
  llm_weight: 0.4          # LLM 推理权重
  overlap_weight: 0.3      # 关键词重叠权重
```

## 📊 评分公式

```
final_score = overlap_score × 0.3 + semantic_score × 0.3 + llm_score × 0.4
```

## 🛠 技术栈

- **框架**: LangGraph (StateGraph), FastAPI (预备)
- **LLM**: Qwen (DashScope) / OpenAI / Claude
- **嵌入**: text-embedding-v3 / sentence-transformers
- **爬虫**: requests + BeautifulSoup + Playwright
- **解析**: pdfplumber + python-docx + trafilatura
- **前端**: Streamlit
