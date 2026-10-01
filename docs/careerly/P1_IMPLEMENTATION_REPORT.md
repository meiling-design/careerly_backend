# Careerly P1 实施报告

> 日期：2026-10-01 ｜ 范围：P1-1a / P1-2 / P1-3 / P1-4 / P1-5 / P1-6
> 目标：完成 P0 + P1 后，Careerly 从「有完整 UI 的 Demo」变成「真正工作的产品 MVP」

---

## 一、P1 验收对照（用户目标逐条核验）

| 用户目标 | 状态 | 证据 |
|---|---|---|
| 用户可以登录 | ✅ | `test_p0_auth_boundary.py`（10 条，真实 JWT 全链路） |
| 上传简历 | ✅ | `POST /resume/upload` 已鉴权 + 归属隔离（P0-4） |
| 输入/导入岗位 → 调用真实 AI → 得到分析结果 | ✅ | `test_p1_engine_e2e.py`：真实 LangGraph 8 节点全跑通 |
| 结果被正确保存 | ✅ | `analysis_tasks` 状态机 + `careerly.db` 项目持久化，可经 `GET /api/v1/analysis/tasks/{id}` 回读 |
| 用户 A 看不到用户 B 的数据 | ✅ | `test_p0_user_isolation.py`（19 条）+ e2e 越权用例（统一 404） |
| Android 真机可以访问后端 | ✅ | P0 G1：INTERNET 入 main、cleartext 仅 debug source set |
| 分析失败有明确状态 | ✅ | P1-2：`NO_RESUME` / `NO_BASELINE` / `ANALYSIS_EMPTY` / 502 → task `failed` + error_code |
| Token 不因冷启动丢失 | ✅ | P1-6：flutter_secure_storage 持久化 + 冷启动恢复测试（11 条） |
| Agent 真正被 Careerly API 调起来 | ✅ | P1-5：注入假模型供应商、跑真图，断言节点副作用与产物 |

## 二、各子项交付明细

### P1-1a 分析任务状态机（`dff279a`）
- `app/careerly/store.py` 新增 `analysis_tasks` 表（user_id 隔离）：queued → processing → completed/failed
- `app/careerly/router.py`：`analyze()` 建任务并围绕引擎调用推进状态；新增 `GET /api/v1/analysis/tasks`、`/tasks/{id}`
- 测试：`tests/test_p1_analysis_tasks.py`（11 条）

### P1-2 失败语义（同提交）
- `app/careerly/engine.py`：`AnalysisError` 带 `NO_RESUME` / `NO_BASELINE` / `ANALYSIS_EMPTY` 错误码
- NO_BASELINE 预检失败也落一条 failed 任务（不再「无痕」）

### P1-3 根路径重复端点收敛（`69fe6c7`）
- `server.py` 根路径 `/match`、`/sessions*` 标记 `deprecated=True`（历史别名，不删除 —— D-H）
- 裸抛 `HTTPException` 全部换成 `app/core/errors.py` 领域异常 → 两个入口返回同一统一信封
- 回归：38 passed

### P1-4 契约文档回写（`56f8075`）
- `docs/careerly/BACKEND_API_CONTRACT.md` 拆「已落地子集 / 目标态」两栏，冲突 C1–C5 全部标注裁决结果

### P1-5 端到端闭环（`5336ddb`）
- `tests/test_p1_engine_e2e.py`（9 条）：假 LLM/Embedding 供应商 + **真实 DecisionGraph**
- 覆盖：完整闭环、结果保存回读、越权 404、失败状态、多用户隔离
- **意外收获：发现并修复一个阻断性 Bug** —— `engine.py` 把 `state.resume_output` 误写为 `state.resume`，C4 能力评分每次必炸，导致所有成功分析在收尾阶段变 502。该 Bug 自引入起从未被任何测试触达，本次 e2e 首次暴露。

### P1-6 Token 持久化（后端 `dcbe52a` + Flutter `f87724b`）
- 后端：`ACCESS_TOKEN_EXPIRE_MINUTES` / `REFRESH_TOKEN_EXPIRE_DAYS` 环境变量化，refresh 默认 7 天 → 90 天（D-B）；`.env.example` 同步
- Flutter：新增 `lib/data/api/token_store.dart`（flutter_secure_storage v11），ApiClient 冷启动自动恢复会话；401 自动 refresh 并重放请求
- 测试：后端 `test_p1_token_expiry.py`（4 条）+ Flutter `test/data/token_persistence_test.dart`（11 条，模拟进程杀掉后冷启动）
- 注意：v11 已移除 `encryptedSharedPreferences` 参数（改原生 AES-GCM + RSA-OAEP 密钥包装），勿按 v9 写法回退

## 三、测试总账

| 侧 | 结果 |
|---|---|
| 后端全量 | 71 passed，1 failed（`test_skill_synonyms`，**既有问题**，git stash 已证明非本次引入） |
| Flutter | `flutter analyze` 0 issue；180 + 11 单测全绿 |

## 四、提交清单（均未 push，遵守 P0-0）

- work_agent：`dff279a` → `dcbe52a` → `5336ddb` → `69fe6c7` → `56f8075`（另 `be61506` gitignore）
- careerly：`f87724b`

## 五、遗留事项（进入 P2 前知悉）

1. `test_skill_synonyms` 既有失败 —— 建议 P2 顺手修
2. `analysis_tasks` 结果回读依赖 `project_id` 关联，P2 做成长时间轴时需补 `capability_snapshots` / `growth_events` 表
3. 分析仍是同步阻塞（对内异步/对外同步的 P1-1b 未做，弱网下 210s vs 300s 超时余量偏薄）
4. 所有提交停留在本地分支，未 push
