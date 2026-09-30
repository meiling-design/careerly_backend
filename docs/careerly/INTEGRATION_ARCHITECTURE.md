# Careerly Backend × Job Matching Agent 整合架构审计

> **审计对象**：`G:\ai_agent\work_agent`（Python / FastAPI / LangGraph） + `G:\ai_agent\careerly`（Flutter）
> **审计性质**：只读代码审计 + 整合方案设计。**本阶段不写任何业务代码、不做任何重构。**
> **上游依据**：`docs/careerly/BACKEND_API_CONTRACT.md`、`careerly/docs/*`（PRD V2.1 / IA V2.1 / Flutter Spec V2.1）
> **结论语言**：所有现状判断均可回溯到具体文件；未经验证的推断已显式标注「待核对」。

---

## 0. 审计结论速览

### 0.1 先纠正三个前提假设

你发来的任务描述建立在三个假设之上。审计发现：**其中三个都不成立**，而这恰恰是好消息。

| # | 你的假设 | 审计事实 | 影响 |
|---|---|---|---|
| 1 | 存在「Careerly Backend」和「Job Matching AI Agent」**两个项目**，需要整合 | **不存在两个项目。** Careerly 后端层已经长在 `work_agent` 仓库内部，是一个仓库里的三层代码。分支 `careerly-backend`（本地未合并 main） | 不需要「合并项目」，也不需要「两套 FastAPI 互调」。要做的是**收敛边界 + 补全缺口** |
| 2 | 需要**新建** Agent Adapter / Service Layer | **适配层已经存在**，就是 `app/careerly/engine.py`（`DecisionGraph.run_sync()` 桥接，`engine.py:212` `run_analysis`） | 不需要新建，需要**加固 + 补 user 上下文** |
| 3 | 需要决定「是否复制一份 Agent 代码」 | **从未复制，也永远不该复制。** 只有一份引擎：`app/core/orchestrator.py` | 这条约束你已经实现了，继续保持 |

**所以本次审计的命题要从「如何整合两个项目」改写为：**

> **如何把已经共处一室的引擎层 / 适配层 / 产品层，收敛出清晰边界，并补齐端到端跑通所缺的那几块。**

### 0.2 真实缺口清单（按严重度）

| 级别 | 编号 | 缺口 | 证据 | 影响 |
|---|---|---|---|---|
| 🔴 | **G1** | **App 连不上后端**：Android 未声明 `INTERNET` 权限、未放行明文 HTTP | `careerly/android/app/src/main/AndroidManifest.xml` 无 `uses-permission` | 联调第一步即失败，一切后端工作无法验证 |
| 🔴 | **G2** | **越权访问**：`/api/v1/resume/upload`、`/api/v1/tasks/{id}/result` 无鉴权；`/tasks/{id}`、`/sessions/{id}` 有鉴权但无归属校验 | `app/api/router_app.py:142,212`；`app/api/router_v1.py` | 安全一票否决项。凭 id 可读他人分析结果 |
| 🔴 | **G3** | **「看见成长」数据层为零**：无 `capability_snapshots` 时序表、无 `growth_events` 表 | `app/careerly/store.py` 只有 7 张表，`capability_snapshot` 仅是 `analysis_results` 的一个 TEXT 列（`store.py:109`） | Careerly 的核心产品表达（能力变化 / Gap 收窄）**无法闭环** |
| 🟠 | **G4** | **同步分析 210s 的移动端风险**：`POST /analysis` 同步阻塞，Flutter 超时 300s | `app/careerly/router.py:49-95`；Flutter `api_client.dart` | 弱网 / 切后台 / 锁屏必失败，且失败后任务无痕 |
| 🟠 | **G5** | **API 表面重复**：根路径 `/health` `/match` `/sessions*` 与 `/api/v1/*` 平行存在 | `app/api/server.py:188,198,237,256,293` | 两套任务语义（异步 TaskTracker vs 同步 analysis）并存，契约漂移 |
| 🟠 | **G6** | **token 不落盘**：Flutter 侧 token 仅内存（D52 裁决） | `careerly/lib/data/api/api_client.dart:68-84` | 冷启动即失忆 → 后端配额 / 用户隔离**在产品上无意义** |
| 🟡 | **G7** | 引擎技术债：Playwright 并发池未接线、CacheService 未接线、无 LangGraph checkpointer、`restore_graph_state` 空占位 | `app/agents/adapters/playwright_pool.py` 无调用方 | 高并发下有资源风险；任务无法断点续跑 |
| 🟡 | **G8** | 测试覆盖薄：仅规则与纯函数的确定性测试 | `tests/test_routing.py`、`tests/test_matching.py` | 无 API / 认证 / 多用户 / 爬虫集成测试，重构无安全网 |

### 0.3 已经做对的事（不要动）

- ✅ **一次架构升级而非推倒重来** —— Agent 的 LangGraph / 爬虫 / Embedding / 匹配 / 排序**全部保留**，代码零复制。
- ✅ **双库物理分离已落地** —— `graph_state.db`（引擎会话状态）与 `careerly.db`（业务数据）是两个独立 SQLite 文件，边界方向正确。
- ✅ **业务表已按 `user_id` 隔离** —— `careerly.db` 全部 7 张表带 `user_id` 列，索引就位（`store.py:141-143`）。
- ✅ **契约冲突 C1 / C3 / C4 已解决** —— 手机号身份、配额扣减、能力分值模型均已落地（详见 §1.4）。
- ✅ **统一响应信封 + JWT 双 token** —— `envelope.py` + `security.py`（HS256，access 60min / refresh 7d）。
- ✅ **Flutter 侧防御性解析** —— `api_resume_repository.dart:51-88` 对后端单版本基线做了兼容合成，说明前端已按「后端能力子集」适配，未过度索求。

---

## 1. 现状事实基线

### 1.1 三个代码资产的物理位置

```
G:\ai_agent\
├── work_agent\                    ← Python 单仓库（分支 careerly-backend，未合并 main）
│   ├── app\
│   │   ├── core\                  ← 【L1 引擎层】orchestrator.py(DecisionGraph) / state.py
│   │   │                             state_store.py(graph_state.db) / security.py / auth_store.py
│   │   ├── agents\                ← 【L1 引擎层】crawl / resume / matching / ranking / job_advisor
│   │   │   └── adapters\          ←   tencent / bytedance / generic_html / generic_spa
│   │   ├── models\                ← 【L1 引擎层】job.py / resume.py / match.py
│   │   ├── services\              ← 【L1 引擎层】embedding / llm / cache / demo_guard / export
│   │   ├── careerly\              ← 【L2 适配层】engine.py(Adapter!) / schemas.py / store.py
│   │   │                             router.py / capability.py            (careerly.db)
│   │   └── api\                   ← 【L3 API 层】server.py + router_v1 / router_auth
│   │                                 router_app / envelope.py / middleware
│   ├── docs\careerly\
│   │   ├── BACKEND_API_CONTRACT.md        ← 已有契约文档（目标态）
│   │   └── INTEGRATION_ARCHITECTURE.md    ← 本文档（整合审计）
│   ├── data\app\  → graph_state.db + careerly.db（运行时）
│   └── scripts\e2e_careerly.py            ← 已有端到端验证脚本
│
└── careerly\                      ← Flutter App（分支 feature/phase6-analysis）
    ├── lib\data\api\              ← 已接真实后端：auth / analysis / resume（3 个）
    ├── lib\data\mock\             ← 仍走 Mock：home / ai / growth / career_assets / profile
    └── docs\05_progress\          ← CURRENT_STATUS.md / TODO.md / CHANGELOG.md
```

