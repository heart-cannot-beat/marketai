"""A Chinese fact written from an English source keeps its units (#244).

With the interface in Chinese the AI writes facts in Chinese and quotes the
English source: "adds about 25 miles" becomes "约25英里". The literal check
takes "25英里" from the fact, so the source's "25 miles" has to support it.
"""
import pytest

from app.marketing.claims import unsupported_claims


@pytest.mark.parametrize(
    ('claim', 'evidence'),
    [
        ('每小时增加约25英里续航', 'which adds about 25 miles of range per hour'),
        ('25英里', 'up to 25 mi per hour'),
        ('25英里', 'a 25-mile range'),
        ('1000英里', 'good for 1,000 miles'),
        ('充满约8小时', 'about 8 hours to a full charge'),
        ('8小时', 'runs 8 hr'),
        ('8.5小时', 'lasts 8.5 h'),
        ('30分钟', 'ready in 30 minutes'),
        ('30分钟', '30 min'),
        ('100公里', 'about 100 km'),
        ('100公里', '100 kilometres'),
        # The Chinese unit in a Chinese source still works.
        ('25英里', '每小时约 25 英里'),
    ],
)
def test_a_chinese_unit_is_supported_by_its_english_spelling(claim, evidence):
    assert unsupported_claims(claim, evidence) == []


@pytest.mark.parametrize(
    ('claim', 'evidence'),
    [
        ('25英里', 'adds about 52 miles'),
        ('25英里', 'adds about 125 miles'),
        ('25英里', 'adds about 0.25 miles'),
        ('25英里', 'adds about 25 km'),
        ('25英里', 'adds about 25 min'),
        ('8小时', 'an 8 hp motor'),
        ('30分钟', 'a 30 mile range'),
    ],
)
def test_another_number_or_unit_does_not_support_it(claim, evidence):
    assert unsupported_claims(claim, evidence)


def test_english_spellings_are_still_not_claims_themselves():
    # This fix makes no English sentence stricter.
    assert unsupported_claims('adds about 25 miles in 8 hours', 'no numbers here') == []
