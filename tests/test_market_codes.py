"""Market codes in the shared test database cannot collide (#81).

``tests/support/market_codes.py`` owns every two-letter market code, and
``tests/conftest.py`` checks each market policy written to this process's
shared SQLite database. These tests pin both halves.
"""
from __future__ import annotations

import unittest
from pathlib import Path

from alembic import command
from alembic.config import Config

from app import config
from app.adapters.persistence.database import platform_session
from app.adapters.persistence.models import MarketPolicyRecord, new_id
from tests.support.market_codes import (
    FIXED_MARKET_CODES,
    REPO_ROOT,
    TWO_LETTER_CODE,
    fixed_market_code,
    generated_code_space,
    issued_market_codes,
    next_market_code,
)


class TestMarketCodeSpace(unittest.TestCase):
    def test_the_generator_never_hands_out_a_fixed_code(self) -> None:
        codes = list(generated_code_space())
        self.assertEqual(len(codes), len(set(codes)))
        self.assertEqual(len(codes), 26 * 26 - len(FIXED_MARKET_CODES))
        self.assertTrue(all(TWO_LETTER_CODE.fullmatch(code) for code in codes))
        self.assertEqual(set(codes) & set(FIXED_MARKET_CODES), set())

    def test_every_fixed_code_is_two_letters_and_owned_by_a_test_file(self) -> None:
        for code, owner in FIXED_MARKET_CODES.items():
            with self.subTest(code=code):
                self.assertRegex(code, TWO_LETTER_CODE)
                self.assertTrue((REPO_ROOT / owner).is_file(), owner)

    def test_every_market_helper_takes_codes_from_the_one_generator(self) -> None:
        import tests.support.scale_candidate_fixtures as scale_fixtures
        import tests.test_scale_enrollment_candidates as candidate_tests
        import tests.test_winner_eligibility as winner_tests

        for module in (scale_fixtures, candidate_tests, winner_tests):
            with self.subTest(module=module.__name__):
                self.assertIs(module._next_market, next_market_code)

    def test_next_market_code_never_repeats_in_a_process(self) -> None:
        before = issued_market_codes()
        first, second = next_market_code(), next_market_code()
        self.assertNotEqual(first, second)
        self.assertNotIn(first, before)
        self.assertNotIn(second, before)
        self.assertNotIn(first, FIXED_MARKET_CODES)
        self.assertNotIn(second, FIXED_MARKET_CODES)

    def test_an_unregistered_fixed_code_is_refused(self) -> None:
        with self.assertRaisesRegex(AssertionError, "not registered"):
            fixed_market_code("QZ")
        self.assertEqual(fixed_market_code("US"), "US")


class TestSharedDatabaseGuard(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        url = config.DATABASE_URL
        if url.startswith("sqlite:///"):
            Path(url.replace("sqlite:///", "", 1)).parent.mkdir(parents=True, exist_ok=True)
        config.DATA_DIR.mkdir(parents=True, exist_ok=True)
        command.upgrade(Config(str(REPO_ROOT / "alembic.ini")), "head")

    @staticmethod
    def _write(code: str) -> None:
        with platform_session() as session:
            session.add(MarketPolicyRecord(
                id=new_id("mp"), market_id=code, locale="en-US",
                country=code[:2], currency="USD",
            ))
            session.flush()

    @staticmethod
    def _exists(code: str) -> bool:
        with platform_session() as session:
            return session.query(MarketPolicyRecord).filter_by(market_id=code).count() > 0

    def test_a_generated_code_is_written_once_and_a_second_write_is_named(self) -> None:
        code = next_market_code()
        self._write(code)
        with self.assertRaisesRegex(AssertionError, rf"'{code}' already has a market policy.*{self._testMethodName}"):
            self._write(code)

    def test_a_two_letter_code_from_neither_source_is_refused(self) -> None:
        issued = issued_market_codes()
        stray = next(code for code in reversed(list(generated_code_space())) if code not in issued)
        with self.assertRaisesRegex(AssertionError, rf"'{stray}'.*neither handed out by"):
            self._write(stray)
        self.assertFalse(self._exists(stray))

    def test_a_fixed_code_from_a_file_that_does_not_own_it_is_refused(self) -> None:
        for code, owner in FIXED_MARKET_CODES.items():
            with self.subTest(code=code):
                with self.assertRaisesRegex(AssertionError, rf"'{code}' is reserved for {owner}"):
                    self._write(code)

    def test_longer_market_ids_only_need_to_be_unique(self) -> None:
        code = f"MC{new_id('x')[-8:]}"
        self._write(code)
        with self.assertRaisesRegex(AssertionError, "already has a market policy"):
            self._write(code)