**关键判断**：`work_agent` 是一个**单进程单 FastAPI**，同时承担三层职责。这不是「两个后端」，也不是「微服务」。

### 1.2 Python 侧三层职责（已在位）

| 层 | 目录 | 职责 | 对外暴露 |
|---|---|---|---|
| **L1 引擎层** | `app/core` `app/agents` `app/models` `app/services` | 「**怎么分析**」：LangGraph 8 节点流水线、爬虫、解析、Embedding、LLM 匹配、排序、报告 | 无 HTTP。仅 Python 方法 |
| **L2 适配层** | `app/careerly` | 「**翻译**」：把 Careerly 领域数据 → 引擎输入；引擎输出 → Careerly 领域模型；业务持久化 + 配额 | 无 HTTP（router.py 除外） |
| **L3 API 层** | `app/api` | 「**谁在用 / 怎么拿**」：认证、信封、路由、限流、异常 | HTTP（`/api/v1/*`） |

**L1 与 L2 的边界目前不干净**的两处：

1. `app/careerly/router.py` 混在 L2 包里 —— 它其实是 L3 API 层代码，应与 `app/api/` 同层。
2. `app/core/auth_store.py` 与 `app/careerly/store.py` 是**两个独立 SQLite**，但 `auth_store` 归属 L3（产品用户），却放在 L1 的 `core/` 下 —— 概念归属错位。

### 1.3 LangGraph 引擎内部（L1）

**图结构**（`app/core/orchestrator.py`，`DecisionGraph`）：

```
crawl → normalize → resume_embed → routing ─┬─→ match → rank → report → END
                                            ├─→ recrawl ──┐（回边）
                                            │   (refine_crawl 同节点)
                                            └─→ END（ask_user / end）
                        recrawl ────────────────┘→ normalize
```

- **8 个节点**：`crawl` `normalize` `resume_embed` `routing` `recrawl` `match` `rank` `report`
- **条件路由枚举**：`match` / `recrawl` / `refine_crawl` / `ask_user` / `end`
- **路由判定**：LLM 混合决策，置信度低于阈值（0.6）回退规则；`end` 为强制覆盖项
- **容错**：每个节点均包 `try/except`，错误累积进 `state.errors`，不中断整图 —— **这是很扎实的设计，必须保留**
- **缺失**：无 checkpointer、无 `interrupt` / HITL。`ask_user` 只是终止分支，不是真的人机介入
- **恢复机制**：仅 `restore_session_state()` 的手动 best-effort 重放，非 checkpoint 续跑

**领域模型**（`app/models/`）：

| 模型 | 字段 |
|---|---|
| `JobPosting` | title, location, company, department, description, url, required_skills, preferred_skills, required_experience, responsibilities, soft_skills, source, crawled_at, raw_text |
| `ResumeProfile`（引擎侧） | name, email, phone, skills, experience_years, roles, projects, education, certifications, raw_text |
| `MatchResult` | job, score(0-1), skill_overlap, missing_skills, partial_skills, match_reasons, gaps(SkillGap), semantic_score, llm_score, recommendations |
| `SkillGap` | skill, severity, suggestion |
| `MatchReport` | resume, results, total_jobs, average_score |

**多用户支持现状（关键）**：引擎层**完全不知道 user 的存在**。

- `GraphState`（`app/core/state.py`）无 `user_id` 字段
- `state_store.sessions` 表无 `user_id` 列，session 仅由随机 uuid 标识
- `app/careerly/engine.py:212` `run_analysis()` 调用引擎时**不传 user_id**
- → **用户隔离只发生在 L2 的 `careerly.db` 表层，引擎内部是匿名的**

### 1.4 Flutter 侧对接现状

**已接真实后端（`CAREERLY_USE_API=true` 时）**：auth / analysis / resume 三个仓储。

**仍走 Mock**：home / ai / growth / career_assets / profile —— 即 5 个 feature 的数据源是假的。

**已对齐的契约细节**：

- JSON key 风格：**camelCase**（与后端 `schemas.py` 一致）✅
- 信封解包：`ApiClient` 负责剥 `data` 层 ✅
- baseUrl：`--dart-define=CAREERLY_API_BASE_URL`，默认 `http://10.0.2.2:8000` ✅
- 认证：`POST /api/v1/auth/register|login`（手机号，无密码）+ 401 自动 refresh 重试一次 ✅

**契约文档 vs 代码的实际偏差**（`BACKEND_API_CONTRACT.md` §1 描述的是**目标态**）：

| 契约文档写的 | 代码 + Flutter 实际的 | 判断 |
|---|---|---|
| `GET /api/v1/analysis/quota` | `GET /api/v1/quota` | 文档超前。代码与 Flutter 一致 |
| `GET /api/v1/resume/profile` | `GET /api/v1/resume` | 文档超前。代码与 Flutter 一致 |
| `POST /api/v1/resume/text` | `PUT /api/v1/resume` | 文档超前。代码与 Flutter 一致 |
| `POST /api/v1/resume/file` | 未实现（Flutter 主动抛错） | 两端一致地「未实现」 |
| `POST /api/v1/resume/baseline` | 未实现（Flutter 用 fetchProfile 合成） | 两端一致地「未实现」 |
| `/growth/*` `/career/assets` `/home/summary` `/profile` `/ai/messages` | 未实现（Flutter 走 Mock） | 整段未落地 |

→ **结论：前后端代码之间是一致的，偏差在「文档」与「实现」之间。** 契约文档需要回写标注「已落地子集」，避免后续开发照着目标态重复造。

**已解决的契约冲突**：C1（手机号身份）✅ / C3（配额）✅ / C4（能力分值）✅
**未决**：C2（同步 vs 异步，已选同步但风险见 G4）、C5（会话持久化，D52 不存 → 见 G6）

---

## A. 三层整体架构图

### A.1 目标架构（推荐 · 最小改动）

