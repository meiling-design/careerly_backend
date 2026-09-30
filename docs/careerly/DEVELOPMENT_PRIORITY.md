# Careerly Backend Implementation Roadmap

> **定位**：这是**总开发路线** —— 回答「做什么、为什么、先后关系」。
> **配套**：《P0 Implementation Task》是**施工指令** —— 回答「这一阶段具体怎么做」。
> 两者不是二选一，是路线图与施工图的关系。
>
> 来源：`docs/careerly/INTEGRATION_ARCHITECTURE.md` §H + §I.3
> 版本：**v3（2026-09-30 20:30）** —— 按评审意见修订 5 处 + 6 条实现级约束（标记 `[补]`）；**统一 P0 编号**；落地 5+4 项裁决记录（§五）
> **编号权威（2026-09-30 裁决）**：P0 阶段编号**以《Careerly_Vibe_Coding_开发启动_Prompt_V1.0.md》为准**（P0-0 ~ P0-6）。本文原「P0-1 Red Baseline」已重命名为 **P0-A**，其余任务顺次对齐。**后续所有任务引用统一使用 Prompt 编号。**
> 状态：前置裁决已全部确认 → **P0 已开工**（P0-0 Git 安全点进行中）。只读侦察报告见 `P0_PRE_IMPLEMENTATION_CHECK.md`。

---

## 一、排序的三条原则

1. **先证明能通，再堆功能。** 当前最大的风险不是「缺功能」，而是「无法证明前后端能通」——在未验证的链路上叠功能等于盲写。
2. **先堵安全洞，再谈体验。** 越权是「一票否决」项，且修复成本极低（加依赖 + 加 WHERE），没有理由延后。
3. **产品核心链路优先于页面覆盖率。** Careerly 的表达是「看见能力 · 跨越 Gap · **记录成长**」——成长链路（P2）必须先于为 Mock 页面补端点（P3）。

---

## 二、三处排序修正（与审计文档 §H 的差异）

### 修正 ①：P0 内部改为「先建立红色基线，再修复」（该项编号 = **P0-A**）

审计文档直接列了 4 个修复任务。实操上应先跑一次端到端脚本拿到**当前真实失败点**，再动手改，改完复跑转绿。否则「改完了」和「真的通了」之间没有证据链。

```
跑 e2e（红） → 修 G1/G2 → 跑 e2e（绿） → 模拟器实机走一遍
```

### 修正 ②：🔴 认证模型尚未成立 —— D-NEW（不只是技术债）

**证据**：`app/api/router_auth.py:129-146` —— `POST /auth/login` 只接收手机号，**无密码、无验证码、无设备绑定**，直接签发 access + refresh token 对。
`app/core/auth_store.py:9,67` —— `password_hash` 是 **nullable** 列，注释写明「reserved for a possible future」。

**影响**：知道他人手机号即可取得其完整账号权限 —— 读简历、看分析结果、消耗配额。

**为什么审计漏了**：D85 把它写成了「已裁决事项（身份 = 手机号，无密码无验证码）」，容易被当成设计选择而非风险。**但它不是技术债，是认证模型本身尚未成立。**

#### 处置方案（三选一）

| 方案 | 说明 | 建议 |
|---|---|---|
| **A** | 开发阶段手机号直登；**生产禁止无验证登录** | 🟢 **当前推荐** |
| B | 现在直接接短信验证码 | 暂不需要（会拖住 P0–P4） |
| C | 产品采用其它认证机制 | 需产品重新裁决 |

#### 采纳 A 的硬约束（不是「留个 TODO」）

```
DEV  手机号 → Dev Auth  → JWT
PROD 手机号 → OTP/正式认证 → JWT
```

**代码必须显式区分 Development Authentication 与 Production Authentication**，并加入**生产启动阻断**：

- 以环境变量区分（如 `CAREERLY_AUTH_MODE=dev|prod` + `ENV`）
- **`ENV=production` 且 `AUTH_MODE=dev` → 拒绝启动（fail-fast），不是打一条 warning**
- 现有 JWT secret 的启动告警（默认 `dev-insecure-change-me`）可复用同一模式，但**认证边界必须是硬阻断**

> 目的：让这个临时方案**没有机会**变成正式方案。历史上「先这样跑着，等快上线再补」的结局基本都是补不上。

