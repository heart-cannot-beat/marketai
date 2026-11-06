"""Front-flow products (Issue #161): paste a link, images or text -> product card.

- ``intake_fetch``: the SSRF-safe page/image fetcher (DNS pinned, every hop
  re-checked, bounded size/time/redirects).
- ``intake_html``: stdlib parsing of a product page into JSON-LD Product,
  OpenGraph, title, main text and image candidates.
- ``intake_extraction``: the fixed JSON schema, prompt and controlled-mock
  fixture of the ``product_intake`` ``llm_chat`` semantic, and the
  normalization of the model's answer into a product-card draft.
- ``intake_service``: the API commands (create, read, confirm).
- ``intake_worker``: the three-step Worker job (fetch, extract, images).
- ``v1_router``: ``/api/v1/products/intake``.
"""
