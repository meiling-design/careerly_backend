# P1 批次遗留与后续任务

本文件只记录 **P1-01 ~ P1-06 批次明确不做、但已确认存在风险** 的事项。
修复进度不要写在这里；写进对应实现文件与测试。

## F1 — AI 对话额度的并发竞争（单独任务，未在本批次修复）

**状态**：已确认存在，**未修复**。

**证据**

- `app/careerly/router.py` 的 `POST /api/v1/ai/messages` 前置检查是
  `get_ai_quota()`，真正的扣减是流结束后 `store.consume_ai_quota(user_id)`。
- 与 P1-04 修复前的分析额度同构：**检查与扣减分离**。同一用户并发发起
  多条 AI 对话时，可能同时通过 `used < total` 的检查。
- `app/careerly/store.py` 的 `consume_ai_quota` 也没有任务级幂等键。

**为什么本批次不修**

用户明确要求：「AI 对话额度暂时不在本批次重构范围内；如发现，应记录
单独任务，不要顺手扩大修改范围。」P1-04 的预占模型需要新增
AI 消息任务表 / 预占状态，属于独立设计，不适合塞进分析链路改造。

**建议修复方向**

为 AI 消息引入与分析任务同构的预占 / 确认 / 释放状态机，或用一个
带条件的原子 UPDATE 在开流前预占、流结束后确认、中途失败释放。

## F2 — 服务重启后的「已完成但预占未结清」窗口

**状态**：**已在本批次处理**（记录在此便于追溯）。

进程若在 `analysis_tasks.status = completed` 写入之后、额度确认之前被杀，
`recover_interrupted_tasks()` 会在下次启动时把
`completed + quota_state = reserved` 补确认为 `consumed`；
`failed/cancelled + reserved` 则释放。不会重复扣费，也不会永久占用额度。

见 `app/careerly/store.py::recover_interrupted_tasks` 与
`tests/test_p1_growth_quota_recovery.py`。
