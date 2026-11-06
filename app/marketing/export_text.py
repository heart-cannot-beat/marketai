"""What an export package says, and in which language (#255).

A package has two kinds of text. Stable keys -- ``entry_id``, ``ad_name``,
media paths and the daily-metrics template headers -- are identical in every
language: Meta keeps the ad name, and the CSV import reads the template back.
MarketAI's own words -- the README checklist, the ad list's column labels and
each entry's display name -- follow the language frozen into the manifest
when the export was requested. Plan prose stays in the plan's strategy
language and ad copy in its ad language; neither is translated here.
"""
import re
import unicodedata

from app.platform.tenancy import DEFAULT_UI_LANGUAGE, UI_LANGUAGES
from .reporting import attribution_label

#: The manifest of a package written before #255 carries no language; its
#: README was English.
LEGACY_LANGUAGE = DEFAULT_UI_LANGUAGE

_NAME_PART = 35
_SPACE = re.compile(r"\s+")


def ascii_text(text):
    """Printable ASCII only: accents fold to their base letter, anything else drops."""
    folded = unicodedata.normalize("NFKD", text or "").encode("ascii", "ignore").decode()
    return _SPACE.sub(" ", "".join(c if " " <= c <= "~" else " " for c in folded)).strip()


def _part(text, fallback):
    return ascii_text(text)[:_NAME_PART].rstrip() or fallback


def execution_suffix(index, version):
    return f"#{index}" + (f"v{version}" if version > 1 else "")


def ad_name(entry_id, combo, number):
    """The stable Meta ad name: MA key, the English image brief and the board's execution number.

    The image brief is English by contract (ADR 0013); a combination planned
    before it existed falls back to its ASCII identifiers. No interface or
    strategy language reaches this name.
    """
    brief = combo.get("image_brief") or {}
    proposition = _part(brief.get("proposition"), combo["angle_id"])
    expression = _part(brief.get("expression"), combo["expression_id"])
    return f"MA-{entry_id[:12]} {proposition} / {expression} {execution_suffix(number['index'], number['version'])}"


_EXECUTION = {"en": "Execution {index}", "zh-CN": "执行 {index}"}


def display_name(proposition, expression_name, number, language):
    """The readable name: plan prose as written, MarketAI's words in ``language``."""
    execution = _EXECUTION[language].format(index=number["index"])
    if number["version"] > 1:
        execution += f" v{number['version']}"
    return f"{proposition} / {expression_name} · {execution}"


_ADS_COLUMNS = {
    "en": ["entry_id", "ad_name", "display_name", "file", "headline", "body_copy", "group", "combination"],
    "zh-CN": ["条目 ID", "广告名称", "显示名称", "文件", "标题", "正文", "测试组", "创意组合"],
}

#: Read back by the CSV import (``metrics.ALIASES``), so never translated.
METRICS_TEMPLATE_COLUMNS = ["date", "ad_id", "spend", "impressions", "clicks", "purchases", "purchase_value"]

_ATTRIBUTION_ZH = {"1d_click": "点击后 1 天", "7d_click": "点击后 7 天", "1d_view": "浏览后 1 天",
    "1d_click_1d_view": "点击后 1 天 / 浏览后 1 天", "7d_click_1d_view": "点击后 7 天 / 浏览后 1 天"}

_MODE = {"zh-CN": {"byok": "真实", "sandbox": "沙箱", "mock": "示例（受控 Mock）"}}

_README = {
    "en": (
        "Upload media and copy in Meta Ads Manager. This package does not launch ads.\n"
        "Keep the MA key in each ad name, then bind the actual Ad ID and account in MarketAI.\n"
        "Export one row per ad per day, no breakdowns, in {currency}, account timezone {timezone}.\n"
        "Use {attribution_label} attribution ({attribution}). Report dates: {start} to {end}.\n"
        "Empty fields mean unknown. Explicit zero rows are required for days with zero delivery.\n"
        "Preview the CSV before confirming. Corrections explicitly replace the original batch.\n"
        "Quality approval is not evidence of business effectiveness. Mode: {mode}\n"
    ),
    "zh-CN": (
        "在 Meta 广告管理工具中上传素材和文案。本导出包不会创建或启动广告。\n"
        "保留每个广告名称里的 MA 编号，然后在 MarketAI 中绑定实际的广告 ID 和广告账户。\n"
        "按每个广告每天一行导出，不要细分，币种 {currency}，账户时区 {timezone}。\n"
        "归因口径使用{attribution_label}（{attribution}）。报告日期：{start} 至 {end}。\n"
        "空白表示未知。没有投放的日期也必须写一行明确的 0。\n"
        "确认前先预检 CSV。更正会明确替换原来的数据批次。\n"
        "广告日报模板的表头保持英文，导入时按原样识别。\n"
        "质量审核通过不代表业务效果。模式：{mode}\n"
    ),
}


def package_language(manifest):
    language = manifest.get("language", LEGACY_LANGUAGE)
    if language not in UI_LANGUAGES:
        raise ValueError("unsupported_export_language")
    return language


def ads_rows(entries, language):
    """The ad list: localized column labels over the same stable values."""
    rows = [[e["entry_id"], e["ad_name"], e.get("display_name") or e["ad_name"], e["file"], e["headline"],
             e["body_copy"], e["group_name"], e["combination_id"]] for e in entries]
    return _ADS_COLUMNS[language], rows


def readme(manifest, language):
    currency = manifest.get("currency", manifest["snapshot"]["currency"])
    attribution = manifest.get("attribution_window", manifest["snapshot"]["attribution_window"])
    mode = manifest["execution_mode"]
    return _README[language].format(
        currency=currency, timezone=manifest["account_timezone"], attribution=attribution,
        attribution_label=_ATTRIBUTION_ZH[attribution] if language == "zh-CN" else attribution_label(attribution), start=manifest["start_date"], end=manifest["end_date"],
        mode=_MODE.get(language, {}).get(mode, mode))
