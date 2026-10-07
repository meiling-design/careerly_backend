"""日志脱敏工具（TODO K21）。

背景
----
后端此前在多处把**用户个人数据**直接写进日志：完整手机号、候选人姓名、
上传文件名，以及 LLM 对简历 / JD 的**原始响应片段**。
最后一条是 ERROR 级 —— 默认 INFO 级别下照样输出，属于**已在生产生效**的泄漏，
不是"以后要注意"的理论风险。

本模块把脱敏从「每个调用点各写各的」收敛成一组**纯函数**：
调用点只表达「这是手机号 / 姓名 / 文件名 / 模型输出」，
格式细节集中在这里，改一处即全局一致。

设计原则
--------
1. **保留可诊断性**。
   脱敏不是把值统统换成 ``"***"`` 了事 —— 那会让日志失去排查价值，
   实际结果往往是有人嫌难用又把它改回原文。因此每个函数都保留
   **足以定位问题**的信息（末四位、扩展名、结构骨架），
   丢弃**足以识别到个人**的部分。

2. **不做猜测，异常输入只降级不抛错**。
   拿不准的输入一律退回最保守的掩码，绝不"格式不对就原样返回"——
   那正是泄漏最常发生的路径。

3. **输出保持 ASCII**。
   服务端日志可能落在 GBK 控制台上（参见 Careerly `CLAUDE.md` §10.3），
   骨架串里的长度标记用 ``<3>`` 而不是全角符号。

与产品承诺的对应
----------------
Careerly 隐私政策 §五 承诺「必要的日志和安全监控」、
并明确「不会在客户端代码中公开 API 密钥」。
本模块是**服务端**那条承诺的落点：日志可以留，个人数据不留。
"""

from __future__ import annotations

import re
from typing import Optional

__all__ = [
    "mask_phone",
    "mask_name",
    "mask_filename",
    "structural_preview",
]

# 掩码常量：统一 4 星，不随原值长度变化 —— 长度本身也是信息。
_MASK = "****"

# "词" 的定义：连续的数字 / 字母 / 下划线 / 汉字。
# 用它把文本切成 token，再把每个 token 折成它自己的长度。
_TOKEN_RE = re.compile(r"[0-9A-Za-z_一-鿿]+")


def mask_phone(phone: Optional[str]) -> str:
    """手机号 → 前 3 位 + 掩码 + 后 4 位。

    ``13800138000`` → ``138****8000``

    保留前 3 后 4 是刻意取舍：用户报障时通常会说"我的号是 138 开头、
    结尾 8000"，这个形态**能对上号**；但又不构成一个可直接使用的联系方式。

    长度不足 11 位（含国家码、座机、脏数据）时一律整串掩掉 ——
    截前 3 后 4 在短串上会**重叠**，等于把原文留了大半。
    """
    if not phone:
        return "(none)"
    digits = re.sub(r"\D", "", str(phone))
    if len(digits) != 11:
        return _MASK
    return f"{digits[:3]}{_MASK}{digits[-4:]}"


def mask_name(name: Optional[str]) -> str:
    """姓名 → 首字符 + 掩码。

    ``张三`` → ``张****`` ｜ ``Alex Chen`` → ``A****``

    保留首字符的理由是**可诊断性强于隐私**：排查简历解析时，
    ``name=(none)`` 与 ``name=张****`` 是两个完全不同的结论
    （没解析出姓名 vs 解析出了），而多这一个字并不足以还原全名。

    输入为空白时返回 ``(none)`` —— 便于与"解析失败"区分开。
    """
    if not name:
        return "(none)"
    stripped = str(name).strip()
    if not stripped:
        return "(none)"
    return f"{stripped[0]}{_MASK}"


def mask_filename(filename: Optional[str]) -> str:
    """上传文件名 → 只保留扩展名。

    ``张三的简历-2026.pdf`` → ``****.pdf``

    文件名几乎必然含真实姓名（这恰恰是它被写进日志的原因），
    因此整个主名掩掉；**扩展名保留** —— 它是排查"为什么解析失败"
    的第一手信息（``.doc`` 与 ``.docx`` 走的是完全不同的分支）。

    没有扩展名时返回 ``****``，不返回原名。
    """
    if not filename:
        return "(none)"
    text = str(filename)
    # 只认最后一段扩展名，且限制长度 —— 避免把 "简历.备份.2026" 这类
    # 当作扩展名把真实内容带出来。
    match = re.search(r"\.([0-9A-Za-z]{1,8})$", text)
    if not match:
        return _MASK
    return f"{_MASK}.{match.group(1).lower()}"


def structural_preview(text: Optional[str], limit: int = 300) -> str:
    """模型原始输出 → 只保留**结构**、不保留**内容**的骨架。

    ``{"name":"张三","skills":["Python","SQL"]}``
      → ``{"<4>":"<2>","<6>":["<6>","<3>"]}``

    ⚠️ **键名同样被折成长度** —— 这一点容易与直觉不符。代价是看不出
    这个键叫 ``name`` 还是 ``email``；换来的是不依赖「键一定来自 schema」
    这个假设（模型完全可以返回 ``{"张三": ...}`` 这种把用户数据放进键的
    形状）。上面列的四个判据都与键名无关，所以这个取舍不损伤排查能力。

    **为什么不是直接截断，也不是整体打码**：
    打这两条日志的目的，是排查「LLM 没有返回合法 JSON」。
    真正有用的信号**全在结构上** ——

    * 是否被 markdown 围栏 ``` 包住（开头是不是 ```）；
    * 是否被 ``max_tokens`` 截断（结尾有没有闭合的 ``}`` / ``]``）；
    * 引号是否未转义（值里出现裸 ``"``）；
    * 数组到底回来了几项。

    这些信号与内容**完全无关**，所以内容一个字都不需要留。
    把每个 token 折成 ``<长度>`` 既保住了上述全部判据，
    又让骨架比原文短得多、可读性反而更好。

    超过 ``limit`` 时追加截断说明，串本身走 ASCII。
    """
    if not text:
        return "(empty)"
    body = str(text)
    head = body[:limit]
    skeleton = _TOKEN_RE.sub(lambda m: f"<{len(m.group())}>", head)
    if len(body) > limit:
        # 用 '...' 而不是省略号：日志可能落在 GBK 控制台上。
        skeleton += f"...(truncated; total {len(body)} chars)"
    return skeleton