```
┌────────────────────────────────────────────────────────────────┐
│                      Careerly Flutter App                       │
│              （仓库 careerly · 分支 feature/phase6-analysis）      │
│                                                                 │
│  onboarding │ home │ analysis │ ai │ profile │ footprint        │
│      │          │        │        │       │         │           │
│      └──────────┴────────┴────────┴───────┴─────────┘           │
│                          │                                      │
│                   ApiClient（http · 内存 token · 信封解包）        │
└──────────────────────────┼──────────────────────────────────────┘
                           │  HTTPS / JSON (camelCase)
                           │  ★ 缺口 G1：Android INTERNET 权限未声明
                           ▼
┌────────────────────────────────────────────────────────────────┐
│              Careerly Backend（仓库 work_agent · 单进程）          │
│                                                                │
│  ┌──────────────────────────────────────────────────────────┐  │
│  │ L3 API 层   app/api/                                     │  │
│  │   server.py · envelope.py · middleware/                   │  │
│  │   router_auth.py   /api/v1/auth/*        ← 手机号 + JWT    │  │
│  │   router_careerly  /api/v1/analysis|resume|quota|...      │  │
│  │   router_v1.py     /api/v1/match|tasks|sessions  (内部)    │  │
│  │   router_app.py    /api/v1/resume/upload|tasks/*/result   │  │
│  └───────────────────────┬──────────────────────────────────┘  │
│                          │ Depends(get_current_user) → user_id  │
│  ┌───────────────────────▼──────────────────────────────────┐  │
│  │ L2 适配层（Careerly 产品语义）  app/careerly/               │  │
│  │   engine.py      ★ Agent Adapter（已存在）                 │  │
│  │   capability.py    能力快照归一化（已存在）                  │  │
│  │   store.py         careerly.db（7 表，user_id 隔离）        │  │
│  │   schemas.py       契约 Pydantic 模型（已存在）              │  │
│  │   ──────────────────────────────────────────────────────  │  │
│  │   ★ 缺口 G3：缺 capability_snapshots 时序表 / growth_events │  │
│  └───────────────────────┬──────────────────────────────────┘  │
│                          │  进程内方法调用（非 HTTP！）            │
│  ┌───────────────────────▼──────────────────────────────────┐  │
│  │ L1 引擎层（AI 能力，与产品语义无关）  app/core · agents      │  │
│  │   DecisionGraph（LangGraph · 8 节点）                      │  │
│  │   crawl → normalize → resume_embed → routing              │  │
│  │            ↓ (match | recrawl | refine_crawl | ask_user)   │  │
│  │        match → rank → report                              │  │
│  │   agents/：crawl · resume · matching · ranking · advisor    │  │
│  │   adapters/：tencent · bytedance · generic_html · spa       │  │
│  │   services/：embedding · llm · cache · export              │  │
│  └───────────────────────┬──────────────────────────────────┘  │
│                          │                                      │
│         ┌────────────────┴────────────────┐                     │
│         ▼                                 ▼                     │
│  ┌──────────────────┐            ┌────────────────────┐        │
│  │ data/careerly.db │            │ data/graph_state.db│        │
│  │ 业务真相          │            │ 引擎运行态          │        │
│  │ 7 表 · user_id    │            │ sessions/jobs/...   │        │
│  └──────────────────┘            └────────────────────┘        │
└────────────────────────────────────────────────────────────────┘
```

### A.2 三条铁律（架构边界）

```
铁律 1   Flutter 只认识 Careerly API。
         永不直接调 /api/v1/match。LangGraph / Qwen / Playwright / 爬虫
         对 App 完全不可见。

铁律 2   L2 → L1 是进程内方法调用，不是 HTTP。
         绝不为引擎单独起一个 FastAPI 服务（不引入第二套服务）。

铁律 3   Careerly 领域模型 ≠ 引擎领域模型。
         两者只能通过 engine.py 的显式映射函数互转，
         禁止让 MatchResult 直接变成 API 响应，也禁止让 JobPosting 落进 careerly.db。
```

### A.3 与「你设想的目标架构」的差异

| 你的设想 | 审计建议 | 原因 |
|---|---|---|
| Agent Adapter 需要**新建** | 已存在，**加固**即可 | `engine.py` 已在做映射，缺的是 user 上下文与失败语义 |
| Agent 是**独立部署单元** | 保持**同进程同仓库** | 当前规模下拆服务只增运维成本，无收益 |
| 两个 FastAPI 互调 | **明确否决** | 进程内调用延迟 <1ms，HTTP 往返 + 序列化纯属浪费 |

---

## B. 模块复用矩阵

| 模块 | 当前所在 | 是否复用 | 如何接入 | 需要修改 |
|---|---|---|---|---|
| **DecisionGraph**（LangGraph 8 节点） | `app/core/orchestrator.py` | ✅ **原样保留** | L2 `engine.py` 直接实例化调用 | ❌ 不改。已带完整 try/except 容错 |
| `crawl` 节点 + CrawlAgent | `app/agents/crawl_agent.py` | ✅ 复用 | 经图调用 | ❌ 不改 |
| `normalize` 节点 | orchestrator 内联 | ✅ 复用 | 经图调用 | ❌ 不改 |
| `resume_embed` + ResumeAgent | `app/agents/resume_agent.py` | ✅ 复用 | 经图调用 | ❌ 不改 |
| `routing` 节点（混合路由） | orchestrator 内联 | ✅ **重点复用** | 经图调用 | ❌ 不改。这是数据质量守门员，别重造 |
| `match` + MatchingAgent（2-stage） | `app/agents/matching_agent.py` | ✅ 复用 | 经图调用 | ⚠️ 输出需经映射（见 §C） |
| `rank` + RankingAgent | `app/agents/ranking_agent.py` | ✅ 复用 | 经图调用 | ❌ 不改 |
| `report` 节点 | orchestrator 内联 | ✅ 复用 | 经图调用 | ⚠️ `MatchReport` 不直接对外 |
| 爬虫适配器（4 个站点） | `app/agents/adapters/` | ✅ 复用 | 经 crawl 节点 | ⚠️ 并发池未接线（G7） |
| `EmbeddingService` | `app/services/embedding_service.py` | ✅ 复用 | 经节点调用 | ❌ 不改 |
| `LLMService`（含 `score_capabilities`） | `app/services/llm_service.py` | ✅ **重点复用** | L2 `capability.py` + `engine.py` 调用 | ❌ 不改。C4 能力分值靠它 |
| `CacheService`（Redis/内存） | `app/services/cache_service.py` | ⚠️ **已定义未接线** | 可选择性接入 embedding 缓存 | 🔧 需接线（可选，非阻塞） |
| `DemoGuard` | `app/services/demo_guard.py` | ❌ **不复用** | 被 `user_quota` 取代 | — 保留代码但 Careerly 链路不用 |
| `ExportService` | `app/services/export_service.py` | ⚠️ 暂存 | 未来报告导出用 | 暂不接入 |
| **StateStore**（`graph_state.db`） | `app/core/state_store.py` | ✅ **复用作引擎运行态** | 仅存 session/checkpoint 语义 | ⚠️ 需补 `user_id` 列（G2） |
| **AuthStore**（`auth.db`） | `app/core/auth_store.py` | ✅ 复用 | 手机号身份 + bcrypt + JWT | ⚠️ 位置应迁到产品层侧 |
| `security.py`（JWT/HTTPBearer） | `app/core/security.py` | ✅ 复用 | `Depends(get_current_user)` | ❌ 不改。机制完善 |
| **CareerlyStore**（`careerly.db`） | `app/careerly/store.py` | ✅ **复用作业务真相** | L2 唯一业务持久化入口 | 🔧 需扩表（G3） |
| `engine.run_analysis` | `app/careerly/engine.py:212` | ✅ **这就是 Agent Adapter** | 已桥接 `run_sync` | 🔧 补 user 上下文 + 失败语义 |
| `capability.normalize` | `app/careerly/capability.py` | ✅ 复用 | 校验 LLM 能力输出 | ❌ 不改。设计得当（永不抛异常） |
| `schemas.py`（契约模型） | `app/careerly/schemas.py` | ✅ 复用 | 作为 L3 响应模型 | 🔧 补 growth/assets/home/profile 模型端点 |
| `router_careerly`（7 端点） | `app/careerly/router.py` | ✅ 复用 | L3 挂载 | 🔧 建议迁入 `app/api/` |
| `router_auth`（4 端点） | `app/api/router_auth.py` | ✅ 复用 | L3 挂载 | ❌ 不改。C1 已解决 |
| `envelope.py` | `app/api/envelope.py` | ✅ 复用 | 统一响应 | ❌ 不改 |
| `router_v1.py`（招聘任务语义） | `app/api/router_v1.py` | ⚠️ **收敛** | 保留为内部/调试能力 | 🔧 补归属校验，逐步收窄 |
| `router_app.py`（2 端点，无鉴权） | `app/api/router_app.py` | ⚠️ **整改** | 补鉴权或下线 | 🔴 必修（G2） |
| `server.py` 根路径 6 端点 | `app/api/server.py:117-293` | ⚠️ **合并** | 与 `/api/v1` 去重 | 🔧 收敛 |
| `streamlit_app.py`（UI） | `app/ui/streamlit_app.py` | ⚠️ 保留 | 内部调试工具 | ❌ 不动，不删 |
| `main.py`（CLI） | 根目录 | ✅ 保留 | 引擎 CLI 调试入口 | ❌ 不动 |
| `scripts/e2e_careerly.py` | `scripts/` | ✅ **升级为回归基线** | 扩充为端到端测试 | 🔧 需扩展 |
| Flutter `ApiClient` | `careerly/lib/data/api/api_client.dart` | ✅ 复用 | 已实现信封/refresh/错误归一 | 🔧 需补 token 持久化（G6 待裁决） |
| Flutter 3 个 API 仓储 | `careerly/lib/data/api/` | ✅ 复用 | 已对接 | ❌ 不改 |
| Flutter 5 个 Mock 仓储 | `careerly/lib/data/mock/` | ⚠️ 待替换 | 后端端点就绪后切换 | 🔧 依赖后端补端点 |

