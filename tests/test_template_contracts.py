import unittest
from app.library.contracts import (
    validate_content, extract_dependencies, canonical_hash,
    ENTRY_DIMENSION_TEMPLATE, ENTRY_DIMENSION_SCHEMA,
    ENTRY_PROMPT_POLICY, ENTRY_DATA_SOURCE,
    ENTRY_MANUAL_FACT_SET, ENTRY_TRUTH_RESOLUTION_POLICY,
)


class TestDimensionTemplateValidation(unittest.TestCase):

    def _template_content(self, **overrides):
        base = {
            "dimensions": [
                {
                    "id": "dim-color",
                    "name": "Color",
                    "values": [
                        {"id": "val-red", "name": "Red",
                         "positive_fragments": ["vibrant red"], "negative_fragments": []},
                        {"id": "val-blue", "name": "Blue",
                         "positive_fragments": ["deep blue"], "negative_fragments": []},
                    ],
                }
            ],
            "constraints": [],
            "concept_key_mappings": [],
            "combination_cardinality": "one_per_dimension",
        }
        base.update(overrides)
        return base

    def test_valid_template_passes(self):
        report = validate_content(ENTRY_DIMENSION_TEMPLATE, self._template_content())
        self.assertTrue(report.ok, report.blocking)

    def test_missing_dimensions_is_blocking(self):
        report = validate_content(ENTRY_DIMENSION_TEMPLATE, {"constraints": []})
        self.assertFalse(report.ok)
        codes = {f.code for f in report.blocking}
        self.assertIn("template_dimensions_required", codes)

    def test_empty_dimensions_is_blocking(self):
        report = validate_content(ENTRY_DIMENSION_TEMPLATE, self._template_content(dimensions=[]))
        self.assertFalse(report.ok)

    def test_duplicate_dimension_ids_blocking(self):
        dims = [
            {"id": "dim-a", "name": "A", "values": [{"id": "v1", "name": "V1",
             "positive_fragments": [], "negative_fragments": []}]},
            {"id": "dim-a", "name": "B", "values": [{"id": "v2", "name": "V2",
             "positive_fragments": [], "negative_fragments": []}]},
        ]
        report = validate_content(ENTRY_DIMENSION_TEMPLATE, self._template_content(dimensions=dims))
        self.assertFalse(report.ok)
        codes = {f.code for f in report.blocking}
        self.assertIn("template_duplicate_dimension_id", codes)

    def test_duplicate_value_ids_blocking(self):
        dims = [{
            "id": "dim-a", "name": "A",
            "values": [
                {"id": "same", "name": "V1", "positive_fragments": [], "negative_fragments": []},
                {"id": "same", "name": "V2", "positive_fragments": [], "negative_fragments": []},
            ],
        }]
        report = validate_content(ENTRY_DIMENSION_TEMPLATE, self._template_content(dimensions=dims))
        self.assertFalse(report.ok)

    def test_constraint_references_unknown_dim_blocking(self):
        content = self._template_content(constraints=[
            {"type": "requires", "from_dimension_id": "dim-color", "from_value_id": "val-red",
             "to_dimension_id": "nonexistent", "to_value_id": "v1"},
        ])
        report = validate_content(ENTRY_DIMENSION_TEMPLATE, content)
        self.assertFalse(report.ok)

    def test_invalid_combination_cardinality_blocking(self):
        report = validate_content(ENTRY_DIMENSION_TEMPLATE,
                                  self._template_content(combination_cardinality="invalid"))
        self.assertFalse(report.ok)


