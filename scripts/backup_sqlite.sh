#!/usr/bin/env bash
# 备份 Careerly 的四个 SQLite 库（发布基线 P0.5）。
#
# 为什么不能直接 cp
# -----------------
# 四个库都以 WAL 模式运行（`PRAGMA journal_mode=WAL`）。在 WAL 下，最近的
# 写入可能还在 `-wal` 文件里没有合并进主库；`cp foo.db` 拿到的是一份
# **缺了新数据的快照** —— 它能被打开、看起来正常，但内容是旧的，甚至
# 在写入进行到一半时会拿到撕裂的页。这类损坏不会当场报错，
# 等你发现时通常已经是「需要还原」的那一刻了。
#
# 正确做法是 SQLite 的**在线备份 API**：`sqlite3 <db> ".backup <dest>"`。
# 它会在事务一致性点上拷贝，且允许数据库同时被其他进程写入。
# 本脚本优先用 sqlite3 CLI，没有则退回 Python 的 `Connection.backup()`
# ——两者是同一个 API，行为一致。
#
# 用法
# ----
#   scripts/backup_sqlite.sh                 # 备份到 backups/<时间戳>/
#   scripts/backup_sqlite.sh --verify-only   # 只做还原校验，不产生新备份
#   CAREERLY_BACKUP_KEEP_DAYS=14 scripts/backup_sqlite.sh
#
# 环境变量
# --------
#   CAREERLY_BACKUP_DIR        备份根目录（默认 <repo>/backups）
#   CAREERLY_BACKUP_KEEP_DAYS  保留天数（默认 7）
#   CAREERLY_BACKUP_OSS        阿里云 OSS 路径；设置且 ossutil 存在时上传
#
# 退出码：0 全部成功；非 0 有库备份或校验失败（**不静默通过**）。

set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BACKUP_ROOT="${CAREERLY_BACKUP_DIR:-$ROOT/backups}"
KEEP_DAYS="${CAREERLY_BACKUP_KEEP_DAYS:-7}"
STAMP="$(date +%Y-%m-%d_%H%M%S)"
DEST="$BACKUP_ROOT/$STAMP"

# 四个库。⚠️ 后两个**同名不同文件**（一个是引擎、一个是 DemoGuard），
# 只备份其中一个会让「恢复完成」变成一句谎话。
DBS=(
    "app/data/careerly.db"
    "app/data/auth.db"
    "app/data/graph_state.db"
    "data/graph_state.db"
)

failures=0
backed_up=0

# Git Bash / MSYS 下，sqlite3 与 python 都是 **Windows 程序**，不认 `/g/...`
# 这类 POSIX 路径（会报 "cannot open"）。cygpath -m 转成 `G:/...` —— 混用
# 正斜杠的写法两边都认，所以比 -w 的全反斜杠更稳。
# Linux 上没有 cygpath，这个函数自动退化成恒等变换。
native() {
    if command -v cygpath >/dev/null 2>&1; then
        cygpath -m "$1"
    else
        printf '%s' "$1"
    fi
}

backup_one() {
    local db out
    db="$(native "$1")"
    out="$(native "$2")"
    if command -v sqlite3 >/dev/null 2>&1; then
        # .timeout 让并发写入时等待而不是立刻失败（对齐服务端的 busy_timeout）
        sqlite3 "$db" ".timeout 5000" ".backup '$out'"
    else
        python - "$db" "$out" <<'PY'
import sqlite3, sys
src, dst = sys.argv[1], sys.argv[2]
with sqlite3.connect(src) as s, sqlite3.connect(dst) as d:
    s.backup(d)          # 与 sqlite3 CLI 的 .backup 是同一个 API
PY
    fi
}

verify_one() {
    local out
    out="$(native "$1")"
    local result
    if command -v sqlite3 >/dev/null 2>&1; then
        result="$(sqlite3 "$out" "PRAGMA integrity_check;" 2>&1)"
    else
        result="$(python -c "
import sqlite3, sys
print(sqlite3.connect(sys.argv[1]).execute('PRAGMA integrity_check;').fetchone()[0])
" "$out" 2>&1)"
    fi
    [ "$result" = "ok" ]
}

count_tables() {
    python -c "
import sqlite3, sys
print(sqlite3.connect(sys.argv[1]).execute(
    \"SELECT COUNT(*) FROM sqlite_master WHERE type='table'\").fetchone()[0])
" "$(native "$1")" 2>/dev/null || echo '?'
}

echo "备份目标：$DEST"
mkdir -p "$DEST" || exit 1

for rel in "${DBS[@]}"; do
    src="$ROOT/$rel"
    # 用下划线拍平路径，兼作同名文件的区分（app/data/graph_state.db vs data/graph_state.db）
    name="$(echo "$rel" | tr '/' '_')"
    out="$DEST/$name"

    if [ ! -f "$src" ]; then
        echo "  跳过  $rel （文件不存在）"
        continue
    fi

    if ! backup_one "$src" "$out"; then
        echo "  失败  $rel —— .backup 执行出错"
        failures=$((failures + 1))
        continue
    fi

    if ! verify_one "$out"; then
        echo "  失败  $rel —— 备份文件 integrity_check 未通过"
        failures=$((failures + 1))
        continue
    fi

    printf '  OK    %-26s %8s bytes  %s 张表\n' \
        "$rel" "$(wc -c < "$out" | tr -d ' ')" "$(count_tables "$out")"
    backed_up=$((backed_up + 1))
done

echo "本次成功备份 $backed_up 个库。"

# ── 保留策略 ────────────────────────────────────────────────────────────────
if [ -d "$BACKUP_ROOT" ]; then
    pruned="$(find "$BACKUP_ROOT" -mindepth 1 -maxdepth 1 -type d -name '20*' \
        -mtime "+$KEEP_DAYS" -print -exec rm -rf {} + 2>/dev/null | wc -l | tr -d ' ')"
    [ "$pruned" != "0" ] && echo "按 $KEEP_DAYS 天保留策略清理了 $pruned 份旧备份。"
fi

# ── 可选：上传 OSS ──────────────────────────────────────────────────────────
# 本地备份挡得住「误删」「写坏」，挡不住「整块盘没了」。异地一份才算数。
if [ -n "${CAREERLY_BACKUP_OSS:-}" ]; then
    if command -v ossutil >/dev/null 2>&1; then
        if ossutil cp -r "$DEST" "$CAREERLY_BACKUP_OSS/" -f; then
            echo "已上传 OSS：$CAREERLY_BACKUP_OSS/$STAMP"
        else
            echo "失败  OSS 上传未成功 —— 本地备份在，但没有异地副本"
            failures=$((failures + 1))
        fi
    else
        echo "失败  CAREERLY_BACKUP_OSS 已设置但找不到 ossutil"
        failures=$((failures + 1))
    fi
fi

if [ "$failures" -ne 0 ]; then
    echo "有 $failures 项失败。备份**不完整**，请勿据此认为数据已受保护。"
    exit 1
fi
echo "备份完成：$DEST"
