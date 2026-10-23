"""The local Compose stack (infra/docker/docker-compose.yml) is internally
consistent: every reference resolves, stateful services are health-gated,
and nothing secret is hard-coded beyond the documented dev-only defaults."""

from __future__ import annotations

import re

import pytest

COMPOSE = "infra/docker/docker-compose.yml"


@pytest.fixture(scope="module")
def compose(load_yaml):
    return load_yaml(COMPOSE)


def _depends(service: dict) -> list[str]:
    dep = service.get("depends_on", [])
    return list(dep) if isinstance(dep, (list, dict)) else []


def test_every_service_is_buildable_or_pulls_an_image(compose):
    for name, svc in compose["services"].items():
        assert "image" in svc or "build" in svc, f"{name} has neither image nor build"


def test_dependencies_and_volumes_resolve(compose):
    services = set(compose["services"])
    named_volumes = set(compose.get("volumes") or {})
    for name, svc in compose["services"].items():
        missing = set(_depends(svc)) - services
        assert not missing, f"{name} depends on undefined services {missing}"
        for vol in svc.get("volumes", []):
            source = vol.split(":", 1)[0] if isinstance(vol, str) else vol.get("source", "")
            if source and not source.startswith((".", "/", "~", "$")):
                assert source in named_volumes, f"{name} mounts undeclared volume {source}"


def test_host_ports_do_not_collide(compose):
    seen: dict[str, str] = {}
    for name, svc in compose["services"].items():
        for port in svc.get("ports", []):
            host = str(port).rsplit(":", 1)[0] if ":" in str(port) else str(port)
            assert host not in seen, f"host port {host} used by {seen.get(host)} and {name}"
            seen[host] = name


def test_stateful_dependencies_are_health_gated(compose):
    svc = compose["services"]
    for stateful in ("postgres", "redis"):
        assert "healthcheck" in svc[stateful], f"{stateful} needs a healthcheck"
    sup_deps = svc["supervisor"].get("depends_on", {})
    assert isinstance(sup_deps, dict) and sup_deps.get("postgres", {}).get("condition") == (
        "service_healthy"
    ), "the supervisor must wait for a healthy Postgres, not just a started one"


def test_supervisor_runs_with_auth_and_postgres(compose):
    cmd = [str(c) for c in compose["services"]["supervisor"]["command"]]
    assert "--auth-tokens" in cmd, "the dev stack should exercise bearer auth"
    db = cmd[cmd.index("--db") + 1]
    assert db.startswith("postgresql://"), "the dev stack runs on Postgres (restart-survivable)"


def test_secrets_come_from_the_env_file(repo):
    text = (repo / COMPOSE).read_text()
    # Anything that looks like a live credential must be a ${VAR} reference;
    # the only literals allowed are the explicitly dev-only placeholders.
    for match in re.finditer(r"(sk-[A-Za-z0-9]{16,}|AKIA[0-9A-Z]{16}|0x[0-9a-fA-F]{64})", text):
        pytest.fail(f"hard-coded credential-looking literal in compose: {match.group(0)[:12]}…")
    for literal in re.findall(r"(?:PASSWORD|MASTER_KEY|SECRET)[^:\n]*:-([^}]+)\}", text):
        assert "dev" in literal or "change-me" in literal, f"non-dev default secret {literal!r}"


def test_compose_config_validates(run_tool):
    proc = run_tool(
        "docker", "compose", "-f", COMPOSE, "--env-file", ".env.example", "config", "--quiet",
    )
    if proc.returncode != 0 and "is not a docker command" in proc.stderr:
        pytest.skip("docker compose plugin not installed")
    assert proc.returncode == 0, proc.stderr
