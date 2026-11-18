# Careerly 后端 API 契约与改造方案 V0.1（草案 · 待裁决）

> 状态：**草案**。含 4 处**未决冲突**，其中 C1 为产品级阻塞项，须裁决后方可编码。
>
> 依据优先级（`careerly/CLAUDE.md` §0）：PRD V2.1 > IA V2.1 > Development Baseline V1.0 > Flutter Spec V2.1 > Figma > 代码。
>
> 本文件由 **Flutter 侧 `lib/data/repositories/*.dart` 与 `lib/data/models/*.dart` 倒推**生成 —— 契约不是后端自拟，而是 App 已经写好的 `fromJson` / `toJson` 所要求的形状。

---

## 0. 结论速览

> 📌 **本节已于 P1-4（2026-10-01）按实现回写。** 五处冲突已全部裁决，状态列为**当前实况**而非草案。
>
> 各冲突的裁决依据与时间见 `DEVELOPMENT_PRIORITY.md` §五 决策记录。

| # | 事项 | 状态 |
|---|---|---|
| — | Flutter 契约 → 后端端点映射 | ✅ 已推导；实际对接范围见 **§0.5.1** |
| C1 | **认证身份字段**：手机号（D85）vs 邮箱+密码 | ✅ **已裁决**：手机号直登，`DEV 可用 / PROD 硬阻断`（D-NEW 方案 A）。`app/core/auth_mode.py` 导入期 fail-fast |
| C2 | 分析调用形态：同步 vs 异步 | ✅ **已裁决 D-A ②**：对外同步（Flutter 零改动），内部保留任务状态机便于升级异步。<br>**P1-1b（2026-10-05）落地「双模」**：同步端点保留不动，**新增**异步入口 `POST /analysis/tasks`（202 + 轮询）。Flutter 真实后端路径改走异步，Mock 路径仍走同步 —— 见 **§0.5.9** |
| C3 | 额度规则 | ✅ **已落地**：`careerly.db: user_quota`（免费 3 次）。P1-03 起改为**任务开始时原子预占**（`reserved`），成功确认消耗、失败 / 取消 / 重启恢复释放 |
| C4 | 能力分值模型缺失 | ✅ **已落地**：`LLMService.score_capabilities()` + `capability.normalize()` 严格校验 + 完整降级。🔧 **P1-5 修复了一处取值 bug**（见 §0.5.6） |
| C5 | 会话持久化 → user_id 隔离无处落地 | ✅ **已落地**：**D-B 推翻 D52** —— token 安全持久化（Android Keystore / iOS Keychain），refresh 90 天。冷启动不再丢失身份主体 |

---

## 0.5 落地状态：已落地子集 vs 目标态（P1-4 回写）

> ⚠️ **这一节是唯一可信的实现清单。** 端点表由 `app.openapi()` 实际导出，
> Flutter 调用清单由 `grep '/api/v1/' lib/**/*.dart` 实际导出，避免文档与代码漂移。
>
> 下方 §1–§4 保留为**设计推导**，其中的路径若与本节冲突，**以本节为准**。

### 0.5.1 已落地且已对接（Flutter → 真实后端）

以下端点构成当前 MVP 的完整闭环：**登录 → 存简历（提交即解析）→ 分析 → 读结果 → 查额度**。

| Flutter 侧 | 方法与路径 | 鉴权 | 备注 |
|---|---|---|---|
| `AuthRepository.register` | `POST /api/v1/auth/register` | — | `AUTH_MODE=dev` 下手机号直登，无需验证码 |
| `AuthRepository.login` | `POST /api/v1/auth/login` | — | 同上 |
| `ResumeRepository.submitResumeText` | `POST /api/v1/resume`（multipart `text`） | JWT | **提交即解析**（ResumeAgent），返回 `{text, profile, updatedAt}` |
| `ResumeRepository.submitResumeFile` | `POST /api/v1/resume`（multipart `file`） | JWT | 同上；PDF / DOCX / TXT / 图片（OCR） |
| `ResumeRepository.fetchProfile` | `GET /api/v1/resume` | JWT | 无基线时返回 `null` |
| `ResumeRepository.fetchVersionOriginalPdf` | `GET /api/v1/resume/versions/{id}/original` | JWT | 仅返回该用户已上传并按版本保存的 PDF 原稿；版本删除时原稿一并删除 |
| `AnalysisRepository.analyze` | `POST /api/v1/analysis` | JWT | **同步**跑完 LangGraph 8 节点后返回（10–210s）。Mock 路径仍走它 |
| `AnalysisProgressSource.startAnalysisTask` | `POST /api/v1/analysis/tasks` | JWT | **P1-1b**（2026-10-05）：202 + `taskId`，后台跑。真实后端的分析入口 |
| `AnalysisProgressSource.fetchAnalysisTask` | `GET /api/v1/analysis/tasks/{id}` | JWT | **P1-1b**：轮询读真实节点进度，详见 **§0.5.9** |
| `AnalysisRepository.parseJobUrl` | `POST /api/v1/analysis/jobs/parse-url` | JWT | P4-3；不扣额度、不建任务 |
| `AnalysisRepository.fetchProjects` | `GET /api/v1/analysis/projects` | JWT | |
| `AnalysisRepository.fetchProject` | `GET /api/v1/analysis/projects/{id}` | JWT | 越权 → 404 |
| `AnalysisRepository.fetchJobLibrary` | `GET /api/v1/analysis/jobs` | JWT | |
| `AnalysisRepository.fetchQuota` | `GET /api/v1/quota` | JWT | ⚠️ 路径与 §1 草案不同，见 §0.5.5 |
| `AIRepository.sendMessage` | `POST /api/v1/ai/messages` | JWT | **SSE 流**，详见 **§0.5.8**（2026-10-04 由 §0.5.2 移入） |
| `AuthRepository.deleteCareerData` | `DELETE /api/v1/account/career-data` | JWT | 删除可归属的职业数据与文件；账号、token 保留；返回行/文件计数与未完成提示 |
| `AuthRepository.deleteAccount` | `DELETE /api/v1/account` | JWT | 立即软删除账号并吊销 token；30 天后级联物理清理 |

