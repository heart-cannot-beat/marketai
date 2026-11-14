"""Safety gate for the mutating Localization browser-test seed."""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest


SCRIPT = (
    Path(__file__).resolve().parents[1]
    / "web"
    / "e2e"
    / "localization"
    / "seed_prerequisites.py"
)
SPEC = importlib.util.spec_from_file_location(
    "localization_e2e_seed_prerequisites", SCRIPT
)
assert SPEC is not None and SPEC.loader is not None
seed = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(seed)


def _environment(
    database_url: str,
    *,
    control_database_url: str = (
        "postgresql://marketai_control_owner:not-logged@localhost/marketai_control"
    ),
    opt_in: str = "1",
) -> dict[str, str]:
    return {
        "E2E_LOCALIZATION_ALLOW_MUTATIONS": opt_in,
        "E2E_LOCALIZATION_CONTROL_OWNER_DATABASE_URL": control_database_url,
        "E2E_LOCALIZATION_WORKSPACE_ID": "00000000-0000-4000-8000-000000000001",
        "E2E_MEMBER_EMAIL": "member@example.test",
        "E2E_OWNER_EMAIL": "owner@example.test",
        "E2E_OWNER_USER_ID": "00000000-0000-4000-8000-000000000002",
        "MARKETAI_DATABASE_URL": database_url,
        "MARKETAI_TENANT_CONTEXT_SECRET": "test-only-context-secret",
    }


@pytest.mark.parametrize(
    "host",
    ["localhost", "127.0.0.1", "[::1]", "postgres"],
    ids=["localhost", "ipv4-loopback", "ipv6-loopback", "compose-service"],
)
def test_mutation_preflight_accepts_only_listed_local_targets(
    monkeypatch: pytest.MonkeyPatch,
    host: str,
) -> None:
    environment = _environment(
        f"postgresql+psycopg://owner:not-logged@{host}:5432/marketai_domain",
        control_database_url=(
            f"postgresql://marketai_control_owner:not-logged@{host}:5432/marketai_control"
        ),
    )
    for name, value in environment.items():
        monkeypatch.setenv(name, value)

    assert seed._required_environment() == environment


@pytest.mark.parametrize(
    ("target", "database_url"),
    [
        (
            "MARKETAI_DATABASE_URL",
            "postgresql+psycopg://owner:secret@database.example.com/marketai_domain",
        ),
        (
            "E2E_LOCALIZATION_CONTROL_OWNER_DATABASE_URL",
            "postgresql://owner:secret@10.0.0.8/marketai_control",
        ),
        ("MARKETAI_DATABASE_URL", "sqlite:////tmp/marketai_domain.db"),
        (
            "E2E_LOCALIZATION_CONTROL_OWNER_DATABASE_URL",
            "postgresql+psycopg:///marketai_control",
        ),
        (
            "MARKETAI_DATABASE_URL",
            "postgresql://marketai_domain_api:secret@localhost/marketai_domain",
        ),
        (
            "E2E_LOCALIZATION_CONTROL_OWNER_DATABASE_URL",
            "postgresql://marketai_control_app:secret@localhost/marketai_control",
        ),
        (
            "MARKETAI_DATABASE_URL",
            "postgresql://owner:secret@localhost/not_marketai_domain",
        ),
        (
            "E2E_LOCALIZATION_CONTROL_OWNER_DATABASE_URL",
            "postgresql://owner:secret@localhost/not_marketai_control",
        ),
    ],
)
def test_mutation_preflight_rejects_remote_or_non_postgres_targets(
    monkeypatch: pytest.MonkeyPatch,
    target: str,
    database_url: str,
) -> None:
    environment = _environment(
        "postgresql://owner:not-logged@localhost/marketai_domain"
    )
    environment[target] = database_url
    for name, value in environment.items():
        monkeypatch.setenv(name, value)

    with pytest.raises(RuntimeError) as captured:
        seed._required_environment()

    message = str(captured.value)
    assert "mutation preflight failed" in message
    assert "secret" not in message


@pytest.mark.parametrize("opt_in", ["", "true", "yes", "0"])
def test_mutation_preflight_requires_exact_explicit_opt_in(
    monkeypatch: pytest.MonkeyPatch,
    opt_in: str,
) -> None:
    for name, value in _environment(
        "postgresql://owner:not-logged@localhost/marketai_domain", opt_in=opt_in
    ).items():
        monkeypatch.setenv(name, value)
    if not opt_in:
        monkeypatch.delenv("E2E_LOCALIZATION_ALLOW_MUTATIONS", raising=False)

    with pytest.raises(RuntimeError) as captured:
        seed._required_environment()

    assert "E2E_LOCALIZATION_ALLOW_MUTATIONS" in str(captured.value)


def test_mutation_preflight_reports_every_missing_value_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for name in seed.REQUIRED_ENVIRONMENT:
        monkeypatch.delenv(name, raising=False)

    with pytest.raises(RuntimeError) as captured:
        seed._required_environment()

    message = str(captured.value)
    for name in seed.REQUIRED_ENVIRONMENT:
        assert message.count(name) == 1


