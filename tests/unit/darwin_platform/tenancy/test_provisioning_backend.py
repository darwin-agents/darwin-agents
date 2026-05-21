"""Provisioning backend contract tests + SubprocessProvisioningBackend.

Doc 04/07: ``ProvisioningBackend`` is the Protocol the platform plugs in.
Every backend (InMemory default, Subprocess driver, future Crossplane
operator, etc.) must satisfy the same contract — provision returns
``{"id", "creds"}``, deprovision is idempotent."""

from __future__ import annotations

import json
import os
import stat
import textwrap
from pathlib import Path

import pytest

from darwin_platform.tenancy.provisioning import (
    InMemoryProvisioningBackend,
    ProvisioningError,
    ProvisioningService,
    SubprocessProvisioningBackend,
)


# ---------------------------------------------------------------------------
# Contract: every backend MUST satisfy these
# ---------------------------------------------------------------------------

def _contract_tests(backend):
    """Returns (lambda) -> None tests that any backend must pass.
    Called twice — once with InMemory, once with Subprocess driving a
    stub script."""
    # Provision returns id + creds
    r = backend.provision(kind="postgres", spec={"size": "small"})
    assert "id" in r
    assert "creds" in r
    rid = r["id"]
    # Deprovision is idempotent
    backend.deprovision(kind="postgres", resource_id=rid)
    backend.deprovision(kind="postgres", resource_id=rid)  # no raise


def test_in_memory_backend_contract():
    _contract_tests(InMemoryProvisioningBackend())


def test_subprocess_backend_contract(tmp_path):
    """Drive the SubprocessProvisioningBackend against a tiny shell
    script that mimics a real driver's stdout-JSON contract."""
    script = tmp_path / "fake_driver.sh"
    script.write_text(textwrap.dedent("""\
        #!/usr/bin/env bash
        set -e
        if [[ "$1" == "provision" ]]; then
            shift
            # consume --kind X --spec '{}'
            while [[ $# -gt 0 ]]; do
                case $1 in
                    --kind) KIND=$2; shift 2;;
                    --spec) SPEC=$2; shift 2;;
                    *) shift;;
                esac
            done
            printf '{"id": "%s-1", "creds": "ok"}\\n' "$KIND"
        elif [[ "$1" == "deprovision" ]]; then
            exit 0
        else
            exit 1
        fi
    """))
    script.chmod(script.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)

    backend = SubprocessProvisioningBackend(command=[str(script)])
    _contract_tests(backend)


def test_subprocess_backend_command_not_found():
    backend = SubprocessProvisioningBackend(command=["/no/such/binary"])
    with pytest.raises(ProvisioningError, match="not found"):
        backend.provision(kind="postgres", spec={})


def test_subprocess_backend_non_zero_exit(tmp_path):
    script = tmp_path / "broken.sh"
    script.write_text("#!/usr/bin/env bash\nexit 1\n")
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    backend = SubprocessProvisioningBackend(command=[str(script)])
    with pytest.raises(ProvisioningError, match="failed"):
        backend.provision(kind="postgres", spec={})


def test_subprocess_backend_non_json_output(tmp_path):
    script = tmp_path / "bad_output.sh"
    script.write_text(
        "#!/usr/bin/env bash\necho 'hello world'\n"
    )
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    backend = SubprocessProvisioningBackend(command=[str(script)])
    with pytest.raises(ProvisioningError, match="non-JSON"):
        backend.provision(kind="postgres", spec={})


def test_subprocess_backend_missing_id_field(tmp_path):
    script = tmp_path / "noid.sh"
    script.write_text(
        '#!/usr/bin/env bash\necho \'{"creds": "x"}\'\n'
    )
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    backend = SubprocessProvisioningBackend(command=[str(script)])
    with pytest.raises(ProvisioningError, match="no 'id'"):
        backend.provision(kind="postgres", spec={})


def test_subprocess_backend_empty_command_refused():
    with pytest.raises(ValueError):
        SubprocessProvisioningBackend(command=[])


def test_subprocess_backend_passes_spec_through_to_command(tmp_path):
    """The spec dict reaches the executable as JSON on stdin/args."""
    script = tmp_path / "echo_spec.sh"
    script.write_text(textwrap.dedent("""\
        #!/usr/bin/env bash
        set -e
        if [[ "$1" == "provision" ]]; then
            shift
            while [[ $# -gt 0 ]]; do
                case $1 in
                    --spec) SPEC=$2; shift 2;;
                    *) shift;;
                esac
            done
            # Echo the spec back inside the id so we can assert
            printf '{"id": "x-%s", "creds": "z"}\\n' "$(echo $SPEC | tr -d ' "')"
        fi
    """))
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    backend = SubprocessProvisioningBackend(command=[str(script)])
    r = backend.provision(kind="postgres", spec={"size": "small"})
    assert "size:small" in r["id"]


# ---------------------------------------------------------------------------
# Integration with ProvisioningService
# ---------------------------------------------------------------------------

def test_service_uses_subprocess_backend(tmp_path):
    script = tmp_path / "ok.sh"
    script.write_text(textwrap.dedent("""\
        #!/usr/bin/env bash
        if [[ "$1" == "provision" ]]; then
            shift
            while [[ $# -gt 0 ]]; do
                case $1 in
                    --kind) KIND=$2; shift 2;;
                    *) shift;;
                esac
            done
            printf '{"id": "%s-svc-1", "creds": "abc"}\\n' "$KIND"
        fi
    """))
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    svc = ProvisioningService(
        backend=SubprocessProvisioningBackend(command=[str(script)])
    )
    record, payload = svc.provision(
        darwin_id="d-1", kind="bucket", spec={},
    )
    assert record.id == "bucket-svc-1"
    assert payload["creds"] == "abc"