**一句话**：**26 个模块中 18 个原样复用、7 个需要小改、0 个需要重写、0 个需要删除。**

---

## C. 数据模型映射

### C.1 映射关系总表

```
Careerly 领域模型（API 契约 · camelCase）       引擎领域模型（Python · snake_case）
────────────────────────────────────────────────────────────────────────────
Job                ←───────── JobPosting       （子集：只取 title/company/description）
JobMatch           ←───────── MatchResult      （重组：score × 100、gaps 合并）
GapDetail          ←───────── SkillGap         （扩展：补 0-100 分值）
StrengthScore      ←───────── （无直接来源）    （由 capability 快照派生）
Capability         ←───────── （无直接来源）    （由 LLMService.score_capabilities 产出）
AnalysisProject    ←─────────（无直接来源）     （Careerly 独有：版本化项目容器）
AnalysisResult     ←───────── （无直接来源）    （Careerly 独有：版本化结果）
CapabilitySnapshot ←───────── （无直接来源）    （Careerly 独有：成长的时间轴支点）
GrowthEvent        ←───────── （无直接来源）    （Careerly 独有：由相邻快照 diff 派生）
ResumeProfile(API) ←───────── ResumeProfile    （引擎侧）
                                   （字段结构差异大，只取共性）
```

### C.2 逐字段映射：`MatchResult` → `JobMatch`

| 引擎字段 | 契约字段 | 转换规则 |
|---|---|---|
| `score: float(0-1)` | `score: int(0-100)` | `round(score * 100)`；**硬规则：只由能力匹配计算，薪资/通勤/城市不得进入** |
| `missing_skills: list[str]` + `partial_skills: list[str]` | `gaps: list[str]` | 合并去重 |
| `match_reasons: list[str]` | `reasons: list[str]` | 直传 |
| `recommendations: list[str]` | `suggestion: str \| null` | 拼接为单串 |
| `gaps: list[SkillGap]` | `gapDetails: list[GapDetail]` | `SkillGap.severity` → 无对应；`yours/target` 来自 capabilities |
| ❌ 无来源 | `strengthScores: list[StrengthScore]` | 由 capability 快照中 `yours >= 60` 取前 6 派生 |
| ❌ 无来源 | `rank: int` | 由排序名次生成 |

**已落地的 C4 方案**（`llm_service.score_capabilities` + `capability.normalize` + `engine._apply_capabilities`）：

- 从「简历画像 + JD」提炼 3–8 个**动态能力维度**（维度名随用户简历而变，非固定枚举）
- 每维产出 `yours`（0-100）/ `target` / `why` / `actions`
- `capabilitySnapshot = {name: yours}`
- `target - yours >= 12` → 标记为「关键 Gap」
- **评分失败完整降级**：`yours/target = null`、`snapshot = {}`，分析永不中断

**仍需注意的间隙**：降级时 `GapDetail.yours/target` 为 `null`，而契约期望 0–100。Flutter 需容忍 null（待核对 Flutter `GapDetail.fromJson` 是否可空）。

### C.3 逐字段映射：`ResumeProfile`（引擎）→ `ResumeProfile`（API）

| 引擎字段 | 契约字段 | 转换 |
|---|---|---|
| `skills: list[str]` | `skills: list[str]` | 直传 |
| `projects: list[Project]` | `projectExperiences: list[str]` | 拍平为字符串列表（丢结构） |
| `roles: list[str]` | `workExperiences: list[str]` | 拍平（引擎无公司/时间结构） |
| `education` / `certifications` | `awards: list[str]` | 语义不完全对应，待裁决 |
| `experience_years` | ❌ 无对应 | 丢弃或并入 skills |
| ❌ 无来源 | `versions: list[ResumeVersion]` | 当前后端仅单版本基线，Flutter 合成 `'current'` |
| ❌ 无来源 | `capabilities: Map<String,int>` | 来自 capability 快照 |

**现存实现**（`careerly.db`）：`careerly_resumes(user_id PK, text, profile, updated_at)` —— **单行基线，非多版本**。Flutter 已用防御性解析兼容（`api_resume_repository.dart:67-88`）。

### C.4 ⚠️ 关键架构约束

**不要让引擎模型成为 API 响应模型。**

反面做法（必须禁止）：

```python
# ❌ 禁止：把引擎模型直接扔给 FastAPI
@router.get("/analysis/projects/{id}")
def get(id): return MatchResult(...)   # response_model=MatchResult
```

正确做法（现状是对的，继续保持）：

```python
# ✅ 正确：L2 显式映射 → 契约模型
def to_job_match(match_result: MatchResult, capabilities) -> JobMatch:
    return JobMatch(
        score=round(match_result.score * 100),
        gaps=[*match_result.missing_skills, *match_result.partial_skills],
        ...
    )
```

理由：引擎模型服务于「匹配算法」，契约模型服务于「App 的 fromJson」。两者演进节奏不同 —— 引擎加字段不该破坏 App，App 要视觉字段不该污染引擎。

---

## D. API 调用链

### D.1 主链路（已实现 · 单岗位分析）