### 修正 ③：D-B 从「待裁决」改为「**必须解决**」—— 且要连着过期策略一起定

审计文档把 G6 / D-B 放在「待裁决」，没有排进任何阶段。这是排序错误，因为：

```
token 仅内存（D52）
   ↓
冷启动即失忆 → 需重新登录
   ↓
当前「登录只要手机号」⇒ 重新登录零成本 ⇒ 痛点被掩盖
   ↓
一旦补上正式认证（修正②）⇒ 每次冷启动都要走一遍 OTP
   ↓
G6 从「小痛点」升级为「体验灾难」
   ↓
而 P2 成长时间轴要求「同一用户被持续识别」
```

**结论**：D-B 不是「要不要存」的开放问题，而是「**如何安全地存**」的执行问题。
Careerly 是长期记录职业成长的产品 —— **必须跨冷启动保持用户身份**。放在 **P1 与 P2 之间**完成（技术上可与 P1 并行开发）。

#### `[补]` 关键补充：光持久化还不够，refresh 有效期必须一起改

核实 `app/core/security.py:42-43`：

```
ACCESS_TOKEN_EXPIRE_MINUTES   默认 60     （60 分钟）
REFRESH_TOKEN_EXPIRE_DAYS     默认 7      （7 天）
```

**问题**：Careerly 是**低频**应用 —— 用户可能几周甚至一两个月才打开一次看「成长变化」。
7 天的 refresh 有效期意味着：**即便做了安全持久化，用户超过一周没打开仍要重新登录** —— 而「重新登录」在正式认证下意味着收 OTP。

> 持久化解决的是「**同一会话内**重启 App 不失忆」；
> 过期策略解决的是「**跨周/跨月**回来还记得我是谁」。**两者缺一不可。**

**待定项**：refresh 有效期需按产品目标使用频率重新定（如 90 天 + 滑动续期），并在 `security.py` 中改为可配置。
这不改变 P2 的开发顺序，但**必须在 P1-6 一并定案**，否则 P2 的「足迹连续性」在真机体验上仍是断的。

---

## 三、优先级总表（依次执行）

| 顺位 | 阶段 | 目标 | 前置 | 阻塞后续？ |
|---|---|---|---|---|
| **0** | **拍板** | 4 项裁决（见 §五） | — | 部分阻塞 P1/P2 |
| **1** | **P0** | 端到端连通 + 堵越权 | 无 | ✅ 阻塞一切 |
| **2** | **P1** | 任务可观测 + 失败语义 | P0 | 否 |
| **3** | **P2** | 成长时间轴（产品核心） | P1 + G6 定案 | 否 |
| **4** | **P3** | 聚合端点 + 5 个 Mock 切真 | P2 | 否 |
| **5** | **P4** | 整洁度与技术债 | P3 | 否 |
| **6** | **P5** | 生产就绪 | P4 | 否 |

---

## 四、逐项任务清单

### P0 — 端到端连通（🔴 做完才算「能真跑」）

> **编号已对齐 Prompt V1.0**（2026-09-30 裁决）。原「P0-1 Red Baseline」现为 **P0-A**；原 P0-2 ~ P0-6 顺次对齐后如下。