class TestDimensionSchemaValidation(unittest.TestCase):

    def _schema_content(self, **overrides):
        base = {
            "product_entry_id": "lib_abc123",
            "derived_from_template_version_id": "lib_tmpl_v1",
            "dimensions": [
                {
                    "id": "local-dim-1",
                    "name": "Color",
                    "source_dimension_id": "dim-color",
                    "values": [
                        {"id": "local-val-1", "name": "Red", "source_value_id": "val-red",
                         "positive_fragments": ["vibrant red"], "negative_fragments": []},
                    ],
                }
            ],
            "constraints": [],
            "combination_cardinality": "one_per_dimension",
        }
        base.update(overrides)
        return base

    def test_valid_schema_passes(self):
        report = validate_content(ENTRY_DIMENSION_SCHEMA, self._schema_content())
        self.assertTrue(report.ok, report.blocking)

    def test_missing_product_entry_id_blocking(self):
        report = validate_content(ENTRY_DIMENSION_SCHEMA, self._schema_content(product_entry_id=""))
        self.assertFalse(report.ok)
        codes = {f.code for f in report.blocking}
        self.assertIn("schema_product_entry_required", codes)


class TestPromptPolicyValidation(unittest.TestCase):

    def _policy_content(self, **overrides):
        base = {
            "registered_tokens": [
                {"name": "product_name", "description": "Product name", "required": True}
            ],
            "locked_phrases": [],
            "brand_tone": {"style": "professional"},
            "allowed_template_entry_ids": [],
            "optional_clauses": [],
        }
        base.update(overrides)
        return base

    def test_valid_policy_passes(self):
        report = validate_content(ENTRY_PROMPT_POLICY, self._policy_content())
        self.assertTrue(report.ok, report.blocking)

    def test_duplicate_token_names_blocking(self):
        tokens = [
            {"name": "x", "description": "X", "required": True},
            {"name": "x", "description": "Y", "required": False},
        ]
        report = validate_content(ENTRY_PROMPT_POLICY, self._policy_content(registered_tokens=tokens))
        self.assertFalse(report.ok)

    def test_single_pass_no_recursion(self):
        tokens = [
            {"name": "a", "description": "A", "required": False, "default": "{{b}}"},
            {"name": "b", "description": "B", "required": False, "default": "plain"},
        ]
        report = validate_content(ENTRY_PROMPT_POLICY, self._policy_content(registered_tokens=tokens))
        self.assertFalse(report.ok)
        codes = {f.code for f in report.blocking}
        self.assertIn("policy_recursive_token", codes)


