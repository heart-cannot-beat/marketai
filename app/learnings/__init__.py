"""The results loop: ad results -> dimension-value learnings (Issue #166).

The learning unit is the dimension value, not the image (blueprint §1):

* :mod:`.parsing`     -- CSV / pasted table / JSON rows, header aliases,
  number and date parsing, row validation.
* :mod:`.codes`       -- the printable asset code (``MK-XXXXXX``) a seller
  copies into a sheet or an ad name instead of an internal id.
* :mod:`.attribution` -- AssetVersion -> RunItem -> test-plan combination
  (direction + factor values); evidence partitions.
* :mod:`.stats`       -- deterministic main effects per factor value and
  direction: Wilson / Katz / Poisson intervals hulled with a seeded asset
  bootstrap, minimum samples, balance (confounding) check, verdicts and
  one-line Chinese sentences.
* :mod:`.service`     -- import preview / commit, immutable snapshots,
  views, and the value verdicts the plan drafter receives.
* :mod:`.v1_router`   -- ``/api/v1/results/imports``,
  ``/api/v1/products/{id}/learnings``, ``/api/v1/products/{id}/results/template``
  and ``/api/v1/test-plans/{id}/results``.
"""
