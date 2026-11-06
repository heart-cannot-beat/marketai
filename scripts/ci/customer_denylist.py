#!/usr/bin/env python3
"""
Block customer names from entering the repository.

The term list is read from the CUSTOMER_DENYLIST environment variable (the
GitHub secret of the same name): one regular expression per line, matched
case-insensitively. It is never stored in the repository.

Default mode scans the lines added between --base and --head (merge-base to
head, as a pull request shows them) plus the names of added or renamed files.
--all scans every tracked file in the working tree, for example before a
public snapshot is exported.

A hit is reported as `file:line` and the rule number only. The matched text,
the line and the rule itself are never printed, and any part of a printed path
that matches a rule is masked. Binary files are skipped and counted.

Exit codes: 0 clean or no list configured, 1 hits, 2 configuration or git
error (fail closed).
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

ENV_VAR = "CUSTOMER_DENYLIST"
PREFIX = "customer-denylist:"
MASK = "***"
# Same heuristic git uses: a NUL byte in the first 8000 bytes means binary.
BINARY_SNIFF_BYTES = 8000

Rules = list[tuple[int, re.Pattern[str]]]


class DenylistError(Exception):
    """A failure whose message is safe to print in a public log."""


def load_rules(raw: str | None) -> Rules:
    rules: Rules = []
    lines = [line.strip() for line in (raw or "").splitlines()]
    for number, pattern in enumerate((line for line in lines if line), start=1):
        try:
            rule = re.compile(pattern, re.IGNORECASE)
        except re.error:
            raise DenylistError(f"规则 #{number} 不是合法的正则表达式，请修正 secret {ENV_VAR}。") from None
        if rule.search(""):
            raise DenylistError(f"规则 #{number} 能匹配空字符串，会命中每一行，请修正 secret {ENV_VAR}。")
        rules.append((number, rule))
    return rules


def matching_rules(text: str, rules: Rules) -> list[int]:
    return [number for number, rule in rules if rule.search(text)]


def mask(text: str, rules: Rules) -> str:
    """Replace every rule match in `text` (a path) so the term is not printed."""
    spans = []
    for _, rule in rules:
        for match in rule.finditer(text):
            start, end = match.span()
            # Keep separators such as `/` or `-` caught by boundary prefixes.
            while start < end and not text[start].isalnum():
                start += 1
            while end > start and not text[end - 1].isalnum():
                end -= 1
            if start < end:
                spans.append((start, end))
    merged: list[list[int]] = []
    for start, end in sorted(spans):
        if merged and start <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    for start, end in reversed(merged):
        text = text[:start] + MASK + text[end:]
    return text


def is_binary(data: bytes) -> bool:
    return b"\0" in data[:BINARY_SNIFF_BYTES]


def decode(data: bytes) -> str:
    return data.decode("utf-8", errors="replace")


@dataclass
class Report:
    rules: Rules
    text_files: int = 0
    binary_files: int = 0
    hits: list[str] = field(default_factory=list)

    def check_name(self, path: str) -> None:
        name = printable(path)
        numbers = matching_rules(name, self.rules)
        if numbers:
            self.hits.append(f"{mask(name, self.rules)}: 文件名命中规则 {format_numbers(numbers)}")

    def check_lines(self, path: str, lines) -> None:
        """`lines` yields (line_number, text) pairs of one text file."""
        self.text_files += 1
        shown = None
        for line_number, text in lines:
            numbers = matching_rules(text, self.rules)
            if numbers:
                shown = shown or mask(printable(path), self.rules)
                self.hits.append(f"{shown}:{line_number}: 命中规则 {format_numbers(numbers)}")


def printable(path: str) -> str:
    """Paths come from git as raw bytes; undecodable bytes become U+FFFD."""
    return decode(os.fsencode(path))


def format_numbers(numbers: list[int]) -> str:
    return ", ".join(f"#{number}" for number in numbers)


def numbered(data: bytes):
    # Split on "\n" only so numbering matches git and editors.
    return enumerate(decode(data).split("\n"), start=1)


def git(root: Path, *args: str, error: str) -> bytes:
    result = subprocess.run(
        ["git", "-c", "core.quotePath=false", "--literal-pathspecs", *args],
        cwd=root, capture_output=True, stdin=subprocess.DEVNULL,
    )
    if result.returncode != 0:
        # git's stderr may name paths, so only the caller's message is printed.
        raise DenylistError(error)
    return result.stdout


def split_z(data: bytes) -> list[str]:
    return [os.fsdecode(item) for item in data.split(b"\0") if item]


def scan_all(root: Path, report: Report) -> str:
    for path in split_z(git(root, "ls-files", "-z", error="git ls-files 失败。")):
        target = root / path
        if target.is_symlink():
            data = os.fsencode(os.readlink(target))
        elif target.is_file():
            data = target.read_bytes()
        else:
            continue  # deleted in the working tree or a submodule
        report.check_name(path)
        if is_binary(data):
            report.binary_files += 1
        else:
            report.check_lines(path, numbered(data))
    return "工作树中所有 git 跟踪的文件"


def resolve_commit(root: Path, ref: str, label: str) -> str:
    return decode(git(
        root, "rev-parse", "--verify", "--end-of-options", f"{ref}^{{commit}}",
        error=f"无法解析 {label} 提交 {ref}；checkout 需要 fetch-depth: 0。",
    )).strip()


def added_lines(root: Path, base: str, head: str, paths: list[str]):
    """Yield (head_line_number, text) for lines `head` adds to `paths`."""
    patch = git(
        root, "diff", "-U0", "--text", "--no-color", "--no-ext-diff", "--no-textconv",
        "-M", base, head, "--", *paths, error="git diff 失败。",
    )
    in_hunks = False
    line_number = 0
    for raw in patch.split(b"\n"):
        if raw.startswith(b"diff --git "):
            # A type change or an undetected rename arrives as a second
            # section whose `+++` header must not count as an added line.
            in_hunks = False
        elif raw.startswith(b"@@"):
            in_hunks = True
            match = re.match(rb"@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@", raw)
            if not match:
                raise DenylistError("无法解析 git diff 输出。")
            line_number = int(match.group(1))
        elif in_hunks and raw.startswith(b"+"):
            yield line_number, decode(raw[1:])
            line_number += 1


def scan_diff(root: Path, base_ref: str, head_ref: str, report: Report) -> str:
    head = resolve_commit(root, head_ref, "head")
    if re.fullmatch(r"0+", base_ref):
        # The first push of a branch: everything in head is new.
        base = decode(git(root, "hash-object", "-t", "tree", "--stdin", error="git hash-object 失败。")).strip()
    else:
        base = decode(git(
            root, "merge-base", resolve_commit(root, base_ref, "base"), head,
            error=f"找不到 {base_ref} 与 {head_ref} 的共同祖先；checkout 需要 fetch-depth: 0。",
        )).strip()

    changes = split_z(git(
        root, "diff", "--name-status", "-z", "-M", "--diff-filter=AMRT", base, head,
        error="git diff --name-status 失败。",
    ))
    index = 0
    while index < len(changes):
        status = changes[index]
        if status.startswith(("R", "C")):
            old, new = changes[index + 1], changes[index + 2]
            index += 3
        else:
            old = new = changes[index + 1]
            index += 2
        if status[0] in "AR":
            report.check_name(new)
        data = git(root, "cat-file", "blob", f"{head}:{new}", error="git cat-file 失败。")
        if is_binary(data):
            report.binary_files += 1
        elif status[0] == "A":
            report.check_lines(new, numbered(data))
        else:
            report.check_lines(new, added_lines(root, base, head, sorted({old, new})))
    return f"{base[:12]}..{head[:12]} 新增的行和新增或改名的文件名"


def run(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0].strip())
    parser.add_argument("--all", action="store_true", help="scan every tracked file in the working tree")
    parser.add_argument("--base", default="origin/main", help="base commit for the default mode (default: origin/main)")
    parser.add_argument("--head", default="HEAD", help="head commit for the default mode (default: HEAD)")
    parser.add_argument(
        "--require-denylist", action="store_true",
        help=f"fail instead of skipping when {ENV_VAR} is empty",
    )
    args = parser.parse_args(argv)

    rules = load_rules(os.environ.get(ENV_VAR))
    if not rules:
        if args.require_denylist:
            print(f"{PREFIX} 未配置词表（环境变量 {ENV_VAR} 为空），但本次运行要求词表（--require-denylist），按失败处理。", file=sys.stderr)
            return 2
        print(f"{PREFIX} 未配置词表（环境变量 {ENV_VAR} 为空），跳过检查。")
        return 0

    root = Path(decode(git(Path.cwd(), "rev-parse", "--show-toplevel", error="当前目录不在 git 仓库中。")).strip())
    report = Report(rules)
    scope = scan_all(root, report) if args.all else scan_diff(root, args.base, args.head, report)

    for hit in report.hits:
        print(hit)
    print(
        f"{PREFIX} 范围：{scope}；共 {len(rules)} 条规则；扫描 {report.text_files} 个文本文件，"
        f"跳过 {report.binary_files} 个二进制文件，命中 {len(report.hits)} 处。"
    )
    if report.hits:
        print(
            f"{PREFIX} 发现客户词表命中。请删除或替换为虚构品牌（见 CLAUDE.md 的公开仓库规则）；"
            "输出只给出位置和规则序号，不会显示命中内容。",
            file=sys.stderr,
        )
        return 1
    return 0


def main(argv: list[str] | None = None) -> int:
    try:
        return run(sys.argv[1:] if argv is None else argv)
    except DenylistError as exc:
        print(f"{PREFIX} {exc}", file=sys.stderr)
        return 2
    except Exception as exc:  # noqa: BLE001 - a traceback could quote matched text
        print(f"{PREFIX} 内部错误（{type(exc).__name__}），为避免泄露不输出详情。", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