| # | 任务 | 涉及文件 | 验收标准 |
|---|---|---|---|
| **P0-0** | **建立 Git 安全点**：确认分支/工作区实况，把**未跟踪的既有资产**纳入版本控制，创建 pre-integration baseline commit | 两仓库 | 工作区干净、基线可回溯；**不 push remote** |
| **P0-A** | **建立红色基线（Red Baseline）**：走一次**真实 HTTP** 链路（register → login → resume → analysis → projects），记录实际失败点 | ⚠️ 需新建 HTTP 层脚本。**`scripts/e2e_careerly.py` 是进程内引擎脚本、不走 HTTP，测不到 G1/G2，不可用作基线**（2026-09-30 核实） | 得到一份明确的「哪一步通、哪一步不通」清单 |
| **P0-1** | **Android 网络联调（G1 的一半）**：`main` 源集补 `android.permission.INTERNET` | `careerly/android/app/src/main/AndroidManifest.xml` | release 构建具备联网能力 |
| **P0-2** | **开发环境放行明文 HTTP（G1 的另一半，真正的当前阻塞点）**：`debug` 源集加 `network_security_config.xml`，**仅**放行 `10.0.2.2` / `localhost` | `careerly/android/app/src/debug/` + `res/xml/` | 模拟器内 **debug** 包能命中 `http://10.0.2.2:8000/api/v1/health`；**release 不受影响** |
| **P0-3** | **Auth 流程真实验证**：register / login / me + Flutter token 注入，**实跑 API 而非只读代码** | `app/api/router_auth.py`、`careerly/lib/data/api/` | Register → Login → JWT → 后续请求自带 `Authorization: Bearer` |
| **P0-4** | **用户隔离与鉴权边界**：① `router_app.py` 两端点补 `Depends(get_current_user)`；② `sessions` 加 `user_id` 列（**不进 GraphState**，见 §五 D-F）；③ tasks/sessions 查询加归属校验；④ 全局异常处理器把 `PermissionDenied` → 404 | `app/api/router_app.py:142,212`、`app/core/state_store.py`、`app/api/router_v1.py`、`app/api/server.py` | 无 token → **401**；A 持 B 的 task_id / session_id → **404**（非 200、非 403） |
| **P0-5** | **业务级**端到端验收 + 用户隔离矩阵（非「全 200」） | `scripts/`、`careerly/test/real_backend_integration_test.dart` | 见下方 DoD |
| **P0-6** | 认证真空 DEV / PROD 边界落地（**D-NEW 方案 A**） | `app/api/router_auth.py`、`app/core/config.py`、启动钩子 | `ENV=production` + `AUTH_MODE=dev` → **进程拒绝启动** |

#### ⚠️ `[补]` G1 的真凶不是 INTERNET 权限（2026-09-30 核实，推翻原假设）

Manifest 源集优先级 `debug > main`，实测：

| 构建 | INTERNET | 明文 HTTP | 结果 |
|---|---|---|---|
| `flutter run`（debug） | ✅ 有（Flutter 默认注入给 hot reload） | ❌ 被拦 | 🔴 **连不上 —— 与权限无关** |
| `flutter build apk`（release） | ❌ 无 | —— | 🔴 **连不上 —— 双重原因** |

**结论**：当前联调阻塞点是 **cleartext**（Android 9+ 默认禁明文），不是权限。**只补 INTERNET 权限，debug 依然连不上。** 必须分两处修（P0-1 + P0-2），恰好符合 Prompt §六「不为开发环境修改生产安全策略」。

#### P0-5 的 Definition of Done（替换「register → … → projects 全 200」）

**HTTP 200 不等于业务正确** —— 分析返回 200 但结果为空也是 200。这才是真正的验收：

```
【User A · 正常链路】
register → login → 拿到 JWT
   → upload resume
   → submit analysis → Agent Engine 执行
   → 结果落库（analysis_projects / analysis_results 有 User A 的行）
   → GET analysis / projects
   → 返回与 User A 对应的【真实数据】（非空、字段完整）

【User B · 越权链路】
login（独立账号）
   → 持 User A 的 task_id / session_id / project_id 发起读取
   → 被拒绝
```

**`[补]` 为什么拒绝码是 404 而不是 403**：403 会泄露「该资源**存在**，只是你没权限」这一信息，攻击者据此可探测有效 id。404 让「不存在」与「无权访问」不可区分 —— 这是**故意**的设计，不是偷懒。

#### `[补]` P0-4 的旧数据处理 —— 已核实，不需要复杂策略

实测三个库的当前状态：

| 库 | 现状 | 结论 |
|---|---|---|
| `app/data/graph_state.db` | `sessions` **5 行**，**无 `user_id` 列**；jobs 4 / match_results 3 / resume 5 / progress_logs 5 | 有开发期历史数据，需 ALTER |
| `app/data/careerly.db` | 7 表齐全，但 `analysis_projects` / `analysis_results` / `job_matches` / `jobs` / `project_jobs` / `user_quota` **全为 0 行**；仅 `careerly_resumes` 1 行 | **无用户资产风险** |
| `app/data/auth.db` | `users` 5 行 | 开发测试账号 |

**所以旧数据处理是自动的、且 fail-closed**：