```
① 用户粘贴 JD，点「开始分析」
        │
        ▼
② Flutter   AnalysisProvider.startAnalysis
            → AnalysisRepository.analyze({title, rawJd})
            → POST /api/v1/analysis          （超时 300s）
        │
        ▼
③ L3  API   router_careerly.analyze
            ├─ Depends(get_current_user) → user_id     ★ 归属可信
            ├─ 校验配额（免费 3 次）
            └─ 丢进线程池（不阻塞事件循环）
        │
        ▼
④ L2  适配  engine.run_analysis(title, rawJd, resume_text)
            ├─ 读 careerly_resumes 取简历基线
            ├─ LLMService.score_capabilities(profile, raw_jd)   ← C4
            └─ DecisionGraph.run_sync(
                   job_urls=[],                          ← 关键：不需要 URL
                   manual_job_texts=[raw_jd],            ← JD 走手动分支
                   resume_text=...,
               )
        │
        ▼
⑤ L1  引擎  LangGraph 8 节点
            crawl（手动 JD，跳过爬虫）
            → normalize（LLM 补结构化字段）
            → resume_embed（ResumeAgent + Embedding）
            → routing（混合路由：match / recrawl / end）
            → match（2-stage：embedding 粗筛 → LLM 精排）
            → rank（排序）
            → report（聚合）
            → 输出 state.match_results / state.report
        │
        ▼
⑥ L2  映射  to_job_match(match_result) → JobMatch
            _apply_capabilities(...)     → capabilitySnapshot / strengthScores / gapDetails
            组装 AnalysisProject{ jobs:[Job], results:[AnalysisResult] }
        │
        ▼
⑦ L2  持久  CareerlyStore.save_project(user_id, ...)
            → careerly.db：analysis_projects / jobs / project_jobs
                           analysis_results / job_matches
            → 成功后 consume_quota(user_id)（失败不扣）
        │
        ▼
⑧ L3  响应  ok(AnalysisProject)  → 信封 {success, data, error, meta}
        │
        ▼
⑨ Flutter   解包 data → AnalysisProject.fromJson → 渲染分析结果页
```

### D.2 辅助链路

| 场景 | 调用链 |
|---|---|
| 拉项目列表 | Flutter → `GET /api/v1/analysis/projects` → CareerlyStore.list_projects(user_id) |
| 拉项目详情 | Flutter → `GET /api/v1/analysis/projects/{id}` → CareerlyStore.get_project |
| 岗位库 | Flutter → `GET /api/v1/analysis/jobs` → CareerlyStore.list_jobs(user_id) |
| 保存简历基线 | Flutter → `PUT /api/v1/resume` → CareerlyStore.save_resume(user_id, text) |
| 读简历画像 | Flutter → `GET /api/v1/resume` → CareerlyStore.get_resume(user_id) |
| 查配额 | Flutter → `GET /api/v1/quota` → CareerlyStore.get_quota(user_id) |
| 注册 / 登录 | Flutter → `POST /api/v1/auth/register\|login` → AuthStore（手机号，无密码）→ 发 JWT |
| token 刷新 | Flutter（401 自动）→ `POST /api/v1/auth/refresh` → 新 access token → 重试原请求一次 |

### D.3 明确否决的调用链

```
❌ Flutter → Agent /api/v1/match          （App 不该知道引擎存在）
❌ Careerly Backend → HTTP → 另一个 FastAPI（同一进程内还走 HTTP 是纯浪费）
❌ Flutter → LangGraph / Qwen / Playwright（实现细节，永不暴露）
❌ 复制一份 orchestrator.py 改改再用       （永远只有一份引擎）
```

---

## E. Task 生命周期

### E.1 现状：两套并存的模型（问题所在）

```
【模型 A】router_v1.py —— 异步任务机（招聘语义）
  POST /api/v1/match  → 202 {task_id}
       ↓ asyncio.create_task
  StateStore.sessions.status:
       pending → running → completed
                        ├→ failed
                        ├→ timed_out
                        └→ interrupted（启动期 recover_stale_tasks 回收）
  GET /api/v1/tasks/{id}  轮询状态与结果

【模型 B】router.py —— 同步直返（Careerly 语义）
  POST /api/v1/analysis →（阻塞 10-20s，实测约 210s）→ 201 AnalysisProject
  ❌ 无 queued / processing 状态
  ❌ careerly.db 无 status 列
  ❌ 失败即 HTTP 错误，任务无痕可查
```

**问题**：同一进程里两套任务语义，且 Careerly 用了「无状态」的那套 —— 移动端最脆弱的那套。

### E.2 目标：统一到一套 Analysis Task 语义

```
                     ┌─────────────────────────────────┐
                     │  Careerly Analysis Task          │
                     │  （careerly.db: analysis_tasks）  │
                     └─────────────────────────────────┘

  queued ──→ processing ──┬──→ completed
    │           │          │
    │           │          └──→ failed（含 error_code / error_message）
    │           │
    │           └──→ （引擎内部 routing 决策）
    │                 ├ match        → 继续
    │                 ├ recrawl      → 继续（引擎内部重试，对外仍是 processing）
    │                 └ ask_user/end → failed(reason=NEED_MORE_INPUT)
    │
    └──→ expired（超时未完成，worker 回收）

  配额：仅 completed 时 consume；failed / expired 不计（符合 Baseline §5「失败回滚」）
```

**引擎内部状态 → 对外状态**的映射（关键：引擎的复杂性不能泄漏给 App）：

| 引擎内部 | 对外 Task 状态 | 说明 |
|---|---|---|
| `pending` / `running` | `processing` | 一律压平为 processing，App 不需要知道 8 个节点跑到哪 |
| `completed` | `completed` | 落库 + 扣配额 |
| `failed` / `timed_out` / `interrupted` | `failed` | 带可展示的错误码 |
| `routing_decision == ask_user` | `failed(NEED_MORE_INPUT)` | 引擎要更多输入 → 转成产品可行动的错误 |
| `routing_decision == end`（缺简历等） | `failed(NO_BASELINE)` | 提示用户先补简历 |

### E.3 移动端的现实约束（G4 详解）

**问题量化**：实测分析耗时约 **210s**，Flutter 超时 **300s**，余量仅 90s（30%）。

在移动网络下，这个余量会被轻易吃掉：

- 弱网 / 4G 抖动 → HTTP 长连接被中断
- 用户切后台 / 锁屏 → Android 可能冻结 socket
- 运营商 NAT 超时（常见 60-120s 无数据）→ 连接被静默回收
- 失败后**任务无痕**：引擎可能已经跑完了，但结果没落库，配额白扣或白退

**三个可选方案**（**需你裁决，本文不擅自实施**）：

| 方案 | 改动 | Flutter 改动 | 优点 | 缺点 |
|---|---|---|---|---|
| **① 维持现状（同步）** | 无 | 无 | 零成本 | 移动端失败率不可控；不可观测 |
| **② 双模兼容（推荐）** | 后端 `engine.run_analysis` 包一层任务记录；`POST /analysis` 默认同步，识别 `Prefer: respond-async` → 返回 202 + task_id | **零改动**（默认走同步分支） | 向后兼容；弱网可切异步；任务可观测 | 后端需补 `analysis_tasks` 表 + 状态机 |
| **③ 全量异步** | 分析改 202 + 创建任务，新增 `GET /analysis/tasks/{id}` | **需改** ApiClient 加轮询 | 最稳；最可观测 | 破坏已裁决的 C2；Flutter 要动 |

**我的建议：方案 ②**。它把「已裁决的 C2 同步语义」保留为默认行为（不改产品裁决、不改 App），同时给弱网留出逃生通道，并把任务状态变成可观测的一等公民 —— 这正是 G3（成长时间轴）和 G4（可靠性）共同需要的底座。

