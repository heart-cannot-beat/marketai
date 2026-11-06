"""Supported media reporting units; AI provider billing remains independent.

Amounts are never converted. A round freezes its currency, attribution window
and account timezone; CSV intake must match that exact reporting context.
"""
from typing import Literal, get_args

MediaCurrency = Literal['AED', 'AUD', 'BRL', 'CAD', 'CHF', 'CNY', 'EUR', 'GBP', 'HKD',
    'IDR', 'INR', 'JPY', 'KRW', 'MXN', 'MYR', 'NZD', 'PHP', 'PLN', 'SAR', 'SEK',
    'SGD', 'THB', 'TWD', 'USD', 'VND', 'ZAR']
AttributionWindow = Literal['1d_click', '7d_click', '1d_view', '1d_click_1d_view', '7d_click_1d_view']
MEDIA_CURRENCIES = list(get_args(MediaCurrency))
ATTRIBUTION_WINDOWS = list(get_args(AttributionWindow))
DEFAULT_ATTRIBUTION = '7d_click_1d_view'
MIN_ATTRIBUTION_LAG = {'1d_click': 2, '7d_click': 8, '1d_view': 2,
    '1d_click_1d_view': 2, '7d_click_1d_view': 8}


def attribution_label(window):
    return {'1d_click': '1-day click', '7d_click': '7-day click', '1d_view': '1-day view',
        '1d_click_1d_view': '1-day click / 1-day view', '7d_click_1d_view': '7-day click / 1-day view'}[window]
