"""Parse and validate ad-level result rows (CSV, pasted table or JSON rows).

The downloadable template (``results_template``) is headed in the reader's
interface language (#255); both header sets, plus the common Ads Manager
export headers, are accepted so a seller can upload any of them as-is:

==============  =========================================  ==========
canonical        accepted headers (case / spaces ignored)   required
==============  =========================================  ==========
asset_code       素材编号, asset code                       one of the
ad_name          广告名称, ad name                          three
ad_id            广告编号, 广告 ID, ad id                    identifiers
date_start       开始日期, 报告开始日期, reporting starts     yes (or
date_end         结束日期, 报告结束日期, reporting ends       "日期")
impressions      展示次数, impressions                      yes
clicks           链接点击量, link clicks, clicks             yes
spend            花费, 已花费金额, amount spent              no
conversions      购买次数, purchases, conversions           no
revenue          购买金额, purchases conversion value        no
currency         币种, currency                             no
==============  =========================================  ==========

``clicks`` means **link clicks** (Meta reference §3: the default ``clicks``
counts likes and "see more" too); a header that says "(all)" / "全部" is
ignored on purpose. A currency in a header's parentheses ("Amount spent
(USD)") becomes the file's currency. Template-only columns (方向, 画面组合)
are ignored.
"""
from __future__ import annotations

import csv
import io
import re
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal, InvalidOperation

from . import codes

MAX_ROWS = 5000
MAX_TEXT_CHARS = 1_000_000

FIELDS = (
    "asset_code", "ad_name", "ad_id", "date_start", "date_end", "impressions", "clicks",
    "spend", "conversions", "revenue", "currency",
)

#: The header row of the downloadable template, in order, per interface
#: language. Every label must parse back to its key (``tests/test_learnings_api.py``).
TEMPLATE_HEADERS = {
    "zh-CN": (
        ("asset_code", "素材编号"),
        ("direction", "方向"),
        ("combination", "画面组合"),
        ("ad_name", "广告名称"),
        ("ad_id", "广告编号"),
        ("date_start", "开始日期"),
        ("date_end", "结束日期"),
        ("impressions", "展示次数"),
        ("clicks", "链接点击量"),
        ("spend", "花费"),
        ("conversions", "购买次数"),
        ("revenue", "购买金额"),
        ("currency", "币种"),
    ),
    "en": (
        ("asset_code", "Asset code"),
        ("direction", "Direction"),
        ("combination", "Combination"),
        ("ad_name", "Ad name"),
        ("ad_id", "Ad ID"),
        ("date_start", "Start date"),
        ("date_end", "End date"),
        ("impressions", "Impressions"),
        ("clicks", "Link clicks"),
        ("spend", "Amount spent"),
        ("conversions", "Purchases"),
        ("revenue", "Purchase value"),
        ("currency", "Currency"),
    ),
}

#: The template's file name and the label of an asset no test plan describes.
TEMPLATE_FILE_NAME = {"zh-CN": "{name}-投放结果模板.csv", "en": "{name}-results-template.csv"}
TEMPLATE_PRODUCT_FALLBACK = {"zh-CN": "产品", "en": "product"}

_ALIASES: dict[str, tuple[str, ...]] = {
    "asset_code": ("素材编号", "素材码", "assetcode", "code"),
    "ad_name": ("广告名称", "广告名", "adname"),
    "ad_id": ("广告编号", "广告id", "adid"),
    "date_start": ("开始日期", "报告开始日期", "开始", "reportingstarts", "datestart", "startdate", "start"),
    "date_end": ("结束日期", "报告结束日期", "结束", "reportingends", "dateend", "enddate", "end"),
    "day": ("日期", "date", "day"),
    "impressions": ("展示次数", "展示量", "展示", "impressions"),
    "clicks": (
        "链接点击量", "链接点击次数", "链接点击", "点击量", "点击次数", "点击",
        "linkclicks", "inlinelinkclicks", "clicks",
    ),
    "spend": ("花费", "花费金额", "已花费金额", "消耗", "amountspent", "spend", "cost"),
    "conversions": ("购买次数", "购物次数", "购买", "转化次数", "转化", "purchases", "conversions"),
    "revenue": (
        "购买金额", "购买转化价值", "购物转化价值", "销售额", "purchasesconversionvalue",
        "purchasevalue", "conversionvalue", "revenue",
    ),
    "currency": ("币种", "货币", "currency"),
}
_HEADER_TO_FIELD = {alias: name for name, aliases in _ALIASES.items() for alias in aliases}
#: Template-only, informational columns.
_IGNORED = {"方向", "画面组合", "direction", "combination"}

