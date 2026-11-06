import pytest

from app.marketing.claims import unsupported_claims


@pytest.mark.parametrize(
    ('claim', 'evidence'),
    [
        ('Rated 1000V', 'Rated 1,000V'),
        ('Rated 1,000 V', 'Rated 1000 V'),
        ('Capacity 1000000 mAh', 'Capacity 1,000,000 mAh'),
        ('Price $1234.50', 'Price $1,234.50'),
    ],
)
def test_grouped_numbers_retain_the_same_literal_evidence(claim, evidence):
    assert unsupported_claims(claim, evidence) == []


@pytest.mark.parametrize(
    ('claim', 'evidence'),
    [
        ('Rated 1000V', 'Rated 100V'),
        ('Rated 1000V', 'Rated 10,00V'),
        ('Rated 1000V', 'Rated 1,00,0V'),
        ('Rated 1000V', 'Rated 1.000V'),
        ('Rated 1000V', 'Rated 1,000A'),
        ('Price $1234.50', 'Price $1,234.05'),
        ('UL 2252 certified', 'ULTRA product name'),
        ('ISO 9001 certified', 'ISO 9,001 certified'),
    ],
)
def test_grouping_does_not_support_different_or_ambiguous_claims(claim, evidence):
    assert unsupported_claims(claim, evidence)
