"""Kill-switch operational drill (docs/engineering/09 §operational drills).

Spawns one Darwin, flips kill via the platform API, asserts the run loop
halts within budget. Reuses the same ``criterion_5_kill_latency``
assertion the Phase-0 soak gate uses — a passing drill matches the gate
behaviour bit-for-bit.

Usage::

    # Against a local platform:
    uv run python -m darwin_platform serve --heartbeat-seconds 0.05 &
    uv run python -m tools.drills.kill_switch

    # Against a remote endpoint:
    uv run python -m tools.drills.kill_switch \\
        --url https://darwin-dev.internal:8000 \\
        --auth-token owner-dev-token --budget-seconds 5
"""

from __future__ import annotations

import argparse
import sys
import time

import httpx

from tools.soak.assertions import criterion_5_kill_latency
from tools.soak.config import SoakConfig


def _client(url: str, token: str | None) -> httpx.Client:
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    return httpx.Client(base_url=url, headers=headers, timeout=15.0)


def run_drill(
    url: str,
    *,
    token: str | None = None,
    opening_usdc: float = 100.0,
    budget_seconds: float = 5.0,
    confirm_timeout_seconds: float = 30.0,
    poll_seconds: float = 0.2,
    warmup_seconds: float = 0.5,
) -> int:
    client = _client(url, token)

    spawn = client.post(
        "/darwins",
        json={"opening_usdc": opening_usdc, "escalation_auto": "approve"},
    )
    spawn.raise_for_status()
    darwin_id = spawn.json()["darwin_id"]
    print(f"[drill] spawned {darwin_id}")

    # Let the run loop actually tick before we kill it — confirms the
    # latency we measure is "kill flipped → loop halted", not "kill
    # flipped → loop never started".
    time.sleep(warmup_seconds)

    kill_t0 = time.monotonic()
    client.post(f"/darwins/{darwin_id}/kill", json={"reason": "drill: kill-switch"})

    kill_latency: float | None = None
    deadline = kill_t0 + confirm_timeout_seconds
    while time.monotonic() < deadline:
        try:
            st = client.get(f"/darwins/{darwin_id}").json()
        except Exception:
            time.sleep(poll_seconds)
            continue
        if not st.get("alive", True):
            kill_latency = time.monotonic() - kill_t0
            break
        time.sleep(poll_seconds)

    config = SoakConfig(
        url=url,
        kill_latency_budget_seconds=budget_seconds,
        kill_confirm_timeout_seconds=confirm_timeout_seconds,
        auth_token=token,
    )
    result = criterion_5_kill_latency(kill_latency, config)
    verdict = "PASS" if result.ok else "FAIL"
    print(f"[drill] kill-switch: {verdict} — {result.detail}")
    return 0 if result.ok else 1


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        description="Kill-switch operational drill (doc 09).",
    )
    p.add_argument("--url", default="http://127.0.0.1:8000")
    p.add_argument("--auth-token", default=None)
    p.add_argument("--opening-usdc", type=float, default=100.0)
    p.add_argument("--budget-seconds", type=float, default=5.0,
                   help="Kill latency budget (doc 09: 5s).")
    p.add_argument("--confirm-timeout-seconds", type=float, default=30.0,
                   help="Polling deadline before declaring TIMEOUT.")
    p.add_argument("--poll-seconds", type=float, default=0.2)
    p.add_argument("--warmup-seconds", type=float, default=0.5)
    args = p.parse_args(argv)
    return run_drill(
        args.url,
        token=args.auth_token,
        opening_usdc=args.opening_usdc,
        budget_seconds=args.budget_seconds,
        confirm_timeout_seconds=args.confirm_timeout_seconds,
        poll_seconds=args.poll_seconds,
        warmup_seconds=args.warmup_seconds,
    )


if __name__ == "__main__":
    sys.exit(main())