```sql
ALTER TABLE sessions ADD COLUMN user_id TEXT;   -- 历史行 user_id = NULL
SELECT ... WHERE user_id = ?                     -- NULL 永不匹配任何 ? ⇒ 旧 session 对所有人生效不可见
```

**纪律**：
1. 查询条件必须严格写 `WHERE user_id = ?`，**不许**退化成 `WHERE user_id IS ?`、`OR user_id IS NULL` 之类的可空比较
2. **禁止**为了通过测试把历史 session 硬塞给某个用户
3. 验收时显式加一条：确认旧 session（user_id 为 NULL）对任意新用户均不可读

> ⚠️ **P0 未完成前不写任何新功能。** 理由见排序原则 1。
> 注：经核实，`server.py:188-293` 的根路径 6 个端点**其实都有鉴权**，故 G5 是整洁度/契约漂移问题、非安全问题，保持在 P1-3。

### P1 — 任务可观测（🟠）

| # | 任务 | 前置 | 备注 |
|---|---|---|---|
| **P1-1a** | 内部状态机：`analysis_tasks` 表 + `queued → processing → completed/failed` | P0 + 裁决 D-A | **POST /analysis 仍等待完成再返回结果** —— 对外语义不变，Flutter 零改动 |
| **P1-1b** | 对外异步能力（可选、第二阶段）：支持 `Prefer: respond-async` → `202 + task_id` + 轮询 | P1-1a | **不要在第一阶段就做** —— 避免把 Flutter/API/Agent/状态机一次性搅在一起 |
| **P1-2** | 引擎失败语义映射：`ask_user` → `NEED_MORE_INPUT`、缺简历 → `NO_BASELINE` | P1-1a | 消灭「失败无痕」 |
| **P1-3** | 根路径重复端点收敛（`server.py:188-293` 与 `/api/v1/*` 去重） | P0 | 先确认 Streamlit / 旧调用方是否还在用 |
| **P1-4** | 回写 `BACKEND_API_CONTRACT.md`：拆「已落地子集」/「目标态」两栏 | — | 按实现回写（裁决 D-C） |
| **P1-5** | 扩 `e2e_careerly.py` + 补认证/越权/多用户测试 | P0 | 建立重构安全网 |
| **P1-6** | **安全 Token 持久化 + 有效期重定**（**D-B 必须解决**） | 裁决 D-B | **P2 的产品前提**；须含 refresh 有效期调整，见修正 ③ |

> `[补]` **P1-1 的两阶段拆分是刻意的**：第一阶段只把「状态」引入内部，不改变对外契约 —— 这样任务可观测、失败有痕、且 Flutter 完全不用动。
> 「异步」作为能力可后补；一旦顺序反过来（先对外异步），Flutter、ApiClient、轮询、Agent 状态机就要同时改，风险面成倍放大。

### P2 — 成长时间轴（🔴 产品核心）

| # | 任务 | 说明 |
|---|---|---|
| **P2-1** | 新建 `capability_snapshots(id, user_id, source, source_id, scores JSON, created_at)` | 每次分析完成写一条 |
| **P2-2** | 新建 `growth_events(id, user_id, type, title, description, delta, occurred_at)` | 由相邻两条快照 diff 派生，**不存冗余字段** |
| **P2-3** | `growth.py`：快照 diff 算法（能力分变化 / Gap 收窄 / 匹配分变化 / 排名变化） | `type` 必须是 Dart enum 名：`experience\|skill\|capability\|matchScore\|rank` |
| **P2-4** | `GET /api/v1/growth/events`、`GET /api/v1/growth/timeline` | 补齐契约 §1 |
| **P2-5** | Flutter `GrowthRepository` 从 Mock 切真实 API | 依赖 P2-4 |
| **P2-6** | **Growth Event → 用户真正看到的足迹事件** 映射 | 见下方 `[补]` |

> **没有 P2，产品只有「一次分析」，没有「成长」。** 这是核心链路的断点，不是可选功能。

> `[补]` **P2-6 是产品语义问题，不是技术问题**：现有 `type` 枚举（`experience|skill|capability|matchScore|rank`）描述的是「**数据库发生了什么**」，而「我的足迹」要表达的是「**成长发生了什么**」。
> 例：「简历新增一个项目」与「能力评分 +8」很可能是**同一次变化的两个层级**，而不是两个并列事件。
> **该枚举在 P2 实施前先当作工程占位**，不要过早固化为最终产品模型 —— 需要一次显式映射对齐。**不阻塞 P0。**

