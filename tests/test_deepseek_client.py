"""DeepSeek 客户端 JSON 解析测试（不调 API，只测容错解析）。"""
from __future__ import annotations

import pytest

from analyst.deepseek_client import DeepSeekError, parse_json_response


def test_parse_plain_json():
    assert parse_json_response('{"a": 1}') == {"a": 1}


def test_parse_with_fence():
    text = '```json\n{"a": 1}\n```'
    assert parse_json_response(text) == {"a": 1}


def test_parse_with_surrounding_text():
    text = 'Sure! Here is the result:\n{"a": [1, 2]}\nsome trailing'
    assert parse_json_response(text) == {"a": [1, 2]}


def test_parse_invalid_raises():
    with pytest.raises(DeepSeekError):
        parse_json_response("not json at all")