---

## F. 双 SQLite State Store 与 Careerly DB 的边界

### F.1 边界定义（方向已对，需强化）

```
┌─────────────────────────────────────┐   ┌─────────────────────────────────────┐
│  graph_state.db                     │   │  careerly.db                        │
│  【引擎运行态 · 可丢弃】              │   │  【业务真相 · 不可丢弃】              │
│                                     │   │                                     │
│  sessions(session_id PK, ...)       │   │  analysis_projects(user_id, ...)    │
│  jobs(job_index, data_json, ...)    │   │  jobs(id, user_id, title, raw_jd)   │
│  resume(session_id UNIQUE, ...)     │   │  project_jobs(project_id, job_id)   │
│  match_results(score, result_json)  │   │  analysis_results(project_id, ver)  │
│  progress_logs(seq, message)        │   │  job_matches(result_id, score, ...) │
│                                     │   │  careerly_resumes(user_id PK, text) │
│  语义：这次任务跑到哪了               │   │  user_quota(user_id PK, ...)        │
│  生命周期：可 rebuild、可清空          │   │                                     │
│  归属：无 user_id（★ 待补）           │   │  语义：用户真正拥有的资产             │
│  WAL ✅ busy_timeout 5000 ✅          │   │  生命周期：永久，需备份               │
│  TTL / 清理：❌ 无                    │   │  归属：全表 user_id 隔离 ✅           │
│                                     │   │  索引：idx_projects_user 等 ✅       │
└─────────────────────────────────────┘   └─────────────────────────────────────┘

  数据流：graph_state.db 是「过程」  careerly.db 是「结果」
          删掉前者，用户数据毫无损失
          删掉后者，用户资产永久丢失
```

另有第三个库：`auth.db`（`AuthStore`，users 表）—— 归属产品层，是「谁」的真相。三个库职责不重叠，方向是对的。

### F.2 必须遵守的四条纪律

| # | 纪律 | 理由 |
|---|---|---|
| 1 | **引擎永不直接读写 `careerly.db`** | 引擎必须保持产品无关。它只接收输入、返回结果 |
| 2 | **`graph_state.db` 永不出现在 API 响应里** | 它是过程态。App 拿到的必须是 `careerly.db` 的产物 |
| 3 | **`user_id` 只在 L2/L3 落地，不下沉到 `GraphState`** | 引擎不需要知道「谁」，只需知道「分析什么」（唯一例外：日志/审计埋点可透传，但不参与业务逻辑） |
| 4 | **`graph_state.db` 要能随时清空而系统不崩** | 这是它「可丢弃」定位的验证标准 |

### F.3 `user_id` 缺失的具体风险（G2）

现状：`sessions` 表无 `user_id`，`GraphState` 无 `user_id`，`StateStore` 查询只凭 `session_id`。

带来的漏洞：

```
攻击者拿到（或猜到）任意 session_id / task_id
        ↓
GET /api/v1/tasks/{task_id}        ← 有 JWT，但不校验归属
GET /api/v1/sessions/{session_id}  ← 同上
        ↓
读取他人的匹配结果、简历片段（match_results.result_json 含 job 详情）
```

**最小修复（本次不实施，仅记录）**：给 `sessions` 表加 `user_id` 列（向前兼容，老数据置 NULL），`run_match` 写入，查询时加 `WHERE user_id = ?`。同时给 `router_app.py` 两个端点补 `Depends(get_current_user)`。

---

## G. 推荐目录结构

### G.1 目标结构（渐进式，非一次性重构）

```
work_agent/
├── app/
│   ├── core/                      【L1 引擎层 · 产品无关】
│   │   ├── orchestrator.py          DecisionGraph（8 节点）—— 不动
│   │   ├── state.py                 GraphState —— 不动
│   │   ├── state_store.py           引擎运行态（graph_state.db）
│   │   └── config.py                引擎配置
│   │
│   ├── agents/                    【L1 引擎层】—— 不动
│   │   ├── crawl_agent.py
│   │   ├── resume_agent.py
│   │   ├── matching_agent.py
│   │   ├── ranking_agent.py
│   │   └── adapters/                站点适配器
│   │
│   ├── models/                    【L1 引擎层】—— 不动
│   │   ├── job.py  resume.py  match.py
│   │
│   ├── services/                  【L1 引擎层】—— 不动
│   │   ├── embedding_service.py  llm_service.py
│   │   ├── cache_service.py  export_service.py  demo_guard.py
│   │
│   ├── careerly/                  【L2 适配层 · Careerly 产品语义】
│   │   ├── engine.py                ★ Agent Adapter（已存在，加固）
│   │   ├── capability.py            能力快照归一化（已存在）
│   │   ├── mapping.py               ← 建议：从 engine.py 抽出映射函数
│   │   ├── store.py                 careerly.db（业务真相）
│   │   ├── growth.py                ← 新增：快照 diff → GrowthEvent
│   │   └── schemas.py               契约 Pydantic 模型
│   │
│   ├── api/                       【L3 API 层】
│   │   ├── server.py                装配（收敛根路径重复端点）
│   │   ├── envelope.py              统一信封 —— 不动
│   │   ├── middleware/
│   │   ├── router_auth.py           /auth/*        —— 不动
│   │   ├── router_careerly.py       ← 建议：从 app/careerly/router.py 迁来
│   │   ├── router_tasks.py          ← 新增：任务状态查询（配合方案②）
│   │   └── router_v1.py             招聘语义（收窄为内部/调试）
│   │
│   └── ui/
│       └── streamlit_app.py         内部调试工具 —— 保留
│
├── docs/careerly/
│   ├── BACKEND_API_CONTRACT.md      契约（目标态，需回写标注已落地子集）
│   └── INTEGRATION_ARCHITECTURE.md  本文档
│
└── scripts/
    └── e2e_careerly.py              端到端回归（升级）
```

### G.2 变更规模说明

**只有 3 处是「移动文件」**（`router.py` → `api/router_careerly.py`、映射函数抽出、`auth_store.py` 概念归位），其余全是「新增文件」或「原地加固」。**没有一处需要重写现有逻辑。**

如果你希望连这 3 处移动都不做 —— 也可以。它们是「整洁度」而非「正确性」问题。我建议**放到最后一个阶段做**，避免和功能开发抢时间。

---

## H. 分阶段实施计划

### 阶段总览

| 阶段 | 目标 | 前置 | 是否阻塞联调 |
|---|---|---|---|
| **P0** | **端到端连通**（G1 + G2） | 无 | ✅ **是** |
| **P1** | **任务可观测**（G4 + G5 + G8 部分） | P0 | 否 |
| **P2** | **成长时间轴**（G3） | P1 | 否 |
| **P3** | **聚合端点**（Home / Assets / Profile / AI） | P2 | 否 |
| **P4** | **整洁度**（目录收敛 + 技术债 G7） | P3 | 否 |
| **P5** | **生产就绪**（备份 / 监控 / 部署） | P4 | 否 |

### P0 — 端到端连通（🔴 最高优先，做完就能真跑）

