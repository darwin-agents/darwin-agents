"""DR failover drill — leader-election handover (docs/engineering/08, 09).

Two supervisor replicas point at one Postgres; only one holds the
advisory lock and serves writes (its ``/readyz`` → 200). On leader loss
a follower must acquire the lock and start serving within budget.

This drill identifies the current leader, waits for the operator to
take it down (manually, or by passing ``--kill-cmd``), then times how
long until the follower's ``/readyz`` reports leader.

Setup (one-time)::

    # Bring up Postgres + two supervisors against it. The simplest is
    # docker-compose with two supervisor services, but any two
    # processes pointing at the same DATABASE_URL work.
    export DATABASE_URL=postgres://darwin:dev@localhost:5432/darwin
    uv run python -m darwin_platform serve --port 8001 --db "$DATABASE_URL" &
    uv run python -m darwin_platform serve --port 8002 --db "$DATABASE_URL" &

Run the drill::

    # Interactive (operator kills the leader on prompt):
    uv run python -m tools.drills.dr_failover \\
        --replica http://127.0.0.1:8001 \\
        --replica http://127.0.0.1:8002

    # Automated (drill kills the leader via shell):
    uv run python -m tools.drills.dr_failover \\
        --replica http://127.0.0.1:8001 \\
        --replica http://127.0.0.1:8002 \\
        --kill-cmd "docker compose stop sup1 sup2 --filter leader"

Budget: doc 09 names a 30-second handover SLO for advisory-lock
re-acquisition; the drill defaults to that.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
import time

import httpx


def _is_leader(client: httpx.Client) -> bool | None:
    """``True`` / ``False`` / ``None`` (probe failed)."""
    try:
        r = client.get("/readyz", timeout=5.0)
    except Exception:
        return None
    if r.status_code >= 500:
        return False  # 503 = follower (doc 08 readyz contract)
    if r.status_code != 200:
        return None
    try:
        return bool(r.json().get("leader", False))
    except Exception:
        return None


def run_drill(
    replicas: list[str],
    *,
    token: str | None = None,
    budget_seconds: float = 30.0,
    poll_seconds: float = 0.5,
    kill_cmd: str | None = None,
) -> int:
    if len(replicas) < 2:
        print("[drill] need at least two replicas to test failover", file=sys.stderr)
        return 2
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    clients = {url: httpx.Client(base_url=url, headers=headers, timeout=5.0)
               for url in replicas}

    leader_url: str | None = None
    for url, c in clients.items():
        if _is_leader(c):
            leader_url = url
            break
    if leader_url is None:
        print("[drill] no replica reports leader — election not started or stack down",
              file=sys.stderr)
        return 1
    follower_urls = [u for u in replicas if u != leader_url]
    print(f"[drill] leader: {leader_url}   followers: {follower_urls}")

    # Trigger the failover.
    if kill_cmd:
        print(f"[drill] running --kill-cmd: {kill_cmd}")
        subprocess.run(kill_cmd, shell=True, check=False)  # noqa: S602
    else:
        print(f"[drill] manually take down the leader at {leader_url} now")
        input("[drill] press Enter once the leader is down... ")

    # Time how long until a follower takes over.
    failover_t0 = time.monotonic()
    new_leader: str | None = None
    deadline = failover_t0 + max(budget_seconds * 2.0, budget_seconds + 30.0)
    while time.monotonic() < deadline:
        for url in follower_urls:
            if _is_leader(clients[url]):
                new_leader = url
                break
        if new_leader is not None:
            break
        time.sleep(poll_seconds)

    if new_leader is None:
        print(f"[drill] FAIL — no follower became leader within "
              f"{deadline - failover_t0:.0f}s")
        return 1

    elapsed = time.monotonic() - failover_t0
    ok = elapsed < budget_seconds
    verdict = "PASS" if ok else "FAIL"
    print(f"[drill] dr_failover: {verdict} — {new_leader} took leadership in "
          f"{elapsed:.2f}s (budget {budget_seconds:.0f}s)")
    return 0 if ok else 1


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        description="DR failover operational drill (doc 09).",
    )
    p.add_argument("--replica", action="append", default=[],
                   metavar="URL",
                   help="Supervisor replica URL (pass twice or more).")
    p.add_argument("--auth-token", default=None)
    p.add_argument("--budget-seconds", type=float, default=30.0,
                   help="Failover handover budget (doc 09: 30s).")
    p.add_argument("--poll-seconds", type=float, default=0.5)
    p.add_argument("--kill-cmd", default=None,
                   help="Shell command to take the leader down. "
                        "Without it the drill prompts the operator interactively.")
    args = p.parse_args(argv)
    return run_drill(
        args.replica,
        token=args.auth_token,
        budget_seconds=args.budget_seconds,
        poll_seconds=args.poll_seconds,
        kill_cmd=args.kill_cmd,
    )


if __name__ == "__main__":
    sys.exit(main())