_CURRENCY_IN_HEADER = re.compile(r"[(（]\s*([A-Za-z]{3})\s*[)）]")
_DATE = re.compile(r"^\s*(\d{4})\s*[-/.年]\s*(\d{1,2})\s*[-/.月]\s*(\d{1,2})\s*日?\s*$")
_BLANK = {"", "-", "—", "--", "n/a", "na", "null"}

#: Row refusal codes -> the Chinese sentence stored with the row. The browser
#: words a row from its ``reason_code`` (+ ``reason_params``) through the
#: ``error-<code>`` table (#253, #255); this text stays for logs and older
#: clients. ``web/__tests__/locales/error-codes.spec.ts`` reads these keys.
REASONS: dict[str, str] = {
    "missing_identifier": "缺少素材编号、广告名称或广告编号",
    "missing_date": "缺少日期",
    "invalid_date": "日期格式不对（应为 2026-09-01）",
    "date_order": "结束日期早于开始日期",
    "future_date": "结束日期还没到",
    "missing_impressions": "缺少展示次数",
    "missing_clicks": "缺少链接点击量",
    "invalid_number": "数字格式不对",
    "negative_number": "数字不能为负",
    "not_integer": "展示次数和点击量必须是整数",
    "clicks_exceed_impressions": "点击量大于展示次数",
    "currency_invalid": "币种应为三个字母，例如 USD",
    "currency_missing": "有花费但没有币种，请在表里填写或在导入时选择币种",
    "currency_mismatch": "币种和这个产品已导入的数据不一致",
    "unknown_code": "找不到这个素材编号",
    "ambiguous_code": "这个素材编号对应多个素材，请改用广告编号",
    "unknown_ad": "找不到这个广告编号对应的素材",
    "identifier_conflict": "广告编号和素材编号指向不同的素材",
    "other_product": "这张素材属于另一个产品",
    "duplicate_in_file": "和文件里的另一行重复",
    "overlaps_in_file": "和文件里的另一行日期重叠",
    "overlaps_existing": "和已导入的数据日期重叠（同一素材不能重复计数）",
    "meta_source_unknown": "无法确认这条 Meta 数据的来源（未绑定 Meta 连接）",
}

#: The refusal codes that carry values with the row (``reason_params``); every
#: other code carries none. A rejected-rows summary words each code once for
#: many rows, so the copy may use fewer of them
#: (``web/__tests__/locales/text-codes.spec.ts``).
REASON_PARAMS: dict[str, tuple[str, ...]] = {
    "currency_mismatch": ("expected", "found"),
}


class ParseError(ValueError):
    def __init__(self, code: str, message: str, *, params: dict | None = None):
        self.code = code
        self.message = message
        self.params = params or {}
        super().__init__(f"{code}: {message}")


def _header_key(header: str) -> str:
    text = _CURRENCY_IN_HEADER.sub("", header or "")
    text = re.sub(r"[\s_\-:：·]", "", text).lower()
    return text


@dataclass
class ParsedTable:
    rows: list[dict] = field(default_factory=list)
    columns: dict[str, str] = field(default_factory=dict)  # field -> original header
    ignored_columns: list[str] = field(default_factory=list)
    header_currency: str | None = None


def _delimiter(first_line: str) -> str:
    counts = {delimiter: first_line.count(delimiter) for delimiter in ("\t", ",", ";")}
    return max(counts, key=lambda item: (counts[item], item == ","))