> 另有 `POST /api/v1/auth/refresh`（刷新令牌）与 `GET /api/v1/auth/me`，由 `ApiClient`
> 在 401 时自动调用，Flutter Repository 层不直接感知。

### 0.5.2 已存在但 Flutter 未对接（仍走 Mock）

> ⚠️ **2026-10-04 更正**：本节原把 `POST /api/v1/ai/messages` 也列在这里，
> 但 Flutter 侧 `ApiAiRepository` 已落地并已在 `providers.dart` 绑定
> （`useApi ? ApiAiRepository(client) : MockAIRepository()`）—— **该行已不成立**，
> 移入 §0.5.1。同时 `POST /api/v1/resume/upload` 的「排 P3」理由已失效
> （P4-2 已用统一入口 `POST /api/v1/resume` 收掉文件上传，见 §0.5.3 与
> `app/careerly/router.py` 的端点分工表）。

| 方法与路径 | 状态 | 卡在哪 |
|---|---|---|
| `GET /api/v1/analysis/tasks` | ✅ 已建（P1-1a） | Flutter 未接（列表是终态历史，不需要实时进度） |
| `GET /api/v1/analysis/tasks/{id}` | ✅ 已建（P1-1a / **P1-1b 加实时进度**） | **已接**：`ApiAnalysisRepository.fetchAnalysisTask`，见 §0.5.9 |
| `POST /api/v1/resume/upload` | ✅ 已建（multipart） | **已被取代，勿再新增调用**：统一入口是 `POST /api/v1/resume`（文件 / 图片 / 文本三合一）。旧端点只收 pdf/docx/txt，与 Flutter 白名单不一致 —— 属历史包袱 |

### 0.5.3 P3 新增：已落地且已对接（2026-10-02 回写）

以下 3 个端点 + P2 的 growth 两端点，使 Flutter 除 AI 外**全部切真**。

| Flutter 侧 | 方法与路径 | 鉴权 | 数据源与口径 |
|---|---|---|---|
| `GrowthRepository.fetchEvents` | `GET /api/v1/growth/events?type=&limit=` | JWT | 相邻能力快照 diff 派生（P2-4/5，已上线） |
| `GrowthRepository.fetchTimeline` | `GET /api/v1/growth/timeline?from=&to=` | JWT | 同上 |
| `CareerAssetsRepository.fetchAssets` | `GET /api/v1/career/assets` | JWT | 最新分析持久化的结构化简历画像 + 最新/次新能力快照 diff（`app/careerly/assets.py`）。时间字段只填真实可得项，解析不到的留空，**不伪造** |
| `HomeRepository.fetchSummary` | `GET /api/v1/home/summary` | JWT | headline/建议由最新分析匹配分与真实 Gap 派生；`dailyContent` 无来源，下发 `null`（`app/careerly/home.py`） |
| `ProfileRepository.fetchUserProfile` | `GET /api/v1/profile` | JWT | auth.db users 表；`handle` 为**脱敏**手机号（`139****0002`），完整号码不下发（`app/careerly/identity.py`） |

> **P2 事件派生口径（2026-10-01 裁决）**：事件由「相邻两条 `capability_snapshots`
> diff」在分析成功时派生（`app/careerly/growth.py`）。首次分析产生一条
> `experience`「建立职业基线」；`rank` 事件不生成（一次分析一个岗位，无排名
> 变化语义）；`experience`/`skill` 其余来源（职业资产）仍 Mock，**不伪造** ——
> 即 roadmap 标注的 P2-6 产品语义开放项，待职业资产接真源时单独对齐。
> 非法 `type` 返回 422 `INVALID_TYPE`；非法/倒置时间区间返回 422 `INVALID_RANGE`。

### 0.5.4 目标态（契约里有、后端未建）

| §1 契约要求 | 计划 | 阻塞什么 |
|---|---|---|
| `GET/POST /api/v1/resume/versions`、`versions/{id}/current` | P4+ | 简历多版本对比 |

### 0.5.5 ⚠️ 路径分歧（契约草案 ≠ 实际实现）

**这些都是 Flutter 已按「实际实现」写好的**，回头要把 §1 的草案改过来，而不是改代码 ——
否则会为了对齐文档把已经跑通的链路弄断。

