"""kill -9 mid-soak recovery drill (docs/engineering/20 §"Definition of done").

Proves the doc-20 DoD bullet that the soak harness can't, because it needs
to *take the supervisor down hard mid-run*:

    "A process restart mid-soak (kill -9 the supervisor, restart) results in
     the Darwin resuming from checkpoint with zero lost audit entries."

Unlike ``dr_failover`` (two replicas, leader handover) this is the
single-supervisor checkpoint-resume path: one process, killed with SIGKILL
(no graceful shutdown — no checkpoint flush on the way out), restarted on the
SAME database. The drill then asserts:

  1. **Resume**     — the Darwin is re-adopted and its tick advances past the
                      pre-kill tick (it picked up from the checkpoint, not a
                      cold restart).
  2. **No loss**    — every pre-kill audit event survives unchanged (same seq
                      → same hash); the post-restart log is a superset.
  3. **Chain valid**— the full post-restart audit log still verifies by the
                      runtime's own hash-chain rules (reuses the soak gate's
                      ``criterion_3_hash_chain``) across the restart boundary.

Self-contained: it manages the ``darwin-platform serve`` subprocess itself
(SIGKILL to the whole process group, then a fresh process on the same ``--db``)
so it runs unattended in CI. Use a persistent ``--db`` file (the default is a
temp file) — an in-memory DB cannot survive a process restart, by definition.

Usage::

    # Fast self-contained run (CI):
    uv run python -m tools.drills.kill9_recovery --heartbeat-seconds 0.1

    # Realistic pace, real LiteLLM (pass through serve flags after --):
    uv run python -m tools.drills.kill9_recovery --heartbeat-seconds 5 \\
        --warmup-seconds 60 -- --litellm-url http://localhost:4000 ...

Exit 0 = resumed cleanly with zero lost entries; non-zero = a gate failed.
"""

from __future__ import annotations

import argparse
import os
import signal
import subprocess
import sys
import tempfile
import time

import httpx

from tools.soak.assertions import criterion_3_hash_chain
from tools.soak.config import SoakConfig
from tools.soak.run_soak import _fetch_all_events


def _log(msg: str) -> None:
    print(f"[kill9] {msg}", flush=True)


def _serve_command(args: argparse.Namespace, db: str) -> list[str]:
    cmd = [
        sys.executable, "-m", "darwin_platform", "serve",
        "--db", db,
        "--port", str(args.port),
        "--heartbeat-seconds", str(args.heartbeat_seconds),
    ]
    passthrough = list(args.serve_args or [])
    # `darwin-platform serve` defaults --wallet-provider to cdp (which needs
    # keys). Checkpoint-resume is adapter-agnostic, so when the operator
    # hasn't chosen a provider, fall back to the in-memory FakeWallet
    # (--wallet-provider web3 with no chains configured) so the drill runs
    # self-contained. A real-adapter run passes its own --wallet-provider.
    if not any(a == "--wallet-provider" or a.startswith("--wallet-provider=")
               for a in passthrough):
        cmd.extend(["--wallet-provider", "web3"])
    cmd.extend(passthrough)
    return cmd


def _start_supervisor(cmd: list[str]) -> subprocess.Popen:
    # New session → its own process group, so a SIGKILL reaches every child
    # (uvicorn workers, drive-loop tasks) — a real hard crash, not a polite
    # one the supervisor could trap and flush a checkpoint on.
    return subprocess.Popen(cmd, start_new_session=True)


def _wait_healthy(client: httpx.Client, timeout_seconds: float) -> bool:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        try:
            if client.get("/healthz").status_code == 200:
                return True
        except Exception:
            pass
        time.sleep(0.25)
    return False


def _hard_kill(proc: subprocess.Popen) -> None:
    """SIGKILL the whole process group, then reap so no zombie lingers."""
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except ProcessLookupError:
        return
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        _log("warning: killed process did not reap within 10s")


def _tick_of(client: httpx.Client, darwin_id: str) -> int:
    try:
        return int(client.get(f"/darwins/{darwin_id}").json().get("tick", 0))
    except Exception:
        return -1


def _wait_for_tick_advance(
    client: httpx.Client, darwin_id: str, baseline: int, timeout_seconds: float,
) -> int | None:
    """Wait until the Darwin's tick exceeds ``baseline`` (proof it resumed
    and is making progress). Returns the observed tick, or None on timeout."""
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        t = _tick_of(client, darwin_id)
        if t > baseline:
            return t
        time.sleep(0.5)
    return None


def _no_loss(
    pre: list[dict], post: list[dict],
) -> tuple[bool, str]:
    """Every pre-kill event must survive unchanged: same seq → same hash,
    and post must be a superset (>= length)."""
    if len(post) < len(pre):
        return False, (
            f"post-restart log shrank: {len(post)} < {len(pre)} pre-kill events"
        )
    post_by_seq = {e.get("seq"): e.get("hash") for e in post}
    for e in pre:
        seq, h = e.get("seq"), e.get("hash")
        if post_by_seq.get(seq) != h:
            return False, (
                f"pre-kill event seq={seq} changed/lost across restart "
                f"(hash {h} → {post_by_seq.get(seq)})"
            )
    return True, (
        f"all {len(pre)} pre-kill events intact; "
        f"log grew to {len(post)} after resume"
    )


