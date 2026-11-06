"""Two draft Experiment commands that read the same revision before either writes (#340).

Each command runs on its own thread, so on its own database connection. A
barrier inside ``_check_revision`` holds both until each has read and
accepted ``expected_revision``; only then do they race to take the next
revision. Exactly one may win.
"""
from __future__ import annotations

import threading
from typing import Any, Callable
from unittest.mock import patch

from app.experiments import service


def race_on_one_revision(*commands: Callable[[], Any], timeout: float = 20.0) -> list[tuple[str, Any]]:
    """Run ``commands`` at once; each result is ``("ok", value)`` or ``("error", exc)``."""
    barrier = threading.Barrier(len(commands), timeout=timeout)
    check_revision = service._check_revision
    results: list[tuple[str, Any] | None] = [None] * len(commands)

    def check(experiment, expected):
        check_revision(experiment, expected)
        barrier.wait()

    def run(index: int, command: Callable[[], Any]) -> None:
        try:
            results[index] = ("ok", command())
        except Exception as exc:  # noqa: BLE001 - the caller asserts on it
            results[index] = ("error", exc)

    with patch.object(service, "_check_revision", check):
        threads = [threading.Thread(target=run, args=(index, command)) for index, command in enumerate(commands)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout * 2)
    if any(thread.is_alive() for thread in threads):
        raise AssertionError("a racing draft command never finished")
    return [result for result in results if result is not None]