def parse_table(text: str) -> ParsedTable:
    """CSV / TSV text (a file or a block pasted from a spreadsheet)."""
    if len(text) > MAX_TEXT_CHARS:
        raise ParseError(
            "learnings_text_too_long", f"内容超过 {MAX_TEXT_CHARS:,} 个字符，请拆分后导入",
            params={"limit": MAX_TEXT_CHARS},
        )
    text = text.lstrip("\ufeff")
    lines = [line for line in text.splitlines() if line.strip()]
    if len(lines) < 2:
        raise ParseError("no_rows", "至少需要表头和一行数据")
    reader = csv.reader(io.StringIO("\n".join(lines), newline=""), delimiter=_delimiter(lines[0]))
    header = next(reader)
    table = ParsedTable()
    positions: dict[str, int] = {}
    for index, raw in enumerate(header):
        name = (raw or "").strip()
        if not name:
            continue
        lowered = name.lower()
        if "(all)" in lowered.replace(" ", "") or "（全部）" in name or "全部" in name:
            table.ignored_columns.append(name)
            continue
        key = _header_key(name)
        target = _HEADER_TO_FIELD.get(key)
        if target is None or name in _IGNORED or key in _IGNORED:
            table.ignored_columns.append(name)
            continue
        if target in positions:
            # The first matching column wins ("链接点击量" before a later "点击").
            table.ignored_columns.append(name)
            continue
        positions[target] = index
        table.columns[target] = name
        match = _CURRENCY_IN_HEADER.search(name)
        if match and target in ("spend", "revenue"):
            table.header_currency = match.group(1).upper()
    if not ({"asset_code", "ad_name", "ad_id"} & set(positions)):
        raise ParseError("missing_identifier_column", "表头里需要「素材编号」「广告名称」或「广告编号」其中一列")
    if not ({"date_start", "date_end"} <= set(positions) or "day" in positions):
        raise ParseError("missing_date_columns", "表头里需要「开始日期」和「结束日期」（或一列「日期」）")
    if "impressions" not in positions:
        raise ParseError("missing_impressions_column", "表头里需要「展示次数」")
    if "clicks" not in positions:
        raise ParseError("missing_clicks_column", "表头里需要「链接点击量」")
    for line_number, cells in enumerate(reader, start=2):
        if not any((cell or "").strip() for cell in cells):
            continue
        row = {"line": line_number}
        for name, position in positions.items():
            row[name] = cells[position].strip() if position < len(cells) and cells[position] is not None else ""
        if "day" in row:
            day = row.pop("day")
            row.setdefault("date_start", day)
            row.setdefault("date_end", day)
            row["date_start"] = row["date_start"] or day
            row["date_end"] = row["date_end"] or day
        table.rows.append(row)
        if len(table.rows) > MAX_ROWS:
            raise ParseError("too_many_rows", f"一次最多导入 {MAX_ROWS:,} 行", params={"limit": MAX_ROWS})
    if not table.rows:
        raise ParseError("no_rows", "至少需要一行数据")
    return table


def rows_from_manual(rows: list[dict]) -> ParsedTable:
    if not rows:
        raise ParseError("no_rows", "至少需要一行数据")
    if len(rows) > MAX_ROWS:
        raise ParseError("too_many_rows", f"一次最多导入 {MAX_ROWS:,} 行", params={"limit": MAX_ROWS})
    table = ParsedTable(columns={name: name for name in FIELDS})
    for index, raw in enumerate(rows, start=1):
        row = {"line": index}
        for name in FIELDS:
            value = raw.get(name)
            row[name] = "" if value is None else str(value).strip()
        table.rows.append(row)
    return table


# ---------------------------------------------------------------------------
# Values
# ---------------------------------------------------------------------------


def parse_date(raw: str) -> date | None:
    match = _DATE.match(raw or "")
    if not match:
        return None
    try:
        return date(int(match.group(1)), int(match.group(2)), int(match.group(3)))
    except ValueError:
        return None


