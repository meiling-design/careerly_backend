"""物理清理已注销账号（决策 D119 / 发布基线 K19）。

背景
----
用户点「删除账号」时后端只做**软删除**（``status='deleted'`` + 吊销凭据），
用户视角立刻生效，数据留 30 天作为误删缓冲。本脚本负责窗口期之后的
**物理级联清除** —— 真正把简历、报告、上传件从磁盘上抹掉。

这一步必须跑，否则「删除账号」就是一句不完整的承诺：账号登不进来了，
但简历还躺在 ``careerly.db`` 里。

用法
----
```bash
# 预演（默认）：列出将清理哪些账号、各删多少
python scripts/purge_deleted_accounts.py

# 真正执行
python scripts/purge_deleted_accounts.py --apply

# 自定义窗口（例如测试时用 0 天）
python scripts/purge_deleted_accounts.py --days 0
```

建议每天由 systemd timer / crontab 跑一次（``--apply``）。

⚠️ 不可撤销。执行前请确认 ``scripts/backup_sqlite.sh`` 已按日运行。
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.core.account_purge import purge_user  # noqa: E402
from app.core.auth_store import AuthStore  # noqa: E402

DEFAULT_GRACE_DAYS = 30


def main() -> int:
    parser = argparse.ArgumentParser(
        description="物理清除已过缓冲期的注销账号及其全部关联数据。",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="真正执行删除。不给这个参数时只做预演（dry-run）。",
    )
    parser.add_argument(
        "--days",
        type=int,
        default=DEFAULT_GRACE_DAYS,
        help=f"软删除后经过多少天才物理清理（默认 {DEFAULT_GRACE_DAYS}）。",
    )
    args = parser.parse_args()
    dry_run = not args.apply

    store = AuthStore()
    cutoff = time.time() - args.days * 86400
    candidates = store.list_users_deleted_before(cutoff)

    if not candidates:
        print(f"没有超过 {args.days} 天的已注销账号 —— 无事可做。")
        return 0

    print(f"发现 {len(candidates)} 个待清理账号（软删除已超过 {args.days} 天）：")
    for row in candidates:
        age_days = (time.time() - (row["deleted_at"] or 0)) / 86400
        print(f"  #{row['id']}  注销于 {age_days:.1f} 天前")
    if dry_run:
        print("\n=== 预演模式（不会改动任何数据）===\n")

    total_rows = total_files = 0
    warnings: set[str] = set()
    failures = 0

    for row in candidates:
        user_id = int(row["id"])
        report = purge_user(user_id, dry_run=dry_run)
        total_rows += report.total_rows
        total_files += report.total_files
        warnings.update(report.warnings)
        for line in report.lines():
            if "[警告]" not in line:
                print(line)

        if not dry_run:
            # 文件删失败时**不要**删 users 行：那一行是「这个账号还存在过」的
            # 最后线索，也是下次重跑的唯一入口（user_id）。留着它，脚本下次
            # 还能再来一遍；删了它，残留文件就永远找不到主人了。
            file_failed = any("文件删除失败" in w for w in report.warnings)
            if file_failed:
                failures += 1
                print(f"  -> 保留 users 行 #{user_id}，下次重跑会再尝试")
            else:
                store.hard_delete_user(user_id)
                print(f"  -> 账号 #{user_id} 已彻底清除")
        print()

    if warnings:
        print("需要注意：")
        for warning in sorted(warnings):
            print(f"  [警告] {warning}")
        print()

    print("=" * 60)
    if dry_run:
        print(
            f"预演完成：将清理 {len(candidates)} 个账号、{total_rows} 行关联数据、"
            f"{total_files} 个文件。"
        )
        print("确认无误后加 --apply 执行。")
        return 0

    print(
        f"已清理 {len(candidates) - failures} 个账号、{total_rows} 行关联数据、"
        f"{total_files} 个文件。"
    )
    if failures:
        print(f"有 {failures} 个账号的文件未删净，users 行已保留待下次重跑。")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
