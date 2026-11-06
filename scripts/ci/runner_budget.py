#!/usr/bin/env python3
"""Choose CI runners before any paid runner starts; uncertainty selects GitHub.

This is an admission guard, not a substitute for a provider spending cap.
No credentials, raw API replies or subprocess errors are written to CI logs.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import tempfile
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path

GITHUB_RUNNER = "ubuntu-24.04"
BLACKSMITH_2 = "blacksmith-2vcpu-ubuntu-2404"
BLACKSMITH_4 = "blacksmith-4vcpu-ubuntu-2404"
FREE_MINUTES = Decimal(3000)
CUTOFF_MINUTES = Decimal(2400)
# Sum of all seven jobs' maximum 2-vCPU-equivalent timeouts is 165 minutes.
# Add provisioning/rounding headroom. Reserve whole runs, never average runtimes.
RUN_RESERVATION = Decimal(210)
CLI_URL = "https://clireleases.blacksmith.sh/cli/latest/linux/amd64/blacksmith"
CLI_SHA256 = "089a301ea5cc93a7560c7f1106911547fdfd46fc5c0cef7079e2a62a22e971ce"
ACTIVE_STATUSES = ("queued", "in_progress", "waiting", "pending", "requested")


class UnverifiedUsage(ValueError):
    """A response cannot safely authorize additional Blacksmith usage."""


@dataclass(frozen=True)
class Decision:
    provider: str
    reason: str
    used: Decimal | None = None
    reserved: Decimal = Decimal(0)

    def outputs(self) -> dict[str, str]:
        blacksmith = self.provider == "blacksmith"
        return {
            "provider": self.provider,
            "linux_2": BLACKSMITH_2 if blacksmith else GITHUB_RUNNER,
            "linux_4": BLACKSMITH_4 if blacksmith else GITHUB_RUNNER,
        }


def nonnegative(value: object) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, (str, int, float, Decimal)):
        raise UnverifiedUsage("invalid numeric usage")
    try:
        number = Decimal(str(value))
    except InvalidOperation as exc:
        raise UnverifiedUsage("invalid numeric usage") from exc
    if not number.is_finite() or number < 0:
        raise UnverifiedUsage("invalid numeric usage")
    return number


def decide(used: Decimal, other_runs: int, *, force_github: bool = False) -> Decision:
    if force_github:
        return Decision("github", "manual override")
    used = nonnegative(used)
    if isinstance(other_runs, bool) or not isinstance(other_runs, int) or other_runs < 0:
        raise UnverifiedUsage("invalid concurrent-run count")
    reserved = RUN_RESERVATION * (1 + other_runs)
    # Equality also falls back; never admit a run at the cutoff.
    if used + reserved >= CUTOFF_MINUTES:
        return Decision("github", "free-tier safety threshold", used, reserved)
    return Decision("blacksmith", "verified headroom for the whole CI run", used, reserved)


def month_start(now: datetime) -> datetime:
    return now.astimezone(timezone.utc).replace(day=1, hour=0, minute=0, second=0, microsecond=0)


def parse_usage(payload: object) -> Decimal:
    """Only accept an explicit aggregate of normalized vCPU billing minutes.

    Runtime minutes, per-repository rows, estimated spend and an absent value
    must not be mistaken for zero. Unknown CLI schemas deliberately fall back.
    """
    if not isinstance(payload, dict):
        raise UnverifiedUsage("unknown billing response")
    candidates = []
    for aggregate in (payload, payload.get("totals"), payload.get("summary")):
        if isinstance(aggregate, dict) and "billing_minutes" in aggregate:
            candidates.append(nonnegative(aggregate["billing_minutes"]))
    if len(candidates) != 1:
        raise UnverifiedUsage("missing or ambiguous billing aggregate")
    # CLI 0.5.3 help specifies that one 2-vCPU x64 minute is TWO billing
    # minutes. The free allowance and this guard use 2-vCPU equivalents.
    return candidates[0] / 2


def fetch_json(url: str, token: str) -> dict:
    request = urllib.request.Request(url, headers={
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "marketai-runner-budget",
    })
    with urllib.request.urlopen(request, timeout=15) as response:
        value = json.load(response)
    if not isinstance(value, dict):
        raise UnverifiedUsage("invalid GitHub response")
    return value


def concurrent_runs(repo: str, current_id: int, now: datetime, token: str, fetch=fetch_json,
                    *, current_attempt: int = 1) -> int:
    """Reserve active runs and recently finished runs (billing ingestion lag).

    Each API query is bounded. A truncated result cannot authorize Blacksmith.
    De-duplicate API listings by ID, but reserve every re-run attempt because
    several attempts can finish before billing ingestion. The current attempt
    is covered by the unconditional current-run reserve.
    """
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repo):
        raise UnverifiedUsage("invalid repository")
    if isinstance(current_attempt, bool) or not isinstance(current_attempt, int) or current_attempt < 1:
        raise UnverifiedUsage("invalid workflow attempt")
    root = f"https://api.github.com/repos/{repo}/actions/workflows/ci.yml/runs"
    queries = [{"status": status} for status in ACTIVE_STATUSES]
    queries.append({"created": ">=" + (now - timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M:%SZ")})
    reservations = {current_id: current_attempt - 1}
    for query in queries:
        url = root + "?" + urllib.parse.urlencode({**query, "per_page": 100})
        response = fetch(url, token)
        runs = response.get("workflow_runs")
        count = response.get("total_count")
        if (not isinstance(runs, list) or isinstance(count, bool)
                or not isinstance(count, int) or count != len(runs)):
            raise UnverifiedUsage("incomplete GitHub run listing")
        for run in runs:
            if (not isinstance(run, dict) or isinstance(run.get("id"), bool)
                    or not isinstance(run.get("id"), int)):
                raise UnverifiedUsage("invalid GitHub run")
            attempt = run.get("run_attempt")
            if isinstance(attempt, bool) or not isinstance(attempt, int) or attempt < 1:
                raise UnverifiedUsage("invalid workflow attempt")
            # Recent completions can still be missing from Blacksmith billing.
            # Double-reserving a completed or GitHub-hosted run is conservative.
            count = attempt - 1 if run["id"] == current_id else attempt
            reservations[run["id"]] = max(reservations.get(run["id"], 0), count)
    return sum(reservations.values())


def blacksmith_usage(token: str, organization: str, now: datetime) -> Decimal:
    """Use the official CLI, pinned by digest, with an ephemeral CI credential."""
    env = {key: value for key, value in os.environ.items()
           if key not in ("GH_TOKEN", "BLACKSMITH_USAGE_TOKEN")}
    env["BLACKSMITH_DISABLE_AUTO_UPDATE"] = "1"
    with tempfile.TemporaryDirectory(prefix="blacksmith-budget-") as directory:
        binary = Path(directory) / "blacksmith"
        subprocess.run([
            "curl", "-fLsS", "--proto", "=https", "--connect-timeout", "5",
            "--max-time", "25", "--max-filesize", str(40 * 1024 * 1024),
            CLI_URL, "-o", str(binary),
        ], capture_output=True, check=True, timeout=30)
        content = binary.read_bytes()
        if hashlib.sha256(content).hexdigest() != CLI_SHA256:
            raise UnverifiedUsage("CLI checksum changed")
        binary.write_bytes(content)
        binary.chmod(0o700)
        # A GitHub-hosted job is ephemeral. No human browser credentials are used.
        subprocess.run([
            str(binary), "auth", "login", "--api-token", "-", "--non-interactive",
            "--organization", organization,
        ], input=token, text=True, capture_output=True, check=True, timeout=30, env=env)
        result = subprocess.run([
            str(binary), "usage", "--org", organization,
            "--start-time", month_start(now).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "--end-time", now.strftime("%Y-%m-%dT%H:%M:%SZ"), "--format", "json",
        ], text=True, capture_output=True, check=True, timeout=30, env=env)
        return parse_usage(json.loads(result.stdout))


def select(env: dict[str, str], now: datetime, usage=blacksmith_usage, runs=concurrent_runs,
           clock=lambda: datetime.now(timezone.utc)) -> Decision:
    override = env.get("BLACKSMITH_FORCE_GITHUB", "").strip().lower()
    if override not in ("", "false", "0"):
        return Decision("github", "manual override")
    token = env.get("BLACKSMITH_USAGE_TOKEN", "")
    if not token:
        return Decision("github", "Blacksmith usage credential is not configured")
    # No cost-bearing decision is based on a local wall clock with no timezone.
    if now.tzinfo is None:
        return Decision("github", "invalid billing clock")
    try:
        organization, _ = env["GITHUB_REPOSITORY"].split("/", 1)
        used = usage(token, organization, now)
        others = runs(env["GITHUB_REPOSITORY"], int(env["GITHUB_RUN_ID"]), now, env["GH_TOKEN"],
                      current_attempt=int(env.get("GITHUB_RUN_ATTEMPT", "1")))
        # Do not carry a previous month's read across midnight.
        if month_start(clock()) != month_start(now):
            return Decision("github", "billing month changed during the check")
        return decide(used, others)
    except Exception:
        # Errors may contain credentials or signed URLs. Only this constant reason
        # reaches logs. Missing/invalid/partial data must never be treated as zero.
        return Decision("github", "usage verification unavailable; safe fallback")


def publish(decision: Decision, env: dict[str, str], now: datetime) -> None:
    outputs = "".join(f"{key}={value}\n" for key, value in decision.outputs().items())
    if env.get("GITHUB_OUTPUT"):
        with open(env["GITHUB_OUTPUT"], "a", encoding="utf-8") as handle:
            handle.write(outputs)
    summary = (
        "### CI runner budget\n\n"
        f"- Provider: **{decision.provider}**\n"
        f"- Reason: {decision.reason}\n"
        f"- Billing month (UTC): {month_start(now):%Y-%m}\n"
        f"- Blacksmith cutoff: {CUTOFF_MINUTES}/{FREE_MINUTES} equivalent minutes (80%).\n"
    )
    if decision.used is not None:
        summary += f"- Reported usage: {decision.used}; whole-run reservations: {decision.reserved}.\n"
    if env.get("GITHUB_STEP_SUMMARY"):
        with open(env["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as handle:
            handle.write(summary)
    print(summary)


if __name__ == "__main__":
    current = datetime.now(timezone.utc)
    publish(select(dict(os.environ), current), dict(os.environ), current)