def parse_number(raw: str) -> Decimal | None:
    """``"1,234.50"`` / ``"$12"`` / ``"US$ 3.2"`` -> Decimal; blank -> None.

    Raises ``ValueError`` for text that is not a number.
    """
    text = (raw or "").strip()
    if text.lower() in _BLANK:
        return None
    cleaned = re.sub(r"[,\s，_]", "", text)
    cleaned = re.sub(r"^[A-Za-z]{0,3}[$¥€£￥]", "", cleaned)
    cleaned = cleaned.rstrip("元")
    try:
        value = Decimal(cleaned)
    except InvalidOperation:
        raise ValueError(text) from None
    if not value.is_finite():
        raise ValueError(text)
    return value


@dataclass
class NormalizedRow:
    line: int
    asset_code: str | None
    ad_name: str
    ad_id: str
    date_start: date | None
    date_end: date | None
    impressions: int | None
    clicks: int | None
    spend: Decimal | None
    conversions: Decimal | None
    revenue: Decimal | None
    currency: str | None
    error: str | None = None
    error_field: str | None = None

    def identity(self) -> dict:
        """The normalized content the import fingerprint hashes."""
        def text(value):
            return None if value is None else format(value, "f") if isinstance(value, Decimal) else str(value)

        return {
            "asset_code": self.asset_code, "ad_name": self.ad_name, "ad_id": self.ad_id,
            "date_start": text(self.date_start), "date_end": text(self.date_end),
            "impressions": text(self.impressions), "clicks": text(self.clicks),
            "spend": text(self.spend), "conversions": text(self.conversions),
            "revenue": text(self.revenue), "currency": self.currency,
        }


def normalize_row(raw: dict, *, default_currency: str | None, today: date) -> NormalizedRow:
    row = NormalizedRow(
        line=int(raw.get("line") or 0),
        asset_code=codes.normalize_code(raw.get("asset_code")) or ((raw.get("asset_code") or "").strip().upper() or None),
        ad_name=(raw.get("ad_name") or "").strip()[:500],
        ad_id=re.sub(r"\s", "", raw.get("ad_id") or "")[:100],
        date_start=None, date_end=None, impressions=None, clicks=None,
        spend=None, conversions=None, revenue=None, currency=None,
    )

    def fail(code: str, field_name: str | None = None) -> NormalizedRow:
        if row.error is None:
            row.error, row.error_field = code, field_name
        return row

    if not (row.asset_code or row.ad_name or row.ad_id):
        fail("missing_identifier")
    start_raw, end_raw = raw.get("date_start") or "", raw.get("date_end") or ""
    if not start_raw.strip() or not end_raw.strip():
        fail("missing_date")
    row.date_start, row.date_end = parse_date(start_raw), parse_date(end_raw)
    if (start_raw.strip() and row.date_start is None) or (end_raw.strip() and row.date_end is None):
        fail("invalid_date")
    if row.date_start and row.date_end:
        if row.date_end < row.date_start:
            fail("date_order")
        elif row.date_end > today:
            fail("future_date")

    numbers: dict[str, Decimal | None] = {}
    for name in ("impressions", "clicks", "spend", "conversions", "revenue"):
        try:
            numbers[name] = parse_number(raw.get(name) or "")
        except ValueError:
            numbers[name] = None
            fail("invalid_number", name)
            continue
        if numbers[name] is not None and numbers[name] < 0:
            fail("negative_number", name)
    for name in ("impressions", "clicks"):
        value = numbers.get(name)
        if value is None:
            if row.error is None:
                fail(f"missing_{name}", name)
            continue
        if value != value.to_integral_value():
            fail("not_integer", name)
            continue
        setattr(row, name, int(value))
    row.spend, row.conversions, row.revenue = numbers.get("spend"), numbers.get("conversions"), numbers.get("revenue")
    if row.impressions is not None and row.clicks is not None and row.clicks > row.impressions:
        fail("clicks_exceed_impressions", "clicks")

    currency = (raw.get("currency") or "").strip().upper() or (default_currency or None)
    if currency is not None and not re.fullmatch(r"[A-Z]{3}", currency):
        fail("currency_invalid", "currency")
        currency = None
    row.currency = currency
    if row.error is None and currency is None and ((row.spend or 0) > 0 or (row.revenue or 0) > 0):
        fail("currency_missing", "currency")
    return row
