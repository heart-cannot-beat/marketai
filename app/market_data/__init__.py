"""Market data: the Workspace's own data-vendor credentials and their price table.

Competitor ad scans (``docs/product/competitor-scan-spec.md`` §6) pay a data
vendor with a key the Workspace brings (BYOK, ``binding_scope='market_data'``).
This package holds what every market-data caller shares: which providers and
binding scope exist, the free key check run when an Owner saves a key, and
the deployment price table every real resolve or scan is quoted from.
"""