class TestDataSourceValidation(unittest.TestCase):

    def _ds_content(self, **overrides):
        base = {
            "adapter_kind": "manual",
            "capabilities": ["price", "shipping"],
            "market_context": ["US"],
            "config": {},
            "config_schema_version": "manual/1.0",
            "execution_mode": "real",
        }
        base.update(overrides)
        return base

    def test_valid_manual_source_passes(self):
        report = validate_content(ENTRY_DATA_SOURCE, self._ds_content())
        self.assertTrue(report.ok, report.blocking)

    def test_invalid_adapter_kind_blocking(self):
        report = validate_content(ENTRY_DATA_SOURCE, self._ds_content(adapter_kind="unknown"))
        self.assertFalse(report.ok)

    def test_invalid_execution_mode_blocking(self):
        report = validate_content(ENTRY_DATA_SOURCE, self._ds_content(execution_mode="invalid"))
        self.assertFalse(report.ok)

    def test_invalid_execution_mode_message_lists_the_accepted_modes(self):
        report = validate_content(ENTRY_DATA_SOURCE, self._ds_content(execution_mode="scheduled"))
        finding = next(f for f in report.blocking if f.code == "ds_execution_mode")
        self.assertIn("'mock'", finding.message)
        self.assertIn("'real'", finding.message)

    def test_source_type_and_execution_mode_pairs_publish(self):
        # 来源类型决定执行模式（#396）：受控 Mock 只能是 mock，人工和 Shopify 只能是 real。
        for adapter_kind, execution_mode in (("manual", "real"), ("mock", "mock"), ("shopify", "real")):
            with self.subTest(adapter_kind=adapter_kind):
                report = validate_content(ENTRY_DATA_SOURCE, self._ds_content(
                    adapter_kind=adapter_kind, execution_mode=execution_mode,
                ))
                self.assertTrue(report.ok, report.blocking)

    def test_mismatched_execution_mode_blocking(self):
        # 这三种组合在解析 Truth 时会撞 ck_tsa_manual_adapter / ck_tsa_mock_fields。
        for adapter_kind, execution_mode in (("manual", "mock"), ("mock", "real"), ("shopify", "mock")):
            with self.subTest(adapter_kind=adapter_kind):
                report = validate_content(ENTRY_DATA_SOURCE, self._ds_content(
                    adapter_kind=adapter_kind, execution_mode=execution_mode,
                ))
                self.assertFalse(report.ok)
                mismatch = [f for f in report.blocking if f.code == "ds_execution_mode_mismatch"]
                self.assertEqual(len(mismatch), 1, report.blocking)
                self.assertEqual(mismatch[0].path, "execution_mode")
                self.assertIn("manual/real", mismatch[0].message)
                self.assertIn("mock/mock", mismatch[0].message)
                self.assertIn("shopify/real", mismatch[0].message)

    def test_mismatch_is_not_reported_on_top_of_an_invalid_field(self):
        for overrides, code in (
            ({"adapter_kind": "Shopify", "execution_mode": "mock"}, "ds_adapter_kind"),
            ({"adapter_kind": "mock", "execution_mode": "scheduled"}, "ds_execution_mode"),
        ):
            with self.subTest(**overrides):
                report = validate_content(ENTRY_DATA_SOURCE, self._ds_content(**overrides))
                self.assertEqual({f.code for f in report.blocking}, {code})


class TestManualFactSetValidation(unittest.TestCase):

    def _fact_content(self, **overrides):
        base = {
            "data_source_entry_id": "lib_ds_001",
            "facts": [
                {
                    "fact_id": "fact-001",
                    "fact_type": "price",
                    "offer_entry_id": "lib_offer_001",
                    "country_code": "US",
                    "amount": "29.99",
                    "currency": "USD",
                    "valid_from": "2026-09-01",
                    "valid_to": "2026-12-31",
                    "evidence_source": "manual_entry",
                    "evidence_note": "Q4 pricing",
                }
            ],
        }
        base.update(overrides)
        return base

    def test_valid_price_fact_passes(self):
        report = validate_content(ENTRY_MANUAL_FACT_SET, self._fact_content())
        self.assertTrue(report.ok, report.blocking)

    def test_float_amount_blocking(self):
        facts = [{**self._fact_content()["facts"][0], "amount": 29.99}]
        report = validate_content(ENTRY_MANUAL_FACT_SET, self._fact_content(facts=facts))
        self.assertFalse(report.ok)
        codes = {f.code for f in report.blocking}
        self.assertIn("fact_amount_not_string", codes)

    def test_nan_amount_blocking(self):
        facts = [{**self._fact_content()["facts"][0], "amount": "NaN"}]
        report = validate_content(ENTRY_MANUAL_FACT_SET, self._fact_content(facts=facts))
        self.assertFalse(report.ok)

    def test_lowercase_currency_blocking(self):
        facts = [{**self._fact_content()["facts"][0], "currency": "usd"}]
        report = validate_content(ENTRY_MANUAL_FACT_SET, self._fact_content(facts=facts))
        self.assertFalse(report.ok)

    def test_missing_valid_to_blocking(self):
        fact = {k: v for k, v in self._fact_content()["facts"][0].items() if k != "valid_to"}
        report = validate_content(ENTRY_MANUAL_FACT_SET, self._fact_content(facts=[fact]))
        self.assertFalse(report.ok)

    def _blocking_with(self, **fields):
        fact = {**self._fact_content()["facts"][0], **fields}
        report = validate_content(ENTRY_MANUAL_FACT_SET, self._fact_content(facts=[fact]))
        return {(f.code, f.path) for f in report.blocking}

    def test_evidence_source_is_required(self):
        # #23: contract §11.4 lists the evidence source among the required fields.
        for missing in ("", "  ", None):
            with self.subTest(evidence_source=missing):
                self.assertEqual(
                    self._blocking_with(evidence_source=missing),
                    {("fact_evidence_source_required", "facts[0].evidence_source")},
                )

    def test_validity_must_start_before_it_ends(self):
        cases = {
            "reversed": ("2026-12-31", "2026-09-01"),
            "empty window": ("2026-09-01", "2026-09-01"),
            "same instant, two offsets": ("2026-09-01T02:00:00+02:00", "2026-09-01T00:00:00Z"),
        }
        for name, (valid_from, valid_to) in cases.items():
            with self.subTest(name):
                self.assertEqual(
                    self._blocking_with(valid_from=valid_from, valid_to=valid_to),
                    {("fact_valid_range", "facts[0].valid_to")},
                )
        self.assertEqual(
            self._blocking_with(valid_from="2026-09-01", valid_to="2026-09-01T00:00:01Z"), set(),
            "a date without an offset is UTC midnight, as Truth reads it",
        )

    def test_validity_dates_must_be_iso(self):
        self.assertEqual(
            self._blocking_with(valid_from="09/01/2026", valid_to="end of year"),
            {("fact_date_format", "facts[0].valid_from"), ("fact_date_format", "facts[0].valid_to")},
        )

    def test_every_row_names_its_offer(self):
        # #54: contract §11.4 lists the Offer among the fields of every row.
        for missing in ("", "  ", None, 7):
            with self.subTest(offer_entry_id=missing):
                self.assertEqual(
                    self._blocking_with(offer_entry_id=missing),
                    {("fact_offer_required", "facts[0].offer_entry_id")},
                )