### P3 — 聚合端点（🟠）

| # | 任务 | 备注 |
|---|---|---|
| **P3-1** | `GET /api/v1/career/assets`（CareerAssets） | 计数类字段一律**由 App 派生**，后端不下发 |
| **P3-2** | `GET /api/v1/home/summary`（HomeSummary，含 matchChanges delta） | delta 依赖 P2 数据 |
| **P3-3** | `GET /api/v1/profile`（UserProfile） | |
| **P3-4** | `POST /api/v1/ai/messages` | 需先裁决 AI 能力边界 |
| **P3-5** | Flutter 侧 5 个 Mock 仓储逐个切换 | 避免「Demo 能跑、真机不能」 |

### P4 — 整洁度与技术债（🟡）

| # | 任务 |
|---|---|
| **P4-1** | `app/careerly/router.py` → `app/api/router_careerly.py` |
| **P4-2** | 映射函数从 `engine.py` 抽到 `mapping.py` |
| **P4-3** | Playwright 并发池接线：`playwright_pool.acquire_playwright` 替换直连 |
| **P4-4** | `CacheService` 接入 embedding 缓存 |
| **P4-5** | LangGraph checkpointer（若要支持断点续跑） |
| **P4-6** | `graph_state.db` 加 TTL 清理任务 |
| **P4-7** | `[补]` 收敛同名文件混淆：根目录 `data/graph_state.db` 与 `app/data/graph_state.db` |

> `[补]` **P4-7 的实测依据**：两者**都不是残留、也不是配置漂移**，而是同名不同用途的两个文件 —— 根目录那个里面只有一张 `demo_usage` 表（属 demo guard 用途），`app/data/` 那个才是引擎的 sessions/jobs/match_results。三个 Store（`state_store.py:28`、`careerly/store.py:39`、`auth_store.py:33`）**都正确指向 `app/data/`**。所以这是**命名混淆**而非路径 bug —— 但在排障时极易误读，P4 收敛掉。

### P5 — 生产就绪

| # | 任务 |
|---|---|
| **P5-1** | `careerly.db` 备份策略（用户资产，必须备份） |
| **P5-2** | 监控告警：分析成功率 / P95 耗时 / **越权拒绝计数（404）** |
| **P5-3** | JWT secret 生产化（当前默认 `dev-insecure-change-me`，有启动告警） |
| **P5-4** | **正式认证通道**（OTP / 产品裁决的机制）—— 移除 DEV 认证路径，P0-6 的阻断检查转为常态 |
| **P5-5** | 部署（Dockerfile / render.yaml 已有基础） |

---

## 五、裁决项与决策记录（Decision Log）

### 5.1 已确认决策（2026-09-30 20:30，产品侧拍板）

| # | 决策 | 结论 | 落地位置 |
|---|---|---|---|
| **D-编号** | P0 编号冲突 | **以《Careerly_Vibe_Coding_开发启动_Prompt_V1.0.md》为准**（P0-0 ~ P0-6）；本文原 P0-1 Red Baseline → **P0-A** | 本文 §四 |
| **D-A** | 分析调用形态 | **②双模兼容**（对内异步、对外默认同步，Flutter 零改动），按 `[补]` 拆两阶段执行 | P1-1a |
| **D-B** | Flutter D52「token 不落盘」 | **🔴 必须解决**：改为「如何**安全**持久化」，并**连带重定 refresh 有效期**（修正 ③） | P1-6 → P2 |
| **D-NEW** | 认证策略 | **🟢 方案 A**：开发环境手机号直登为 **DEV ONLY**；生产**禁止**无验证登录，代码显式区分 DEV/PROD + **生产启动硬阻断**。正式认证留 P5 | P0-6 |
| **D-F** | `user_id` 是否下沉到引擎 | **不下沉**。`sessions` 表**有** `user_id` 列（持久层归属），**GraphState 不携带**（引擎层纯粹）。`user_id` 由 **API 层**在调用引擎前后读写，引擎对用户身份**无感知** | `state_store.py`（含架构注释）、P0-4 |
| **D-G** | 越权错误码 | **统一 404**。资源不存在与无权访问**返回同一码**，不泄露资源存在性（防 id 枚举）。在 FastAPI 层用**全局异常处理器**把 `PermissionDenied` / `NotFound` 统一映射为 404，前端无需区分 | `app/api/server.py` 异常处理器、P0-4 |
| **D-H** | 旧 API（`/match`、`/sessions`、`/tasks`、`/resume/upload`）修还是下线 | **不删除，默认修复**（Prompt §三-16）。经核实**零外部调用方**（Flutter 只调 5 个新路径；无 Streamlit 目录；全仓仅自身 docstring 引用）→ 修复目标是**安全 + 不报 500**，不做深度重构 | `app/api/router_app.py`、P0-4 |
| **D-I** | 未跟踪的既有测试资产 | **P0-0 首步即 `git add` 纳入版本控制**，提交信息注明「既有集成测试资产，非新写文件」 | P0-0 |

