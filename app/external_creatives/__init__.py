"""External creatives: images made elsewhere join the same tests (Issue #168).

Front-flow contract §5.2: "用户在任何上游（设计工具、设计师、真人 UGC）
产出的图片必须能上传进来成为候选". An uploaded image passes the reference
gate, becomes an AssetVersion of origin ``upload`` (source ``external``), is
tagged by the governed ``llm_chat`` (vision) against the product's current
dimension vocabulary, and -- once the user confirms the tags -- carries the
same concept identity a generated combination carries, so it can be frozen
into an experiment next to generated images and attributed by dimension
value in the learnings.

Modules:

* :mod:`.vocabulary` -- the product's dimension vocabulary (test-plan
  factors, the Dimension Templates its drafts used and the expression
  mechanism catalog), frozen per upload.
* :mod:`.tagging` -- the tagging prompt, the per-request JSON schema, the
  answer's normalization and the controlled-Mock fixture.
* :mod:`.identity` -- confirmed tags -> concept keys, signature and the
  learning placement; the readers experiments and learnings use.
* :mod:`.service` -- API commands (create, list, read, confirm) and views.
* :mod:`.worker` -- the Worker job: gate -> asset -> tag.
* :mod:`.v1_router` -- ``/api/v1/external-creatives``.
"""