class TestTruthResolutionPolicyValidation(unittest.TestCase):

    def _policy_content(self, **overrides):
        base = {
            "rules": [
                {
                    "fact_type": "price",
                    "source_priority": ["lib_ds_001"],
                    "allow_fallback": False,
                    "max_staleness_seconds": 86400,
                    "on_failure": "block",
                }
            ],
        }
        base.update(overrides)
        return base

    def test_valid_policy_passes(self):
        report = validate_content(ENTRY_TRUTH_RESOLUTION_POLICY, self._policy_content())
        self.assertTrue(report.ok, report.blocking)

    def test_duplicate_fact_type_blocking(self):
        rules = [
            {"fact_type": "price", "source_priority": ["lib_ds_001"],
             "allow_fallback": False, "max_staleness_seconds": 0, "on_failure": "block"},
            {"fact_type": "price", "source_priority": ["lib_ds_002"],
             "allow_fallback": False, "max_staleness_seconds": 0, "on_failure": "block"},
        ]
        report = validate_content(ENTRY_TRUTH_RESOLUTION_POLICY, self._policy_content(rules=rules))
        self.assertFalse(report.ok)

    def test_price_cannot_degrade(self):
        rules = [{"fact_type": "price", "source_priority": ["lib_ds_001"],
                  "allow_fallback": False, "max_staleness_seconds": 0, "on_failure": "degrade"}]
        report = validate_content(ENTRY_TRUTH_RESOLUTION_POLICY, self._policy_content(rules=rules))
        self.assertFalse(report.ok)

    def test_source_priority_lists_data_source_entries_not_adapter_kinds(self):
        # #70: the legacy adapter-kind form gets its own finding, not a
        # generic dependency_not_found from the publish lookup.
        rules = [{"fact_type": "price", "source_priority": ["lib_ds_001", "mock"],
                  "allow_fallback": False, "max_staleness_seconds": 0, "on_failure": "block"}]
        report = validate_content(ENTRY_TRUTH_RESOLUTION_POLICY, self._policy_content(rules=rules))
        self.assertEqual(
            [(f.code, f.path) for f in report.blocking],
            [("trp_source_priority_adapter_kind", "rules[0].source_priority[1]")],
        )