### 5.2 仍待产品侧确认

| # | 待裁决 | 阻塞谁 | 结论 / 建议 |
|---|---|---|---|
| **D-C** | 契约文档 `quota` / `resume` 路径：改文档还是改代码 | P1-4 | **按实现回写**（代码与 Flutter 已一致，改代码会连带改 App） |
| **D-D** | `ResumeProfile` 的 `education`/`certifications` → `awards` 语义对应 | C.3 映射细节 | 需产品侧确认 |
| **D-E** | `GapDetail.yours/target` 降级为 `null` 时 App 如何展示 | Flutter 容错 | 建议显示「—」而非 0 |

---

## 六、开工前必读的「不要做」清单

以下能力**全部已存在**，任何「重新实现」都是架构错误：

| ❌ 不要做 | 已存在于 |
|---|---|
| 新建 Agent Adapter | `app/careerly/engine.py:212` |
| 新建第二套 FastAPI | `app/api/server.py` |
| 重写 LangGraph 图 | `app/core/orchestrator.py`（8 节点带容错） |
| 重写爬虫 / 加站点适配器 | `app/agents/adapters/`（4 站点） |
| 重实现 Embedding / LLM | `app/services/embedding_service.py` / `llm_service.py` |
| 重实现能力分值 | `LLMService.score_capabilities()` + `capability.normalize()` |
| 重实现配额 | `user_quota` 表 + `consume_quota()` |
| 重实现认证 | `security.py` + `auth_store.py` |
| 重实现统一信封 | `app/api/envelope.py` |
| 把 `MatchResult` 直接当 API 响应 | 必须经映射层 |
| 让 Flutter 调 `/api/v1/match` | 三条铁律之一 |
| 把引擎状态写进 `careerly.db` | 双库边界不可破 |

---

## 七、下一步

### 裁决状态

**全部前置裁决已确认**（见 §五 5.1），无阻塞项。仍待产品侧确认的仅 `D-D` / `D-E`（字段映射与容错细节，**不影响 P0**）。

### 分工原则（避免开发线拆散）

**P0 由后端架构师主导，不单独开 Flutter 任务。**
仅在触及以下三处时让 Flutter 侧协作：Android 网络配置、Token 存储、`ApiClient`。

### 执行顺序（已开工）

```
P0-0 Git 安全点 → P0-A 红色基线 → P0-1 Android 权限 → P0-2 明文 HTTP
  → P0-3 Auth 验证 → P0-4 用户隔离（核心）→ P0-5 业务级 E2E → P0-6 DEV/PROD 边界
```

### 验收分层（三条路径不可混淆）

| 目标 | 手段 |
|---|---|
| 后端 API + 用户隔离 | `pytest` 或 `flutter test`（**不经过 Android 网络栈**） |
| 引擎可用性 | `scripts/e2e_careerly.py`（进程内引擎脚本，约 200s） |
| **Android 真实联网（G1）** | **只能模拟器/真机 `flutter run` 实跑** |

> 顺序提醒：**P0 → P1 → P2 → P3 → P4 → P5**，其中 P2 是产品核心，P0 阻塞一切，P4/P5 现在不要碰。
> 它们属于「让系统更漂亮」，而不是「让产品先真实跑起来」。
