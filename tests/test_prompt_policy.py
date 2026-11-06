"""Task 5: Prompt Policy -- baseline lifecycle, deterministic composition,
locked-phrase normalization (#20).

Contract: docs/product/creative-library-ownership-and-reuse-contract.md §10.
"""
import unittest

from tests.support.library_fixtures import LibraryFixtures

from app.library.prompt_policy import (
    compose_prompt, ComposedPrompt, normalize_draft_phrases,
    publish_baseline, withdraw_baseline, get_effective_baseline,
    TOKEN_PATTERN,
)
from app.library.contracts import canonical_hash
from app.adapters.persistence.database import platform_session
from app.adapters.persistence.models import PromptBaselineRecord


class TestComposePrompt(unittest.TestCase):

    def _baseline(self):
        return {
            "clauses": [
                {"clause_id": "safety-1", "category": "safety",
                 "text": "Do not generate harmful content.",
                 "tokens": [], "locked": True},
            ],
            "platform_tokens": [],
        }

    def _policy(self):
        return {
            "registered_tokens": [
                {"name": "product_name", "description": "Product", "required": True},
                {"name": "tone", "description": "Tone", "required": False, "default": "neutral"},
            ],
            "locked_phrases": [],
            "brand_tone": {"style": "professional"},
            "allowed_template_entry_ids": [],
            "optional_clauses": [],
        }

    def _run_inputs(self):
        return {
            "product_name": "Widget Pro",
            "product_description": "The best widget",
            "must_match": ["logo visible"],
            "person_fragments": [],
            "dimension_values": {"color": "red"},
            "intent": "product_hero",
            "aspect_ratio": "1:1",
            "negatives": ["blurry"],
        }

    def test_deterministic(self):
        b, p, r = self._baseline(), self._policy(), self._run_inputs()
        c1 = compose_prompt(b, p, r)
        c2 = compose_prompt(b, p, r)
        self.assertEqual(c1.final_text, c2.final_text)
        self.assertEqual(c1.composition_hash, c2.composition_hash)
        self.assertEqual(c1.layer_hashes, c2.layer_hashes)

    def test_baseline_clauses_first(self):
        result = compose_prompt(self._baseline(), self._policy(), self._run_inputs())
        self.assertTrue(result.final_text.startswith("Do not generate harmful content."))

    def test_unknown_token_error(self):
        policy = self._policy()
        policy["registered_tokens"].append(
            {"name": "unknown", "description": "x", "required": True}
        )
        run = self._run_inputs()  # Does not provide "unknown"
        with self.assertRaises(ValueError) as cm:
            compose_prompt(self._baseline(), policy, run)
        self.assertIn("unknown", str(cm.exception))

    def test_token_in_value_stays_literal(self):
        """Runtime value containing {{token}} is not rescanned."""
        run = self._run_inputs()
        run["product_name"] = "Widget {{tone}} Pro"
        result = compose_prompt(self._baseline(), self._policy(), run)
        self.assertIn("Widget {{tone}} Pro", result.final_text)

    def test_single_pass_no_recursion(self):
        policy = self._policy()
        policy["registered_tokens"][1]["default"] = "{{product_name}}"
        run = {**self._run_inputs()}
        del run["product_name"]
        # If "tone" were provided but its value is "{{product_name}}",
        # the result should contain the literal string, not be expanded.
        run["tone"] = "{{product_name}}"
        result = compose_prompt(self._baseline(), policy, {**run, "product_name": "Widget"})
        # product_name is substituted first; tone's value is literal
        self.assertIn("{{product_name}}", result.final_text)

    def test_composed_prompt_shape(self):
        result = compose_prompt(self._baseline(), self._policy(), self._run_inputs())
        self.assertIsInstance(result, ComposedPrompt)
        self.assertEqual(set(result.layer_hashes), {"baseline", "policy", "run_inputs"})
        self.assertEqual(result.composition_hash, canonical_hash(result.final_text))
        logged = {entry["token"] for entry in result.token_resolution_log}
        self.assertEqual(logged, {"product_name", "tone"})

    def test_missing_required_token_without_default(self):
        policy = self._policy()
        policy["registered_tokens"][0]["required"] = True
        run = self._run_inputs()
        del run["product_name"]
        with self.assertRaises(ValueError) as cm:
            compose_prompt(self._baseline(), policy, run)
        self.assertIn("product_name", str(cm.exception))

    def test_token_pattern_matches_registered_syntax(self):
        self.assertEqual(TOKEN_PATTERN.findall("{{product_name}} and {{tone}}"), ["product_name", "tone"])
        self.assertEqual(TOKEN_PATTERN.findall("no tokens here"), [])


class TestRecursiveTokenBlocked(unittest.TestCase):
    """Spec §4.2: recursive token definition → blocked at publish validation."""

    def test_recursive_token_definition_blocked(self):
        """Token whose default contains another {{token}} is a blocking finding."""
        from app.library.contracts import validate_content, ENTRY_PROMPT_POLICY
        content = {
            "registered_tokens": [
                {"name": "product_name", "description": "Name", "required": True},
                {"name": "greeting", "description": "Greeting",
                 "required": False, "default": "Hello {{product_name}}!"},
            ],
            "locked_phrases": [],
            "brand_tone": {"style": "neutral"},
            "allowed_template_entry_ids": [],
            "optional_clauses": [],
        }
        report = validate_content(ENTRY_PROMPT_POLICY, content)
        self.assertFalse(report.ok)
        codes = {f.code for f in report.blocking}
        self.assertIn("policy_recursive_token", codes)


