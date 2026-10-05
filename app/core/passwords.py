"""密码哈希（bcrypt）。

────────────────────────────────────────────────────────────────────────────
背景
────────────────────────────────────────────────────────────────────────────
在 2026-10-04 之前，Careerly 的账号身份**只有手机号**（产品裁决 D85：
``register(name, phone, email?)`` / ``login(phone)``，无密码、无验证码）。
新版 Figma 设计把「密码」放进了注册页与登录页，产品侧据此裁决
**密码端到端接通**，规则取最保守的一档：

    ✅ 非空且长度 >= MIN_PASSWORD_LENGTH(6)
    ❌ 不做强度校验（大小写 / 数字 / 符号的组合一律不要求）

本模块是**唯一**做密码哈希的地方 —— 路由层不直接碰 bcrypt，
存储层只负责把哈希字符串原样落库。

────────────────────────────────────────────────────────────────────────────
为什么直接用 ``bcrypt`` 而不是 ``passlib.CryptContext``
────────────────────────────────────────────────────────────────────────────
``passlib`` 1.7.4 的 bcrypt 后端会去读 ``bcrypt.__about__.__version__``，
而该属性在 bcrypt 4.1 起被移除 —— 届时 passlib 会打印
``(trapped) error reading bcrypt version`` 之类的噪声日志。
``requirements.txt`` 写的是 ``bcrypt>=4.0``（不是 ``~=4.0``），
下一次安装就可能拉到 4.1+。直接用 ``bcrypt`` 少一层、也没有这个隐患。

────────────────────────────────────────────────────────────────────────────
bcrypt 的 72 字节上限
────────────────────────────────────────────────────────────────────────────
bcrypt 只取前 72 字节。若不显式截断，不同 bcrypt 版本的行为不完全一致
（静默截断 / 抛 ``ValueError``）。这里**统一先截断再哈希**，
两端（注册与登录）走同一函数，因此判定始终一致。

注意：按**字节**截断可能切断一个多字节 UTF-8 字符 —— 这对 bcrypt
没有影响（它只认字节），且不会让两个不同的长密码碰撞成同一个哈希。
"""

from __future__ import annotations

import bcrypt

#: 密码最短长度。产品侧 2026-10-04 裁决定为「最保守」档，仅此一条规则。
MIN_PASSWORD_LENGTH = 6

#: bcrypt 的算法上限：只处理前 72 字节。
BCRYPT_MAX_BYTES = 72


def _encode(password: str) -> bytes:
    """把明文编码成 bcrypt 可消费的字节串（按 72 字节截断）。"""
    return password.encode("utf-8")[:BCRYPT_MAX_BYTES]


def hash_password(password: str) -> str:
    """把明文密码哈希成可入库的字符串（bcrypt，自带随机 salt）。"""
    return bcrypt.hashpw(_encode(password), bcrypt.gensalt()).decode("ascii")


def verify_password(password: str, password_hash: str) -> bool:
    """校验明文密码是否匹配已存储的哈希。

    哈希串损坏 / 不是 bcrypt 格式时返回 ``False`` 而不是抛异常 ——
    调用方（登录路由）只需要「匹不匹配」这一个答案，
    把库层的解析错误冒泡成 500 会让一条脏数据变成整条登录链路的故障。
    """
    try:
        return bcrypt.checkpw(_encode(password), password_hash.encode("ascii"))
    except (ValueError, TypeError):
        return False


__all__ = [
    "MIN_PASSWORD_LENGTH",
    "BCRYPT_MAX_BYTES",
    "hash_password",
    "verify_password",
]
