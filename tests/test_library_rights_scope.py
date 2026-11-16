"""授权范围的服务端规则（Issue #182）。

上传表单把授权范围从自由文本改成了下拉框（``web/lib/library/rights-scope.ts``），
存储值仍是代码。服务端有意不限定取值，只校验非空和长度：之前保存的自由文本
（以及以后从列表里去掉的代码）照常有效，已发布的 ReferenceAssetVersion 不受影响。
"""
from __future__ import annotations

import re
import unittest
from datetime import date, timedelta
from pathlib import Path

# Sets the MARKETAI_* env vars before any ``app.*`` import (see the module).
from tests.support.library_fixtures import FUTURE  # noqa: E402

from app.library.service import LibraryError, normalize_rights_declaration  # noqa: E402

WEB_SCOPES = Path(__file__).resolve().parents[1] / "web" / "lib" / "library" / "rights-scope.ts"


def _web_scopes() -> list[str]:
    source = WEB_SCOPES.read_text(encoding="utf-8")
    block = re.search(r"RIGHTS_SCOPES = \[(.*?)\] as const", source, re.S)
    assert block, "RIGHTS_SCOPES not found"
    return re.findall(r"'([a-z_]+)'", block.group(1))


class RightsScopeTests(unittest.TestCase):
    def _normalize(self, scope: str) -> dict:
        return normalize_rights_declaration(
            {"scope": scope, "valid_to": FUTURE}, declared_by="usr_owner", today=date.today()
        )

    def test_every_scope_the_upload_form_offers_is_stored_as_its_code(self):
        scopes = _web_scopes()
        self.assertIn("owned_product_photo", scopes)
        for scope in scopes:
            with self.subTest(scope=scope):
                rights = self._normalize(scope)
                self.assertEqual(rights["scope"], scope)
                self.assertEqual((rights["state"], rights["declared_by"]), ("granted", "usr_owner"))

    def test_a_scope_saved_before_the_list_stays_valid_verbatim(self):
        for scope in ("licensed", "owned", "Photos supplied by the Acme Charge factory", "经销商授权照片"):
            with self.subTest(scope=scope):
                self.assertEqual(self._normalize(f"  {scope} ")["scope"], scope)

    def test_empty_or_overlong_scope_is_still_refused(self):
        with self.assertRaises(LibraryError) as empty:
            self._normalize("   ")
        self.assertEqual(empty.exception.code, "library_rights_scope_required")
        with self.assertRaises(LibraryError) as long:
            self._normalize("x" * 201)
        self.assertEqual(long.exception.code, "library_rights_scope_too_long")

    def test_expired_declaration_is_refused_whatever_the_scope(self):
        past = (date.today() - timedelta(days=1)).isoformat()
        with self.assertRaises(LibraryError) as expired:
            normalize_rights_declaration(
                {"scope": "owned_product_photo", "valid_to": past}, declared_by="usr_owner"
            )
        self.assertEqual(expired.exception.code, "library_rights_invalid")


if __name__ == "__main__":
    unittest.main()
