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


_PATTERNS = (
    r"(?<![A-Za-z0-9])(?:UL|IEC|ISO|EN)\s*[-:]?\s*\d{2,6}(?:[-.]\d+)*",
    rf"(?<![A-Za-z0-9])\d+(?:\.\d+)?\s*(?:{_UNITS})(?![A-Za-z])",
    r"[$€£¥]\s*\d+(?:[.,]\d+)*",
)
#: "3x faster", "快 3 倍": a comparison is a literal claim too, though not a measurement (#224).
_MULTIPLIER = r"(?<![A-Za-z0-9.])\d+(?:\.\d+)?\s*(?:x|×|倍|times)(?![A-Za-z])"


def _mentions(claim, text):
    """Whether ``text`` states this literal claim, whitespace and case aside."""
    value = re.sub(r"\s+", "", claim).casefold()
    before = r"(?<![0-9])" if value[0].isdigit() else r"(?<![a-z0-9])"
    pattern = before + r"\s*".join(re.escape(c) for c in value) + r"(?![a-z0-9])"
    return re.search(pattern, text, re.IGNORECASE) is not None


def unsupported_claims(observed, support):
    observed = _normalize_grouped_quantities(observed)
    support = _normalize_grouped_quantities(support)
    claims = {m.group(0) for pattern in _PATTERNS for m in re.finditer(pattern, observed, re.IGNORECASE)}
    return sorted(c for c in claims if not _mentions(c, support))[:20]


def literal_claims(text):
    """Every literal claim ``text`` makes: certification ids, measurements, prices and multipliers."""
    text = _normalize_grouped_quantities(text)
    return sorted({m.group(0) for pattern in (*_PATTERNS, _MULTIPLIER)
                   for m in re.finditer(pattern, text, re.IGNORECASE)})


#: The shortest wording of a listed claim matched as words: shorter ones ("fast", "IP55") are matched only
#: through their literal claims, so an ordinary word in an ad is not taken for an unsourced claim.
_WORDING = 8


def _words(text):
    return re.sub(r"\s+", " ", re.sub(r"[\"'“”‘’「」『』《》()（）\[\].,;:!?。，；：！？、]", " ", text)).strip().casefold()


def unsourced_uses(text, listed, support):
    """The listed unsourced claims ``text`` uses (#224): ``listed`` is ``[(id, wording)]`` and ``support``
    the words of every fact that has a source.

    A text uses a listed claim when it repeats the claim's own wording, or one of the claim's literal
    claims (a number with its unit, a price, a certification id, a multiplier) that no sourced fact
    states. Conservative and literal, like ``unsupported_claims``: it never judges meaning."""
    observed = _normalize_grouped_quantities(text)
    support = _normalize_grouped_quantities(support)
    words = _words(observed)
    used = []
    for claim_id, wording in listed:
        own = _words(_normalize_grouped_quantities(wording))
        if len(own) >= _WORDING and own in words:
            used.append(claim_id)
            continue
        if any(_mentions(claim, observed) and not _mentions(claim, support) for claim in literal_claims(wording)):
            used.append(claim_id)
    return used