| # | 任务 | 涉及文件 | 验收标准 |
|---|---|---|---|
| P0-1 | Android 补 `INTERNET` 权限 + 调试构建放行明文 HTTP | `careerly/android/app/src/main/AndroidManifest.xml` + `network_security_config.xml` | 模拟器能 `curl` 到 `http://10.0.2.2:8000/api/v1/health` |
| P0-2 | `router_app.py` 两个端点补 `Depends(get_current_user)` | `app/api/router_app.py:142,212` | 无 token 返回 401 |
| P0-3 | `sessions` 表加 `user_id` 列 + 写入 + 查询校验 | `app/core/state_store.py`、`app/api/router_v1.py` | A 用户凭 B 用户的 task_id 拿到 404 |
| P0-4 | 定一个「单条 `curl` 全链路冒烟脚本」并跑通 | `scripts/` | register → put resume → post analysis → get projects 四步 200 |

> ⚠️ P0 完成后再做任何功能开发。理由：**当前无法证明前后端能通**，在此之上堆功能等于盲写。

### P1 — 任务可观测（🟠）

| # | 任务 | 前置 |
|---|---|---|
| P1-1 | 裁决 E.3 方案（推荐②双模）；实施 `analysis_tasks` 表 + 状态机 | P0 |
| P1-2 | 引擎失败语义映射（`ask_user` → NEED_MORE_INPUT、缺简历 → NO_BASELINE） | P1-1 |
| P1-3 | 根路径重复端点收敛（`server.py:188-293` 与 `/api/v1` 去重） | P0 |
| P1-4 | 回写 `BACKEND_API_CONTRACT.md`：标注已落地子集 vs 目标态 | — |
| P1-5 | 端到端回归测试（扩 `e2e_careerly.py`）+ 认证/越权测试 | P0 |

### P2 — 成长时间轴（🔴 产品核心）

| # | 任务 | 说明 |
|---|---|---|
| P2-1 | 新建 `capability_snapshots(id, user_id, source, source_id, scores JSON, created_at)` | 每次分析完成写入一条 |
| P2-2 | 新建 `growth_events(id, user_id, type, title, description, delta, occurred_at)` | 由相邻两条快照 diff 派生，**不存冗余字段** |
| P2-3 | `growth.py`：快照 diff 算法（能力分变化、Gap 收窄、匹配分变化、排名变化） | `type` 必须是 Dart enum 名：`experience\|skill\|capability\|matchScore\|rank` |
| P2-4 | `GET /api/v1/growth/events`、`GET /api/v1/growth/timeline` | 补齐契约 §1 |
| P2-5 | Flutter `GrowthRepository` 从 Mock 切真实 API | 依赖 P2-4 |

> 这是 Careerly「看见成长」的数学前提。**没有 P2，产品只有「一次分析」，没有「成长」。**

### P3 — 聚合端点（🟠）

| # | 任务 |
|---|---|
| P3-1 | `GET /api/v1/career/assets`（CareerAssets，计数一律由 App 派生） |
| P3-2 | `GET /api/v1/home/summary`（HomeSummary，含 matchChanges delta） |
| P3-3 | `GET /api/v1/profile`（UserProfile） |
| P3-4 | `POST /api/v1/ai/messages`（需先裁决 AI 能力边界） |
| P3-5 | Flutter 侧 5 个 Mock 仓储逐个切换 |

### P4 — 整洁度与技术债（🟡）

| # | 任务 |
|---|---|
| P4-1 | `app/careerly/router.py` → `app/api/router_careerly.py` |
| P4-2 | 映射函数从 `engine.py` 抽到 `mapping.py` |
| P4-3 | Playwright 并发池接线（G7）：`playwright_pool.acquire_playwright` 替换直连 |
| P4-4 | `CacheService` 接入 embedding 缓存（可选） |
| P4-5 | LangGraph checkpointer（若要支持断点续跑） |
| P4-6 | `graph_state.db` 加 TTL 清理任务 |

### P5 — 生产就绪

| # | 任务 |
|---|---|
| P5-1 | 数据库备份策略（`careerly.db` 是用户资产，必须备份） |
| P5-2 | 监控与告警（分析成功率、P95 耗时、越权 403 计数） |
| P5-3 | JWT secret 生产化（当前默认值 `dev-insecure-change-me` 有启动告警） |
| P5-4 | 部署（Dockerfile / render.yaml 已有基础） |

---

## I. 风险与需要避免的重复开发

### I.1 重复开发清单（明确「不要做」）

| # | 不要做 | 因为已存在 |
|---|---|---|
| 1 | ❌ 新建 Agent Adapter 层 | `app/careerly/engine.py:212` 已在做 |
| 2 | ❌ 新建第二套 FastAPI 服务 | `app/api/server.py` 就是后端 |
| 3 | ❌ 重写 LangGraph 图 | `orchestrator.py` 8 节点完备且带容错 |
| 4 | ❌ 重写爬虫 / 适配器 | 4 个站点适配器已就绪 |
| 5 | ❌ 重新实现 Embedding / LLM 调用 | `embedding_service.py` / `llm_service.py` 已封装 |
| 6 | ❌ 重新实现能力分值模型 | C4 已解决（`score_capabilities` + `capability.normalize`） |
| 7 | ❌ 重新实现配额 | `user_quota` 表 + `consume_quota` 已落地 |
| 8 | ❌ 重新实现认证 | `security.py` + `auth_store.py`（手机号 + JWT + bcrypt）已落地 |
| 9 | ❌ 重新实现统一信封 | `envelope.py` 已定义 |
| 10 | ❌ 重写简历解析 | `resume_agent.py` 已在分析链路内完成 |
| 11 | ❌ 把 `MatchResult` 直接当 API 响应 | 必须经 `mapping` 层 |
| 12 | ❌ 在 Flutter 里做分析逻辑 | 计算全在后端，App 只渲染 |
| 13 | ❌ 让 Flutter 调 `/api/v1/match` | App 只认识 Careerly API |
| 14 | ❌ 把引擎状态写进 `careerly.db` | 两库边界不可破 |
| 15 | ❌ 新增爬虫适配器 | 契约已用 `source: "import"` 预留，当前用 `paste` 即可 |

### I.2 架构风险

| 风险 | 等级 | 缓解 |
|---|---|---|
| **移动端同步分析失败率高** | 🔴 高 | 见 E.3 方案② |
| **越权访问** | 🔴 高 | P0-2 / P0-3 |
| **成长模块无法闭环 → 产品表达落空** | 🔴 高 | P2 优先于 P3 |
| **契约文档与实现双轨漂移** | 🟠 中 | P1-4 回写文档，明确「已落地 / 目标态」两栏 |
| **Flutter 5 个 Mock 仓储长期不切真** | 🟠 中 | P3 排期，避免「Demo 能跑，真机不能」 |
| **token 不落盘（D52）导致用户隔离形同虚设** | 🟠 中 | 需产品侧复核 D52 —— 这不只是技术问题 |
| **Playwright 并发无上限** | 🟡 低 | P4-3（当前 analysis 不走爬虫，风险可控） |
| **`graph_state.db` 无 TTL 无限增长** | 🟡 低 | P4-6 |
| **JWT 默认 secret** | 🟡 低 | P5-3（生产前必须改） |
| **测试薄弱，重构无安全网** | 🟡 低 | P1-5 + P0-4 冒烟脚本 |