| §1 草案写的 | 实际实现的 | 谁迁就谁 |
|---|---|---|
| `GET /api/v1/analysis/quota` | `GET /api/v1/quota` | 改文档 |
| `GET /api/v1/resume/profile` | `GET /api/v1/resume` | 改文档 |
| `POST /api/v1/resume/text` | `PUT /api/v1/resume` | 改文档 |
| `POST /api/v1/resume/baseline` | `GET /api/v1/resume`（无独立解析端点） | 改文档 |
| `POST /api/v1/resume/file`（multipart） | `POST /api/v1/resume/upload` | 改文档 |

### 0.5.6 P1-5 发现并修复的阻断性 Bug（值得记一笔）

`run_analysis()` 取简历画像时写成 `state.resume`，而 `GraphState` 的字段名是
`resume_output` → `AttributeError`。这行**不在** try 块内，异常一路冒到 router 兜底分支，
把一次**已经成功**的分析（节点全跑完、报告已导出）变成 `502 ANALYSIS_FAILED`。

它潜伏了整整一个版本没被发现，正因为此前**没有任何测试真的跑引擎** —— 既有测试把
`run_analysis` 整个 monkeypatch 掉了。P1-5 把替换点下移到模型供应商层后第一行就暴露。
回归套件见 `tests/test_p1_engine_e2e.py`。

### 0.5.7 历史别名（勿再新增调用）

根路径 `/health`、`/match`、`/sessions*`、`/sessions/{id}/report` 是 `/api/v1/*`
的历史别名，**P1-3 已标记 `deprecated`**，错误格式也已与 `/api/v1/*` 统一（此前裸抛
`HTTPException` 返回 `{"detail": ...}`）。保留是为了兼容 README 中的旧脚本（决策 D-H），
但**两侧输入契约不同**（v1 侧强制 `job_urls` 非空且有 X-Client-Key / demo 配额 / 输入长度
三重守门），故未合并实现，真正合并列 P4。

### 0.5.8 P-AI-1 落地：`POST /api/v1/ai/messages`（2026-10-03）

> §0.5.2 / §0.5.4 曾把本端点标为「⏸ 未建 / 未排期」，理由是**AI 能力边界未定**
> （上下文含什么、token 成本、Prompt 注入防护）。该边界已于 **2026-10-03** 裁决，
> 端点已实现并进入回归套件。**本小节是它的契约** —— 它与 §1 / §2.12 的旧草案
> **形状不同**（那里写的是单次 `AIMessage`，这里是 SSE 流）。

#### 请求

```json
POST /api/v1/ai/messages          Authorization: Bearer <JWT>
{
  "message": "为什么这个岗位只有 78%？",   // 必填，1–4000 字
  "projectId": "p_abc",                  // 可选；缺省回退「最近一次分析」
  "jobId": "j_7",                        // 可选；聚焦某个岗位的匹配结果
  "history": [                           // 可选；客户端本地会话，随请求回传
    {"role": "user", "content": "…"},
    {"role": "assistant", "content": "…"}
  ]
}
```

⚠️ **请求体里没有 user id** —— 身份一律取自 JWT，数据作用域在 SQL 层按 `user_id` 过滤
（`CareerlyStore.get_project`）。`history[].role` 只接受 `user` / `assistant`，
出现 `system` → 422（注入边界）。

⚠️ **客户端不上传 Career Context**，只带 `projectId`。会话历史存**客户端本地**，
后端不留存会话 —— 多轮上下文靠 `history` 回传。

#### Current Context（服务端组装）

服务端按 `projectId` **重新取数**再组装，而不是收下客户端传来的一份快照 ——
这样用户更新简历后，对话里不会还拿着几个月前的旧数据。

| 层 | 内容 | 本轮 |
|---|---|---|
| **Current Context** | 当前简历画像 · 当前 JD（截断 4000 字）· 当前分析结果 · 匹配度 · 能力模型（capabilitySnapshot）· Gap / GapDetail · AI 建议 · 同项目其它岗位摘要 | ✅ 已实现 |
| **Historical Context** | 历史简历版本 / 历史岗位 / 能力变化 / 历史分析 | ❌ **暂不实现**（「按问题召回」留后续单独开发，本轮不做 RAG / 向量库） |

组装入口 `app/careerly/ai_context.py::build_current_context` —— **只读已落库的事实，
不重算分数，不额外调用 LLM 检索**。

`projectId` 为空 → 回退该用户**最近一次分析**；一次分析都没有 → 通用顾问
（**合法状态，不是错误**，不返回 4xx）。

#### 响应：SSE（`Content-Type: text/event-stream`）

⚠️ **本端点是 Careerly 唯一响应不是 envelope 的端点。** 流一旦开始，HTTP 状态码就已经
发出去了，没法再改成 403/404 —— 因此**前置失败返回普通 envelope**（见下表），
只有**生成中途**的失败才走 SSE error 事件。

| 帧 | 含义 |
|---|---|
| `{"type":"start"}` | 流开始 |
| `{"type":"delta","text":"…"}` | 增量文本，可多次 |
| `{"type":"done"}` | 正常结束 |
| `{"type":"error","code":"…","message":"…"}` | **中途**失败（已发出的 delta 不撤回） |

#### 前置 HTTP 错误（普通 envelope，非 SSE）

| 状态 | code | 触发条件 |
|---|---|---|
| 403 | `AI_QUOTA_EXCEEDED` | AI 独立额度耗尽（`details.quota` 带快照） |
| 404 | `PROJECT_NOT_FOUND` | 显式传了 `projectId` 但不存在 / 不属于该用户 —— **不静默回退** |
| 503 | `AI_UNAVAILABLE` | 未配置 LLM（缺 `LLM_API_KEY` / `DASHSCOPE_API_KEY`） |
| 422 | `VALIDATION_ERROR` | `message` 为空 / 超长 / `history[].role` 非法 |

