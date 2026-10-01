# Careerly P2 实施报告

> 日期：2026-10-01 ｜ 范围：P2-1 / P2-2 / P2-3 / P2-4 / P2-5（+ P2-6 语义开放项记账）
> 目标：打通 Careerly 的第二个「看见」—— **看见自己随着经历 / 技能 / 简历变化而发生的变化**

---

## 一、P2 达成了什么

P1 让 Careerly 变成「真正工作的产品 MVP」：能分析、能保存、能隔离。但那时产品只回答了一个问题：

```text
我与目标岗位之间的距离是多少？
```

P2 补上第二个问题：

```text
和上一次相比，我变化了吗？变化在哪？
```

这条闭环的实现方式不是新增一张「成长」表让前端自己去算，而是：

```text
分析成功
   ↓
写一条能力快照（capability_snapshots）—— 我当时长什么样
   ↓
与上一条快照 diff
   ↓
派生成长事件（growth_events）—— 我变化了什么
   ↓
GET /growth/events | /growth/timeline
   ↓
Flutter 成长足迹页读真源
```

**关键设计**：`growth_events` 只存 diff 产物，不存冗余状态。真相在快照，事件是派生物。这样「重新派生」永远是可能的，事件表损坏也不会污染能力事实。

---

## 二、各子项交付明细

### P2-1 / P2-2 两张新表（`535890f`）

`app/careerly/store.py`：

```sql
CREATE TABLE IF NOT EXISTS capability_snapshots (
    id         TEXT PRIMARY KEY,
    user_id    INTEGER NOT NULL,          -- 全 user_id 隔离
    source     TEXT NOT NULL DEFAULT 'analysis',
    source_id  TEXT,                      -- source=analysis 时为 project_id
    scores     TEXT NOT NULL DEFAULT '{}',-- {"capabilities": {...}, "matchScore": n}
    created_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS growth_events (
    id          TEXT PRIMARY KEY,
    user_id     INTEGER NOT NULL,
    type        TEXT NOT NULL,            -- Dart enum 名，原样下发
    title       TEXT NOT NULL DEFAULT '',
    description TEXT,
    delta       INTEGER,                  -- 无方向语义的事件为 NULL
    occurred_at REAL NOT NULL
);

CREATE INDEX idx_snapshots_user ON capability_snapshots(user_id, created_at);
CREATE INDEX idx_growth_user   ON growth_events(user_id, occurred_at);
```

设计要点：

- **快照只追加、不修改**。能力事实是历史，不是当前状态。
- **`matchScore` 存在 `scores` JSON 里，不单列**。避免为每个指标开一列，新增指标无需改表。
- **C4 评分降级（capabilities 为空）时快照照写**。保证「每次成功分析都留下一个锚点」，否则一次降级会让下一次 diff 的基准错位。
- 建表走 `CREATE TABLE IF NOT EXISTS`，既有 `careerly.db` 自动迁移，无需手工 DDL。

### P2-3 快照 diff 派生（`app/careerly/growth.py`，新增 129 行）

派生规则（`diff_snapshots`）：

| 情况 | 产出事件 |
|---|---|
| 无上一条快照（用户首次分析） | `experience`「建立职业基线」 |
| 新维度出现在本次快照 | `capability`「{维度}能力纳入画像」，`delta=null` |
| 同维度分数变化 | `capability`「{维度}能力提升/下降」，`delta=±n` |
| `matchScore` 变化（两侧都非空） | `matchScore`「岗位匹配度变化」，`delta=±n` |
| 上一条有、本次消失的维度 | **静默，不产事件** |

四条边界决策（2026-10-01 用户裁决 + 方案确认）：

1. **首次分析也产出一条事件**（`experience`「建立职业基线」）—— 不让新用户的成长足迹是空的。这是产品判断，不是技术判断：空白页会让人以为功能坏了。
2. **`rank` 事件不生成** —— 一次分析对应一个岗位，不存在「同一岗位排名变化」的语义。硬造出来就是假数据。
3. **`experience` / `skill` 其余来源不生成** —— 它们的数据源是职业资产（仍 Mock）。留在枚举里但不伪造，等职业资产接真源时再对齐（即 roadmap 的 P2-6 开放项）。
4. **维度消失静默** —— 能力维度随 JD 措辞波动，某次没被评到 ≠ 能力退步。把它记成「下降」是误导。

**防御性设计**（`record_analysis_growth`）：整个成长记录包在 `try/except` 里，失败只记日志、不抛出。

> 这条不是洁癖。P1-5 刚吃过一次教训：`state.resume` 那个属性名写错，让 C4 评分每次必炸、成功分析在收尾阶段变成 502。成长记录是**分析成功之后**的附加动作，它绝不允许把一个已经成功的分析回滚成失败。

### P2-4 两个只读端点（`app/careerly/router.py`）

```text
GET /api/v1/growth/events?type=&limit=     → [GrowthEvent]（默认 limit=50）
GET /api/v1/growth/timeline?from=&to=      → [GrowthEvent]（ISO 时间区间）
```

