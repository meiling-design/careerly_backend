"""清空历史无密码账号（发布基线 B6 / 决策 D85 遗留）。

背景
----
``AUTH_MODE=dev`` 下，**无密码账号**（``password_hash IS NULL``）可以仅凭
手机号登录 —— 那是 2026-10-04 Figma 新设计之前的产品形态留下的账号。
切到 ``AUTH_MODE=formal`` 之后它们本来就登不进来（会收到 501），但把它们
**留着**有两个问题：

1. 它们持有 ``careerly.db`` 里的真实简历数据，却是一个谁都无法再认证的
   身份 —— 一份没有归属、也不会被任何注销流程触发的数据。
2. 只要有人把 ``AUTH_MODE`` 改回 ``dev``（排查问题时的常见操作），
   这些账号立刻又能被任何人凭手机号接管。

用法
----
```bash
# 预演（默认）：只报告将删除什么，不改动任何数据
python scripts/purge_passwordless_accounts.py

# 真正执行
python scripts/purge_passwordless_accounts.py --apply
```

⚠️ 不可撤销。执行前请确认数据库已有可用备份（``scripts/backup_sqlite.sh``）。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.core.account_purge import purge_user  # noqa: E402
from app.core.auth_store import AuthStore  # noqa: E402


def _passwordless_user_ids() -> list[int]:
    """列出无密码账号的 id。

    直接查库而不是走 ``AuthStore`` 的公开读口：``get_by_phone`` 需要一个
    手机号，而这里要的是**全集**，属于运维脚本的合理越界。
    """
    from app.core import auth_store as auth_store_module

    conn = auth_store_module._get_conn(str(auth_store_module.DB_PATH))
    rows = conn.execute(
        "SELECT id, phone, name FROM users WHERE password_hash IS NULL ORDER BY id"
    ).fetchall()
    return [int(row["id"]) for row in rows]


def main() -> int:
    parser = argparse.ArgumentParser(
        description="删除所有无密码（历史 / DEV）账号及其关联数据。",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="真正执行删除。不给这个参数时只做预演（dry-run），不改动任何数据。",
    )
    args = parser.parse_args()
    dry_run = not args.apply

    store = AuthStore()  # 顺带触发 users 表迁移（补齐 status / token_version）
    user_ids = _passwordless_user_ids()

    if not user_ids:
        print("没有无密码账号 —— 无需清理。")
        return 0

    print(f"发现 {len(user_ids)} 个无密码账号：{user_ids}")
    if dry_run:
        print("=== 预演模式（不会改动任何数据）===\n")

    total_rows = total_files = 0
    # 有些警告是**全局**性质而非某个账号独有（例如「outputs/ 下有 N 个文件
    # 归属不到用户」），逐个账号打印会刷屏并掩盖真正逐账号不同的信息。
    # 这里去重后统一在末尾呈现。
    warnings: set[str] = set()
    for user_id in user_ids:
        report = purge_user(user_id, dry_run=dry_run)
        total_rows += report.total_rows
        total_files += report.total_files
        warnings.update(report.warnings)
        for line in report.lines():
            if "[警告]" not in line:
                print(line)
        if not dry_run:
            # users 行本身最后删。顺序其实是语义上的：先删 user 行，后续
            # 就没有「这个 id 是谁」可查了（虽然这里 id 早已拿在手上）。
            store.hard_delete_user(user_id)
        print()

    if warnings:
        print("需要注意：")
        for warning in sorted(warnings):
            print(f"  [警告] {warning}")
        print()

    print("=" * 60)
    if dry_run:
        print(
            f"预演完成：将删除 {len(user_ids)} 个账号、{total_rows} 行关联数据、"
            f"{total_files} 个文件。"
        )
        print("确认无误后加 --apply 执行。")
    else:
        print(
            f"已删除 {len(user_ids)} 个账号、{total_rows} 行关联数据、"
            f"{total_files} 个文件。"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