#### 额度

- **独立池**：表 `user_ai_quota`，与 `user_quota`（`CLAUDE.md` §5.2「免费 3 次完整**分析**」）
  **互不影响** —— 2026-10-03 裁决：§5.2 一字不动，AI 另开一池。
- **成功才扣**：流正常 `done` 才扣 1 次；中途 error、或客户端断开，**均不扣**。
- ⚠️ **免费次数未锁定**：MVP 沿用开发暂定值（登记在 Flutter 侧
  `docs/05_progress/CURRENT_STATUS.md` §3.6），**不是**最终商业化额度。

#### 实现位置

`app/careerly/router.py::ai_messages` / `_ai_stream` · `app/careerly/ai_context.py` ·
`app/careerly/store.py`（`get_ai_quota` / `consume_ai_quota`）·
`app/services/llm_service.py::chat_stream` · 回归套件 `tests/test_p5_ai_messages.py`。

### 0.5.9 P1-1b 落地：分析进度可观测（2026-10-05）

> **背景**：`GET /api/v1/analysis/tasks/{id}` 自 P1-1a 就在，但它只报**终态**，
> 而引擎每个节点**早就在写**实时进度（`orchestrator.py::_update_status` →
> `store.update_session_progress`）。缺的从来不是数据，是**关联**：
> `analysis_tasks` 行与引擎的 `session_id` 之间没有绑定，而且
> `POST /analysis` 是同步的 —— 客户端在运行期间拿不到任何句柄。
>
> **后果**（本次修掉的真实缺陷）：Flutter 的「正在分析」页只能靠组件内的
> 一只**本地时钟**驱动，7.3 秒就跑完并定形「分析完成」。而实测引擎单是
> `routing` 一个节点就可能跑 **100 秒以上**（契约 C2：10–210s）——
> 用户在「已完成」的画面上等一个还在飞的请求。

**改了什么**

| 层 | 改动 |
|---|---|
| `analysis_tasks` | 新增 `session_id` 列（含既有库的 `ALTER TABLE` 迁移） |
| `CareerlyStore.create_task` | 收 `session_id`；`_task_row` 回吐 `sessionId` |
| `StateStore.create_session` | 新增 `session_id=` 参数：允许调用方指定 id（不传则沿用自动命名） |
| `StateStore.get_session_progress` | 新增读侧 API（原先只有写） |
| `DecisionGraph.run` | 显式传入 session id 时，`create_session` 必须带同一个 id —— 否则 `sessions` 表没有该行，后续每个节点的进度写入都静默 return，表现为「任务永远 processing、进度不动」 |
| `run_analysis` | 透传 `session_id` |
| `POST /analysis/tasks` | **新增**：202 + `taskId`，后台执行 |
| `GET /analysis/tasks/{id}` | 附 `progress`（`_progress_payload`，含 `_NODE_TO_STEP` 映射） |

**关键设计点**

- **绑定顺序是刻意的**：API 层先 `StateStore.new_session_id()` → 写进任务行 →
  交给引擎。反过来（等引擎自己生成再回写）在同步接口下永远拿不到 id。
- **映射单点**：引擎 8 节点 → 产品 5 步的对应关系只写在
  `app/careerly/router.py::_NODE_TO_STEP`，Flutter 不认识引擎节点名。
- **归属纪律**：进度是按任务行携带的 `sessionId` 去**另一个库**读的。
  必须**先** `get_task(user_id, task_id)` 确认归属，**再**用它查进度 ——
  不能让客户端直接传 `session_id` 进来查，那是一条绕过任务归属的越权读路径。
- **两个刻度不混用**：`completedCount/totalNodes` 是引擎刻度，
  `stepIndex/totalSteps` 是产品刻度。响应里分开成两组字段，理由见 §2.1b。

**关联的 Flutter 侧改动**（`careerly` 仓库）

`AnalysisProgressSource`（新增能力接口，只有 `ApiAnalysisRepository` 实现）·
`AnalysisTask` / `AnalysisTaskProgress` 模型 ·
`AnalysisProvider` 的轮询（`ApiClient.analysisPollInterval`）·
`CareerlyAnalysisProgress.progress` 的两种驱动模式。

> Mock **不**实现 `AnalysisProgressSource` —— 它背后没有 agent 流程，
> 没有节点可报告。强行统一只能靠编造节点名，那是 `CLAUDE.md` §7 禁止的。
> 所以 Mock 路径沿用组件内时钟，真实后端路径用真实节点。

**尚未做的**（明确留着，不在本次范围）

- 未做 SSE：轮询足够，且 SSE 要动 Flutter 的传输层。
- 未做断线续接：客户端重连后要靠 `GET /analysis/tasks` 找回未完成任务。
- 额度上限的并发竞态未加锁：两个异步请求可同时通过配额门禁
  （同步路径本来就无此问题，因为它在一请求内串行）。影响面小，先记录。

---

## 1. 契约总表（Flutter Repository → 后端端点）

响应统一信封（后端现状，`router_v1` / `router_auth`）：

