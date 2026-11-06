#!/usr/bin/env python3
"""
Poll a controlled-provider checkpoint until it reaches terminal state.

Usage:
  python wait_adapter_checkpoint.py --url <checkpoint_url> --timeout <seconds>

Exits 0 with JSON terminal state on stdout.
Exits 1 with bounded error code on timeout or error.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.request
import urllib.error

POLL_INTERVAL_S = 0.5


def poll_checkpoint(url: str, timeout_s: float) -> dict | None:
    deadline = time.monotonic() + timeout_s

    while time.monotonic() < deadline:
        try:
            req = urllib.request.Request(url, method="GET")
            with urllib.request.urlopen(req, timeout=5) as resp:
                data = json.loads(resp.read().decode())

                if (
                    data.get("phase") == "after_remote_commit"
                    and data.get("remote_effect_committed") is True
                    and data.get("response_received_by_adapter") is True
                    and data.get("worker_barrier_waiting") is True
                ):
                    return data
        except urllib.error.HTTPError as e:
            if e.code == 404:
                pass  # checkpoint not yet created
            else:
                print(
                    f"EC01: HTTP error {e.code}",
                    file=sys.stderr,
                )
                return None
        except Exception as e:
            print(f"EC02: {e}", file=sys.stderr)

        time.sleep(POLL_INTERVAL_S)

    return None


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Poll controlled-provider checkpoint"
    )
    parser.add_argument("--url", required=True, help="Checkpoint URL")
    parser.add_argument(
        "--timeout", type=float, default=60, help="Timeout in seconds"
    )
    args = parser.parse_args()

    result = poll_checkpoint(args.url, args.timeout)
    if result is None:
        print("EC03: checkpoint timeout", file=sys.stderr)
        return 1

    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    sys.exit(main())
