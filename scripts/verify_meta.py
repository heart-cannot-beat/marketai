"""Meta 凭证连通性自检：只发 GET，不创建/修改任何广告对象。

用法：
    source .venv/bin/activate && python scripts/verify_meta.py
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import config  # noqa: E402
from app.adapters.meta.client import MetaApiError, MetaClient  # noqa: E402

CHECKS = [
    ("广告账户", lambda a: {
        "path": a,
        "params": {"fields": "name,account_status,currency,timezone_name,amount_spent,disable_reason"},
    }),
    ("Token 权限", lambda a: {"path": "me/permissions", "params": {}}),
    ("战役列表", lambda a: {
        "path": f"{a}/campaigns",
        "params": {"fields": "id,name,status,effective_status", "limit": 3},
    }),
    ("近 7 日洞察", lambda a: {
        "path": f"{a}/insights",
        "params": {"fields": "spend,impressions,inline_link_click_ctr", "date_preset": "last_7d"},
    }),
]


async def main() -> int:
    missing = [
        name for name, value in (
            ("META_API_VERSION", config.META_API_VERSION),
            ("META_ACCESS_TOKEN", config.META_ACCESS_TOKEN),
            ("META_APP_SECRET", config.META_APP_SECRET),
            ("META_AD_ACCOUNT_ID", config.META_AD_ACCOUNT_ID),
        ) if not value
    ]
    if missing:
        print("缺少配置：" + ", ".join(missing))
        return 1

    print(f"版本 {config.META_API_VERSION} | 账户 {config.META_AD_ACCOUNT_ID} | 时区 {config.META_TIMEZONE}")
    print(f"token {len(config.META_ACCESS_TOKEN)} 字符 | app secret {len(config.META_APP_SECRET)} 字符")
    print("-" * 60)

    client = MetaClient()
    failed = 0
    for label, build in CHECKS:
        try:
            page = await client.fetch_page(build(config.META_AD_ACCOUNT_ID))
        except MetaApiError as exc:
            failed += 1
            print(f"✗ {label}: [{exc.code}] {exc} (可重试={exc.retryable})")
            continue
        body = page.body
        if label == "广告账户":
            print(f"✓ {label}: {body.get('name')} | 状态 {body.get('account_status')} | "
                  f"{body.get('currency')} | {body.get('timezone_name')} | 累计花费 {body.get('amount_spent')}")
            if str(body.get("timezone_name")) != config.META_TIMEZONE:
                print(f"  ⚠ .env 的 META_TIMEZONE={config.META_TIMEZONE} 与账户 {body.get('timezone_name')} 不一致")
        elif label == "Token 权限":
            granted = sorted(d["permission"] for d in page.data if d.get("status") == "granted")
            print(f"✓ {label}: {', '.join(granted) or '（系统用户 Token 通常返回空，属正常）'}")
            if "ads_management" in granted:
                print("  ⚠ 这个只读 Token 带了 ads_management 写权限，建议重发一个只含 ads_read 的")
        else:
            print(f"✓ {label}: {len(page.data)} 条" + ("（新账户为空是正常的）" if not page.data else ""))
        for key, value in page.usage_headers.items():
            if key.lower() == "x-business-use-case-usage":
                print(f"  用量：{value[:160]}")
    print("-" * 60)
    print("全部通过 ✅" if not failed else f"{failed} 项失败 ❌")
    return 0 if not failed else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
