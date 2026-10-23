"""The GitHub Actions workflows are valid, bounded, and gate what they
publish: every job has a timeout, actions are version-pinned, the fast
tier excludes ``slow``, and the slow workflow runs the e2e + performance
tiers the fast loop skips."""

from __future__ import annotations

import re

import pytest

WORKFLOWS = ".github/workflows"


def _workflows(repo):
    return sorted((repo / WORKFLOWS).glob("*.yml"))


@pytest.fixture(scope="module")
def workflows(repo, load_yaml):
    return {p.name: load_yaml(f"{WORKFLOWS}/{p.name}") for p in _workflows(repo)}


def _steps(workflow: dict):
    for job in workflow["jobs"].values():
        yield from job.get("steps", [])


def test_every_workflow_has_triggers_and_jobs(workflows):
    assert workflows, "no workflows found"
    for name, wf in workflows.items():
        # YAML 1.1 parses the bare key `on:` as boolean True.
        assert "on" in wf or True in wf, f"{name}: no triggers"
        assert wf.get("jobs"), f"{name}: no jobs"


def test_every_job_is_time_bounded(workflows):
    for name, wf in workflows.items():
        for job_id, job in wf["jobs"].items():
            if "uses" in job:  # reusable-workflow call; the callee bounds itself
                continue
            assert "timeout-minutes" in job, f"{name}:{job_id} has no timeout-minutes"


def test_actions_are_version_pinned(workflows):
    for name, wf in workflows.items():
        for step in _steps(wf):
            uses = step.get("uses")
            if uses and not uses.startswith("./"):
                ref = uses.rsplit("@", 1)[-1] if "@" in uses else ""
                assert re.fullmatch(r"v\d+(\.\d+)*|[0-9a-f]{40}", ref), f"{name}: {uses} is unpinned"


def test_fast_tier_excludes_slow_and_slow_tier_runs_it(workflows):
    fast = " ".join(s.get("run", "") for s in _steps(workflows["test.yml"]))
    slow = " ".join(s.get("run", "") for s in _steps(workflows["slow.yml"]))
    assert 'pytest -m "not slow"' in fast
    # e2e and performance are auto-marked slow, so `-m slow` covers them.
    assert "pytest -m slow" in slow


def test_image_publish_is_gated_on_lint_and_test(workflows):
    jobs = workflows["build.yml"]["jobs"]
    publish = [j for j in jobs.values() if "steps" in j and any(
        "docker/build-push-action" in s.get("uses", "") for s in j["steps"]
    )]
    assert publish, "build.yml should publish the image"
    for job in publish:
        assert {"lint", "test"} <= set(job.get("needs", [])), "a red commit must never publish"


def test_python_jobs_install_from_the_lockfile(workflows):
    for name, wf in workflows.items():
        for step in _steps(wf):
            run = step.get("run", "")
            if "uv sync" in run:
                assert "--frozen" in run or "--locked" in run or "uv.lock" in str(wf), name
