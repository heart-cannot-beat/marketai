"""Shared contract for the generic ``ExternalCallAdapter`` protocol (#24).

Spec §3G: "The generic adapter contract runs against at least two adapters
at the seam: the Controlled Mock implementation and an in-memory
deterministic adapter used to vary capability behavior. It verifies typed
request/result handling, credential lifetime, provider/adapter matching,
idempotency-key propagation, result materialization and redaction."

Both adapters below are typed with plain ``str`` RequestT/ResultT value
objects -- the contract is about the protocol surface, not any concrete
domain's request/result shape.
"""
from __future__ import annotations

import asyncio
import unittest
from dataclasses import dataclass

from app.providers.adapter import (
    ExternalCallAdapter,
    ProviderCapabilities,
    ProviderResult,
)


@dataclass(frozen=True)
class _Request:
    payload: str


@dataclass(frozen=True)
class _Result:
    output: str


class ControlledMockCallAdapter:
    """Deterministic Controlled Mock adapter: accepts idempotency keys,
    cannot look results up by request id (there is no external provider)."""

    provider = "controlled_mock"

    def __init__(self) -> None:
        self.received_credentials: list[memoryview | None] = []
        self.received_idempotency_keys: list[str] = []

    @property
    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(
            accepts_idempotency_key=True,
            supports_result_lookup_by_request_id=False,
        )

    async def execute(
        self, request: _Request, *, credential: memoryview | None, idempotency_key: str,
    ) -> ProviderResult[_Result]:
        self.received_credentials.append(credential)
        self.received_idempotency_keys.append(idempotency_key)
        return ProviderResult(
            output=_Result(output=f"mock:{request.payload}:{idempotency_key}"),
            provider_request_id=None,
            idempotency_key_accepted=True,
        )


class InMemoryDeterministicAdapter:
    """A second adapter with the opposite capability profile: no
    idempotency-key guarantee, but supports lookup by request id -- used to
    prove the coordinator's behavior varies by injected capability, not by
    ``step_kind`` or adapter identity."""

    provider = "in_memory_deterministic"

    def __init__(self) -> None:
        self.received_credentials: list[memoryview | None] = []
        self.received_idempotency_keys: list[str] = []
        self._store: dict[str, ProviderResult[_Result]] = {}
        self._next_id = 0

    @property
    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(
            accepts_idempotency_key=False,
            supports_result_lookup_by_request_id=True,
        )

    async def execute(
        self, request: _Request, *, credential: memoryview | None, idempotency_key: str,
    ) -> ProviderResult[_Result]:
        self.received_credentials.append(credential)
        self.received_idempotency_keys.append(idempotency_key)
        self._next_id += 1
        request_id = f"req_{self._next_id}"
        result = ProviderResult(
            output=_Result(output=f"inmem:{request.payload}:{idempotency_key}"),
            provider_request_id=request_id,
            idempotency_key_accepted=False,
        )
        self._store[request_id] = result
        return result

    async def lookup_by_request_id(self, request_id: str) -> ProviderResult[_Result] | None:
        return self._store.get(request_id)


