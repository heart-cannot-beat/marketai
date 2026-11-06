"""Ad test plans: "set the ad test" front flow (Issue #163, contract §3.2).

Not to be confused with the legacy ``plans`` table (``PlanRecord``), which
anchors every GenerationRun. A *test plan* here is the provider-neutral
hypothesis + factor design the user confirms before anything is generated:

* :mod:`.drafting`  -- the ``PlanDrafter`` interface, the deterministic mock
  drafter and the draft normalization (stable ids).
* :mod:`.llm_drafter` -- the governed ``llm_chat`` drafter byok plans use
  (#164): prompt, strict schema, deterministic answer -> draft mapping.
* :mod:`.defaults`  -- the Workspace「生成默认值」setting and cost quotes.
* :mod:`.service`   -- create / read / list / revise plans and their views.
* :mod:`.runs`      -- materialise a plan stage into a Library template, a
  generation draft, a preview and a submitted Run.
* :mod:`.worker`    -- the ``test_plans.draft`` Job.
* :mod:`.v1_router` -- ``/api/v1/test-plans`` and ``/api/v1/generation/defaults``.
"""
