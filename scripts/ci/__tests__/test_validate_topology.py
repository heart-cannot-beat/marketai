"""Tests for the golden-path topology validator."""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

import pytest

# Allow importing from parent directory
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from validate_topology import (
    ValidationResult,
    build_attestation,
    sign_attestation,
    validate_config,
    validate_live,
    verify_attestation,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _base_config(**overrides: object) -> dict:
    """Minimal valid compose config."""
    config: dict = {
        "services": {
            "web": {
                "image": "web:latest",
                "networks": {"gp-net": {}},
            },
            "api": {
                "image": "api:latest",
                "networks": {"gp-net": {}},
            },
        },
        "networks": {
            "gp-net": {"internal": True},
        },
    }
    for k, v in overrides.items():
        config[k] = v
    return config


def _base_container(
    name: str = "web",
    **overrides: object,
) -> dict:
    """Minimal valid container inspect result."""
    container: dict = {
        "Name": f"/{name}",
        "Config": {
            "Labels": {"com.docker.compose.service": name},
        },
        "HostConfig": {
            "NetworkMode": "gp-net",
            "Privileged": False,
            "PortBindings": {},
        },
        "NetworkSettings": {
            "Networks": {
                "gp-net": {"NetworkID": "abc123"},
            },
        },
        "Mounts": [],
        "State": {"StartedAt": "2026-01-01T00:00:00Z"},
    }
    for k, v in overrides.items():
        if isinstance(v, dict) and k in container and isinstance(container[k], dict):
            container[k] = {**container[k], **v}
        else:
            container[k] = v
    return container


# ---------------------------------------------------------------------------
# Pass 1 — config validation
# ---------------------------------------------------------------------------


class TestConfigValidation:
    def test_valid_config_passes(self) -> None:
        result = validate_config(_base_config())
        assert result.ok
        assert "web" in result.services_checked
        assert result.network_hash != ""

    def test_rejects_network_mode_host(self) -> None:
        config = _base_config()
        config["services"]["web"]["network_mode"] = "host"
        result = validate_config(config)
        assert not result.ok
        assert any(v.rule == "network-mode-host" for v in result.violations)

    def test_rejects_published_port_dict(self) -> None:
        config = _base_config()
        config["services"]["web"]["ports"] = [
            {"published": 8080, "target": 80}
        ]
        result = validate_config(config)
        assert not result.ok
        assert any(v.rule == "published-port" for v in result.violations)

    def test_rejects_published_port_string(self) -> None:
        config = _base_config()
        config["services"]["web"]["ports"] = ["8080:80"]
        result = validate_config(config)
        assert not result.ok
        assert any(v.rule == "published-port" for v in result.violations)

    def test_rejects_multi_network(self) -> None:
        config = _base_config()
        config["services"]["web"]["networks"] = {
            "gp-net": {},
            "external-net": {},
        }
        result = validate_config(config)
        assert not result.ok
        assert any(v.rule == "multi-network" for v in result.violations)

    def test_rejects_non_internal_network(self) -> None:
        config = _base_config()
        config["networks"]["gp-net"]["internal"] = False
        result = validate_config(config)
        assert not result.ok
        assert any(v.rule == "network-not-internal" for v in result.violations)

    def test_rejects_docker_socket_mount(self) -> None:
        config = _base_config()
        config["services"]["web"]["volumes"] = [
            {
                "type": "bind",
                "source": "/var/run/docker.sock",
                "target": "/var/run/docker.sock",
            }
        ]
        result = validate_config(config)
        assert not result.ok
        assert any(v.rule == "docker-socket-mount" for v in result.violations)

    def test_accepts_only_golden_path_control_bind_mounts(self) -> None:
        config = _base_config()
        config["services"]["web"]["volumes"] = [
            {
                "type": "bind",
                "source": "/var/run/lifecycle-broker",
                "target": "/var/run/lifecycle-broker",
            },
            {
                "type": "bind",
                "source": "/var/run/topology",
                "target": "/var/run/topology",
            },
            {
                "type": "bind",
                "source": "/tmp/gp-artifacts",
                "target": "/results",
            },
        ]
        assert validate_config(config).ok

        config["services"]["web"]["volumes"].append(
            {
                "type": "bind",
                "source": "/tmp/unapproved",
                "target": "/tmp/unapproved",
            }
        )
        result = validate_config(config)
        assert not result.ok
        assert any(v.rule == "unapproved-host-mount" for v in result.violations)

    def test_rejects_privileged(self) -> None:
        config = _base_config()
        config["services"]["web"]["privileged"] = True
        result = validate_config(config)
        assert not result.ok
        assert any(v.rule == "privileged" for v in result.violations)

    def test_rejects_host_gateway_alias(self) -> None:
        config = _base_config()
        config["services"]["web"]["extra_hosts"] = [
            "myhost:host-gateway"
        ]
        result = validate_config(config)
        assert not result.ok
        assert any(v.rule == "host-gateway-alias" for v in result.violations)


# ---------------------------------------------------------------------------
# Pass 2 — live validation
# ---------------------------------------------------------------------------


class TestLiveValidation:
    def test_valid_containers_pass(self) -> None:
        containers = [_base_container("web"), _base_container("api")]
        result = validate_live(containers)
        assert result.ok
        assert len(result.services_checked) == 2

    def test_rejects_host_network_mode(self) -> None:
        container = _base_container("web")
        container["HostConfig"]["NetworkMode"] = "host"
        result = validate_live([container])
        assert not result.ok
        assert any(v.rule == "network-mode-host" for v in result.violations)

    def test_rejects_published_port(self) -> None:
        container = _base_container("web")
        container["HostConfig"]["PortBindings"] = {
            "80/tcp": [{"HostPort": "8080"}]
        }
        result = validate_live([container])
        assert not result.ok
        assert any(v.rule == "published-port" for v in result.violations)

    def test_rejects_privileged(self) -> None:
        container = _base_container("web")
        container["HostConfig"]["Privileged"] = True
        result = validate_live([container])
        assert not result.ok
        assert any(v.rule == "privileged" for v in result.violations)

    def test_rejects_multi_network(self) -> None:
        container = _base_container("web")
        container["NetworkSettings"]["Networks"] = {
            "gp-net": {"NetworkID": "abc"},
            "external": {"NetworkID": "def"},
        }
        result = validate_live([container])
        assert not result.ok
        assert any(v.rule == "multi-network" for v in result.violations)

    def test_rejects_docker_socket_mount(self) -> None:
        container = _base_container("web")
        container["Mounts"] = [
            {"Source": "/var/run/docker.sock", "Destination": "/var/run/docker.sock"}
        ]
        result = validate_live([container])
        assert not result.ok
        assert any(v.rule == "docker-socket-mount" for v in result.violations)

# ---------------------------------------------------------------------------
# Attestation
# ---------------------------------------------------------------------------


class TestAttestation:
    def test_build_attestation_structure(self) -> None:
        live_result = ValidationResult(
            phase="live",
            services_checked=["web", "api"],
            network_hash="abc123",
        )
        att = build_attestation(
            project_name="gp-test-1",
            config_result=None,
            live_result=live_result,
            compose_started_at=1000.0,
        )
        assert att["project_name"] == "gp-test-1"
        assert att["services"] == ["web", "api"]
        assert att["network_hash"] == "abc123"
        assert att["compose_started_at"] == 1000.0
        assert att["live_phase"]["ok"] is True

    def test_sign_and_verify_roundtrip(self) -> None:
        try:
            from cryptography.hazmat.primitives.asymmetric.ed25519 import (
                Ed25519PrivateKey,
            )
            from cryptography.hazmat.primitives.serialization import (
                Encoding,
                NoEncryption,
                PrivateFormat,
                PublicFormat,
            )
        except ImportError:
            pytest.skip("cryptography package not available")

        private_key = Ed25519PrivateKey.generate()
        public_key = private_key.public_key()

        with tempfile.NamedTemporaryFile(suffix=".pem", delete=False) as f:
            f.write(
                private_key.private_bytes(
                    Encoding.PEM, PrivateFormat.PKCS8, NoEncryption()
                )
            )
            priv_path = f.name

        with tempfile.NamedTemporaryFile(suffix=".pem", delete=False) as f:
            f.write(
                public_key.public_bytes(Encoding.PEM, PublicFormat.SubjectPublicKeyInfo)
            )
            pub_path = f.name

        try:
            live_result = ValidationResult(
                phase="live",
                services_checked=["web"],
                network_hash="net123",
            )
            att = build_attestation("gp-test", None, live_result, 1000.0)
            signed = sign_attestation(att, priv_path)

            assert signed["signature_algorithm"] == "Ed25519"
            assert "signature" in signed
            assert verify_attestation(signed, pub_path)

            signed["project_name"] = "tampered"
            assert not verify_attestation(signed, pub_path)
        finally:
            Path(priv_path).unlink(missing_ok=True)
            Path(pub_path).unlink(missing_ok=True)
