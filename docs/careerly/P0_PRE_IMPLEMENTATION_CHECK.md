# P0 Pre-Implementation Check

> 执行时间：2026-09-30 17:45
> 状态：**只读侦察完成，未修改任何代码**（符合 P0 Prompt 第一步要求）
> 对应 Prompt：`Careerly P0 — Real API E2E & Security Baseline`

---

## 0. 结论速览

| 项 | 原假设 | 核实后的事实 |
|---|---|---|
| **G1 真凶** | Android 缺 INTERNET 权限 → 联调阻塞 | ⚠️ **部分错误**：`debug`/`profile` 源集**已有** INTERNET 权限（Flutter 默认注入）。真凶是**明文 HTTP 被 Android 9+ 默认策略拦截**；缺权限只影响 **release** 构建 |
| **G2 范围** | Careerly API 存在越权 | ✅ **收窄**：Careerly 的 7 个端点**全部有鉴权 + 归属校验**。风险**只在旧 Job Matching API 表面**（Flutter 不调用） |
| **P0-5 工作量** | 需新建 E2E | ✅ **大幅减少**：`test/real_backend_integration_test.dart` 已覆盖正向全链路。**唯一大缺口 = User B 越权矩阵** |

**净结果**：P0 的实际工作量比预期小，但**修复动作的预期必须修正** —— 只补 INTERNET 权限**不会**让联调通。

---

## 1. 当前状态

### 1.1 Git（P0-0）

| 仓库 | 分支 | 工作区 | 未跟踪文件 |
|---|---|---|---|
| `G:\ai_agent\careerly` | `feature/phase6-analysis` | ✅ 干净 | `docs/Careerly 全栈开发总控 Prompt.md`（你的文档）、`test/real_backend_integration_test.dart`（**既有联调测试，须保留**） |
| `G:\ai_agent\work_agent` | `careerly-backend` | ✅ 干净 | 3 个本次审计产出文档 |

- **无未提交的已跟踪文件修改** → 两个仓库都可安全建立 baseline commit
- 最近提交（work_agent）：`84781d4 fix(auth): 老库重建 users 表，修复手机号注册 500`
- **不 push remote**

⚠️ `test/real_backend_integration_test.dart` 是未跟踪文件，**P0-0 commit 前必须先决定**：它是既有资产（要保留并纳入版本控制），不是临时文件。

### 1.2 检查项 ① ｜ Android 网络配置

```
android/app/src/main/AndroidManifest.xml      ❌ 无 uses-permission INTERNET
android/app/src/debug/AndroidManifest.xml     ✅ 有 INTERNET（Flutter 默认注入，注释明示给 hot reload）
android/app/src/profile/AndroidManifest.xml   ✅ 有 INTERNET
android/app/src/main/res/xml/                 ❌ 目录不存在
network_security_config.xml                   ❌ 全项目不存在
usesCleartextTraffic                          ❌ 全项目未配置
targetSdk                                     = flutter.targetSdkVersion（≥ 28）
```

**Manifest 合并规则**：源集优先级 `debug > main`。因此：

| 构建类型 | INTERNET | 明文 HTTP | 结果 |
|---|---|---|---|
| `flutter run`（debug） | ✅ 有 | ❌ 被拦 | 🔴 **连不上 —— 与权限无关** |
| `flutter build apk`（release） | ❌ 无 | —— | 🔴 **连不上 —— 双重原因** |

**两个独立缺陷，必须分开修：**

1. **联调阻塞**（当下）：明文 `http://10.0.2.2:8000` 被 targetSdk≥28 的默认 cleartext 策略拦截 —— **debug 构建同样中招**
2. **上线炸弹**（未来）：release 包无 INTERNET 权限，**装到手机上完全无法联网**

> 只做「补 INTERNET 权限」这一件事，**debug 联调依然连不上**。这是本报告最重要的修正。

### 1.3 检查项 ② ｜ Auth 实现

| 项 | 现状 |
|---|---|
| 端点 | `POST /api/v1/auth/register`(201) / `login` / `refresh` / `GET /me` —— 全部就位 |
| 身份 | 手机号（D85），无密码无验证码 |
| Token | JWT HS256；access **60min** / refresh **7d**（`security.py:42-43`） |
| 字段命名 | `access_token` / `refresh_token` / `token_type` / `expires_in` —— **snake_case**（OAuth2 惯例） |
| Flutter | `setSession()` 写入内存；请求自动带 `Authorization: Bearer`；**401 自动 refresh + 只重试一次**（防死循环）✅ |

