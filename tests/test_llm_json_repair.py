"""LLM JSON 解析修复 —— ``LLMService._safe_json`` / ``_safe_json_array``。

## 这个测试防的是什么

2026-10-02 真机验证首次注册时，上传简历拿不到结构化画像：

```text
LLM output is not valid JSON, attempting repair...
Failed to parse LLM output as JSON: {... "roles": ["产品经理",
LLM returned empty result for resume
ResumeAgent done | name=None | skills=0 | exp=None
Careerly resume asset produced no structured profile | user=6 | keeping previous profile
```

复现两次，``finish_reason`` 都是 ``stop``（**不是**被 max_tokens 截断），
内容也是完整的 ``{...}``。真正的原因是模型把原文里的引号原样抄进了 JSON：

```json
"highlights": ["晋级第五届“互联网+"大学生创新创业大赛校级决赛"]
```

``+`` 后面那个半角双引号提前闭合了字符串，后面的 ``大学生…`` 变成非法
token，``json.loads`` 抛 ``Expecting ',' delimiter``。

而当时的"修复"只是在 ``{`` 和 ``}`` 之间重切一刀 —— 输出本来就是完整的
``{...}``，切完还是同一段文本，必然再失败，于是返回 ``{}``，整份简历降级
成只有 ``raw_text`` 的空画像。

因此这里钉死三条：

```text
① 值内部的裸引号必须被转义，且 JSON 语义不变（引号是内容，不是分隔符）
② 已经合法的 JSON（含 \\" 转义）不得被改动
③ 真坏掉的输出仍然要老老实实返回空，不能假装成功（D108 的反面）
```

运行：
    ./venv/Scripts/python.exe -m pytest tests/test_llm_json_repair.py -v
"""

from __future__ import annotations

import json

from app.services.llm_service import LLMService

# 真机事故里模型吐出的原样片段（run 2，char 485 处报
# "Expecting ',' delimiter"）。
BUGGY_RESUME_JSON = (
    "{\n"
    '  "name": "曹美玲",\n'
    '  "experience_years": 4,\n'
    '  "highlights": ["晋级第五届“互联网+"大学生创新创业大赛校级决赛", '
    '"输出 50+ 页商业计划书及完整产品方案"],\n'
    '  "skills": ["Python", "Figma"]\n'
    "}"
)


class TestStrayQuoteRepair:
    """① 裸引号：转义它，但语义必须不变。"""

    def test_buggy_resume_json_now_parses(self) -> None:
        result = LLMService._safe_json(BUGGY_RESUME_JSON)

        assert result, "修复前这里是 {}，整份简历因此降级为空画像"
        assert result["name"] == "曹美玲"
        assert result["experience_years"] == 4
        assert result["skills"] == ["Python", "Figma"]

    def test_repaired_quote_stays_part_of_the_value(self) -> None:
        """那个引号是**内容**，修复后必须还在字符串里，不能被当成分隔符丢掉。"""
        result = LLMService._safe_json(BUGGY_RESUME_JSON)

        highlight = result["highlights"][0]
        assert highlight == "晋级第五届“互联网+\"大学生创新创业大赛校级决赛", (
            "裸引号必须被转义成值的一部分，而不是把值劈成两半"
        )

    def test_escape_stray_quotes_only_touches_the_stray_one(self) -> None:
        repaired = LLMService._escape_stray_quotes(BUGGY_RESUME_JSON)

        # 结构没变：还是 4 个键、highlights 还是 2 条
        assert json.loads(repaired)["highlights"].__len__() == 2
        # 只在出问题的那一处插入反斜杠
        assert repaired.count('\\"') == 1

    def test_multiple_stray_quotes_in_one_value(self) -> None:
        text = '{"name": "遗墨"AI+VR"非遗社区产品", "n": 1}'

        result = LLMService._safe_json(text)

        assert result["name"] == '遗墨"AI+VR"非遗社区产品'
        assert result["n"] == 1

    def test_already_valid_json_is_untouched(self) -> None:
        """已经合法的 JSON（含 \\" 转义）不得被修复逻辑改动。"""
        valid = '{"a": "he said \\"hi\\"", "b": 1}'

        assert LLMService._escape_stray_quotes(valid) == valid
        assert LLMService._safe_json(valid) == {"a": 'he said "hi"', "b": 1}


class TestSafeJsonCandidates:
    """② 围栏 / 前后杂文：先试最省事的方案。"""

    def test_markdown_fence_is_stripped(self) -> None:
        text = '```json\n{"ok": true}\n```'

        assert LLMService._safe_json(text) == {"ok": True}

    def test_surrounding_prose_is_dropped(self) -> None:
        text = '好的，这是结果：\n{"ok": true}\n希望对你有帮助。'

        assert LLMService._safe_json(text) == {"ok": True}

    def test_non_dict_json_is_not_accepted_as_an_object(self) -> None:
        """顶层是数组时不能冒充对象 —— 调用方都按 dict 用。"""
        assert LLMService._safe_json('["a", "b"]') == {}


class TestSafeJsonArray:
    """数组路径共用同一套修复。"""

    def test_array_with_stray_quote_parses(self) -> None:
        text = '[{"name": "遗墨"AI+VR"产品", "score": 1}]'

        result = LLMService._safe_json_array(text)

        assert result[0]["name"] == '遗墨"AI+VR"产品'

    def test_array_wrapped_in_object_is_unwrapped(self) -> None:
        text = '{"results": [{"a": 1}]}'

        assert LLMService._safe_json_array(text) == [{"a": 1}]

    def test_array_in_markdown_fence_parses(self) -> None:
        text = '```json\n[{"a": 1}]\n```'

        assert LLMService._safe_json_array(text) == [{"a": 1}]


class TestFailuresStayLoud:
    """③ 真坏掉的输出仍然返回空，不假装成功。"""

    def test_garbage_still_returns_empty_object(self) -> None:
        assert LLMService._safe_json("这完全不是 JSON") == {}

    def test_truncated_json_still_returns_empty_object(self) -> None:
        """截断的 JSON 修不回来 —— 返回空，而不是半个画像。"""
        truncated = '{"name": "曹美玲", "skills": ["Python",'

        assert LLMService._safe_json(truncated) == {}

    def test_garbage_still_returns_empty_array(self) -> None:
        assert LLMService._safe_json_array("这完全不是 JSON") == []


class TestJsonModeFallback:
    """provider 不支持 response_format 时才降级；网络抖动不算能力缺失。"""

    def test_unrecognized_parameter_counts_as_unsupported(self) -> None:
        exc = Exception(
            "Error code: 400 - Unrecognized request argument supplied: response_format"
        )

        assert LLMService._json_mode_unsupported(exc) is True

    def test_invalid_parameter_counts_as_unsupported(self) -> None:
        exc = Exception("400 invalid parameter: response_format is not supported")

        assert LLMService._json_mode_unsupported(exc) is True

    def test_network_error_is_not_a_capability_gap(self) -> None:
        """抖动不能让我们把 json mode 永久关掉。"""
        assert LLMService._json_mode_unsupported(Exception("Connection error")) is False
        assert LLMService._json_mode_unsupported(Exception("Request timed out")) is False


class TestTokenHeadroom:
    """max_tokens 必须给 reasoning 留出余量。"""

    def test_default_max_tokens_is_above_measured_usage(self) -> None:
        """实测 reasoning_tokens ~2.9k、completion ~3.5k，4096 贴着上限。"""
        assert LLMService().max_tokens >= 8192
