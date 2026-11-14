"""SQLite-backed user store for JWT authentication.

Identity model
--------------
Careerly's account identity is the **phone number**; the credential is an
**optional password**.

```text
2026-10-04 之前   身份 = 手机号，无密码（产品裁决 D85）—— 手机号即凭据
2026-10-04 起     身份 = 手机号，密码作为可选凭据（新 Figma 设计带来）
```

``password_hash`` 因此有两种合法取值：

```text
非 NULL   走密码校验的账号（新注册流程创建）
NULL      遗留 / DEV 账号 —— 仍按 D85 的「手机号直登」处理，
          且仍受 ``app.core.auth_mode.is_dev_auth()`` 门禁
```

本层只负责**存取**哈希字符串，不做任何哈希 / 校验运算
（那是 ``app.core.passwords`` 的职责）。

⚠️ ``_row_to_user()`` **永远不得**返回 ``password_hash`` ——
它产出的 dict 会直接进入响应体与 ``get_current_user`` 依赖。
需要哈希的调用方走 :meth:`AuthStore.get_password_hash`。

Storage
-------
Mirrors ``app.core.state_store`` (thread-local connections + WAL) but uses a
**dedicated database file** (``app/data/auth.db``) so authentication data
never mixes with graph session state in ``graph_state.db``.

All queries are parameterised (``?`` placeholders) — no string building —
to prevent SQL injection.
"""

from __future__ import annotations

import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

from app.utils.logger import logger
from app.utils.paths import APP_DATA_DIR as DATA_DIR  # 可被 DATA_DIR 重定向
from app.utils.redact import mask_phone

DB_PATH = DATA_DIR / "auth.db"

# Thread-local connections for async safety (separate slot from StateStore)
_local = threading.local()


def _get_conn(db_path: str = str(DB_PATH)) -> sqlite3.Connection:
    """Get a thread-local SQLite connection for auth.db."""
    if not hasattr(_local, "auth_conn") or _local.auth_conn is None:
        _local.auth_conn = sqlite3.connect(db_path, check_same_thread=False)
        _local.auth_conn.row_factory = sqlite3.Row
        _local.auth_conn.execute("PRAGMA journal_mode=WAL")
        _local.auth_conn.execute("PRAGMA busy_timeout=5000")
    return _local.auth_conn