def test_foreign_workspace_fixture_is_distinct_from_test_actors() -> None:
    assert seed.FOREIGN_WORKSPACE_ID != _environment(
        "postgresql://owner:not-logged@localhost/marketai_domain"
    )["E2E_LOCALIZATION_WORKSPACE_ID"]
    assert seed.FOREIGN_OWNER_USER_ID != _environment(
        "postgresql://owner:not-logged@localhost/marketai_domain"
    )["E2E_OWNER_USER_ID"]
    assert seed.FOREIGN_OWNER_EMAIL not in {
        "owner@example.test",
        "member@example.test",
    }
    assert seed.FOREIGN_OWNER_MEMBER_ID not in {
        "80808080-8080-4080-8080-808080800004",
        "80808080-8080-4080-8080-808080800005",
    }


def test_plain_postgresql_url_uses_installed_psycopg_driver() -> None:
    url = "postgresql://owner:not-logged@localhost/marketai_control"

    assert seed._sqlalchemy_postgres_url(url) == (
        "postgresql+psycopg://owner:not-logged@localhost/marketai_control"
    )


@pytest.mark.parametrize(
    "version_id",
    [
        "",
        "version_123",
        "lv_",
        "libver_",
        "lv_has space",
        "lv_../other",
        "libver_has space",
        "libver_../other",
        "libver;rm",
        "lv_" + "a" * 62,
        "libver_" + "a" * 58,
    ],
)
def test_withdraw_cli_rejects_unsafe_or_unbounded_version_ids(
    version_id: str,
) -> None:
    with pytest.raises(argparse.ArgumentTypeError):
        seed._library_version_id(version_id)


@pytest.mark.parametrize(
    "version_id",
    [
        # What the Library mints (``new_id("libver")``) and so what the
        # seed hands to ``withdraw-version`` (#172).
        "libver_aaead1ca7a6b4727aaba0398a1e8a7ff",
        "libver_" + "a" * 57,
        # The prefix hand-built test fixtures use.
        "lv_1234567890abcdef1234567890abcdef",
        "lv_" + "a" * 61,
    ],
)
def test_withdraw_cli_accepts_library_version_ids(version_id: str) -> None:
    assert seed._library_version_id(f"  {version_id} ") == version_id


def test_withdraw_cli_parses_a_library_minted_version_id() -> None:
    version_id = "libver_aaead1ca7a6b4727aaba0398a1e8a7ff"

    arguments = seed._arguments(["withdraw-version", "--version-id", version_id])

    assert arguments.command == "withdraw-version"
    assert arguments.version_id == version_id


def test_withdraw_helper_targets_exact_workspace_truth_version() -> None:
    workspace_id = "00000000-0000-4000-8000-000000000001"
    version_id = "lv_1234567890abcdef1234567890abcdef"
    context = SimpleNamespace(workspace_id=workspace_id)
    version = SimpleNamespace(
        id=version_id,
        workspace_id=workspace_id,
        entry_type="truth_resolution_policy",
        state="effective",
        lifecycle_revision=1,
    )
    command_calls: list[tuple[object, str, str]] = []

    @contextmanager
    def session_factory(actual_context):
        assert actual_context is context
        yield SimpleNamespace(get=lambda _model, exact_id: version if exact_id == version_id else None)

    def withdraw_command(actual_context, exact_id: str, *, reason: str) -> None:
        command_calls.append((actual_context, exact_id, reason))
        version.state = "withdrawn"
        version.lifecycle_revision += 1

    result = seed._withdraw_truth_policy_version(
        context,
        version_id,
        session_factory=session_factory,
        version_model=object,
        withdraw_command=withdraw_command,
    )

    assert command_calls == [(context, version_id, seed.WITHDRAW_REASON)]
    assert result == {
        "action": "withdraw_truth_policy_version",
        "entryType": "truth_resolution_policy",
        "lifecycleRevision": 2,
        "previousState": "effective",
        "state": "withdrawn",
        "versionId": version_id,
        "workspaceId": workspace_id,
    }


@pytest.mark.parametrize(
    ("workspace_id", "entry_type"),
    [
        ("00000000-0000-4000-8000-000000000099", "truth_resolution_policy"),
        ("00000000-0000-4000-8000-000000000001", "data_source"),
    ],
)
def test_withdraw_helper_rejects_foreign_or_non_truth_version(
    workspace_id: str,
    entry_type: str,
) -> None:
    expected_workspace_id = "00000000-0000-4000-8000-000000000001"
    context = SimpleNamespace(workspace_id=expected_workspace_id)
    version = SimpleNamespace(
        id="lv_1234567890abcdef1234567890abcdef",
        workspace_id=workspace_id,
        entry_type=entry_type,
        state="effective",
    )

    @contextmanager
    def session_factory(_context):
        yield SimpleNamespace(get=lambda _model, _version_id: version)

    with pytest.raises(RuntimeError):
        seed._withdraw_truth_policy_version(
            context,
            version.id,
            session_factory=session_factory,
            version_model=object,
            withdraw_command=lambda *_args, **_kwargs: pytest.fail(
                "withdraw command must not run"
            ),
        )
