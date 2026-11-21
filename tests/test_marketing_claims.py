import pytest

from app.marketing.claims import literal_claims, unsupported_claims


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


@pytest.mark.parametrize(
    ('claim', 'evidence'),
    [
        # #243: a price with its currency prefix supports itself.
        ('Price: US$449, with a 3-year warranty.', 'Price: US$449, with a 3-year warranty.'),
        ('It costs C$59.', 'It costs C$59.'),
        ('Now HK$1,299', 'Now HK$1299'),
        ('Price US$449', 'Price US $449'),
        # A bare symbol names no currency: "$449" is in "US$449" too, so ad
        # copy may write the price the way the market reads it.
        ('Only $449', 'Price: US$449'),
        ('Only $449', 'Price: $449'),
    ],
)
def test_a_price_is_supported_by_the_same_price_with_or_without_its_currency_prefix(claim, evidence):
    assert unsupported_claims(claim, evidence) == []


@pytest.mark.parametrize(
    ('claim', 'evidence'),
    [
        ('It costs C$59.', 'It costs US$59.'),
        ('Price US$449', 'Price $449'),
        ('It costs C$59.', 'It costs C$95.'),
        ('Only $449', 'Price: US$494'),
        ('Only $449', 'Price: 1$449'),
    ],
)
def test_a_price_is_not_supported_by_another_currency_or_amount(claim, evidence):
    assert unsupported_claims(claim, evidence)


@pytest.mark.parametrize(
    ('text', 'claims'),
    [
        ('Only US$449 today', ['US$449']),
        ('Now HK$1,299', ['HK$1299']),
        ('Only $449 today', ['$449']),
        # A word glued to the symbol is not a currency prefix.
        ('plus$5 shipping', ['$5']),
        ('is$5', ['$5']),
    ],
)
def test_a_price_keeps_an_uppercase_currency_prefix(text, claims):
    assert literal_claims(text) == claims
