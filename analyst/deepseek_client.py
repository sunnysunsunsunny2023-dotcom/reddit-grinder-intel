"""DeepSeek API 客户端（阿里云侧调用，ADR-001）。

配置全部来自环境变量（部署时写 .env，走 GitHub Secrets，代码库不落明文）：
- ANALYZER_API_BASE   默认 https://api.deepseek.com
- ANALYZER_API_KEY    必填
- ANALYZER_MODEL      默认 deepseek-chat
- ANALYZER_TIMEOUT    默认 120 秒
"""
from __future__ import annotations

import json
import logging
import os
from typing import Any, Dict, List, Optional

import requests

logger = logging.getLogger(__name__)

DEFAULT_API_BASE = "https://api.deepseek.com"
DEFAULT_MODEL = "deepseek-chat"


class DeepSeekError(RuntimeError):
    pass


def _env(key: str, default: Optional[str] = None) -> Optional[str]:
    return os.environ.get(key, default)


def is_configured() -> bool:
    """ANALYZER_API_KEY 存在即视为已配置（否则 analyzer 不可用）。"""
    return bool(_env("ANALYZER_API_KEY"))


def chat(
    messages: List[Dict[str, str]],
    temperature: float = 0.3,
    max_tokens: int = 4096,
    json_mode: bool = True,
) -> str:
    """调用 DeepSeek chat completions，返回文本内容。

    Args:
        messages: OpenAI 格式消息列表。
        temperature: 分析任务用低温度保证稳定。
        max_tokens: 输出上限。
        json_mode: 要求 JSON 输出（response_format json_object）。

    Returns:
        模型返回文本。
    """
    api_key = _env("ANALYZER_API_KEY")
    if not api_key:
        raise DeepSeekError("ANALYZER_API_KEY is not set")
    api_base = _env("ANALYZER_API_BASE", DEFAULT_API_BASE)
    model = _env("ANALYZER_MODEL", DEFAULT_MODEL)
    timeout = float(_env("ANALYZER_TIMEOUT", "120"))

    url = api_base.rstrip("/") + "/chat/completions"
    payload: Dict[str, Any] = {
        "model": model,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
    }
    if json_mode:
        payload["response_format"] = {"type": "json_object"}

    try:
        resp = requests.post(
            url,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json=payload,
            timeout=timeout,
        )
    except requests.RequestException as exc:
        raise DeepSeekError(f"DeepSeek request failed: {exc}") from exc

    if resp.status_code != 200:
        raise DeepSeekError(
            f"DeepSeek returned {resp.status_code}: {resp.text[:500]}"
        )

    try:
        data = resp.json()
        return data["choices"][0]["message"]["content"]
    except (ValueError, KeyError, IndexError) as exc:
        raise DeepSeekError(f"Invalid DeepSeek response: {resp.text[:500]}") from exc


def parse_json_response(text: str) -> Dict[str, Any]:
    """容错解析模型 JSON 输出（可能带 ```json 围栏或前后杂文本）。"""
    t = text.strip()
    if t.startswith("```"):
        # 去掉围栏
        lines = t.splitlines()
        lines = lines[1:] if lines[0].startswith("```") else lines
        if lines and lines[-1].strip().startswith("```"):
            lines = lines[:-1]
        t = "\n".join(lines).strip()
    try:
        return json.loads(t)
    except json.JSONDecodeError:
        # 尝试截取第一个 { 到最后一个 }
        start = t.find("{")
        end = t.rfind("}")
        if start != -1 and end != -1 and end > start:
            try:
                return json.loads(t[start : end + 1])
            except json.JSONDecodeError:
                pass
        raise DeepSeekError(f"Model output is not valid JSON: {t[:300]}")
