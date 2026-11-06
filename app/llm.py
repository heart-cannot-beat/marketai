"""角度引擎用的 LLM 客户端：Gemini 或 OpenAI，按可用 key 自动选择。

Legacy, ungoverned path -- do not use for new code. It calls providers with
the platform's environment keys and has no job, idempotency, cost
reservation or evidence, which CLAUDE.md's invariants and ADR 0005 forbid
for paid calls (and production has no platform LLM key). New LLM calls go
through the governed ``llm_chat`` semantic in ``app.providers.llm_chat``
(Workspace BYOK key, reservation and usage settlement, ProviderCallAttempt
evidence, controlled mock). Kept only for the legacy angle engine.
"""
from __future__ import annotations

import json
import re

import httpx

from . import config


class LLMUnavailable(Exception):
    pass


async def chat(system: str, user: str, temperature: float = 0.8) -> str:
    """调用可用的 LLM，返回纯文本。没有 key 时抛 LLMUnavailable。"""
    resolved = config.resolve_llm()
    if not resolved:
        raise LLMUnavailable("未配置 GEMINI_API_KEY / OPENAI_API_KEY")
    provider, model = resolved
    async with httpx.AsyncClient(timeout=90) as client:
        if provider == "gemini":
            url = f"{config.GEMINI_BASE_URL}/models/{model}:generateContent"
            payload = {
                "system_instruction": {"parts": [{"text": system}]},
                "contents": [{"role": "user", "parts": [{"text": user}]}],
                "generationConfig": {"temperature": temperature},
            }
            r = await client.post(url, json=payload, headers={"x-goog-api-key": config.GEMINI_API_KEY})
            r.raise_for_status()
            data = r.json()
            parts = data["candidates"][0]["content"]["parts"]
            return "".join(p.get("text", "") for p in parts)
        else:  # openai
            url = f"{config.OPENAI_BASE_URL}/chat/completions"
            payload = {
                "model": model,
                "temperature": temperature,
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
            }
            r = await client.post(url, json=payload, headers={"Authorization": f"Bearer {config.OPENAI_API_KEY}"})
            r.raise_for_status()
            return r.json()["choices"][0]["message"]["content"]


def extract_json(text: str) -> dict:
    """从 LLM 输出里稳健地抠出 JSON（容忍 ```json 围栏和前后废话）。"""
    fence = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.S)
    if fence:
        return json.loads(fence.group(1))
    start = text.find("{")
    end = text.rfind("}")
    if start >= 0 and end > start:
        return json.loads(text[start : end + 1])
    raise ValueError(f"LLM 输出中未找到 JSON: {text[:200]}")