```json
{ "success": true, "data": <payload>, "error": null, "meta": {"timestamp": 0, "version": "v1"} }
```

→ **Flutter 侧 `ApiClient` 负责解包 `data`**；下方「响应体」均指 `data` 的内容。

| Flutter 接口 | 方法与路径 | 响应体 |
|---|---|---|
| `AnalysisRepository.fetchProjects()` | `GET /api/v1/analysis/projects` | `[AnalysisProject]` |
| `AnalysisRepository.fetchProject(id)` | `GET /api/v1/analysis/projects/{id}` | `AnalysisProject \| null` |
| `AnalysisRepository.fetchJobLibrary()` | `GET /api/v1/analysis/jobs` | `[Job]` |
| `AnalysisRepository.analyze({title, rawJd})` | `POST /api/v1/analysis` | `AnalysisProject` |
| `AnalysisRepository.fetchQuota()` | `GET /api/v1/analysis/quota` | `QuotaState` |
| `ResumeRepository.fetchProfile()` | `GET /api/v1/resume/profile` | `ResumeProfile` |
| `ResumeRepository.fetchVersions()` | `GET /api/v1/resume/versions` | `[ResumeVersion]` |
| `ResumeRepository.setCurrentVersion(id)` | `PUT /api/v1/resume/versions/{id}/current` | `void` |
| `ResumeRepository.submitResumeText(text)` | `POST /api/v1/resume/text` | `void` |
| `ResumeRepository.submitResumeFile(file)` | `POST /api/v1/resume/file`（multipart） | `void` |
| `ResumeRepository.buildBaseline()` | `POST /api/v1/resume/baseline` | `ResumeProfile` |
| `GrowthRepository.fetchEvents({type})` | `GET /api/v1/growth/events?type=` | `[GrowthEvent]` |
| `GrowthRepository.fetchTimeline({from,to})` | `GET /api/v1/growth/timeline?from=&to=` | `[GrowthEvent]` |
| `CareerAssetsRepository.fetchAssets()` | `GET /api/v1/career/assets` | `CareerAssets` |
| `HomeRepository.fetchSummary()` | `GET /api/v1/home/summary` | `HomeSummary` |
| `ProfileRepository.fetchUserProfile()` | `GET /api/v1/profile` | `UserProfile` |
| `AIRepository.sendMessage(text)` | `POST /api/v1/ai/messages` | ⚠️ **SSE 流，不是 `AIMessage`** —— 见 **§0.5.8** |
| `AuthRepository.register({name, phone, email?})` | `POST /api/v1/auth/register` | `void` ⚠️C1 |
| `AuthRepository.login({phone})` | `POST /api/v1/auth/login` | `void` ⚠️C1 |

**后端现状**：`/api/v1/match`、`/api/v1/tasks`、`/api/v1/sessions`、`/api/v1/auth/*`。
→ `/match`、`/tasks`、`/sessions` 是**招聘任务语义**，Careerly 契约里没有对应接口，建议在 Careerly 分支上逐步替换为 §1 的 analysis 命名空间。

---

## 2. 请求 / 响应 JSON 契约（逐字段对齐 Dart）

### 2.1 `POST /api/v1/analysis`（核心 · 单岗位分析）

**Request**

```json
{ "title": "AI 产品经理", "rawJd": "<岗位 JD 原文>" }
```

对应 `AnalysisRepository.analyze({required String title, required String rawJd})`。
→ 后端必须走 `GraphState.manual_job_texts` 分支构造 `JobPosting`，**不得要求 URL**。

**Response**：`AnalysisProject`（见 §3.1）。

### 2.1b `POST /api/v1/analysis/tasks`（P1-1b · 异步入口）

与 §2.1 **同一个请求体**，但**立刻**返回 `202`，分析在后台跑。

```json
{ "title": "AI 产品经理", "rawJd": "<岗位 JD 原文>" }

→ 202
{ "success": true, "data": { "taskId": "task_…", "status": "processing" }, … }
```

对应 `AnalysisProgressSource.startAnalysisTask`（Dart）。客户端随后轮询
`GET /api/v1/analysis/tasks/{taskId}` 直到终态，再用 `projectId` 取结果。

- 与同步端点**并存**，不是替换（D-A ②「双模兼容」）。
- 额度语义不变：**成功才扣**，前置拒绝（`QUOTA_EXCEEDED` / `NO_BASELINE`）
  与引擎失败都不扣。

**轮询响应**（在 P1-1a 的任务字段之外，多了 `sessionId` 与 `progress`）：

```json
{ "taskId": "task_…", "status": "processing", "projectId": null,
  "sessionId": "session-…",
  "progress": {
    "currentNode": "routing", "nodeStatus": "running",
    "completedCount": 4, "totalNodes": 8,
    "stepIndex": 2, "totalSteps": 5,
    "updatedAt": 1791201608.26 } }
```

⚠️ **两个刻度，不要混用**：

```text
completedCount / totalNodes   引擎节点刻度（8 个 LangGraph 节点）
stepIndex      / totalSteps   产品步骤刻度（「正在分析」页的 5 步）
```

5 步是 8 节点的**产品级抽象**，映射由后端 `app/careerly/router.py` 的
`_NODE_TO_STEP` **单点维护**，客户端不需要认识引擎节点名：

