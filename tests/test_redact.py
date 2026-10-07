"""日志脱敏工具测试（发布基线 K21）。

这里检验的不是「函数返回值长什么样」，而是**一条安全性质**：

    日志行里不得出现可用于识别个人的原文。

因此每个用例除断言形态外，都补一条 `assert 原文 not in 结果` —— 形态断言
只证明「现在是这个样」，泄漏断言才证明「敏感串确实没了」。两者都需要：
只测形态的话，某次「顺手优化」把掩码去掉仍可能让形态断言通过。
"""

from app.utils.redact import (
    mask_filename,
    mask_name,
    mask_phone,
    structural_preview,
)

# ── mask_phone ────────────────────────────────────────────────────────────


def test_mask_phone_keeps_head_and_tail() -> None:
    """11 位手机号 → 前 3 后 4，中间掩掉。"""
    assert mask_phone("13800138000") == "138****8000"


def test_mask_phone_strips_separators() -> None:
    """带分隔符的输入按数字归一化后再掩码。"""
    assert mask_phone("138-0013-8000") == "138****8000"
    assert mask_phone("+86 138 0013 8000") == "****"


def test_mask_phone_does_not_leak_full_number() -> None:
    assert "13800138000" not in mask_phone("13800138000")


def test_mask_phone_masks_short_input_entirely() -> None:
    """长度不足 11 位时整串掩掉 —— 截前 3 后 4 会在短串上重叠。"""
    assert mask_phone("12345") == "****"


def test_mask_phone_handles_missing() -> None:
    assert mask_phone(None) == "(none)"
    assert mask_phone("") == "(none)"


# ── mask_name ─────────────────────────────────────────────────────────────


def test_mask_name_keeps_first_char() -> None:
    assert mask_name("张三") == "张****"
    assert mask_name("Alex Chen") == "A****"


def test_mask_name_does_not_leak_rest() -> None:
    assert "三" not in mask_name("张三")
    assert "Chen" not in mask_name("Alex Chen")


def test_mask_name_blank_is_none_like_missing() -> None:
    """空串与 None 都返回 (none)，便于与「解析失败」区分。"""
    assert mask_name("   ") == "(none)"
    assert mask_name(None) == "(none)"


# ── mask_filename ─────────────────────────────────────────────────────────


def test_mask_filename_keeps_extension_only() -> None:
    assert mask_filename("张三的简历-2026.pdf") == "****.pdf"


def test_mask_filename_lowercases_extension() -> None:
    assert mask_filename("Resume.DOCX") == "****.docx"


def test_mask_filename_does_not_leak_stem() -> None:
    result = mask_filename("张三的简历-2026.pdf")
    assert "张三" not in result
    assert "2026" not in result


def test_mask_filename_without_extension_is_fully_masked() -> None:
    assert mask_filename("README") == "****"


def test_mask_filename_keeps_only_the_last_dot_segment() -> None:
    """多段文件名只取最后一段当扩展名，前面各段（真正的简历信息）全部掩掉。

    ``简历.备份.pdf`` → ``****.pdf`` —— 若正则不加 ``$`` 锚定 /
    用了贪婪匹配，``****.备份.pdf`` 就会把中间那段带出来。
    """
    assert mask_filename("简历.备份.pdf") == "****.pdf"
    assert mask_filename("张三.2026.简历.docx") == "****.docx"


def test_mask_filename_rejects_overlong_final_segment() -> None:
    """末段过长（>8 字符）时**不当作扩展名**，整串掩掉。

    这是 ``{1,8}`` 长度上限的实际作用：``张三.13800138000`` 这种把手机号
    写在末段的文件名必须整体掩掉，而不是原样留在日志里。
    """
    assert mask_filename("张三.13800138000") == "****"
    assert mask_filename("简历.199001011234") == "****"


def test_mask_filename_handles_missing() -> None:
    assert mask_filename(None) == "(none)"


# ── structural_preview ────────────────────────────────────────────────────


def test_structural_preview_masks_keys_and_values() -> None:
    """键名与值都被折成长度 —— 这是刻意取舍，见函数文档。"""
    result = structural_preview('{"name":"张三","skills":["Python","SQL"]}')
    assert result == '{"<4>":"<2>","<6>":["<6>","<3>"]}'


def test_structural_preview_does_not_leak_content() -> None:
    raw = '{"name":"张三","phone":"13800138000","skills":["Python"]}'
    result = structural_preview(raw)
    for secret in ("张三", "13800138000", "Python", "name", "phone"):
        assert secret not in result


def test_structural_preview_preserves_fence_signal() -> None:
    """markdown 围栏是「模型没守格式」的第一手信号，必须保住。"""
    assert structural_preview("```json\n{\"a\":1}").startswith("```")


def test_structural_preview_preserves_truncation_signal() -> None:
    """结尾有没有闭合括号 = 是不是被 max_tokens 截断。"""
    closed = structural_preview('{"a":1}')
    assert closed.endswith("}")
    truncated = structural_preview("x" * 500, limit=10)
    assert "truncated" in truncated
    assert "total 500 chars" in truncated


def test_structural_preview_empty() -> None:
    assert structural_preview(None) == "(empty)"
    assert structural_preview("") == "(empty)"


def test_structural_preview_is_ascii() -> None:
    """服务端日志可能落在 GBK 控制台上（CLAUDE.md §10.3），输出必须能编码。"""
    result = structural_preview('{"name":"张三"}')
    result.encode("ascii")