def run_drill(args: argparse.Namespace) -> int:
    db = args.db or os.path.join(
        tempfile.mkdtemp(prefix="kill9-"), "recovery.db"
    )
    url = f"http://127.0.0.1:{args.port}"
    config = SoakConfig(url=url, auth_token=args.auth_token)
    headers = (
        {"Authorization": f"Bearer {args.auth_token}"} if args.auth_token else {}
    )
    client = httpx.Client(base_url=url, headers=headers, timeout=15.0)
    cmd = _serve_command(args, db)
    _log(f"db={db}")
    _log(f"serve: {' '.join(cmd)}")

    proc = _start_supervisor(cmd)
    results: list[tuple[str, bool, str]] = []
    try:
        if not _wait_healthy(client, args.boot_timeout_seconds):
            _log("FAIL: supervisor never became healthy")
            return 1

        spawn = client.post(
            "/darwins",
            json={"opening_usdc": args.opening_usdc, "escalation_auto": "approve"},
        )
        spawn.raise_for_status()
        darwin_id = spawn.json()["darwin_id"]
        _log(f"spawned {darwin_id}; warming up {args.warmup_seconds}s "
             "so a checkpoint is written")
        time.sleep(args.warmup_seconds)

        pre_tick = _tick_of(client, darwin_id)
        pre_events, pre_total = _fetch_all_events(client, darwin_id, config)
        _log(f"pre-kill: tick={pre_tick}, audit_events={pre_total}")
        if pre_tick <= 0 or pre_total <= 0:
            _log("FAIL: nothing to recover — Darwin never ticked / logged "
                 "before kill (raise --warmup-seconds)")
            return 1

        # ---- the hard crash ----
        _log("SIGKILL the supervisor (no graceful shutdown)")
        _hard_kill(proc)
        proc = None  # killed

        # ---- restart on the same DB ----
        _log("restarting supervisor on the same db")
        proc = _start_supervisor(cmd)
        if not _wait_healthy(client, args.boot_timeout_seconds):
            _log("FAIL: supervisor did not come back up after restart")
            return 1

        # Gate 1 — resume + progress.
        resumed_tick = _wait_for_tick_advance(
            client, darwin_id, pre_tick, args.resume_timeout_seconds,
        )
        if resumed_tick is None:
            cur = _tick_of(client, darwin_id)
            results.append((
                "resume", False,
                f"tick did not advance past pre-kill {pre_tick} within "
                f"{args.resume_timeout_seconds}s (now {cur}) — Darwin not "
                "re-adopted from checkpoint",
            ))
        else:
            results.append((
                "resume", True,
                f"tick resumed {pre_tick}→{resumed_tick} after restart",
            ))

        # Gates 2 + 3 — no lost entries + chain still valid.
        post_events, post_total = _fetch_all_events(client, darwin_id, config)
        ok_loss, loss_detail = _no_loss(pre_events, post_events)
        results.append(("no_lost_entries", ok_loss, loss_detail))

        chain = criterion_3_hash_chain(post_events, post_total)
        results.append((
            "hash_chain_intact", chain.ok,
            f"{chain.detail} (across the restart boundary)",
        ))
    finally:
        client.close()
        if proc is not None:
            _hard_kill(proc)

    print()
    all_ok = True
    for name, ok, detail in results:
        verdict = "PASS" if ok else "FAIL"
        all_ok = all_ok and ok
        print(f"[kill9] {verdict:4s} {name}: {detail}")
    print(f"[kill9] {'PASS — recovered cleanly' if all_ok else 'FAIL'}")
    return 0 if all_ok else 1


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        description="kill -9 mid-soak recovery drill (doc 20 DoD).",
    )
    p.add_argument("--db", default=None,
                   help="Persistent SQLite/Postgres DSN the supervisor uses; "
                        "MUST survive a restart. Default: a temp file.")
    p.add_argument("--port", type=int, default=8077)
    p.add_argument("--heartbeat-seconds", default="0.1",
                   help="Drive-loop cadence for the managed supervisor.")
    p.add_argument("--opening-usdc", type=float, default=100.0)
    p.add_argument("--warmup-seconds", type=float, default=6.0,
                   help="Run time before the kill so a checkpoint + audit "
                        "chain exist to recover from.")
    p.add_argument("--boot-timeout-seconds", type=float, default=30.0)
    p.add_argument("--resume-timeout-seconds", type=float, default=30.0)
    p.add_argument("--auth-token", default=None)
    p.add_argument(
        "serve_args", nargs="*", metavar="-- SERVE_ARGS",
        help="Extra flags passed through to `darwin-platform serve` (after "
             "`--`), e.g. real-LiteLLM/wallet wiring.",
    )
    args = p.parse_args(argv)
    return run_drill(args)


if __name__ == "__main__":
    sys.exit(main())