| UI 步骤 | 引擎节点 |
|---|---|
| 0 岗位需求解析 | `crawl` + `normalize` |
| 1 简历信息解析 | `resume_embed` |
| 2 能力匹配分析 | `routing` + `match`（含 `recrawl` 回环） |
| 3 识别能力 Gap | `rank` |
| 4 生成分析报告 | `report` |

`progress` 为 `null` = 还没有可报告的进度（排队中 / 任务未绑定 session）。
遇到映射表外的节点时 `stepIndex` 也为 `null` —— 客户端应**保持当前步不动**，
不要回退到第 0 步。

### 2.2 `AnalysisProject` JSON（Dart `AnalysisProject.toJson` 逐字）

```json
{
  "id": "string",
  "name": "string",
  "jobs": [ <Job> ],
  "results": [ <AnalysisResult> ],
  "createdAt": "ISO8601 | null",
  "updatedAt": "ISO8601 | null"
}
```

### 2.3 `AnalysisResult` JSON

```json
{
  "id": "string",
  "projectId": "string",
  "version": 1,
  "resumeVersionId": "string | null",
  "resumeVersionLabel": "string | null",
  "matches": [ <JobMatch> ],
  "capabilitySnapshot": { "产品规划": 85, "AI 应用能力": 76 },
  "jobSnapshots": [ <JobSnapshot> ],
  "createdAt": "ISO8601 | null"
}
```

> `resumeVersionId` / `resumeVersionLabel`（P1-01）记录**这次分析实际使用**的
> 简历版本，不是「当前简历」。内联 `resumeText` 且无对应版本时为 `null`。

### 2.3.1 `JobSnapshot` JSON（P1-02）

```json
{
  "jobId": "string",
  "title": "string",
  "company": "string",
  "rawJd": "string",
  "source": "paste | url | history | null",
  "sourceUrl": "string | null",
  "importMethod": "string | null",
  "capturedAt": "ISO8601 | null",
  "available": true
}
```

> 每次分析成功时写入、**永不更新**。迁移前的历史结果没有快照行，
> 后端返回 `available: false` —— 前端必须显示「原文缺失」，
> **不得**回退到当前 `jobs.raw_jd` 冒充历史原文。

> ⚠️ `capabilitySnapshot`（能力名 → 0–100 分）与 `version` 是**「看见成长」的数据支点**。

### 2.4 `JobMatch` JSON

```json
{
  "jobId": "string",
  "score": 86,
  "rank": 1,
  "strengths": ["产品规划能力"],
  "gaps": ["AI Agent 产品落地"],
  "reasons": ["产品规划", "用户研究"],
  "suggestion": "string | null",
  "strengthScores": [ { "name": "产品规划能力", "score": 85 } ],
  "gapDetails": [
    { "name": "AI Agent 产品落地", "yours": 68, "target": 82,
      "level": "关键 Gap", "why": "…", "actions": ["…"] }
  ]
}
```

**硬规则**（PRD §9.4 / Baseline §6.1）：`score` 0–100，**只由能力匹配计算**；薪资 / 通勤 / 城市不得进入。

### 2.5 `Job` JSON

```json
{ "id": "string", "title": "string", "company": "string",
  "rawJd": "string", "source": "paste | import | history | demo", "createdAt": "ISO8601 | null" }
```

> `source: "import"` 即为**爬虫产出**——用户选择保留抓取能力，契约已预留该值，不冲突。

### 2.6 `QuotaState` JSON

```json
{ "total": 3, "used": 0, "reserved": 0, "isMember": false }
```

> P1-03：`used` = 已确认消耗；`reserved` = 进行中任务已预占。
> 剩余额度 = `total - used - reserved`。并发请求由 SQLite 条件 UPDATE
> 原子预占，不会同时通过门禁。

### 2.7 `ResumeProfile` JSON

```json
{
  "versions": [ { "id": "string", "label": "string", "fileName": "string | null",
                  "uploadedAt": "ISO8601 | null", "isCurrent": true } ],
  "workExperiences": ["string"],
  "projectExperiences": ["string"],
  "skills": ["string"],
  "awards": ["string"],
  "capabilities": { "产品规划": 85 },
  "updatedAt": "ISO8601 | null"
}
```

### 2.8 `GrowthEvent` JSON

```json
{ "id": "string", "type": "experience | skill | capability | matchScore | rank",
  "title": "string", "description": "string | null", "delta": 8, "occurredAt": "ISO8601 | null" }
```

> `type` 是 **Dart enum 的 `.name`**（`experience` / `skill` / `capability` / `matchScore` / `rank`）——
> 不是中文 label。后端产出中文会解析失败。

### 2.9 `CareerAssets` JSON

```json
{
  "workExperiences": [ { "id": "string", "period": "2024.06 — 至今", "title": "string",
      "company": "string", "description": "string",
      "skills": [ { "name": "产品规划", "delta": 5 } ], "projects": ["string"] } ],
  "projects": [ { "id": "string", "name": "string", "year": "2026", "type": "string",
      "role": "string", "description": "string", "outcome": "string",
      "skills": ["string"], "jobImpact": [ { "job": "AI 产品经理", "delta": 8 } ] } ],
  "skillGroups": [ { "category": "产品能力", "skills": ["string"] } ],
  "awards": [ { "name": "string", "year": "2026", "org": "string", "description": "string" } ],
  "capabilities": [ { "name": "AI 应用能力", "score": 76, "delta": 8, "previousScore": 68 } ],
  "spanLabel": "2021/6/8 - 2026/9/9",
  "monthlyAdded": 12
}
```

