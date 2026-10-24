"""The Terraform tree (infra/terraform) is formatted, every module pins its
toolchain, module sources resolve, and inputs are documented.

``terraform validate`` needs provider downloads, so it stays in the
deploy-hetzner gate; ``terraform fmt -check`` is offline and runs here when
terraform is on PATH."""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

TF = "infra/terraform"


def _module_dirs(repo: Path) -> list[Path]:
    return sorted(p for p in (repo / TF / "modules").iterdir() if p.is_dir())


def _env_dirs(repo: Path) -> list[Path]:
    return sorted(p for p in (repo / TF / "envs").iterdir() if p.is_dir())


def _tf_text(directory: Path) -> str:
    return "\n".join(p.read_text(encoding="utf-8") for p in sorted(directory.glob("*.tf")))


def test_terraform_fmt(run_tool):
    proc = run_tool("terraform", "fmt", "-check", "-recursive", TF)
    assert proc.returncode == 0, f"terraform fmt would change:\n{proc.stdout}{proc.stderr}"


def test_every_root_and_module_pins_terraform_and_providers(repo):
    for directory in _module_dirs(repo) + _env_dirs(repo):
        versions = directory / "versions.tf"
        assert versions.is_file(), f"{directory.name}: missing versions.tf"
        text = versions.read_text()
        assert "required_version" in text, f"{directory.name}: no required_version"
        providers = re.search(r"required_providers\s*{(.*?)\n\s*}\n", text, flags=re.DOTALL)
        if providers:
            for name, body in re.findall(r"(\w[\w-]*)\s*=\s*{([^}]*)}", providers.group(1)):
                assert "version" in body, f"{directory.name}: provider {name} is unpinned"


def test_module_sources_resolve(repo):
    for directory in _module_dirs(repo) + _env_dirs(repo):
        for source in re.findall(r'source\s*=\s*"(\.{1,2}/[^"]+)"', _tf_text(directory)):
            assert (directory / source).resolve().is_dir(), f"{directory.name}: bad source {source}"


@pytest.mark.parametrize("kind", ["variable", "output"])
def test_inputs_and_outputs_are_described(repo, kind):
    undocumented = []
    for directory in _module_dirs(repo):
        for name, body in re.findall(rf'{kind}\s+"(\w+)"\s*{{(.*?)\n}}', _tf_text(directory), re.DOTALL):
            if "description" not in body:
                undocumented.append(f"{directory.name}.{name}")
    assert not undocumented, f"{kind}s without a description: {undocumented}"


def test_sensitive_inputs_are_marked_sensitive(repo):
    leaky = []
    for directory in _module_dirs(repo) + _env_dirs(repo):
        for name, body in re.findall(r'variable\s+"(\w+)"\s*{(.*?)\n}', _tf_text(directory), re.DOTALL):
            if re.search(r"(token|password|private_key|master_key|api_key|wallet_key)$", name):
                if "sensitive" not in body:
                    leaky.append(f"{directory.name}.{name}")
    assert not leaky, f"secret-shaped variables not marked sensitive: {leaky}"


def test_no_state_or_real_tfvars_committed(repo):
    # Ask git: a local `make dev-up` leaves (gitignored) state on disk.
    tracked = set(subprocess.run(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard", TF],
        cwd=repo, capture_output=True, text=True, check=True,
    ).stdout.split())
    assert not [p for p in tracked if p.endswith((".tfstate", ".tfstate.backup"))]
    # Committed tfvars carry shape only (sizes, network, caps): no tfvars may
    # assign a sensitive variable — secrets arrive as TF_VAR_* from CI.
    leaks = []
    for rel in sorted(p for p in tracked if p.endswith(".tfvars") and "/.terraform/" not in p):
        path = repo / rel
        sensitive = {
            name
            for name, body in re.findall(r'variable\s+"(\w+)"\s*{(.*?)\n}', _tf_text(path.parent), re.DOTALL)
            if "sensitive" in body
        }
        assigned = set(re.findall(r"^\s*(\w+)\s*=", path.read_text(), re.MULTILINE))
        leaks += [f"{rel}: {name}" for name in sorted(assigned & sensitive)]
    assert not leaks, f"tfvars assign sensitive variables: {leaks}"