class TestDependencyExtraction(unittest.TestCase):

    def test_dimension_schema_dependencies(self):
        content = {
            "product_entry_id": "lib_abc123",
            "derived_from_template_version_id": "lib_tmpl_v1",
            "dimensions": [],
        }
        edges = extract_dependencies(ENTRY_DIMENSION_SCHEMA, content)
        relations = {e.relation for e in edges}
        self.assertIn("schema_template", relations)
        self.assertIn("schema_product", relations)

    def test_manual_fact_set_dependencies(self):
        content = {
            "data_source_entry_id": "lib_ds_001",
            "facts": [],
        }
        edges = extract_dependencies(ENTRY_MANUAL_FACT_SET, content)
        relations = {e.relation for e in edges}
        self.assertIn("fact_set_data_source", relations)

    def test_each_fact_row_names_an_offer_entry(self):
        # #54: one edge per row; publish resolves each to the Offer's current
        # version and stores one edge per distinct version.
        content = {
            "data_source_entry_id": "lib_ds_001",
            "facts": [
                {"fact_id": "a", "offer_entry_id": "lib_offer_a"},
                "not a row",
                {"fact_id": "b", "offer_entry_id": "lib_offer_b"},
                {"fact_id": "c", "offer_entry_id": "lib_offer_a"},
                {"fact_id": "d"},
            ],
        }
        edges = extract_dependencies(ENTRY_MANUAL_FACT_SET, content)
        self.assertEqual(
            [(e.to_version_id, e.relation, e.path) for e in edges],
            [
                ("lib_ds_001", "fact_set_data_source", "data_source_entry_id"),
                ("lib_offer_a", "fact_set_offer", "facts[0].offer_entry_id"),
                ("lib_offer_b", "fact_set_offer", "facts[2].offer_entry_id"),
                ("lib_offer_a", "fact_set_offer", "facts[3].offer_entry_id"),
            ],
        )

    def test_truth_resolution_policy_dependencies(self):
        content = {
            "rules": [
                {"fact_type": "price", "source_priority": ["lib_ds_001", "lib_ds_002"],
                 "allow_fallback": False, "max_staleness_seconds": 0, "on_failure": "block"},
            ],
        }
        edges = extract_dependencies(ENTRY_TRUTH_RESOLUTION_POLICY, content)
        self.assertEqual(
            [(e.to_version_id, e.relation, e.path) for e in edges],
            [
                ("lib_ds_001", "truth_policy_data_source", "rules[0].source_priority[0]"),
                ("lib_ds_002", "truth_policy_data_source", "rules[0].source_priority[1]"),
            ],
        )

    def test_truth_resolution_policy_adapter_kinds_are_not_dependencies(self):
        content = {
            "rules": [
                {"fact_type": "price", "source_priority": ["manual", "lib_ds_001"],
                 "allow_fallback": False, "max_staleness_seconds": 0, "on_failure": "block"},
            ],
        }
        edges = extract_dependencies(ENTRY_TRUTH_RESOLUTION_POLICY, content)
        self.assertEqual([(e.to_version_id, e.path) for e in edges],
                         [("lib_ds_001", "rules[0].source_priority[1]")])

    def test_prompt_policy_dependencies(self):
        content = {
            "registered_tokens": [],
            "allowed_template_entry_ids": ["lib_tmpl_1", "lib_tmpl_2"],
        }
        edges = extract_dependencies(ENTRY_PROMPT_POLICY, content)
        relations = {e.relation for e in edges}
        self.assertIn("policy_template", relations)
        self.assertEqual(len([e for e in edges if e.relation == "policy_template"]), 2)


if __name__ == "__main__":
    unittest.main()