**契约一致性**：后端返回 snake_case ↔ Flutter 期望 snake_case → ✅ **完全一致，无 break**。
（注：业务数据是 camelCase，**token 字段是 snake_case 特例** —— 两套并存，不是 bug。）

🔴 **仍待裁决 D-NEW**：`login` 仅凭手机号即签发 token，无任何凭证校验。这正是需要 **DEV/PROD 硬边界**的原因。

### 1.4 检查项 ③④⑤ ｜ Protected APIs 与归属校验（P0-4 核心）

**完整鉴权矩阵**（实测源码）：

| 端点 | 鉴权 | 归属校验 | Flutter 是否调用 | 判定 |
|---|---|---|---|---|
| **`careerly/router.py` 全部 7 个**<br>`POST /analysis`、`GET /analysis/projects`、`/projects/{id}`、`/analysis/jobs`、`PUT /resume`、`GET /resume`、`GET /quota` | ✅ `Depends(get_current_user)` | ✅ **`store.xxx(user_id, ...)`** | ✅ **是（全部）** | ✅ **安全** |
| `router_app.py:142` `POST /resume/upload` | ❌ **无** | ❌ | ❌ | 🔴 **未鉴权** |
| `router_app.py:212` `GET /tasks/{id}/result` | ❌ **无** | ❌ | ❌ | 🔴 **未登录可读他人 score/advantages/gaps** |
| `router_v1.py` `GET /tasks`、`/tasks/{id}` | ✅ | ❌ **无** | ❌ | 🟠 登录后可读他人任务 |
| `router_v1.py` `GET /sessions`、`/sessions/{id}`、`/{id}/report` | ✅ | ❌ **无** | ❌ | 🟠 登录后可读他人会话 |
| `server.py:237,256,293` 同 sessions 系 | ✅ | ❌ **无** | ❌ | 🟠 同上 |
| `server.py` `/health`、`/` | ✅ | n/a | ❌ | ✅ |

**两条结论**：

1. ✅ **Careerly API（Flutter 实际消费的 10 个路径）全部在有鉴权的一侧** —— 业务层隔离是成立的
2. 🔴 **越权风险 100% 集中在旧 Job Matching API 表面**，而 Flutter **完全不调用**它们（`submitResumeFile` 是主动抛异常的 stub）

**因此 G2 的性质是「暴露面收敛」，不是「Flutter 功能阻塞」** —— 修复风险低、对 App 零影响，但**必须修**（安全一票否决：端点对外开放，注册在 `server.py` 上）。

### 1.5 检查项 ⑥ ｜ Analysis API

`POST /api/v1/analysis`（`careerly/router.py:49`）现状：

```
current_user (JWT) → user_id = int(current_user["id"])
  → get_quota(user_id)          ← 配额前置检查
  → get_resume(user_id)         ← 读当前用户简历基线；无则 422
  → run_analysis(...)           ← L2 → L1 进程内调用
  → save_project(user_id, ...)  ← 结果绑定 user_id
  → consume_quota(user_id, ...) ← 配额扣减
```

✅ **链路与 Prompt §十二 描述完全一致，无需重新设计**。
🟠 同步阻塞实测约 **210s**（`ApiClient.analysisTimeout = 300s`）→ 记为 **P1-TASK-ASYNC**，P0 不动。

### 1.6 检查项 ⑦ ｜ Flutter ApiClient

`lib/data/api/api_client.dart` 质量良好，**无第二套客户端**：

- baseUrl 默认 `http://10.0.2.2:8000`（模拟器正确）✅，`--dart-define=CAREERLY_API_BASE_URL` 可覆盖
- `CAREERLY_USE_API` 开关（默认 `false` = Mock First）
- 统一 envelope 解包 + 错误归一化（`QuotaError` / `AuthError` / `ParseError` / `AIError` / `NetworkError`）
- 超时分级：默认 30s / 分析 300s
- 🔵 小缺口：`_rawSend` 仅支持 **GET/POST/PUT**，无 DELETE —— 当前后端无 DELETE 端点，不阻塞

