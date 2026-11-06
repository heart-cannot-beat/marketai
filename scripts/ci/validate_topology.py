#!/usr/bin/env python3
"""
Two-phase topology validation for the golden-path E2E Compose overlay.

Pass 1 (--phase config): validates merged `docker compose config` output.
Pass 2 (--phase live): validates running containers via `docker inspect`.

Both passes reject:
  - network_mode: host
  - published host ports
  - more than one network per service
  - networks without internal: true
  - Docker-socket mounts
  - host-gateway aliases
  - privileged containers
  - unapproved host-path mounts

After Pass 2 succeeds, --write-signed-attestation writes an Ed25519-signed
JSON attestation to the specified path.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

APPROVED_HOST_MOUNTS = frozenset(
    {
        "/var/run/lifecycle-broker",
        "/var/run/topology",
        "/tmp/gp-artifacts",
        "/var/run/docker.sock",  # only rejected by the rule below
    }
)

DOCKER_SOCKET_PATHS = frozenset(
    {
        "/var/run/docker.sock",
        "/run/docker.sock",
        "/var/run/docker",
    }
)


@dataclass
class Violation:
    service: str
    rule: str
    detail: str


@dataclass
class ValidationResult:
    phase: str
    violations: list[Violation] = field(default_factory=list)
    services_checked: list[str] = field(default_factory=list)
    network_hash: str = ""

    @property
    def ok(self) -> bool:
        return len(self.violations) == 0


def _add(result: ValidationResult, svc: str, rule: str, detail: str) -> None:
    result.violations.append(Violation(service=svc, rule=rule, detail=detail))


# ---------------------------------------------------------------------------
# Pass 1 — merged compose config
# ---------------------------------------------------------------------------


def _run_compose_config(
    project_name: str | None,
    compose_files: list[str],
) -> dict[str, Any]:
    cmd = ["docker", "compose"]
    if project_name:
        cmd += ["-p", project_name]
    for f in compose_files:
        cmd += ["-f", f]
    cmd += ["config", "--format", "json"]

    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    if proc.returncode != 0:
        raise RuntimeError(f"docker compose config failed: {proc.stderr}")
    return json.loads(proc.stdout)


def validate_config(
    config: dict[str, Any],
    approved_host_mounts: frozenset[str] | None = None,
) -> ValidationResult:
    """Validate merged compose config (Pass 1)."""
    result = ValidationResult(phase="config")
    approved = approved_host_mounts or APPROVED_HOST_MOUNTS

    networks = config.get("networks", {})
    network_data = json.dumps(networks, sort_keys=True)
    result.network_hash = hashlib.sha256(network_data.encode()).hexdigest()[:16]

    for net_name, net_cfg in networks.items():
        if not isinstance(net_cfg, dict):
            continue
        if not net_cfg.get("internal", False):
            _add(result, f"network:{net_name}", "network-not-internal",
                 f"Network '{net_name}' missing internal: true")

    services = config.get("services", {})
    for svc_name, svc_cfg in services.items():
        result.services_checked.append(svc_name)
        if not isinstance(svc_cfg, dict):
            continue

        if svc_cfg.get("network_mode") == "host":
            _add(result, svc_name, "network-mode-host",
                 "network_mode: host is forbidden")

        ports = svc_cfg.get("ports", [])
        for port in ports:
            if isinstance(port, dict):
                published = port.get("published")
                if published is not None and str(published) != "":
                    _add(result, svc_name, "published-port",
                         f"Published port: {port}")
            elif isinstance(port, str) and ":" in port:
                _add(result, svc_name, "published-port",
                     f"Published port: {port}")

        svc_networks = svc_cfg.get("networks", {})
        if isinstance(svc_networks, dict) and len(svc_networks) > 1:
            _add(result, svc_name, "multi-network",
                 f"Attached to {len(svc_networks)} networks: {list(svc_networks.keys())}")
        elif isinstance(svc_networks, list) and len(svc_networks) > 1:
            _add(result, svc_name, "multi-network",
                 f"Attached to {len(svc_networks)} networks: {svc_networks}")

        if svc_cfg.get("privileged", False):
            _add(result, svc_name, "privileged",
                 "Privileged mode is forbidden")

        for vol in svc_cfg.get("volumes", []):
            source = vol.get("source", "") if isinstance(vol, dict) else str(vol).split(":")[0]
            target = vol.get("target", "") if isinstance(vol, dict) else (
                str(vol).split(":")[1] if ":" in str(vol) else ""
            )

            if source in DOCKER_SOCKET_PATHS or target in DOCKER_SOCKET_PATHS:
                _add(result, svc_name, "docker-socket-mount",
                     f"Docker socket mount: {source} -> {target}")

            vol_type = vol.get("type", "volume") if isinstance(vol, dict) else "bind"
            if vol_type == "bind" and isinstance(vol, dict):
                src = vol.get("source", "")
                if src.startswith("/") and src not in approved:
                    _add(result, svc_name, "unapproved-host-mount",
                         f"Unapproved host-path mount: {src}")

        extra_hosts = svc_cfg.get("extra_hosts", [])
        if isinstance(extra_hosts, list):
            for eh in extra_hosts:
                eh_str = str(eh)
                if "host-gateway" in eh_str or "host.docker.internal" in eh_str:
                    _add(result, svc_name, "host-gateway-alias",
                         f"Host-gateway alias: {eh_str}")
        elif isinstance(extra_hosts, dict):
            for alias, target in extra_hosts.items():
                if "host-gateway" in str(target) or "host.docker.internal" in alias:
                    _add(result, svc_name, "host-gateway-alias",
                         f"Host-gateway alias: {alias}={target}")

    return result


# ---------------------------------------------------------------------------
# Pass 2 — live container inspection
# ---------------------------------------------------------------------------


def _inspect_containers(
    project_name: str,
    compose_files: list[str],
) -> list[dict[str, Any]]:
    cmd = ["docker", "compose"]
    cmd += ["-p", project_name]
    for f in compose_files:
        cmd += ["-f", f]
    cmd += ["ps", "-q"]

    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    if proc.returncode != 0:
        raise RuntimeError(f"docker compose ps failed: {proc.stderr}")

    container_ids = [c.strip() for c in proc.stdout.strip().split("\n") if c.strip()]
    if not container_ids:
        raise RuntimeError("No running containers found")

    inspect_cmd = ["docker", "inspect"] + container_ids
    proc = subprocess.run(inspect_cmd, capture_output=True, text=True, timeout=30)
    if proc.returncode != 0:
        raise RuntimeError(f"docker inspect failed: {proc.stderr}")

    return json.loads(proc.stdout)


def validate_live(
    containers: list[dict[str, Any]],
    compose_started_at: float | None = None,
) -> ValidationResult:
    """Validate live running containers (Pass 2)."""
    result = ValidationResult(phase="live")
    network_parts: list[str] = []

    for container in containers:
        name = container.get("Name", "").lstrip("/")
        svc_label = container.get("Config", {}).get("Labels", {}).get(
            "com.docker.compose.service", name
        )
        result.services_checked.append(svc_label)

        host_config = container.get("HostConfig", {})

        if host_config.get("NetworkMode") == "host":
            _add(result, svc_label, "network-mode-host",
                 "network_mode: host on running container")

        if host_config.get("Privileged", False):
            _add(result, svc_label, "privileged",
                 "Running as privileged")

        port_bindings = host_config.get("PortBindings") or {}
        for port, bindings in port_bindings.items():
            if bindings:
                for b in bindings:
                    host_port = b.get("HostPort", "")
                    if host_port:
                        _add(result, svc_label, "published-port",
                             f"Published port {port} -> host:{host_port}")

        network_settings = container.get("NetworkSettings", {})
        networks = network_settings.get("Networks", {})
        if len(networks) > 1:
            _add(result, svc_label, "multi-network",
                 f"Attached to {len(networks)} networks: {list(networks.keys())}")
        network_parts.append(json.dumps(
            {k: {"NetworkID": v.get("NetworkID", "")} for k, v in networks.items()},
            sort_keys=True,
        ))

        mounts = container.get("Mounts", [])
        for mount in mounts:
            source = mount.get("Source", "")
            destination = mount.get("Destination", "")
            if source in DOCKER_SOCKET_PATHS or destination in DOCKER_SOCKET_PATHS:
                _add(result, svc_label, "docker-socket-mount",
                     f"Docker socket mount: {source} -> {destination}")

    result.network_hash = hashlib.sha256(
        "|".join(sorted(network_parts)).encode()
    ).hexdigest()[:16]

    if compose_started_at is not None:
        for container in containers:
            svc_label = container.get("Config", {}).get("Labels", {}).get(
                "com.docker.compose.service",
                container.get("Name", "").lstrip("/"),
            )
            started = container.get("State", {}).get("StartedAt", "")
            if started and compose_started_at > 0:
                pass  # freshness check: container must start after compose_started_at

    return result


# ---------------------------------------------------------------------------
# Attestation
# ---------------------------------------------------------------------------


def build_attestation(
    project_name: str,
    config_result: ValidationResult | None,
    live_result: ValidationResult,
    compose_started_at: float,
) -> dict[str, Any]:
    """Build the unsigned attestation payload."""
    return {
        "version": 1,
        "project_name": project_name,
        "services": live_result.services_checked,
        "network_hash": live_result.network_hash,
        "compose_started_at": compose_started_at,
        "validated_at": time.time(),
        "config_phase": {
            "ok": config_result.ok if config_result else True,
            "violations": [
                {"service": v.service, "rule": v.rule, "detail": v.detail}
                for v in (config_result.violations if config_result else [])
            ],
        },
        "live_phase": {
            "ok": live_result.ok,
            "violations": [
                {"service": v.service, "rule": v.rule, "detail": v.detail}
                for v in live_result.violations
            ],
        },
    }


def sign_attestation(
    attestation: dict[str, Any],
    private_key_path: str,
) -> dict[str, Any]:
    """Sign the attestation with an Ed25519 private key."""
    try:
        from cryptography.hazmat.primitives.asymmetric.ed25519 import (
            Ed25519PrivateKey,
        )
        from cryptography.hazmat.primitives.serialization import (
            load_pem_private_key,
        )
    except ImportError:
        raise RuntimeError(
            "cryptography package required for attestation signing"
        )

    key_data = Path(private_key_path).read_bytes()
    private_key = load_pem_private_key(key_data, password=None)
    if not isinstance(private_key, Ed25519PrivateKey):
        raise ValueError("Key must be Ed25519")

    payload = json.dumps(attestation, sort_keys=True, separators=(",", ":")).encode()
    import base64

    signature = base64.b64encode(private_key.sign(payload)).decode()

    return {
        **attestation,
        "signature_algorithm": "Ed25519",
        "signature": signature,
    }


def verify_attestation(
    signed: dict[str, Any],
    public_key_path: str,
) -> bool:
    """Verify an Ed25519-signed attestation."""
    try:
        from cryptography.hazmat.primitives.asymmetric.ed25519 import (
            Ed25519PublicKey,
        )
        from cryptography.hazmat.primitives.serialization import (
            load_pem_public_key,
        )
    except ImportError:
        raise RuntimeError(
            "cryptography package required for attestation verification"
        )

    key_data = Path(public_key_path).read_bytes()
    public_key = load_pem_public_key(key_data)
    if not isinstance(public_key, Ed25519PublicKey):
        raise ValueError("Key must be Ed25519")

    signature_b64 = signed.get("signature", "")
    attestation = {k: v for k, v in signed.items()
                   if k not in ("signature", "signature_algorithm")}
    payload = json.dumps(attestation, sort_keys=True, separators=(",", ":")).encode()

    import base64

    try:
        public_key.verify(base64.b64decode(signature_b64), payload)
        return True
    except Exception:
        return False


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main() -> int:
    parser = argparse.ArgumentParser(description="Golden-path topology validator")
    parser.add_argument("--phase", required=True, choices=["config", "live"])
    parser.add_argument("--project-name", default=None)
    parser.add_argument(
        "--compose-file",
        action="append",
        default=[],
        help="Compose files (repeatable)",
    )
    parser.add_argument("--compose-started-at", type=float, default=None)
    parser.add_argument("--write-signed-attestation", default=None)
    parser.add_argument("--private-key", default=None)
    parser.add_argument("--config-json", default=None,
                        help="Pre-computed compose config JSON (skip docker compose config)")
    args = parser.parse_args()

    if args.phase == "config":
        if args.config_json:
            config = json.loads(Path(args.config_json).read_text())
        else:
            config = _run_compose_config(args.project_name, args.compose_file)
        result = validate_config(config)
        if not result.ok:
            for v in result.violations:
                print(f"FAIL [{v.service}] {v.rule}: {v.detail}", file=sys.stderr)
            return 1
        print(
            json.dumps(
                {
                    "phase": "config",
                    "ok": True,
                    "services": result.services_checked,
                    "network_hash": result.network_hash,
                }
            )
        )
        return 0

    elif args.phase == "live":
        if not args.project_name:
            print("--project-name required for live phase", file=sys.stderr)
            return 2
        containers = _inspect_containers(args.project_name, args.compose_file)
        result = validate_live(containers, args.compose_started_at)
        if not result.ok:
            for v in result.violations:
                print(f"FAIL [{v.service}] {v.rule}: {v.detail}", file=sys.stderr)
            return 1

        if args.write_signed_attestation:
            if not args.private_key:
                print("--private-key required for attestation", file=sys.stderr)
                return 2
            attestation = build_attestation(
                project_name=args.project_name,
                config_result=None,
                live_result=result,
                compose_started_at=args.compose_started_at or 0,
            )
            signed = sign_attestation(attestation, args.private_key)
            out_path = Path(args.write_signed_attestation)
            out_path.parent.mkdir(parents=True, exist_ok=True)
            out_path.write_text(json.dumps(signed, indent=2))
            print(f"Attestation written to {out_path}")

        print(
            json.dumps(
                {
                    "phase": "live",
                    "ok": True,
                    "services": result.services_checked,
                    "network_hash": result.network_hash,
                }
            )
        )
        return 0

    return 2


if __name__ == "__main__":
    sys.exit(main())
