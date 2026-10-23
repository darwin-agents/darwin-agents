"""The configuration reference (docs/engineering/27) and .env.example account
for every knob the code actually reads: each ``serve`` flag, and each
environment variable ``src/`` reads. Fix a failure with
``make docs-config`` (regenerates the flag tables) or by documenting the
variable in doc 27 / .env.example."""

from __future__ import annotations

import argparse
import re

import pytest

DOC27 = "docs/engineering/27-configuration-reference.md"
_ENV_READ = re.compile(r"""(?:environ\.get|getenv|environ\[)\(?\s*["']([A-Z][A-Z0-9_]+)["']""")
# The CLI's ("dest", "ENV_VAR") fallback tables (val()/on()/_POD_SUBSTRATE).
_CLI_ENV_PAIR = re.compile(r'\(\s*"[a-z_]+",\s*"([A-Z][A-Z0-9_]+)"')
# Read by tooling, not operators: test-only DSNs and interpreter plumbing.
_NOT_CONFIG = {"DARWIN_POSTGRES_TEST_DSN", "PYTHONPATH", "HOME", "PATH"}


@pytest.fixture(scope="module")
def doc27(repo) -> str:
    return (repo / DOC27).read_text(encoding="utf-8")


def _serve_flags() -> set[str]:
    from darwin_platform.__main__ import build_parser

    serve = build_parser()._subparsers._group_actions[0].choices["serve"]  # noqa: SLF001
    return {
        opt for action in serve._actions  # noqa: SLF001
        if not isinstance(action, argparse._HelpAction)
        for opt in action.option_strings if opt.startswith("--")
    }


def test_every_serve_flag_is_documented(doc27):
    documented = set(re.findall(r"`(--[a-z0-9-]+)`", doc27))
    missing = _serve_flags() - documented
    assert not missing, f"undocumented serve flags (run `make docs-config`): {sorted(missing)}"


def test_documented_flags_still_exist(doc27):
    stale = set(re.findall(r"`(--[a-z0-9-]+)`", doc27)) - _serve_flags() - {"--help"}
    assert not stale, f"doc 27 documents flags that no longer exist: {sorted(stale)}"


def test_every_env_var_read_by_the_code_is_documented(repo, doc27):
    env_example = (repo / ".env.example").read_text(encoding="utf-8")
    read: set[str] = set()
    for path in (repo / "src").rglob("*.py"):
        read.update(_ENV_READ.findall(path.read_text(encoding="utf-8")))
    cli = (repo / "src" / "darwin_platform" / "__main__.py").read_text(encoding="utf-8")
    read.update(_CLI_ENV_PAIR.findall(cli))
    undocumented = sorted(
        v for v in read - _NOT_CONFIG if f"`{v}`" not in doc27 and v not in env_example
    )
    assert not undocumented, (
        f"env vars read by src/ but documented nowhere: {undocumented} — "
        f"add them to {DOC27} or .env.example"
    )