### 1.7 检查项 ⑧ ｜ 现有测试盘点（重要）

| 资产 | 覆盖 | 可用性 |
|---|---|---|
| `careerly/test/real_backend_integration_test.dart` | **正向全链路**：register→login→JWT→resume→quota→**真实分析(200s)**→快照非空→匹配分→列表/详情/岗位库→**额度扣减**→**无 token → AuthError**→文件上传 stub | ✅ **复用**。默认 `skip`，需 `--dart-define=CAREERLY_REAL_BACKEND=true` 启用 |
| `careerly/test/` 其余 17 个 | UI / 设计系统 / 各 phase 页面 | ✅ 保留 |
| `work_agent/tests/` | `test_matching.py`、`test_routing.py`（+conftest/cassettes） | 🟠 **无 API / 认证 / 多用户测试** |
| `work_agent/scripts/e2e_careerly.py` | ⚠️ **进程内引擎脚本，不走 HTTP** —— 直接 `run_analysis()`，tempfile 隔离 DB | 🟠 引擎可用性验证有用，**但测不到 G1/G2** |
| `pytest.ini` | ✅ 存在 | —— |

**`real_backend_integration_test.dart` 的唯一大缺口**：
- ❌ **User B 越权矩阵**（User A 建数据 → User B 读 → 拒绝）—— P0 核心安全验收，**当前完全没有**

---

## 2. 需要修改的文件

| # | 文件 | 动作 | 为谁 |
|---|---|---|---|
| 1 | `careerly/android/app/src/main/AndroidManifest.xml` | **加** `<uses-permission INTERNET>` | release 联网 |
| 2 | `careerly/android/app/src/debug/AndroidManifest.xml` | **加** `<application android:networkSecurityConfig>`（`tools:replace`） | 开发明文 HTTP |
| 3 | `careerly/android/app/src/debug/res/xml/network_security_config.xml` | **新建**，仅放行 `10.0.2.2` / `localhost` | 开发明文 HTTP |
| 4 | `work_agent/app/api/router_app.py` | **加** `Depends(get_current_user)` ×2 + 归属校验 | G2 |
| 5 | `work_agent/app/core/state_store.py` | `sessions` 加 `user_id` 列 + 写入 + 查询条件 | G2 |
| 6 | `work_agent/app/api/router_v1.py` | tasks/sessions 查询加 `WHERE user_id = ?` | G2 |
| 7 | `work_agent/app/api/server.py` | 根路径 sessions 加 `WHERE user_id = ?` | G2 |
| 8 | `careerly/test/real_backend_integration_test.dart` | **补** User B 越权矩阵 | P0-5 |
| 9 | `work_agent/tests/` | **新建** API/auth/多用户测试 | P0-5 |
| 10 | Flutter `ApiClient` / `ApiAuthRepository` | **本次不动** —— 契约一致，无缺口 | —— |

**明确不动**：`app/core/orchestrator.py`、`app/agents/*`、`app/models/*`、`app/services/*`、`app/careerly/router.py`、`engine.py`、全部 Flutter 页面。

---

## 3. 修改计划

### P0-0 ｜ Git 安全点
1. 确认 `test/real_backend_integration_test.dart` 为既有资产 → 纳入版本控制
2. careerly：commit `chore(p0): establish pre-integration baseline`
3. work_agent：commit `chore(p0): establish pre-integration baseline`
4. 不 push

### P0-1/2 ｜ Android 网络（**两处独立修复，勿合并理解**）
1. `main` manifest 加 INTERNET → 为 **release**
2. `debug` 源集加 network_security_config（**只在 debug**，release 完全不受影响）→ 为**联调**
3. 不改 `main` 的 cleartext 策略（遵守「不为开发环境修改生产安全策略」）
4. 记录 DEV / PROD 网络安全差异

### P0-3 ｜ Auth 真实验证
实际运行 API（非只读代码）：register → login → `/me` → 确认 `Authorization: Bearer` 生效 → 确认 401 分支。

