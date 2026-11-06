"""Conservative literal evidence guards, not a claim of semantic entailment."""
import re


_UNITS = r"mAh|kWh|Wh|kW|W|A|V|%|分钟|小时|英里|公里"
_GROUPED_NUMBER = r"(?<![\d.,])\d{1,3}(?:,\d{3})+(?:\.\d+)?(?![\d.,])"


def _normalize_grouped_quantities(value):
    # Only unambiguous English thousands grouping on measurements/prices is
    # formatting. Keep decimals, malformed grouping and certification IDs literal.
    return re.sub(
        rf"(?:{_GROUPED_NUMBER}\s*(?:{_UNITS})(?![A-Za-z])|[$€£¥]\s*{_GROUPED_NUMBER})",
        lambda match: match.group(0).replace(",", ""), value, flags=re.IGNORECASE,
    )


def unsupported_claims(observed, support):
    observed = _normalize_grouped_quantities(observed)
    support = _normalize_grouped_quantities(support)
    patterns = (
        r"(?<![A-Za-z0-9])(?:UL|IEC|ISO|EN)\s*[-:]?\s*\d{2,6}(?:[-.]\d+)*",
        rf"(?<![A-Za-z0-9])\d+(?:\.\d+)?\s*(?:{_UNITS})(?![A-Za-z])",
        r"[$€£¥]\s*\d+(?:[.,]\d+)*",
    )
    claims = {m.group(0) for pattern in patterns for m in re.finditer(pattern, observed, re.IGNORECASE)}
    def supported(claim):
        value = re.sub(r"\s+", "", claim).casefold()
        before = r"(?<![0-9])" if value[0].isdigit() else r"(?<![a-z0-9])"
        pattern = before + r"\s*".join(re.escape(c) for c in value) + r"(?![a-z0-9])"
        return re.search(pattern, support, re.IGNORECASE) is not None
    return sorted(c for c in claims if not supported(c))[:20]