### I.3 需要你裁决的事项（阻塞项）

| # | 待裁决 | 影响 | 我的建议 |
|---|---|---|---|
| **D-A** | E.3 分析调用形态：① 维持同步 ② 双模兼容 ③ 全量异步 | 决定 P1 工作量与移动端可靠性 | **②双模**（Flutter 零改动） |
| **D-B** | Flutter D52「不存 token」是否维持 | 决定用户隔离与配额在产品上是否有效 | **建议复核**：不存 token ⇒ 冷启动即新用户 ⇒ 配额与成长时间轴失去主体 |
| **D-C** | 契约文档 `quota` / `resume` 路径：按实现回写，还是按文档改代码 | 决定 P1-4 方向 | 按实现回写（代码与 Flutter 已一致，改代码会连带改 App） |
| **D-D** | `ResumeProfile` 的 `education`/`certifications` → `awards` 语义对应 | 决定 C.3 映射细节 | 需产品侧确认 |
| **D-E** | `GapDetail.yours/target` 降级为 `null` 时 App 如何展示 | 决定 Flutter 侧容错 | 建议 App 显示「—」而非 0 |
| **D-F** | P0/P1/P2 的排期顺序是否认可 | 决定下一步动手方向 | P0 先做，做完再议 |

---

## J. 你提出的十问 · 逐条回答

| # | 问题 | 回答 |
|---|---|---|
| **①** | 哪些 Agent 代码**直接复用**？ | **几乎全部。** LangGraph 8 节点、4 个爬虫适配器、EmbeddingService、LLMService（含 `score_capabilities`）、ResumeAgent、MatchingAgent（2-stage）、RankingAgent、security.py。共 18 个模块零修改复用。详见 §B |
| **②** | 哪些代码应该**移动**？ | 只有 3 处，且都是整洁度而非正确性：`app/careerly/router.py` → `app/api/router_careerly.py`；`engine.py` 的映射函数抽到 `mapping.py`；`auth_store.py` 概念上归属产品层。**建议放到 P4，不阻塞任何功能** |
| **③** | 哪些**保持独立**？ | 引擎层（`core/` + `agents/` + `models/` + `services/`）保持**产品无关**，不引入任何 Careerly 概念。这是最重要的边界 |
| **④** | Careerly Backend **怎么调用 Agent**？ | **进程内方法调用，不走 HTTP。** `engine.run_analysis()` → `DecisionGraph.run_sync(job_urls=[], manual_job_texts=[jd], resume_text=...)`。已实现（`engine.py:212`） |
| **⑤** | Agent 的**输入输出**是什么？ | **输入**：`job_urls`（爬虫路径）或 `manual_job_texts`（粘贴 JD 路径，Careerly 用这个）、`resume_text`/`resume_file_path`、`use_llm`、`use_embedding`。**输出**：`GraphState` 里的 `match_results: list[MatchResult]` + `report: MatchReport` + `routing_decision` + `errors` |
| **⑥** | 两边 **Model 怎么映射**？ | 见 §C。核心：`MatchResult.score(0-1)` → `JobMatch.score(0-100)`；`missing_skills + partial_skills` → `gaps`；`SkillGap.severity` → 由 `capabilities` 派生 `GapDetail.yours/target`。**必须经显式映射函数，禁止直传** |
| **⑦** | Agent **SQLite State Store 怎么处理**？ | **保留，但明确它只是「引擎运行态」。** 它与 `careerly.db`（业务真相）物理与语义双重分离，永不交叉。唯一待补：`sessions` 表加 `user_id` 列用于归属校验。详见 §F |
| **⑧** | **Analysis Task 怎么处理**？ | 现状**没有 Task**（同步直返，无状态）。建议引入 `analysis_tasks` 表 + `queued → processing → completed/failed` 状态机（§E.2），但对外默认保持同步语义（方案②），Flutter 零改动 |
| **⑨** | **API 怎么设计**？ | **已设计且已实现大半**：统一信封 + JWT + camelCase + `/api/v1/{analysis,resume,quota,auth}`。需补：`growth/*`、`career/assets`、`home/summary`、`profile`、`ai/messages`（P2/P3）。需收敛：`server.py` 根路径重复端点 |
| **⑩** | Flutter 最终**只调哪些 API**？ | 只看 3 类共 12 个端点：**auth**（register/login/refresh）· **analysis**（POST /analysis、GET /analysis/projects、GET /analysis/projects/{id}、GET /analysis/jobs）· **resume**（PUT /resume、GET /resume）· **quota**（GET /quota）· 待补 **growth**（events/timeline）。**永不调** `/match` `/tasks` `/sessions` |

---

## K. 审计停止点

本审计已完成，**未修改任何业务代码**。

**下一步需要你确认的（阻塞）**：

1. **D-A 分析调用形态** —— 推荐方案②双模兼容（Flutter 零改动、弱网可退、任务可观测）
2. **D-B Flutter D52 不存 token 是否维持** —— 直接影响「用户隔离 / 配额 / 成长时间轴」是否有主体
3. **P0 是否立即执行** —— Android 网络权限 + 越权修复 + 冒烟脚本（这是唯一「不做就没法验证任何东西」的阶段）

确认后我再进入编码。在此之前，后端业务代码保持冻结。

---

**附：本文档的审计证据来源**

```
work_agent/
  app/core/orchestrator.py         DecisionGraph · 8 节点 · 条件路由
  app/core/state.py                GraphState 字段全集
  app/core/state_store.py          graph_state.db · 5 表 · WAL
  app/core/security.py             JWT HS256 · HTTPBearer · get_current_user
  app/core/auth_store.py           users(phone) · bcrypt
  app/careerly/engine.py           ★ Agent Adapter · run_analysis(:212) · run_sync 桥接
  app/careerly/capability.py       能力快照归一化（永不抛异常）
  app/careerly/store.py            careerly.db · 7 表 · 全部 user_id 隔离
  app/careerly/router.py           7 端点：analysis(4) / resume(2) / quota(1)
  app/careerly/schemas.py          契约 Pydantic 模型（含未暴露的 growth/assets/home）
  app/api/server.py                装配 · include_router × 4 · 根路径重复端点(:117-293)
  app/api/router_v1.py             招聘任务机 · 异步 + 状态机
  app/api/router_app.py            ⚠️ 2 端点无鉴权(:142,212)
  app/api/router_auth.py           手机号身份（C1 已解决）
  app/api/envelope.py              统一信封
  app/services/llm_service.py      score_capabilities（C4 已解决）
  app/agents/adapters/             4 站点 · playwright_pool 未接线
  docs/careerly/BACKEND_API_CONTRACT.md   契约（目标态，含 C1-C5 冲突记录）
  scripts/e2e_careerly.py          端到端验证脚本

careerly/
  lib/data/api/api_client.dart            信封解包 · 内存 token · 401 自动 refresh
  lib/data/api/api_analysis_repository.dart
  lib/data/api/api_resume_repository.dart  防御性解析单版本基线（:51-88）
  lib/data/api/api_auth_repository.dart
  lib/data/repositories/ / lib/data/models/  契约本体（camelCase）
  android/app/src/main/AndroidManifest.xml ⚠️ 无 INTERNET 权限
  docs/05_progress/CURRENT_STATUS.md / TODO.md
```
