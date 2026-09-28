# Careerly 后端 API 契约与改造方案 V0.1（草案 · 待裁决）

> 状态：**草案**。含 4 处**未决冲突**，其中 C1 为产品级阻塞项，须裁决后方可编码。
>
> 依据优先级（`careerly/CLAUDE.md` §0）：PRD V2.1 > IA V2.1 > Development Baseline V1.0 > Flutter Spec V2.1 > Figma > 代码。
>
> 本文件由 **Flutter 侧 `lib/data/repositories/*.dart` 与 `lib/data/models/*.dart` 倒推**生成 —— 契约不是后端自拟，而是 App 已经写好的 `fromJson` / `toJson` 所要求的形状。

---

## 0. 结论速览

| # | 事项 | 状态 |
|---|---|---|
| — | Flutter 契约 → 后端端点映射 | ✅ 已完整推导（§1–§3） |
| C1 | **认证身份字段**：手机号（D85）vs 邮箱+密码（后端 JWT） | 🔴 **产品级冲突，阻塞** |
| C2 | 分析调用形态：同步返回 Project vs 异步 task_id | 🟠 语义冲突，需定接口形态 |
| C3 | 额度 QuotaState（免费 3 次）vs 后端 DemoGuard | 🟠 规则不同，需后端实现配额 |
| C4 | 能力分值模型缺失：Flutter 需 0–100 能力分 + delta，后端无 | 🔴 能力模型缺口，阻塞成长模块 |
| C5 | 会话持久化（D52 不存 / D54 待裁决）→ user_id 隔离在 App 侧无处落地 | 🔴 阻塞「user_id 隔离」改造项 |

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
| `AIRepository.sendMessage(text)` | `POST /api/v1/ai/messages` | `AIMessage` |
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
  "matches": [ <JobMatch> ],
  "capabilitySnapshot": { "产品规划": 85, "AI 应用能力": 76 },
  "createdAt": "ISO8601 | null"
}
```

> ⚠️ `capabilitySnapshot`（能力名 → 0–100 分）与 `version` 是**「看见成长」的数据支点** ——
> 后端现在**产不出这个 Map**（见 C4）。

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
{ "total": 3, "used": 0, "isMember": false }
```

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
                 capability_snapshot JSON, created_at)
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
user_quota(user_id, total, used, is_member)
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
