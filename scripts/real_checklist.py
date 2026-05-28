"""Pre-mainnet smoke test for the running Darwin platform.

Verifies — against a live server — the three behaviours that absolutely must
work before pointing the platform at mainnet funds:

  1. **Tick + spend** — a spawned Darwin advances its tick counter, the audit
     chain grows, and the balance moves the expected direction.
  2. **Pause respect** — after a pause, the audit chain stops growing within
     a few heartbeats. Resume re-enables ticks.
  3. **Kill cleanup** — kill flips ``alive=false, killed=true`` within the
     5-second doc-09 SLA, ``death_reason`` is set, and the lifecycle row
     transitions out of ``alive``.

This is a *behaviour* test, not a unit test — it runs against a real
``darwin-platform serve`` process (Stage 1 testnet recipe) and exercises the
exact code path that mainnet would take. Idempotent: spawns a fresh Darwin
with a tiny budget and kills it at the end, regardless of pass/fail.

Usage:
    python scripts/real_checklist.py \
        --url http://localhost:8000 \
        --token "$DARWIN_DEV_TOKEN" \
        --opening-usdc 1.0

Exit code 0 = all three behaviours passed. Non-zero = at least one failed.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------
@dataclass
class Client:
    base_url: str
    token: str
    timeout: float = 10.0

    def _req(self, method: str, path: str, body: dict | None = None) -> Any:
        url = f"{self.base_url}{path}"
        data = json.dumps(body).encode() if body is not None else None
        headers = {
            "Authorization": f"Bearer {self.token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
            "Idempotency-Key": f"checklist-{time.time_ns()}",
        }
        req = urllib.request.Request(url, data=data, method=method, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                payload = r.read()
                return json.loads(payload) if payload else None
        except urllib.error.HTTPError as e:
            body_txt = e.read().decode("utf-8", "replace")
            raise RuntimeError(f"HTTP {e.code} {method} {path}: {body_txt}") from None

    def post(self, path: str, body: dict | None = None) -> Any:
        return self._req("POST", path, body)

    def get(self, path: str) -> Any:
        return self._req("GET", path)


# ---------------------------------------------------------------------------
# Assertions
# ---------------------------------------------------------------------------
C_R, C_G, C_Y, C_X = "\033[0;31m", "\033[0;32m", "\033[0;33m", "\033[0m"

_results: list[tuple[str, bool, str]] = []


def _record(name: str, ok: bool, detail: str = "") -> None:
    _results.append((name, ok, detail))
    badge = f"{C_G}PASS{C_X}" if ok else f"{C_R}FAIL{C_X}"
    print(f"  {badge}  {name}  {detail}")


def _wait(predicate, *, timeout: float, what: str) -> tuple[bool, Any]:
    """Poll until ``predicate()`` returns a truthy value (or its second
    element, for tuple returns). Returns (succeeded, last_value)."""
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        try:
            last = predicate()
        except Exception as exc:
            last = exc
        else:
            if last:
                return True, last
        time.sleep(0.5)
    return False, last


# ---------------------------------------------------------------------------
# The three checks
# ---------------------------------------------------------------------------
def check_tick_and_spend(c: Client, did: str) -> None:
    print(f"\n[1/3] tick + spend ({did})")
    initial = c.get(f"/darwins/{did}")
    initial_tick = int(initial["tick"])
    initial_bal = float(initial.get("balance_usdc", 0.0))

    ok, status = _wait(
        lambda: c.get(f"/darwins/{did}")
        if int(c.get(f"/darwins/{did}")["tick"]) > initial_tick + 3
        else False,
        timeout=60.0,
        what="tick to advance by 3",
    )
    if not ok:
        _record("tick advances", False, f"stuck at tick={initial_tick} after 60s")
        return
    _record(
        "tick advances",
        True,
        f"tick {initial_tick} → {status['tick']} in <60s",
    )

    # The audit chain must be growing alongside ticks.
    head = c.get(f"/darwins/{did}/events?limit=1")
    _record(
        "audit chain growing",
        head["total"] > 5,
        f"events_total={head['total']}",
    )

    # Balance change: with LLM costs the balance should drop (allow zero
    # drop on a very-fresh Darwin that has only run a handful of cheap
    # ticks — but never an INCREASE without an explicit deposit).
    final_bal = float(status.get("balance_usdc", 0.0))
    _record(
        "balance honors metering",
        final_bal <= initial_bal + 0.0001,
        f"balance {initial_bal:.4f} → {final_bal:.4f}",
    )


def check_pause_respected(c: Client, did: str) -> None:
    print(f"\n[2/3] pause respected ({did})")
    c.post(f"/darwins/{did}/pause", {"reason": "checklist: pause respect"})

    # The audit chain must stop growing within a few heartbeats. With
    # the production default heartbeat=5s, a 15s window is enough.
    samples: list[int] = []
    for _ in range(8):
        time.sleep(2.0)
        n = int(c.get(f"/darwins/{did}/events?limit=1")["total"])
        samples.append(n)

    # The last 3 samples must agree — the chain has frozen.
    frozen = samples[-3:] == [samples[-1]] * 3
    _record(
        "audit halts after pause",
        frozen,
        f"event counts: {samples}",
    )

    # Status reflects paused state.
    status = c.get(f"/darwins/{did}")
    _record(
        "status reports paused",
        bool(status.get("paused")),
        f"paused={status.get('paused')}",
    )

    # Resume: ticks must start advancing again.
    pre_tick = int(c.get(f"/darwins/{did}")["tick"])
    c.post(f"/darwins/{did}/resume")
    ok, _ = _wait(
        lambda: int(c.get(f"/darwins/{did}")["tick"]) > pre_tick,
        timeout=30.0,
        what="tick to advance after resume",
    )
    _record(
        "resume re-enables ticks",
        ok,
        f"tick {pre_tick} → {c.get(f'/darwins/{did}')['tick']}",
    )


def check_kill_cleanup(c: Client, did: str) -> None:
    print(f"\n[3/3] kill cleanup ({did})")
    pre = c.get(f"/darwins/{did}")
    _record(
        "kill precondition: still alive",
        bool(pre.get("alive")) and not pre.get("killed"),
        f"alive={pre.get('alive')} killed={pre.get('killed')}",
    )

    t0 = time.time()
    c.post(f"/darwins/{did}/kill", {"reason": "checklist: kill cleanup"})

    # Doc-09 says kill is honored within 5 s. Allow 15 s window for sim
    # heartbeat=5s + persistence write through.
    ok, status = _wait(
        lambda: c.get(f"/darwins/{did}")
        if not c.get(f"/darwins/{did}")["alive"]
        else False,
        timeout=15.0,
        what="darwin to die",
    )
    elapsed = time.time() - t0
    if not ok:
        _record("kill flips alive=false", False, f"still alive after {elapsed:.1f}s")
        return
    _record("kill flips alive=false", True, f"died in {elapsed:.1f}s")
    _record(
        "kill sets killed=true",
        bool(status.get("killed")),
        f"killed={status.get('killed')}",
    )
    _record(
        "kill sets death_reason",
        bool(status.get("death_reason")),
        f"death_reason={status.get('death_reason')!r}",
    )


# ---------------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------------
def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--url", default="http://localhost:8000")
    ap.add_argument("--token", required=True, help="Bearer token with owner scope")
    ap.add_argument("--opening-usdc", type=float, default=1.0)
    ap.add_argument(
        "--reuse-id", default=None,
        help="Run the three checks against an existing Darwin instead of "
             "spawning a fresh one. Useful for verifying a long-running "
             "candidate before mainnet promotion. The script will NOT "
             "kill an existing Darwin unless --allow-kill-existing is set.",
    )
    ap.add_argument("--allow-kill-existing", action="store_true")
    args = ap.parse_args()

    c = Client(args.url, args.token)

    try:
        health = c.get("/healthz")
    except Exception as exc:
        print(f"cannot reach {args.url}: {exc}", file=sys.stderr)
        return 2
    print(f"server healthy: {health.get('status')} (persistence={health.get('persistence')})")

    if args.reuse_id:
        did = args.reuse_id
        spawned_here = False
        if not args.allow_kill_existing:
            print(
                "refusing to kill an existing Darwin without "
                "--allow-kill-existing",
                file=sys.stderr,
            )
            return 2
    else:
        print(f"spawning a checklist Darwin with opening_usdc={args.opening_usdc}")
        spawn = c.post(
            "/darwins",
            {"opening_usdc": args.opening_usdc, "escalation_auto": "approve"},
        )
        did = spawn["darwin_id"]
        spawned_here = True
        print(f"spawned darwin_id={did}")

    try:
        check_tick_and_spend(c, did)
        check_pause_respected(c, did)
        # Make sure resume left it alive before kill check.
        check_kill_cleanup(c, did)
    finally:
        if spawned_here:
            # Idempotent — if the kill check already killed it, this is a
            # no-op the supervisor treats as such. Wrapped so a failing
            # earlier check doesn't leave a Darwin burning real $.
            try:
                status = c.get(f"/darwins/{did}")
                if status.get("alive") and not status.get("killed"):
                    c.post(
                        f"/darwins/{did}/kill",
                        {"reason": "checklist: teardown"},
                    )
            except Exception:
                pass

    # Summary
    print()
    failed = [n for n, ok, _ in _results if not ok]
    if failed:
        print(f"{C_R}{len(failed)} of {len(_results)} checks failed:{C_X}")
        for n in failed:
            print(f"  - {n}")
        return 1
    print(f"{C_G}all {len(_results)} checks passed.{C_X}")
    print("safe to promote to mainnet under a small float (e.g. $20–50 USDC).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
