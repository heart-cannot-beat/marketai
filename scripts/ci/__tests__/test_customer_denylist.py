"""Customer denylist gate: hits are reported by location only, never by text.

Every term here is fictional. The real list exists only in the
CUSTOMER_DENYLIST secret and must never appear in the repository.
"""
import os
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
SCRIPT = REPO / "scripts/ci/customer_denylist.py"
WORKFLOW = REPO / ".github/workflows/customer-denylist.yml"

TERM = "zz-secret-brand"
ZERO_SHA = "0" * 40


def git_env() -> dict:
    """A hermetic git environment: no user config, no inherited GIT_* state."""
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env.pop("CUSTOMER_DENYLIST", None)
    env.update(
        GIT_CONFIG_GLOBAL=os.devnull,
        GIT_CONFIG_NOSYSTEM="1",
        GIT_AUTHOR_NAME="Denylist Test",
        GIT_AUTHOR_EMAIL="ci@denylist.example",
        GIT_COMMITTER_NAME="Denylist Test",
        GIT_COMMITTER_EMAIL="ci@denylist.example",
    )
    return env


class Repo:
    def __init__(self, root: Path):
        self.root = root
        self.git("init", "-q", "-b", "main")

    def git(self, *args: str) -> str:
        return subprocess.run(
            ["git", *args], cwd=self.root, env=git_env(), check=True,
            capture_output=True, text=True,
        ).stdout.strip()

    def write(self, path: str, content) -> None:
        target = self.root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(content, bytes):
            target.write_bytes(content)
        else:
            target.write_text(content, encoding="utf-8")

    def commit(self, message: str = "change") -> str:
        self.git("add", "-A")
        self.git("commit", "-q", "--allow-empty", "-m", message)
        return self.git("rev-parse", "HEAD")


class CustomerDenylistTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.repo = Repo(Path(tmp.name))

    def scan(self, *args: str, denylist=TERM):
        env = git_env()
        if denylist is not None:
            env["CUSTOMER_DENYLIST"] = denylist
        result = subprocess.run(
            [sys.executable, str(SCRIPT), *args], cwd=self.repo.root, env=env,
            capture_output=True, text=True,
        )
        return result.returncode, result.stdout + result.stderr

    def hit_lines(self, output: str, path: str) -> set:
        return {int(n) for n in re.findall(rf"^{re.escape(path)}:(\d+):", output, re.M)}

    def assertNoLeak(self, output: str, *fragments: str) -> None:
        for fragment in (TERM, *fragments):
            self.assertNotIn(fragment.lower(), output.lower())

    # ------------------------------------------------------------ --all ---

    def test_hit_reports_file_and_line_without_the_term_or_the_line(self):
        self.repo.write("notes/plan.md", f"intro\nnothing here\nlaunch plan for {TERM} next quarter\n")
        self.repo.commit()

        code, output = self.scan("--all")

        self.assertEqual(code, 1, output)
        self.assertEqual(self.hit_lines(output, "notes/plan.md"), {3})
        self.assertRegex(output, r"(?m)^notes/plan\.md:3: 命中规则 #1$")
        self.assertNoLeak(output, "next quarter", "launch plan")

    def test_clean_tree_passes(self):
        self.repo.write("readme.txt", "nothing to see\n")
        self.repo.commit()

        code, output = self.scan("--all")

        self.assertEqual(code, 0, output)
        self.assertIn("命中 0 处", output)

    def test_matching_is_case_insensitive(self):
        self.repo.write("a.txt", "clean\nWe met ZZ-Secret-Brand today\n")
        self.repo.commit()

        code, output = self.scan("--all", denylist=TERM.upper())

        self.assertEqual(code, 1, output)
        self.assertEqual(self.hit_lines(output, "a.txt"), {2})
        self.assertNoLeak(output)

    def test_prefix_boundary_rule_only_matches_at_a_word_start(self):
        self.repo.write("b.txt", "\n".join([
            "zzbrand opens the line",
            "see Zzbrand here",
            "path/zzbrand-logo.png",
            "snake_zzbrand",
            "unzzbrand is another word",
            "UNZZBRAND is another word too",
        ]) + "\n")
        self.repo.commit()

        code, output = self.scan("--all", denylist="(^|[^a-z])zzbrand")

        self.assertEqual(code, 1, output)
        self.assertEqual(self.hit_lines(output, "b.txt"), {1, 2, 3, 4})
        self.assertNoLeak(output, "zzbrand")

    def test_multiple_rules_are_numbered_and_reported_together(self):
        self.repo.write("f.txt", "alpha-zz\nbeta-zz\nalpha-zz and beta-zz\ngamma\n")
        self.repo.commit()

        code, output = self.scan("--all", denylist="\n  alpha-zz  \n\r\nbeta-zz\r\n")

        self.assertEqual(code, 1, output)
        self.assertRegex(output, r"(?m)^f\.txt:1: 命中规则 #1$")
        self.assertRegex(output, r"(?m)^f\.txt:2: 命中规则 #2$")
        self.assertRegex(output, r"(?m)^f\.txt:3: 命中规则 #1, #2$")
        self.assertEqual(self.hit_lines(output, "f.txt"), {1, 2, 3})
        self.assertNoLeak(output, "alpha-zz", "beta-zz")

    def test_missing_or_blank_denylist_skips_with_notice(self):
        self.repo.write("a.txt", f"{TERM}\n")
        self.repo.commit()
        for denylist in (None, "", " \n\n"):
            with self.subTest(denylist=denylist):
                code, output = self.scan("--all", denylist=denylist)
                self.assertEqual(code, 0, output)
                self.assertIn("未配置词表", output)
                self.assertNotIn("a.txt", output)

    def test_require_denylist_fails_closed_without_a_list(self):
        self.repo.write("a.txt", "clean\n")
        self.repo.commit()

        code, output = self.scan("--all", "--require-denylist", denylist="")

        self.assertEqual(code, 2, output)
        self.assertIn("未配置词表", output)

    def test_invalid_or_empty_matching_rule_is_reported_by_number_only(self):
        self.repo.write("a.txt", "clean\n")
        self.repo.commit()
        for bad_rule in ("(zz-unclosed", "(zz-optional)?"):
            with self.subTest(rule=bad_rule):
                code, output = self.scan("--all", denylist=f"zz-fine\n{bad_rule}")
                self.assertEqual(code, 2, output)
                self.assertIn("#2", output)
                self.assertNoLeak(output, "zz-unclosed", "zz-optional", "zz-fine")

    def test_binary_files_are_skipped_and_counted(self):
        self.repo.write("logo.bin", b"\x89PNG\r\n\x00\x00" + TERM.encode())
        self.repo.write("readme.txt", "clean\n")
        self.repo.commit()

        code, output = self.scan("--all")

        self.assertEqual(code, 0, output)
        self.assertIn("扫描 1 个文本文件", output)
        self.assertIn("跳过 1 个二进制文件", output)

    def test_file_name_hit_is_reported_with_the_term_redacted(self):
        self.repo.write(f"assets/{TERM}-logo.txt", f"clean\n{TERM}\n")
        self.repo.write(f"assets/{TERM}.bin", b"\x00binary")
        self.repo.commit()

        code, output = self.scan("--all")

        self.assertEqual(code, 1, output)
        self.assertRegex(output, r"(?m)^assets/\*\*\*-logo\.txt: 文件名命中规则 #1$")
        self.assertRegex(output, r"(?m)^assets/\*\*\*-logo\.txt:2: 命中规则 #1$")
        self.assertRegex(output, r"(?m)^assets/\*\*\*\.bin: 文件名命中规则 #1$")
        self.assertNoLeak(output)

    def test_all_mode_reads_tracked_files_from_the_working_tree_only(self):
        self.repo.write(".gitignore", "data/\n")
        self.repo.write("tracked.txt", "clean\n")
        self.repo.commit()
        self.repo.write("tracked.txt", f"clean\n{TERM}\n")
        self.repo.write("data/private.txt", f"{TERM}\n")
        self.repo.write("untracked.txt", f"{TERM}\n")

        code, output = self.scan("--all")

        self.assertEqual(code, 1, output)
        self.assertEqual(self.hit_lines(output, "tracked.txt"), {2})
        self.assertNotIn("private.txt", output)
        self.assertNotIn("untracked.txt", output)

    # ------------------------------------------------ default (base..head) ---

    def two_commits(self):
        self.repo.write("old.txt", f"legacy {TERM} mention\n")
        self.repo.write("doc.txt", "l1\nl2\nl3\n")
        base = self.repo.commit("base")
        self.repo.write("doc.txt", f"l1\nl2 edited\nl3\nnew {TERM} mention\n")
        head = self.repo.commit("head")
        return base, head

    def test_default_mode_reports_only_added_lines_at_head_line_numbers(self):
        base, head = self.two_commits()

        code, output = self.scan("--base", base, "--head", head)

        self.assertEqual(code, 1, output)
        self.assertEqual(self.hit_lines(output, "doc.txt"), {4})
        self.assertNotIn("old.txt", output)
        self.assertNoLeak(output, "mention")

    def test_all_mode_also_reports_content_the_diff_does_not_touch(self):
        self.two_commits()

        code, output = self.scan("--all")

        self.assertEqual(code, 1, output)
        self.assertEqual(self.hit_lines(output, "doc.txt"), {4})
        self.assertEqual(self.hit_lines(output, "old.txt"), {1})

    def test_default_mode_ignores_removed_lines(self):
        self.repo.write("a.txt", f"keep\n{TERM}\n")
        base = self.repo.commit("base")
        self.repo.write("a.txt", "keep\n")
        head = self.repo.commit("head")

        code, output = self.scan("--base", base, "--head", head)

        self.assertEqual(code, 0, output)

    def test_default_mode_checks_new_and_renamed_file_names(self):
        body = "".join(f"stable line {i}\n" for i in range(20))
        self.repo.write("clean-name.txt", body)
        self.repo.write(f"{TERM}-existing.txt", "clean\n")
        base = self.repo.commit("base")
        self.repo.git("mv", "clean-name.txt", f"{TERM}-renamed.txt")
        self.repo.write(f"{TERM}-new.txt", "clean\n")
        self.repo.write(f"{TERM}-existing.txt", "clean\nstill clean\n")
        head = self.repo.commit("head")

        code, output = self.scan("--base", base, "--head", head)

        self.assertEqual(code, 1, output)
        self.assertRegex(output, r"(?m)^\*\*\*-renamed\.txt: 文件名命中规则 #1$")
        self.assertRegex(output, r"(?m)^\*\*\*-new\.txt: 文件名命中规则 #1$")
        self.assertNotIn("-existing.txt", output)
        self.assertNoLeak(output)

    def test_default_mode_skips_binary_files_but_still_checks_their_names(self):
        self.repo.write("readme.txt", "clean\n")
        base = self.repo.commit("base")
        self.repo.write("logo.png", b"\x89PNG\x00" + TERM.encode())
        self.repo.write(f"{TERM}.png", b"\x89PNG\x00clean")
        head = self.repo.commit("head")

        code, output = self.scan("--base", base, "--head", head)

        self.assertEqual(code, 1, output)
        self.assertIn("跳过 2 个二进制文件", output)
        self.assertNotIn("logo.png", output)
        self.assertRegex(output, r"(?m)^\*\*\*\.png: 文件名命中规则 #1$")

    def test_zero_base_treats_every_line_of_head_as_added(self):
        _, head = self.two_commits()

        code, output = self.scan("--base", ZERO_SHA, "--head", head)

        self.assertEqual(code, 1, output)
        self.assertEqual(self.hit_lines(output, "old.txt"), {1})
        self.assertEqual(self.hit_lines(output, "doc.txt"), {4})

    def test_unknown_base_fails_closed(self):
        _, head = self.two_commits()

        code, output = self.scan("--base", "1" * 40, "--head", head)

        self.assertEqual(code, 2, output)
        self.assertIn("fetch-depth: 0", output)

    # ------------------------------------------------------------ workflow ---

    def test_workflow_passes_the_secret_through_env_and_never_echoes_it(self):
        text = WORKFLOW.read_text(encoding="utf-8")

        self.assertEqual(
            re.findall(r"(?m)^\s*CUSTOMER_DENYLIST: \$\{\{ secrets\.CUSTOMER_DENYLIST \}\}$", text),
            ["          CUSTOMER_DENYLIST: ${{ secrets.CUSTOMER_DENYLIST }}"],
        )
        self.assertEqual(text.count("secrets.CUSTOMER_DENYLIST"), 1)
        self.assertNotIn("$CUSTOMER_DENYLIST", text)
        self.assertNotIn("set -x", text)
        self.assertNotIn("pull_request_target", text)
        self.assertIn("fetch-depth: 0", text)
        self.assertIn("runs-on: ubuntu-latest", text)
        self.assertRegex(text, r"(?m)^  pull_request:$")
        self.assertRegex(text, r"(?m)^  push:\n    branches: \[main\]$")
        self.assertIn("scripts/ci/customer_denylist.py --base", text)


if __name__ == "__main__":
    unittest.main()
