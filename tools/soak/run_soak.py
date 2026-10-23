"""Phase 0 soak-test driver (docs/engineering/20 §"The soak test").

Spawns one Darwin against an already-running platform endpoint, polls
it for the configured duration, kills it, fetches the full event log,
and asserts the six Phase-0 acceptance criteria. Exits 0 on pass,
non-zero on any failure — CI-friendly.

The platform must be started separately, e.g.::

    # realistic-pace 24h soak (the real Phase-0 gate)
    uv run python -m darwin_platform serve --db soak.db --heartbeat-seconds 600
    uv run python -m tools.soak.run_soak --duration 24h

    # fast CI smoke over the same code path
    uv run python -m darwin_platform serve --heartbeat-seconds 0.05
    uv run python -m tools.soak.run_soak --duration 60s --poll 5s

Pointing at a remote endpoint + auth token::

    uv run python -m tools.soak.run_soak --duration 24h \\
        --url https://darwin-dev.internal:8000 --auth-token owner-dev-token
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from dataclasses import dataclass, field
from typing import Any

import httpx

from .assertions import (
    CriterionResult,
    criterion_1_uptime,
    criterion_2_neuron_dispatches,
    criterion_3_hash_chain,
    criterion_4_ledger_reconciles,
    criterion_5_kill_latency,
    criterion_6_replayable,
)
from .config import SoakConfig, parse_duration


@dataclass
class SoakReport:
    """The full outcome of a soak run."""

    url: str
    darwin_id: str
    duration_seconds: float
    observed_seconds: float
    event_count: int
    kill_latency_seconds: float | None
    criteria: list[CriterionResult] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return bool(self.criteria) and all(c.ok for c in self.criteria)

    def to_json(self) -> dict[str, Any]:
        return {
            "url": self.url,
            "darwin_id": self.darwin_id,
            "duration_seconds": self.duration_seconds,
            "observed_seconds": self.observed_seconds,
            "event_count": self.event_count,
            "kill_latency_seconds": self.kill_latency_seconds,
            "criteria": [c.to_json() for c in self.criteria],
            "ok": self.ok,
        }


def _log(msg: str) -> None:
    print(f"[soak] {msg}", flush=True)


def _client(config: SoakConfig) -> httpx.Client:
    headers = {}
    if config.auth_token:
        headers["Authorization"] = f"Bearer {config.auth_token}"
    return httpx.Client(base_url=config.url, headers=headers, timeout=15.0)


def _fetch_all_events(
    client: httpx.Client, darwin_id: str, config: SoakConfig,
) -> tuple[list[dict[str, Any]], int]:
    """Page through ``/events`` and accumulate the whole log. Returns
    ``(events, total)``; if the cap is hit ``len(events) < total`` and
    criterion 3 fails loudly."""
    events: list[dict[str, Any]] = []
    offset = 0
    page = 1000
    total = 0
    while True:
        r = client.get(
            f"/darwins/{darwin_id}/events",
            params={"limit": page, "offset": offset},
        )
        r.raise_for_status()
        body = r.json()
        total = int(body.get("total", 0))
        batch = body.get("events", []) or []
        events.extend(batch)
        offset += len(batch)
        if not batch or offset >= total or len(events) >= config.max_events:
            break
    return events, total


def run_soak(config: SoakConfig) -> SoakReport:
    """Execute the soak end-to-end and return a :class:`SoakReport`."""
    client = _client(config)
    soak_start = time.monotonic()

    # -- preflight: the endpoint must already be up --------------------
    try:
        health = client.get("/healthz")
        health.raise_for_status()
    except Exception as exc:  # noqa: BLE001 — operator-facing message
        raise SystemExit(
            f"[soak] cannot reach {config.url}/healthz: {exc}\n"
            f"[soak] start the platform first: "
            f"`python -m darwin_platform serve`"
        ) from exc
    _log(f"endpoint healthy: {config.url} ({health.json()})")

    # -- spawn ---------------------------------------------------------
    # Unattended run: nobody answers escalations, so auto-approve explicitly.
    r = client.post(
        "/darwins",
        json={"opening_usdc": config.opening_usdc, "escalation_auto": "approve"},
    )
    r.raise_for_status()
    spawned = r.json()
    darwin_id = spawned["darwin_id"]
    _log(
        f"spawned darwin {darwin_id} "
        f"(opening ${config.opening_usdc:.2f})"
    )

    # -- poll loop -----------------------------------------------------
    observations: list[dict[str, Any]] = []
    poll = config.effective_poll_interval()

    def _observe() -> dict[str, Any]:
        elapsed = time.monotonic() - soak_start
        http_ok = healthz_ok = alive = False
        tick = 0
        try:
            st = client.get(f"/darwins/{darwin_id}")
            http_ok = st.status_code == 200
            if http_ok:
                body = st.json()
                alive = bool(body.get("alive"))
                tick = int(body.get("tick", 0))
        except Exception:  # noqa: BLE001 — a failed poll is a data point
            http_ok = False
        try:
            healthz_ok = client.get("/healthz").status_code == 200
        except Exception:  # noqa: BLE001
            healthz_ok = False
        obs = {
            "elapsed": elapsed, "http_ok": http_ok,
            "healthz_ok": healthz_ok, "alive": alive, "tick": tick,
        }
        observations.append(obs)
        return obs

    obs = _observe()  # t≈0 baseline
    _log(
        f"t+{obs['elapsed']:.0f}s  tick={obs['tick']}  "
        f"alive={obs['alive']}"
    )
    while time.monotonic() - soak_start < config.duration_seconds:
        remaining = config.duration_seconds - (time.monotonic() - soak_start)
        time.sleep(min(poll, max(0.0, remaining)))
        obs = _observe()
        _log(
            f"t+{obs['elapsed']:.0f}s  tick={obs['tick']}  "
            f"alive={obs['alive']}  "
            f"http_ok={obs['http_ok']}  healthz_ok={obs['healthz_ok']}"
        )

    # -- final pre-kill status ----------------------------------------
    pre_kill = client.get(f"/darwins/{darwin_id}").json()
    _log(f"duration reached; pre-kill tick={pre_kill.get('tick')}")

    # -- criterion 5: timed kill --------------------------------------
    kill_t0 = time.monotonic()
    client.post(f"/darwins/{darwin_id}/kill", json={"reason": "soak complete"})
    kill_latency: float | None = None
    kill_deadline = kill_t0 + config.kill_confirm_timeout_seconds
    while time.monotonic() < kill_deadline:
        try:
            st = client.get(f"/darwins/{darwin_id}").json()
        except Exception:  # noqa: BLE001
            time.sleep(0.05)
            continue
        if not st.get("alive"):
            kill_latency = time.monotonic() - kill_t0
            break
        time.sleep(0.05)
    _log(
        f"kill latency: "
        f"{'%.2fs' % kill_latency if kill_latency is not None else 'TIMEOUT'}"
    )

    # -- final status + full event log (post-death, complete) ---------
    final_status = client.get(f"/darwins/{darwin_id}").json()
    events, total = _fetch_all_events(client, darwin_id, config)
    _log(f"fetched {len(events)} of {total} audit events")
    client.close()

    # -- assert the six criteria --------------------------------------
    criteria = [
        criterion_1_uptime(observations, config),
        criterion_2_neuron_dispatches(events, config),
        criterion_3_hash_chain(events, total),
        criterion_4_ledger_reconciles(events, config),
        criterion_5_kill_latency(kill_latency, config),
        criterion_6_replayable(events, final_status),
    ]
    return SoakReport(
        url=config.url,
        darwin_id=darwin_id,
        duration_seconds=config.duration_seconds,
        observed_seconds=(
            observations[-1]["elapsed"] if observations else 0.0
        ),
        event_count=len(events),
        kill_latency_seconds=kill_latency,
        criteria=criteria,
    )


def _parse_args(argv: list[str] | None) -> SoakConfig:
    p = argparse.ArgumentParser(
        prog="tools.soak.run_soak",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument(
        "--url", default="http://127.0.0.1:8000",
        help="Platform endpoint to soak (default: http://127.0.0.1:8000)",
    )
    p.add_argument(
        "--duration", default="24h",
        help="Soak duration: '24h', '90m', '30s', or bare seconds "
             "(default: 24h)",
    )
    p.add_argument(
        "--poll", default="60s",
        help="Status poll interval (default: 60s)",
    )
    p.add_argument(
        "--opening-usdc", type=float, default=1000.0,
        help="Opening balance for the spawned Darwin (default: 1000.0 — "
             "soaks fund generously so bankruptcy does not truncate the run)",
    )
    p.add_argument(
        "--auth-token", default=None,
        help="Bearer token, when the endpoint has auth enabled",
    )
    p.add_argument(
        "--json", action="store_true",
        help="Emit a JSON report on stdout (machine-readable)",
    )
    args = p.parse_args(argv)
    return SoakConfig(
        url=args.url.rstrip("/"),
        duration_seconds=parse_duration(args.duration),
        poll_interval_seconds=parse_duration(args.poll),
        opening_usdc=args.opening_usdc,
        auth_token=args.auth_token,
    ), args.json


def main(argv: list[str] | None = None) -> int:
    config, as_json = _parse_args(argv)
    report = run_soak(config)
    if as_json:
        print(json.dumps(report.to_json(), indent=2))
    else:
        print("\n=== Phase 0 soak test ===")
        print(
            f"endpoint: {report.url}   "
            f"darwin: {report.darwin_id}   "
            f"observed: {report.observed_seconds:.0f}s   "
            f"events: {report.event_count}"
        )
        for c in report.criteria:
            marker = "PASS" if c.ok else "FAIL"
            print(f"  [{marker}] {c.number}. {c.name}: {c.detail}")
        print(f"\noverall: {'PASS' if report.ok else 'FAIL'}")
    return 0 if report.ok else 1


if __name__ == "__main__":
    sys.exit(main())
