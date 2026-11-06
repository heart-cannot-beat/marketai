"""Provider 注册表。"""
from __future__ import annotations

from .. import config
from .base import ImageProvider
from .gemini import GeminiProvider
from .mock import MockProvider
from .openai_img import OpenAIImageProvider
from .seedream import SeedreamProvider

_PROVIDERS: dict[str, ImageProvider] = {
    p.name: p for p in [GeminiProvider(), OpenAIImageProvider(), SeedreamProvider(), MockProvider()]
}


def get_provider(name: str) -> ImageProvider:
    if name not in _PROVIDERS:
        raise KeyError(f"未知 provider: {name}")
    return _PROVIDERS[name]


def providers_info() -> list[dict]:
    out = []
    for p in _PROVIDERS.values():
        out.append({
            "name": p.name,
            "label": p.label,
            "available": p.available(),
            "supports_ref": p.supports_ref,
            "models": p.models,
            "default_model": p.default_model,
            "est_cost_per_image": config.COST_PER_IMAGE.get(p.name, 0.05),
        })
    return out