- `type` 必须是 Dart enum 名（`experience` / `skill` / `capability` / `matchScore` / `rank`）。**非法值显式 422 `INVALID_TYPE`，绝不静默返回空列表** —— 静默会让前端误判成「没有这类事件」，是最难查的一类 bug。
- 非法或倒置的时间区间 → 422 `INVALID_RANGE`。
- 两端点均走统一信封 + `get_current_user` 归属作用域，`user_id` 从 JWT 取，不可由入参指定。
- 中文字段全部在事件创建时生成并落库；端点**原样下发 enum 名**，不翻中文。

### P2-5 Flutter 成长足迹接真源（careerly `67e3dee`）

- 新增 `lib/data/api/api_growth_repository.dart`：实现 `GrowthRepository`，经 `ApiClient` 调两端点。
- **仓储只做查询，不在本地合成或过滤事件** —— 派生口径只有后端一份，避免两侧算法分叉。
- `lib/app/providers.dart`：`--dart-define=CAREERLY_REAL_BACKEND=true` 时切 `ApiGrowthRepository`，否则仍用 Mock。
- 新增 `test/data/api_growth_repository_test.dart`（JSON 解析，含 `type` → `GrowthEventType.tryParse` 直读、`delta` 为 null 的场景）。

> **P2-6（未做，记账）**：`experience` / `skill` 类型目前只被基线事件使用，职业资产类来源仍 Mock。不是遗漏，是刻意不伪造 —— 契约文档已就地标注。

---

## 三、测试总账

| 侧 | 结果 |
|---|---|
| 后端全量（`tests/`） | **79 passed, 1 failed** in 205.91s |
| └ P2 专项 | `tests/test_p2_growth.py` **8 passed**（36.36s） |
| └ 那 1 条失败 | `test_matching.py::TestSkillOverlap::test_skill_synonyms` —— **既有问题**，非本次引入（P1 已用 `git stash` 在 HEAD 复跑证明过） |
| Flutter | `flutter test` **195 passed, 1 skipped**；`flutter analyze` 0 issue |

`tests/test_p2_growth.py` 8 条覆盖：

1. `test_first_analysis_writes_baseline_snapshot_and_event` — 首次分析 → 快照 + 基线事件
2. `test_second_analysis_derives_capability_events` — 第二次分析 → capability diff 事件
3. `test_match_score_drop_derives_matchscore_event` — 匹配分下降 → `matchScore` 事件
4. `test_type_filter_and_invalid_type` — type 过滤 + 非法 type 422
5. `test_timeline_range_filter` — 时间区间过滤
6. `test_growth_events_are_user_scoped` — 用户 A 读不到用户 B 的事件
7. `test_failed_analysis_writes_no_snapshot` — 失败分析不留快照
8. `test_growth_recording_failure_never_breaks_analysis` — 注入故障的成长记录不拖垮成功分析

测试复用 P1-5 的**假模型供应商 + 真实 LangGraph**：唯一可调的是 `TunableLLM.caps`，让「两次分析能力分不同」成为可编排的事实，而不是碰巧对上。

---

## 四、提交清单（均未 push，遵守 P0-0）

**work_agent**

| 提交 | 内容 |
|---|---|
| `535890f` | `feat(p2): 成长时间轴 —— 能力快照 + diff 派生成长事件`（growth.py / router.py / store.py / test_p2_growth.py，+596 行） |
| `9b5308e` | `docs(p2-4): 契约回写` —— growth 两端点移入「已落地」+ 事件派生口径 + 错误码；`.gitignore` 补 `tests/.tmp_engine/` |

**careerly**

| 提交 | 内容 |
|---|---|
| `67e3dee` | `feat(p2-5): 成长足迹仓储从 Mock 切真实 API` |

---

## 五、契约同步状态

`docs/careerly/BACKEND_API_CONTRACT.md`：

- 「已落地子集」新增 `GET /api/v1/growth/events`、`GET /api/v1/growth/timeline`，标注 Flutter 已接（P2-5）。
- 新增**P2 事件派生口径**说明块：派生方式、首次基线事件、`rank` 不生成、职业资产类不伪造、两个 422 错误码。
- 「目标态」移除 growth 两行（已落地）。
- 「P3 未建」剩余：`resume/versions`（多版本对比）、`career/assets`、`home/summary`、`profile`。

---

## 六、遗留事项（进入 P3 前知悉）

1. **`test_skill_synonyms` 既有失败仍未修** —— P1 报告已挂账两项，本次仍未顺手修（P2 改动集中在新增文件，未触碰匹配逻辑，修它属于独立范围）。建议作为独立小提交处理。
2. **分析仍是同步阻塞** —— P1-1b 双模（对内异步/对外同步）未做。实测一个完整分析约 210s，客户端超时 300s，弱网余量偏薄。P3 若加多岗位对比，这个必须先解决。
3. **`experience` / `skill` 类型事件只有基线一条来源** —— 职业资产接真源前，成长足迹的事实密度偏低（首次分析之后只有能力/匹配分两类事件）。
4. **Flutter 侧仍有 5 个页面走 Mock** —— `home` / `ai` / `growth`（部分）/ `career_assets` / `profile`。成长足迹仓储已切真源，但页面数据装配是否全量接上，需在真机联调时确认。
5. **所有提交停留在本地分支，未 push**。
