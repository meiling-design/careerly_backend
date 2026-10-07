# 还原演练（发布基线 P0.5）

> **一份没有验证过还原的备份，不是备份，只是一堆文件。**
>
> 备份脚本最容易失效的方式不是报错，而是**安静地成功**：路径写错、
> 备份了一个空文件、WAL 下用 `cp` 拿到旧快照 —— 这些都会让备份目录里
> 躺着看起来正常的文件，直到真正需要它的那天。
>
> 因此每次改动备份逻辑后，都要按本文跑一遍，并把实测结果记在这里。

---

## 判据

还原成功的标准不是「文件能打开」，而是：

1. 四个库都能被 sqlite3 打开（不是损坏文件）；
2. **每张表的行数与现库一致**（备份是刚做的前提下）；
3. `PRAGMA integrity_check` 返回 `ok`。

---

## 步骤

### 1. 做一次备份

```bash
scripts/backup_sqlite.sh
```

期望输出（四个库各自 `OK`，并给出字节数与表数）：

```text
  OK    app/data/careerly.db        2306048 bytes  12 张表
  OK    app/data/auth.db              36864 bytes  2 张表
  OK    app/data/graph_state.db      180224 bytes  6 张表
  OK    data/graph_state.db           12288 bytes  1 张表
本次成功备份 4 个库。
```

⚠️ 如果上面有任何一个 `失败`，脚本会以非 0 退出，并且**不要**认为
「其他的成功了所以还行」—— 四个库互相引用（`users.id` ↔ `careerly.db.user_id`），
少一个就不是一份可用的数据集。

### 2. 还原到临时目录并核对行数

```bash
LATEST="$(ls -1dt backups/*/ | head -1)"
TMP="$(mktemp -d)"
cp -r "$LATEST". "$TMP"/

python - "$TMP" <<'PY'
import sqlite3, sys, os
tmp = sys.argv[1]
pairs = [
    ("app_data_careerly.db",    "app/data/careerly.db"),
    ("app_data_auth.db",        "app/data/auth.db"),
    ("app_data_graph_state.db", "app/data/graph_state.db"),
    ("data_graph_state.db",     "data/graph_state.db"),
]
bad = 0
for name, live in pairs:
    rp = os.path.join(tmp, name)
    if not os.path.exists(rp):
        print(f"  缺失  {name}"); bad += 1; continue
    r, l = sqlite3.connect(rp), sqlite3.connect(live)
    rt = {n for (n,) in r.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    lt = {n for (n,) in l.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    diffs = []
    for t in sorted(rt | lt):
        if t not in rt: diffs.append(f"{t}(备份缺)"); continue
        if t not in lt: diffs.append(f"{t}(现库缺)"); continue
        a = r.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
        b = l.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
        if a != b: diffs.append(f"{t}: 备份{a} vs 现库{b}")
    if diffs: bad += 1
    print(f"  {live:26s} {len(rt)} 张表  {'一致' if not diffs else '不一致 -> ' + '; '.join(diffs)}")
    r.close(); l.close()
print("\n结论:", "还原校验通过" if bad == 0 else f"{bad} 个库不一致")
PY
rm -rf "$TMP"
```

### 3. 真正的还原（事故时）

还原是**覆盖**操作，先停服务，避免进程继续写：

```bash
sudo systemctl stop careerly-api
cp backups/<时间戳>/app_data_careerly.db  app/data/careerly.db
cp backups/<时间戳>/app_data_auth.db      app/data/auth.db
cp backups/<时间戳>/app_data_graph_state.db app/data/graph_state.db
cp backups/<时间戳>/data_graph_state.db   data/graph_state.db
# ⚠️ WAL 残留会让新库读到旧的 -wal，必须一并清掉
rm -f app/data/*.db-wal app/data/*.db-shm data/*.db-wal data/*.db-shm
sudo systemctl start careerly-api
```

---

## 实测记录

| 日期 | 结果 | 说明 |
|---|---|---|
| 2026-10-06 | ✅ 通过 | 四个库全部还原，12 / 2 / 6 / 1 张表，行数逐表一致 |

> 新增记录时请**填真实输出**，不要沿用上行数字（`DEVELOPMENT_WORKFLOW` §1.3）。

---

## 当前缺口

* **异地副本**：备份默认落在服务器本地 `backups/`。这挡得住「误删」「写坏」，
  挡不住「整块盘没了」。设置 `CAREERLY_BACKUP_OSS` 后脚本会上传阿里云 OSS
  —— **尚未配置**（依赖 OSS bucket 与 ossutil，属于你的基础设施）。
* **定时任务**：`DEPLOY_ECS.md` §4 给了 crontab 示例，**尚未在服务器上启用**
  （没有真实 ECS）。
* **备份加密**：备份是明文 SQLite，含全部用户简历。目前依赖磁盘权限，
  未做静态加密。
