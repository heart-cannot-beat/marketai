import unittest

from app.scale.models import (
    ScaleAdviceThreadRecord,
    ScaleAdviceOccurrenceRecord,
    ScaleAdviceThreadEventRecord,
    ScaleAdviceDispositionRecord,
    ScaleAdviceSnoozeRecord,
    ScaleAdviceNotificationDecisionRecord,
    ScaleAdviceConversionRecord,
    ScaleTestPlanDraftRecord,
    ScaleAdviceOutcomeRecord,
    ScaleUnobservedFactorRecord,
    ScaleExportRecord,
)
from app.adapters.persistence.database import Base
from app.adapters.persistence.models import new_id


class TestScaleAdviceRecords(unittest.TestCase):
    def test_advice_thread_table_name(self):
        self.assertEqual(ScaleAdviceThreadRecord.__tablename__, "scale_advice_threads")

    def test_advice_occurrence_table_name(self):
        self.assertEqual(
            ScaleAdviceOccurrenceRecord.__tablename__, "scale_advice_occurrences",
        )

    def test_advice_thread_event_table_name(self):
        self.assertEqual(
            ScaleAdviceThreadEventRecord.__tablename__, "scale_advice_thread_events",
        )

    def test_advice_disposition_table_name(self):
        self.assertEqual(
            ScaleAdviceDispositionRecord.__tablename__, "scale_advice_dispositions",
        )

    def test_advice_snooze_table_name(self):
        self.assertEqual(ScaleAdviceSnoozeRecord.__tablename__, "scale_advice_snoozes")

    def test_advice_notification_decision_table_name(self):
        self.assertEqual(
            ScaleAdviceNotificationDecisionRecord.__tablename__,
            "scale_advice_notification_decisions",
        )

    def test_advice_conversion_table_name(self):
        self.assertEqual(
            ScaleAdviceConversionRecord.__tablename__, "scale_advice_conversions",
        )

    def test_test_plan_draft_table_name(self):
        self.assertEqual(ScaleTestPlanDraftRecord.__tablename__, "scale_test_plan_drafts")

    def test_advice_outcome_table_name(self):
        self.assertEqual(ScaleAdviceOutcomeRecord.__tablename__, "scale_advice_outcomes")

    def test_unobserved_factor_table_name(self):
        self.assertEqual(
            ScaleUnobservedFactorRecord.__tablename__, "scale_unobserved_factors",
        )

    def test_export_table_name(self):
        self.assertEqual(ScaleExportRecord.__tablename__, "scale_exports")

    def test_advice_thread_id_prefix(self):
        self.assertTrue(new_id("sat").startswith("sat_"))

    def test_advice_occurrence_id_prefix(self):
        self.assertTrue(new_id("sao").startswith("sao_"))

    def test_advice_thread_event_id_prefix(self):
        self.assertTrue(new_id("sate").startswith("sate_"))

    def test_advice_disposition_id_prefix(self):
        self.assertTrue(new_id("sad").startswith("sad_"))

    def test_advice_snooze_id_prefix(self):
        self.assertTrue(new_id("sas").startswith("sas_"))

    def test_advice_notification_decision_id_prefix(self):
        self.assertTrue(new_id("sand").startswith("sand_"))

    def test_advice_conversion_id_prefix(self):
        self.assertTrue(new_id("sacr").startswith("sacr_"))

    def test_test_plan_draft_id_prefix(self):
        self.assertTrue(new_id("stpd").startswith("stpd_"))

    def test_advice_outcome_id_prefix(self):
        self.assertTrue(new_id("saor").startswith("saor_"))

    def test_unobserved_factor_id_prefix(self):
        self.assertTrue(new_id("suf").startswith("suf_"))

    def test_export_id_prefix(self):
        self.assertTrue(new_id("sexp").startswith("sexp_"))

    def test_conversion_output_check_present(self):
        """The CHECK enforcing exactly one of generation_draft_id /
        scale_test_plan_draft_id must actually be attached to the table."""
        check_sqls = [
            str(c.sqltext)
            for c in ScaleAdviceConversionRecord.__table__.constraints
            if hasattr(c, "sqltext")
        ]
        self.assertTrue(
            any("generation_draft_id" in sql and "scale_test_plan_draft_id" in sql
                for sql in check_sqls),
            f"expected exclusive-output CHECK, got: {check_sqls}",
        )

    def test_disposition_conversion_link_check_present(self):
        check_sqls = [
            str(c.sqltext)
            for c in ScaleAdviceDispositionRecord.__table__.constraints
            if hasattr(c, "sqltext")
        ]
        self.assertTrue(
            any("linked_conversion_id" in sql for sql in check_sqls),
            f"expected linked_conversion_id shape CHECK, got: {check_sqls}",
        )

    def test_all_constraint_names_fit_postgres_63_char_limit(self):
        """Guards the naming-convention gotcha: SQLAlchemy re-runs an
        explicit CheckConstraint `name=` through the metadata naming
        convention (`ck_%(table_name)s_%(constraint_name)s`), so the final
        identifier is longer than the literal string passed in the model.
        Every constraint on every table this task added must still resolve
        under PostgreSQL's 63-byte identifier limit."""
        new_table_names = {
            ScaleAdviceThreadRecord.__tablename__,
            ScaleAdviceOccurrenceRecord.__tablename__,
            ScaleAdviceThreadEventRecord.__tablename__,
            ScaleAdviceDispositionRecord.__tablename__,
            ScaleAdviceSnoozeRecord.__tablename__,
            ScaleAdviceNotificationDecisionRecord.__tablename__,
            ScaleAdviceConversionRecord.__tablename__,
            ScaleTestPlanDraftRecord.__tablename__,
            ScaleAdviceOutcomeRecord.__tablename__,
            ScaleUnobservedFactorRecord.__tablename__,
            ScaleExportRecord.__tablename__,
        }
        offenders = []
        for table_name, table in Base.metadata.tables.items():
            if table_name not in new_table_names:
                continue
            for constraint in table.constraints:
                name = getattr(constraint, "name", None)
                if name and len(name) > 63:
                    offenders.append((table_name, type(constraint).__name__, name, len(name)))
        self.assertEqual(offenders, [], f"constraint name(s) exceed 63 chars: {offenders}")


if __name__ == "__main__":
    unittest.main()
