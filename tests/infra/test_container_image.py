"""The supervisor image (infra/docker/Dockerfile) is reproducible, non-root,
probe-able, and can't bake secrets in (docs 08, 25)."""

from __future__ import annotations

import re
import tomllib

import pytest


@pytest.fixture(scope="module")
def dockerfile(repo) -> str:
    return (repo / "infra" / "docker" / "Dockerfile").read_text(encoding="utf-8")


def _stages(dockerfile: str) -> list[str]:
    return re.findall(r"^FROM\s+(\S+)", dockerfile, flags=re.MULTILINE)


def test_multi_stage_on_a_pinned_python_base(dockerfile):
    stages = _stages(dockerfile)
    assert len(stages) >= 2, "builder + runtime stages expected"
    for image in stages:
        assert ":" in image and not image.endswith(":latest"), f"unpinned base image {image}"
        assert image.startswith("python:3.12"), f"{image} — the genome targets Python 3.12"


def test_installs_exactly_the_locked_dependency_set(dockerfile):
    # uv.lock is the single source of truth; hash-checked install, no
    # re-resolution at build time (commit c48f53c).
    assert "COPY pyproject.toml uv.lock" in dockerfile
    assert "uv export --frozen" in dockerfile
    assert "--require-hashes" in dockerfile
    assert re.search(r"ARG UV_VERSION=\d+\.\d+\.\d+", dockerfile), "uv itself must be pinned"


def test_every_baked_extra_exists_in_pyproject(dockerfile, repo):
    extras = set(re.findall(r"--extra\s+([\w-]+)", dockerfile))
    declared = set(
        tomllib.loads((repo / "pyproject.toml").read_text())["project"]["optional-dependencies"]
    )
    assert extras, "the image should bake its platform extras"
    assert extras <= declared, f"Dockerfile installs undeclared extras: {sorted(extras - declared)}"
    assert "dev" not in extras, "test tooling must not ship in the runtime image"


def test_runtime_runs_as_an_unprivileged_user(dockerfile):
    runtime = dockerfile.split("AS runtime", 1)[1]
    users = re.findall(r"^USER\s+(\S+)", runtime, flags=re.MULTILINE)
    assert users and users[-1] not in ("root", "0"), "runtime stage must drop root"
    assert "useradd --uid 1000" in runtime


def test_healthcheck_probes_the_unauthenticated_liveness_route(dockerfile):
    assert re.search(r"HEALTHCHECK .*\n?.*?/healthz", dockerfile), "HEALTHCHECK must hit /healthz"
    assert "EXPOSE 8000" in dockerfile


def test_default_command_serves_the_platform(dockerfile):
    cmd = re.search(r"^CMD\s+(\[.*?\])", dockerfile, flags=re.MULTILINE | re.DOTALL)
    assert cmd, "exec-form CMD expected"
    assert '"darwin_platform", "serve"' in " ".join(cmd.group(1).split())


@pytest.mark.parametrize("pattern", ["**/*.env", "**/*.pem", "**/*.key", "**/tokens.json", ".git/"])
def test_dockerignore_keeps_secrets_and_vcs_out_of_the_build_context(repo, pattern):
    rules = (repo / ".dockerignore").read_text().splitlines()
    assert pattern in rules, f".dockerignore must exclude {pattern}"