> 计数（`workCount` / `projectCount` / `skillCount`）**一律由 App 派生**，后端**不得**下发计数字段。

### 2.10 `HomeSummary` JSON

```json
{
  "headline": "string", "subheadline": "string",
  "suggestions": ["string"],
  "matchChanges": [ { "jobId": "string", "jobTitle": "string", "company": "string | null",
                      "score": 86, "delta": 8 } ],
  "recentGrowth": ["string"], "dailyContent": "string | null", "hasBaseline": true
}
```

### 2.11 `UserProfile` JSON

```json
{ "displayName": "string", "handle": "string", "membershipLabel": "string" }
```

### 2.12 `AIMessage` JSON

```json
{ "id": "string", "text": "string", "isUser": false, "createdAt": "ISO8601 | null" }
```
（Dart 侧**无 `fromJson`**，是纯 Mock 内存模型；后端若要落库需先补序列化。）

> ⚠️ **已过时（2026-10-03）** —— 这是 AI 尚为纯 Mock 时的**旧草案**，
> 描述的是「一次请求换一条完整 `AIMessage`」。P-AI-1 落地后端点改为 **SSE 流**
> （`start` / `delta` / `done` / `error`），且**会话不落后端库**
> —— 历史存在客户端本地，由请求体 `history` 回传。当前契约见 **§0.5.8**。
> `AIMessage` 作为**客户端本地**的消息模型仍然有效（存本地会话用），
> 但它已**不是** `/ai/messages` 的响应体。

---

## 3. 模型字段对照：Flutter ↔ 后端现状

| Flutter 模型 | 后端现状 | 差距 |
|---|---|---|
| `Job` | `JobPosting`（required_skills / responsibilities / soft_skills） | 后端有结构化技能，Flutter 只有 `title/company/rawJd`。需适配层：Flutter 取 `title`+`rawJd`，结构化技能留在后端 |
| `JobMatch` | `MatchResult`（score / missing_skills / partial_skills / reasons / recommendations / grade） | `missing_skills`+`partial_skills` → `gaps`；`recommendations` → `suggestion`。**`strengthScores` / `gapDetails` 无来源** |
| `GapDetail`（yours / target / level / why / actions） | `SkillGap`（skill / severity / suggestion） | 后端**只有 severity，无 0–100 分值** → `yours`/`target`/`gap` 产不出 |
| `Capability`（score / delta / previousScore） | **无对应模型** | 🔴 需新建能力分值模型 |
| `AnalysisProject` / `AnalysisResult`（versioned） | `sessions` 表（一次性任务，无版本） | 🔴 需新建版本化分析存储 |
| `GrowthEvent` | **无** | 🔴 需新建成长事件表 |
| `QuotaState` | `DemoGuard`（demo 用量） | 语义不同，需按 user 计次 |
| `ResumeProfile` | `ResumeProfile`（skills / experience_years / projects / education） | 字段名与结构都不同，需适配层 |
| `ResumeVersion` | `sessions` 里的简历记录 | 无「当前版本 / 历史版本」概念 |
| `CareerAssets` | **无** | 🔴 需新建职业资产富结构 |

---

## 4. 冲突清单与最小修改建议

### 🔴 C1 — 认证身份字段（产品级，阻塞）

| | Flutter 契约（已裁决 D85） | 后端 JWT 层（现有代码） |
|---|---|---|
| 注册 | `register(name, phone, email?)` | `register(email, username, password, confirm_password)` |
| 登录 | `login(phone)` —— **无密码、无验证码** | `login(identifier: email\|username, password)` |
| 身份字段 | **手机号** | 邮箱 / 用户名 |

Flutter 侧原文：**「字段集为 D85 的产品裁决，非 Flutter 侧自拟」**，且「登录没有密码 / 验证码字段 —— Figma 无登录屏，PRD / IA 也没有定义凭据形态」。

**最小修改建议（推荐 A）**

- **A. 对齐 Careerly（推荐）**：后端 `auth` 换身份字段为 `phone`（唯一键），注册 `{name, phone, email?}`、登录 `{phone}`，**保留 JWT 令牌机制**（access/refresh、bcrypt、`get_current_user`）不动，仅去掉密码校验与 email 主身份。改动集中在 `router_auth.py` + `auth_store.users` 表；`security.py` 基本不动。
- **B. 保留标准认证**：保留 email+password，但**必须由产品侧重新裁决 D85**（把身份从手机号改为邮箱+密码）—— 这属于改动已裁决的产品规则，不能由工程侧决定。

⚠️ 附带问题：Flutter 侧 `AuthRepository` 明确 **不保存会话**（D52），所以即便后端发 token，App 也无处存 → 见 C5。

### 🟠 C2 — 分析调用形态

Flutter：`analyze()` **同步返回** `AnalysisProject`。
后端：`POST /match` 返回 `202 + task_id`，需轮询 `GET /tasks/{id}`。
→ 建议：`POST /api/v1/analysis` 内部**同步等待**（移动端可接受 10–20s），或保留异步但由 `ApiClient` 封装轮询。需定一个。

### 🟠 C3 — 额度规则