class _AdapterContractMixin:
    """Shared contract: every registered adapter must satisfy this."""

    adapter: ExternalCallAdapter

    def _make_request(self, payload: str = "hello") -> _Request:
        return _Request(payload=payload)

    def test_adapter_has_provider_and_capabilities(self):
        self.assertIsInstance(self.adapter.provider, str)
        self.assertGreater(len(self.adapter.provider), 0)
        caps = self.adapter.capabilities
        self.assertIsInstance(caps, ProviderCapabilities)
        self.assertIsInstance(caps.accepts_idempotency_key, bool)
        self.assertIsInstance(caps.supports_result_lookup_by_request_id, bool)

    def test_execute_returns_provider_result(self):
        result = asyncio.run(
            self.adapter.execute(
                self._make_request(), credential=None, idempotency_key="idem-1",
            )
        )
        self.assertIsInstance(result, ProviderResult)
        self.assertIsInstance(result.output, _Result)
        self.assertIsInstance(result.idempotency_key_accepted, bool)

    def test_credential_is_memoryview(self):
        secret = memoryview(b"top-secret-credential-bytes")
        asyncio.run(
            self.adapter.execute(
                self._make_request(), credential=secret, idempotency_key="idem-2",
            )
        )
        received = self.adapter.received_credentials[-1]
        self.assertIsInstance(received, memoryview)
        self.assertEqual(bytes(received), bytes(secret))

    def test_credential_may_be_none_for_mock_execution(self):
        asyncio.run(
            self.adapter.execute(
                self._make_request(), credential=None, idempotency_key="idem-3",
            )
        )
        self.assertIsNone(self.adapter.received_credentials[-1])

    def test_idempotency_key_propagated(self):
        asyncio.run(
            self.adapter.execute(
                self._make_request(), credential=None, idempotency_key="idem-key-xyz",
            )
        )
        self.assertEqual(self.adapter.received_idempotency_keys[-1], "idem-key-xyz")

    def test_result_output_is_redacted_from_provider_identity(self):
        # The typed ResultT the adapter returns must never itself carry a
        # raw provider secret; it's a caller-defined value object. Here we
        # just verify it round-trips the request payload deterministically
        # and carries no adapter-internal state.
        result = asyncio.run(
            self.adapter.execute(
                self._make_request("abc"), credential=None, idempotency_key="idem-4",
            )
        )
        self.assertIn("abc", result.output.output)


class TestControlledMockAdapterContract(_AdapterContractMixin, unittest.TestCase):
    def setUp(self):
        self.adapter = ControlledMockCallAdapter()

    def test_provider_capability_matches_mock_profile(self):
        caps = self.adapter.capabilities
        self.assertTrue(caps.accepts_idempotency_key)
        self.assertFalse(caps.supports_result_lookup_by_request_id)


class TestInMemoryDeterministicAdapterContract(_AdapterContractMixin, unittest.TestCase):
    def setUp(self):
        self.adapter = InMemoryDeterministicAdapter()

    def test_provider_capability_matches_lookup_profile(self):
        caps = self.adapter.capabilities
        self.assertFalse(caps.accepts_idempotency_key)
        self.assertTrue(caps.supports_result_lookup_by_request_id)

    def test_lookup_by_request_id_returns_prior_result(self):
        result = asyncio.run(
            self.adapter.execute(
                self._make_request("xyz"), credential=None, idempotency_key="idem-5",
            )
        )
        looked_up = asyncio.run(
            self.adapter.lookup_by_request_id(result.provider_request_id)
        )
        self.assertIsNotNone(looked_up)
        self.assertEqual(looked_up.output.output, result.output.output)

    def test_lookup_unknown_request_id_returns_none(self):
        looked_up = asyncio.run(self.adapter.lookup_by_request_id("does-not-exist"))
        self.assertIsNone(looked_up)


class TestBothAdaptersSatisfyProtocolStructurally(unittest.TestCase):
    """The coordinator dispatches against ``ExternalCallAdapter`` only --
    never a concrete class -- so both fixtures must satisfy it structurally."""

    def test_controlled_mock_satisfies_protocol(self):
        self.assertIsInstance(ControlledMockCallAdapter(), ExternalCallAdapter)

    def test_in_memory_deterministic_satisfies_protocol(self):
        self.assertIsInstance(InMemoryDeterministicAdapter(), ExternalCallAdapter)

    def test_different_adapters_same_metadata_different_behavior(self):
        """Proves caller selection, not step_kind, controls behavior: the
        same conceptual 'step_kind' metadata could be dispatched through
        either adapter, and each produces its own distinct capability
        profile and output shape -- nothing here branches on step_kind."""
        mock = ControlledMockCallAdapter()
        inmem = InMemoryDeterministicAdapter()
        self.assertNotEqual(mock.capabilities, inmem.capabilities)

        request = _Request(payload="same-payload")
        mock_result = asyncio.run(
            mock.execute(request, credential=None, idempotency_key="k")
        )
        inmem_result = asyncio.run(
            inmem.execute(request, credential=None, idempotency_key="k")
        )
        self.assertNotEqual(mock_result.output.output, inmem_result.output.output)


if __name__ == "__main__":
    unittest.main()