### P0-4 ｜ 权限边界（**最小修改，不删 API**）
1. `router_app.py` 两端点补鉴权（**风险最高、优先级最高** —— 未登录即可读数据）
2. `sessions` 表加 `user_id`（详见 §4 张力说明）
3. `router_v1.py` / `server.py` 查询加 `WHERE user_id = ?`
4. 错误码沿用**项目现有策略**（404 优先，见 §4）

### P0-5 ｜ E2E（复用 + 补缺口）
1. **补** User B 越权矩阵到 `real_backend_integration_test.dart`
2. **新建** `tests/test_careerly_api.py`：API/auth/多用户隔离
3. 跑 `flutter analyze` + `flutter test` + `pytest`
4. 拆 3 个 commit（按职责）

---

## 4. 风险与需确认事项

### 🔴 风险 1：两套 P0 编号冲突

| Roadmap v2 | Prompt | 含义 |
|---|---|---|
| P0-1 Red Baseline | （无对应） | —— |
| P0-2 Android | P0-1/2 | Android 网络 |
| P0-3 权限 | P0-4 | 权限边界 |
| P0-5 验收 | P0-5 | E2E |

**建议**：以 **Prompt 编号为准**（施工指令），Roadmap 保留「为什么」。
⚠️ Roadmap 的 `P0-1 Red Baseline` 写的是「跑 `e2e_careerly.py`」—— **该脚本不走 HTTP，测不到 G1/G2**，此处需修正为 HTTP 层脚本。

### 🔴 风险 2：架构张力 —— `user_id` 该不该下沉？

- P0-4 要求 `sessions` 表有归属 → 需 `user_id` 落到该表
- 项目铁律：「`user_id` **不**下沉到 `GraphState`」

**解法（两不违背）**：
```
user_id 写入 sessions 表的【列】   ← 由 L3/L2 上层写入，供归属校验
user_id 不进入 GraphState          ← 引擎运行时状态保持产品无关
```
即：表结构有，引擎内存状态无。**请在动手前确认这个边界。**

### 🟠 风险 3：旧 API 是修还是下线？
本报告按 Prompt §三-16「不删除现有 API」→ **修**。但需确认 Streamlit / 旧客户端是否仍在消费 router_v1 的 endpoints。

### 🟠 风险 4：`real_backend_integration_test.dart` 的验证盲区
它跑在 **Dart VM**，**不经过 Android 网络栈** → 能验证后端 API，**测不到 G1**。
**Android 联网必须用模拟器/真机实跑 `flutter run` 验证** —— 两条路径不可混淆。

### 🟡 风险 5：错误码策略
越权建议返回 **404 而非 403**（不泄露「资源存在」）。Prompt §十说「保持项目现有策略」—— 需确认项目现有约定是否也是 404。

### 🟡 风险 6：`graph_state.db` 旧数据
实测 `sessions` **5 行**、无 `user_id`。加列后历史行为 NULL，`WHERE user_id = ?` **天然不匹配** → fail-closed，无需特殊清理。
**纪律**：禁止退化成 `WHERE user_id IS ?` 或 `OR user_id IS NULL`；禁止为过测试硬塞归属。

---

## 5. 预计测试方式（三条路径，不可混淆）

| 验证目标 | 手段 | 备注 |
|---|---|---|
| **后端 API + 用户隔离** | ① `pytest`（新建测试）<br>② `flutter test --dart-define=CAREERLY_REAL_BACKEND=true`（Dart VM） | 不经过 Android |
| **引擎可用性** | `scripts/e2e_careerly.py`（进程内，约 200s，tempfile 隔离） | 只证明 L2→L1 通 |
| **Android 真实联网（G1）** | **模拟器/真机 `flutter run` 实跑** | 唯一能验证 cleartext 修复的方式 |
| Flutter 静态检查 | `flutter analyze` | —— |

---

## 6. 开工前需要你确认的 4 点

1. **P0 编号以 Prompt 为准**（Roadmap 的 P0-1 Red Baseline 条目需修正）
2. **`user_id` 边界**：写 `sessions` 表列、不进 `GraphState` —— 是否认可？
3. **旧 API 处理**：修（加鉴权+归属）还是下线？本报告默认**修**
4. **越权错误码**：统一 **404** 还是项目现有其它约定？

> 确认后即从 **P0-0（Git 安全点）** 开工，按 §3 顺序推进，每个逻辑单元单独 commit。
> **本次未修改任何代码。**