class AuthStore:
    """Persistent user store backed by SQLite (``auth.db``)."""

    def __init__(self, db_path: str | Path | None = None):
        self.db_path = str(db_path or DB_PATH)
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    # ── Schema ──────────────────────────────────────────────────────────

    def _init_schema(self) -> None:
        conn = _get_conn(self.db_path)
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS users (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                phone         TEXT UNIQUE NOT NULL,
                name          TEXT NOT NULL DEFAULT '',
                email         TEXT UNIQUE,
                password_hash TEXT,
                is_active     INTEGER NOT NULL DEFAULT 1,
                status        TEXT NOT NULL DEFAULT 'active',
                token_version INTEGER NOT NULL DEFAULT 0,
                deleted_at    REAL,
                created_at    REAL NOT NULL,
                updated_at    REAL NOT NULL
            );
        """)
        self._migrate(conn)
        conn.commit()

    def _migrate(self, conn: sqlite3.Connection) -> None:
        """Bring an older ``users`` table up to the current column set.

        ``CREATE TABLE IF NOT EXISTS`` is a no-op on an existing database, so
        columns added after the first release have to be filled in explicitly.
        """
        cols = {row["name"] for row in conn.execute("PRAGMA table_info(users)")}
        if "phone" not in cols:
            conn.execute("ALTER TABLE users ADD COLUMN phone TEXT")
        if "name" not in cols:
            conn.execute("ALTER TABLE users ADD COLUMN name TEXT NOT NULL DEFAULT ''")

        # ── 发布基线新增（决策 D119）─────────────────────────────────────
        # status / deleted_at  软删除：status='deleted' 后 30 天再物理清理
        # token_version        吊销位：注销即 +1，让所有已签发 JWT 立刻失效
        # DEFAULT 让存量行自动落到「正常、未吊销」，无需回填。
        if "status" not in cols:
            conn.execute(
                "ALTER TABLE users ADD COLUMN status TEXT NOT NULL DEFAULT 'active'"
            )
        if "token_version" not in cols:
            conn.execute(
                "ALTER TABLE users ADD COLUMN token_version INTEGER NOT NULL DEFAULT 0"
            )
        if "deleted_at" not in cols:
            conn.execute("ALTER TABLE users ADD COLUMN deleted_at REAL")
        # Cannot add UNIQUE via ALTER TABLE; a unique index is equivalent.
        conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_users_phone ON users(phone)"
        )

        # ── Legacy NOT NULL constraints (pre-D85) ───────────────────────
        # Databases created before the phone-identity switch declared
        # ``email TEXT NOT NULL`` and ``password_hash TEXT NOT NULL``.
        # ``CREATE TABLE IF NOT EXISTS`` is a no-op on those files and SQLite
        # cannot relax a NOT NULL constraint in place, so registering a
        # phone-only user failed with "NOT NULL constraint failed: users.email".
        # The only way out is to rebuild the table with the current schema.
        info = {row["name"]: row for row in conn.execute("PRAGMA table_info(users)")}
        legacy_email = info.get("email")
        legacy_pwd = info.get("password_hash")
        if (legacy_email is not None and legacy_email["notnull"]) or (
            legacy_pwd is not None and legacy_pwd["notnull"]
        ):
            self._rebuild_users(conn)

    @staticmethod
    def _rebuild_users(conn: sqlite3.Connection) -> None:
        """Recreate ``users`` with the current schema, preserving every row.

        Legacy rows created under the email+password identity have no phone
        number. Under D85 (``login(phone)``) they are unreachable anyway, but
        they are **kept** rather than dropped: each gets a unique placeholder
        phone so the UNIQUE NOT NULL constraint holds.
        """
        conn.executescript(
            """
            CREATE TABLE users_new (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                phone         TEXT UNIQUE NOT NULL,
                name          TEXT NOT NULL DEFAULT '',
                email         TEXT UNIQUE,
                password_hash TEXT,
                is_active     INTEGER NOT NULL DEFAULT 1,
                status        TEXT NOT NULL DEFAULT 'active',
                token_version INTEGER NOT NULL DEFAULT 0,
                deleted_at    REAL,
                created_at    REAL NOT NULL,
                updated_at    REAL NOT NULL
            );
            INSERT INTO users_new
                (id, phone, name, email, password_hash, is_active,
                 status, token_version, deleted_at, created_at, updated_at)
            SELECT id,
                   COALESCE(NULLIF(TRIM(COALESCE(phone, '')), ''), 'legacy_' || id),
                   COALESCE(name, ''),
                   email,
                   password_hash,
                   COALESCE(is_active, 1),
                   'active',
                   0,
                   NULL,
                   created_at,
                   updated_at
            FROM users;
            DROP TABLE users;
            ALTER TABLE users_new RENAME TO users;
            """
        )
        conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_users_phone ON users(phone)")

    # ── Create ───────────────────────────────────────────────────────────

    def create_user(
        self,
        phone: str,
        name: str = "",
        email: str | None = None,
        password_hash: str | None = None,
    ) -> int:
        """Insert a new user; return the new auto-increment id.

        [password_hash] 为空表示这是一个**无密码账号**（遗留 / DEV 形态）——
        见模块文档的身份模型说明。哈希由调用方（``app.core.passwords``）算好，
        本层不接触明文。
        """
        now = time.time()
        conn = _get_conn(self.db_path)
        cur = conn.execute(
            "INSERT INTO users "
            "(phone, name, email, password_hash, is_active, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, 1, ?, ?)",
            (phone, name, email, password_hash, now, now),
        )
        conn.commit()
        user_id = int(cur.lastrowid)
        # ⚠️ 手机号必须脱敏（TODO K21）：此前这里是完整明文 INFO 日志。
        logger.info(
            "AuthStore: user created | id=%d | phone=%s | has_password=%s",
            user_id,
            mask_phone(phone),
            password_hash is not None,
        )
        return user_id

    # ── Reads ────────────────────────────────────────────────────────────

    @staticmethod
    def _row_to_user(row: sqlite3.Row) -> dict[str, Any]:
        """Convert a ``users`` row into a dict (never includes ``password_hash``).

        ⚠️ 产物**不是**响应体 —— 对外的一律经 ``router_auth._public_user()``
        白名单过滤（只放 id / phone / name / email）。因此这里可以带上
        认证内部字段：``get_current_user`` 每请求都要读 ``token_version``
        与 ``status``，若再开一个读口就是每请求多一次查询。
        """
        return {
            "id": row["id"],
            "phone": row["phone"],
            "name": row["name"] or "",
            "email": row["email"],
            "is_active": bool(row["is_active"]),
            "status": (row["status"] if "status" in row.keys() else "active") or "active",
            "token_version": (
                row["token_version"] if "token_version" in row.keys() else 0
            ) or 0,
            "deleted_at": row["deleted_at"] if "deleted_at" in row.keys() else None,
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    def get_by_id(self, user_id: int) -> dict[str, Any] | None:
        conn = _get_conn(self.db_path)
        row = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
        return self._row_to_user(row) if row else None

    def get_by_phone(self, phone: str) -> dict[str, Any] | None:
        conn = _get_conn(self.db_path)
        row = conn.execute("SELECT * FROM users WHERE phone = ?", (phone,)).fetchone()
        return self._row_to_user(row) if row else None

    def get_by_email(self, email: str) -> dict[str, Any] | None:
        conn = _get_conn(self.db_path)
        row = conn.execute("SELECT * FROM users WHERE email = ?", (email,)).fetchone()
        return self._row_to_user(row) if row else None

    def get_password_hash(self, user_id: int) -> str | None:
        """Return the stored bcrypt hash, or ``None`` for a passwordless account.

        ⚠️ 单独开一个读口，而不是让它混进 :meth:`_row_to_user` ——
        后者的产物会进响应体（见模块文档的约束说明）。
        """
        conn = _get_conn(self.db_path)
        row = conn.execute(
            "SELECT password_hash FROM users WHERE id = ?", (user_id,)
        ).fetchone()
        if row is None:
            return None
        return row["password_hash"]

    def set_password_hash(self, user_id: int, password_hash: str) -> int:
        """Replace the account's password hash **and revoke existing tokens**.

        密码重置必须与吊销在同一条 SQL 里完成：只换哈希的话，重置前签发的
        access / refresh token 仍然有效，等于「旧设备还能继续用旧会话」——
        用户以为重置已经把别人踢下线，实际没有。
        返回新的 ``token_version``，语义同 :meth:`bump_token_version`。
        """
        conn = _get_conn(self.db_path)
        conn.execute(
            "UPDATE users "
            "SET password_hash = ?, token_version = token_version + 1, updated_at = ? "
            "WHERE id = ?",
            (password_hash, time.time(), user_id),
        )
        conn.commit()
        row = conn.execute(
            "SELECT token_version FROM users WHERE id = ?", (user_id,)
        ).fetchone()
        return int(row["token_version"]) if row else 0

    # ── Uniqueness checks ───────────────────────────────────────────────

    def phone_exists(self, phone: str) -> bool:
        conn = _get_conn(self.db_path)
        row = conn.execute("SELECT 1 FROM users WHERE phone = ?", (phone,)).fetchone()
        return row is not None

    def email_exists(self, email: str) -> bool:
        conn = _get_conn(self.db_path)
        row = conn.execute("SELECT 1 FROM users WHERE email = ?", (email,)).fetchone()
        return row is not None

    # ── Deletion / revocation（决策 D119）────────────────────────────────
    #
    # 为什么是「软删除 + 延迟物理清理」而不是直接 DELETE：
    # 用户数据散在 4 个 SQLite 库 + 库外明文文件（见 scripts/purge_deleted_accounts.py），
    # 物理删除必须跨库级联，中途失败会留下**比不删更难处理**的半删状态，
    # 而且用户已经看到「删除成功」了。
    #
    # 所以拆成两件事：
    #   ① 立即：标记 status='deleted' + 吊销全部凭据 —— 用户视角「马上生效」；
    #   ② 30 天后：离线任务做物理级联清理 —— 那时失败可以重跑，代价低得多。
    # 30 天的窗口同时给了「误删可恢复」的余地。

    def bump_token_version(self, user_id: int) -> int:
        """Increase ``token_version`` and return the new value.

        这是吊销的全部实现：``get_current_user`` 与 ``/auth/refresh`` 都拿
        token 里的快照与数据库当前值比对，版本一变，**所有**已签发的
        access / refresh token 在下一次请求即失效。
        """
        conn = _get_conn(self.db_path)
        conn.execute(
            "UPDATE users SET token_version = token_version + 1, updated_at = ? WHERE id = ?",
            (time.time(), user_id),
        )
        conn.commit()
        row = conn.execute(
            "SELECT token_version FROM users WHERE id = ?", (user_id,)
        ).fetchone()
        return int(row["token_version"]) if row else 0

    def soft_delete_user(self, user_id: int) -> bool:
        """Mark the account deleted **and** revoke its tokens, in one transaction.

        两步必须同一次提交：只标记不吊销，用户在 90 天 refresh 有效期内仍能
        继续访问；只吊销不标记，清理任务找不到这个账号。返回是否命中了行。
        """
        conn = _get_conn(self.db_path)
        cur = conn.execute(
            "UPDATE users "
            "SET status = 'deleted', deleted_at = ?, "
            "    token_version = token_version + 1, updated_at = ? "
            "WHERE id = ? AND status != 'deleted'",
            (time.time(), time.time(), user_id),
        )
        conn.commit()
        return cur.rowcount > 0

    def list_users_deleted_before(self, cutoff_ts: float) -> list[dict[str, Any]]:
        """Accounts soft-deleted at or before *cutoff_ts* —— 物理清理的输入。"""
        conn = _get_conn(self.db_path)
        rows = conn.execute(
            "SELECT * FROM users "
            "WHERE status = 'deleted' AND deleted_at IS NOT NULL AND deleted_at <= ? "
            "ORDER BY deleted_at",
            (cutoff_ts,),
        ).fetchall()
        return [dict(row) for row in rows]

    def hard_delete_user(self, user_id: int) -> bool:
        """Physically remove the account row. Only the purge script should call this."""
        conn = _get_conn(self.db_path)
        cur = conn.execute("DELETE FROM users WHERE id = ?", (user_id,))
        conn.commit()
        return cur.rowcount > 0