Flutter / Baseline §5（硬规则）：免费 3 次；上传简历、粘贴 JD、进入分析页**均不扣**，只有点「开始分析」扣；失败回滚。
后端：`DemoGuard` 是 demo 用量，语义不同。
→ 建议：后端新建 `user_quota`（user_id、total、used、is_member），`POST /analysis` 成功才 +1，失败回滚。

### 🔴 C4 — 能力分值模型缺失

`Capability.score` / `AnalysisResult.capabilitySnapshot` / `GapDetail.yours,target` / `StrengthScore.score` 都要求**每项能力一个 0–100 分**；后端只有 `_skill_overlap()` 的命中率与语义相似度，**没有能力维度的分值**。
→ 这是「看见成长」（能力变化、gap 收窄）的**数学前提**。需要新增：能力维度定义 + 从简历/JD 推导每维分值（可由 `llm_service` 新增一个 `CAPABILITY_SCORING` 提示词产出）。

> **✅ 已解决（2026-09-29，commit 见 git log）**：方案 = **LLM 评分 + 失败降级**（用户裁决）。
> - `llm_service.score_capabilities()` + `CAPABILITY_SCORING_PROMPT`：从简历画像+JD 提炼 3–8 个**动态**能力维度（维度名随用户简历而变，与 demo_fixture 的 `产品规划/AI 应用能力` 形态一致），每维产出 yours/target/why/actions；
> - `careerly/capability.normalize()`：严格校验（类型/0–100 截断/去重/数量），任何不合格 → `None`；
> - `careerly/engine._apply_capabilities()`：`capabilitySnapshot = {name: yours}`；`strengthScores` = yours≥60 按 desc 取前 6（demo fixture L146 的产品意图：`strengths` 镜像 strengthScores 的 name）；`gapDetails` = 能力级（`target-yours ≥ 12` → 关键 Gap）；
> - 评分失败时**完整回退**到 C4 之前的行为（yours/target=null、snapshot={}），分析永不因此中断。

### 🔴 C5 — 会话持久化 → user_id 隔离无处落地

Flutter D52：**不引入本地存储、不保存会话**；D54 待裁决（「已登录则跳过 Onboarding」）。
→ 含义：**App 当前没有「我是谁」的本地表达**，所以「user_id 隔离」在 App 侧此刻没有落点。
→ 建议：先在后端把 `user_id` 列加上（向前兼容），App 侧待 D54 裁决后再接。

---

## 5. 目标数据表设计（草案）

```sql
-- 认证（改造自现有 users 表，按 C1 方案 A）
users(id, phone UNIQUE, name, email, is_active, created_at, updated_at)

-- 分析（版本化，对齐 AnalysisProject / AnalysisResult）
analysis_projects(id, user_id, name, created_at, updated_at)
jobs(id, user_id, title, company, raw_jd, source, created_at)
project_jobs(project_id, job_id)                       -- 引用，不级联删除
analysis_results(id, project_id, version, resume_version_id,
                 resume_version_label, capability_snapshot JSON, created_at)
-- P1-02：分析实际使用的 JD 原文快照（只追加、不更新）
analysis_job_snapshots(id, result_id, job_id, title, company, raw_jd,
                       source, source_url, import_method, captured_at, created_at)
job_matches(result_id, job_id, score, rank, reasons JSON,
            suggestion, strength_scores JSON, gap_details JSON)

-- 成长（对齐 GrowthEvent + Capability）
capability_snapshots(id, user_id, source, source_id, scores JSON, created_at)
growth_events(id, user_id, type, title, description, delta, occurred_at)

-- 简历（对齐 ResumeVersion / ResumeProfile）
resume_versions(id, user_id, label, file_name, uploaded_at, is_current)
resume_baseline(user_id, work_experiences JSON, project_experiences JSON,
                skills JSON, awards JSON, capabilities JSON, updated_at)

-- 额度（对齐 QuotaState）
user_quota(user_id, total, used, reserved, is_member)
```

`capability_snapshots` 是**成长模块的核心**：每次分析 / 每次简历更新写入一条，
`growth_events` 与 `Capability.delta` 由**相邻两条快照 diff** 派生（不存字段）。

---

## 6. 分阶段实施计划（提议）

| 阶段 | 内容 | 前置 |
|---|---|---|
| **P0** | 裁决 C1 / C2 / C3 / C4（本文件 §4） | 产品侧 |
| **P1** | 后端新增 `analysis` / `resume` / `growth` 命名空间骨架 + Pydantic 契约模型（与 §2 逐个对齐） | P0 C2 |
| **P2** | `POST /api/v1/analysis` 走 `manual_job_texts`，返回 `AnalysisProject` | P1 |
| **P3** | 能力分值模型（C4）→ `capabilitySnapshot` 可产出 | P0 C4 |
| **P4** | 版本化存储 + 成长事件 diff（§5 表） | P3 |
| **P5** | `user_id` 隔离 + 配额（C3 / C5） | P0 C1 |
| **P6** | 职业资产 / 首页摘要 / 资料 三个聚合端点 | P4 |

---

## 附：本文件的来源文件（Flutter 侧契约本体）

```
lib/data/repositories/  analysis | resume | growth | auth | career_assets | home | profile | ai
lib/data/models/        analysis_project | analysis_result | job | growth_event | capability
                        resume_profile | resume_file | career_assets | career_skill
                        work_experience | project_experience | award | quota_state
                        home_summary | user_profile | ai_message
```