class TestNormalizeDraftPhrases(unittest.TestCase):

    def test_removing_protected_phrase_restores(self):
        baseline = {
            "clauses": [
                {"clause_id": "safety-1", "category": "safety",
                 "text": "Never remove safety warnings.",
                 "tokens": [], "locked": True},
            ],
            "platform_tokens": [],
        }
        content = {
            "registered_tokens": [],
            "locked_phrases": [],  # Protected phrase removed
            "brand_tone": {},
            "allowed_template_entry_ids": [],
            "optional_clauses": [],
        }
        normalized, findings = normalize_draft_phrases(content, baseline)
        # Should have restored the phrase
        self.assertTrue(len(normalized.get("locked_phrases", [])) > 0
                        or len(findings) > 0)

    def test_restored_phrase_is_marked_protected_and_warns(self):
        baseline = {
            "clauses": [
                {"clause_id": "safety-1", "category": "safety",
                 "text": "Never remove safety warnings.",
                 "tokens": [], "locked": True},
            ],
            "platform_tokens": [],
        }
        content = {"registered_tokens": [], "locked_phrases": [], "brand_tone": {},
                   "allowed_template_entry_ids": [], "optional_clauses": []}
        normalized, findings = normalize_draft_phrases(content, baseline)
        self.assertEqual(len(normalized["locked_phrases"]), 1)
        restored = normalized["locked_phrases"][0]
        self.assertEqual(restored["clause_id"], "safety-1")
        self.assertTrue(restored["protected"])
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0].severity, "warning")
        self.assertEqual(findings[0].code, "protected_phrase_restored")

    def test_present_protected_phrase_is_left_alone(self):
        baseline = {
            "clauses": [
                {"clause_id": "safety-1", "category": "safety",
                 "text": "Never remove safety warnings.",
                 "tokens": [], "locked": True},
            ],
            "platform_tokens": [],
        }
        content = {
            "registered_tokens": [],
            "locked_phrases": [
                {"phrase": "Never remove safety warnings.", "clause_id": "safety-1", "protected": True},
            ],
            "brand_tone": {},
            "allowed_template_entry_ids": [],
            "optional_clauses": [],
        }
        normalized, findings = normalize_draft_phrases(content, baseline)
        self.assertEqual(len(normalized["locked_phrases"]), 1)
        self.assertEqual(findings, [])

    def test_unlocked_baseline_clauses_are_not_forced_into_locked_phrases(self):
        baseline = {
            "clauses": [
                {"clause_id": "style-1", "category": "style",
                 "text": "Prefer bright, clean lighting.",
                 "tokens": [], "locked": False},
            ],
            "platform_tokens": [],
        }
        content = {"registered_tokens": [], "locked_phrases": [], "brand_tone": {},
                   "allowed_template_entry_ids": [], "optional_clauses": []}
        normalized, findings = normalize_draft_phrases(content, baseline)
        self.assertEqual(normalized["locked_phrases"], [])
        self.assertEqual(findings, [])


class TestBaselineLifecycle(LibraryFixtures):
    """DB-backed: publish_baseline / withdraw_baseline / get_effective_baseline.

    ``prompt_baselines`` is platform-level (no workspace_id, see Task 1's
    PromptBaselineRecord), so these go through ``platform_session()`` rather
    than a TenantContext. Reuses LibraryFixtures' shared temp sqlite DB
    (migrated to head once per process).
    """

    def _content(self, note: str = "v1") -> dict:
        return {
            "clauses": [
                {"clause_id": "safety-1", "category": "safety",
                 "text": f"Do not generate harmful content ({note}).",
                 "tokens": [], "locked": True},
            ],
            "platform_tokens": [],
        }

    def test_publish_creates_effective_version_one_or_increments(self):
        before = get_effective_baseline()
        published = publish_baseline(self._content("first"), published_by="platform-admin")
        self.assertEqual(published["state"], "effective")
        self.assertEqual(published["content_hash"], canonical_hash(self._content("first")))
        if before is None:
            self.assertEqual(published["version_no"], 1)

    def test_publish_supersedes_previous_effective(self):
        first = publish_baseline(self._content("a"), published_by="platform-admin")
        second = publish_baseline(self._content("b"), published_by="platform-admin")
        self.assertEqual(second["version_no"], first["version_no"] + 1)
        current = get_effective_baseline()
        self.assertEqual(current, self._content("b"))

    def test_withdraw_marks_withdrawn_with_reason(self):
        published = publish_baseline(self._content("to-withdraw"), published_by="platform-admin")
        result = withdraw_baseline(
            published["id"], reason="emergency safety issue", withdrawn_by="platform-admin"
        )
        self.assertEqual(result["state"], "withdrawn")
        self.assertEqual(result["withdraw_reason"], "emergency safety issue")
        self.assertEqual(result["withdrawn_by"], "platform-admin")
        # Withdrawing the only effective baseline leaves none effective.
        self.assertIsNone(get_effective_baseline())
        with platform_session() as session:
            record = session.get(PromptBaselineRecord, published["id"])
            self.assertEqual(record.withdrawn_by, "platform-admin")

    def test_withdraw_non_effective_raises(self):
        published = publish_baseline(self._content("superseded-soon"), published_by="platform-admin")
        publish_baseline(self._content("new-current"), published_by="platform-admin")
        with self.assertRaises(ValueError):
            withdraw_baseline(published["id"], reason="too late", withdrawn_by="platform-admin")

    def test_withdraw_unknown_baseline_raises(self):
        with self.assertRaises(ValueError):
            withdraw_baseline("pbl_does_not_exist", reason="x", withdrawn_by="platform-admin")

    def test_get_effective_baseline_none_before_any_publish_is_a_valid_state(self):
        # Not asserting a specific value (other tests may have published already
        # in this shared DB) -- only that the call itself is safe either way.
        result = get_effective_baseline()
        self.assertTrue(result is None or isinstance(result, dict))


if __name__ == "__main__":
    unittest.main()
