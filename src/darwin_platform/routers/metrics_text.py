"""Prometheus exposition for the supervisor (extracted from api.py)."""

from __future__ import annotations

import time
from typing import Any

from ..runtime.supervisor import Supervisor


# -- Prometheus exposition --------------------------------------------------
def _render_metrics(sup: Supervisor, started_ts: float) -> str:
    """Render the supervisor's current state as Prometheus text. No external
    dep — the format is simple text we can build directly. Pulled into a
    module-level function so tests can exercise it without spinning a full
    HTTP server."""
    lines: list[str] = []

    def metric(
        name: str,
        kind: str,
        help_: str,
        samples: list[tuple[dict[str, str], float]],
    ) -> None:
        lines.append(f"# HELP {name} {help_}")
        lines.append(f"# TYPE {name} {kind}")
        for labels, value in samples:
            if labels:
                lbl = ",".join(f'{k}="{_escape_label(v)}"' for k, v in sorted(labels.items()))
                lines.append(f"{name}{{{lbl}}} {_fmt(value)}")
            else:
                lines.append(f"{name} {_fmt(value)}")

    # -- per-Darwin state buckets ----------------------------------------
    alive = killed = paused = quarantined = 0
    for h in sup._darwins.values():  # noqa: SLF001
        if h.killed:
            killed += 1
        elif h.runtime.state.alive:
            alive += 1
        if h.runtime.state.paused:
            paused += 1
        if h.quarantine.active:
            quarantined += 1
    metric(
        "darwin_platform_darwins",
        "gauge",
        "Number of Darwins by state.",
        [
            ({"state": "alive"}, alive),
            ({"state": "killed"}, killed),
            ({"state": "paused"}, paused),
            ({"state": "quarantined"}, quarantined),
        ],
    )

    # -- audit log -------------------------------------------------------
    audit_total = len(sup.audit_log)
    metric(
        "darwin_platform_audit_entries_total",
        "counter",
        "Total operator audit entries ever written.",
        [({}, float(audit_total))],
    )

    # -- escalations -----------------------------------------------------
    open_escalations = sum(
        len(sup.router.for_darwin(did, include_resolved=False)) for did in sup.list_ids()
    )
    metric(
        "darwin_platform_escalations_open",
        "gauge",
        "Currently-open escalation tickets across the fleet.",
        [({}, float(open_escalations))],
    )

    # -- alerts ----------------------------------------------------------
    severity_counts: dict[str, int] = {"info": 0, "warn": 0, "critical": 0}
    for alert in sup.monitor.all_alerts():
        severity_counts[alert.severity.value] = severity_counts.get(alert.severity.value, 0) + 1
    metric(
        "darwin_platform_alerts_total",
        "counter",
        "Monitor alerts emitted by severity, since process start (or restored from persistence).",
        [({"severity": sev}, float(count)) for sev, count in severity_counts.items()],
    )

    # -- billing ---------------------------------------------------------
    fleet = sup.billing_service.fleet_summary()
    metric(
        "darwin_platform_billing_revenue_usdc",
        "gauge",
        "Total platform revenue (markup + harvest fees) across the fleet.",
        [({}, float(fleet["total"]["platform_revenue_usdc"]))],
    )
    metric(
        "darwin_platform_billing_harvest_fees_usdc",
        "gauge",
        "Total harvest fees collected across the fleet.",
        [({}, float(fleet["total"]["harvest_fees_usdc"]))],
    )

    # -- lineage --------------------------------------------------------
    edges = sup._lineage_registry.edges()  # noqa: SLF001
    metric(
        "darwin_platform_lineage_edges_total",
        "counter",
        "Parent->child lineage edges observed (across all reproductions).",
        [({}, float(len(edges)))],
    )

    # -- vault: watermarks issued + retention sweeps ---------------------
    vault_entries = sum(
        1
        for _ in sup.vault_admin._index.values()
        for _ in _  # noqa: SLF001
    )
    metric(
        "darwin_platform_vault_entries_total",
        "gauge",
        "Vault entries currently tracked across the fleet.",
        [({}, float(vault_entries))],
    )
    watermarked = 0
    for darwin_id, by_path in sup.vault_admin._index.items():  # noqa: SLF001
        for ref in by_path.values():
            if ref.watermarked:
                watermarked += 1
    metric(
        "darwin_platform_vault_watermarked_total",
        "gauge",
        "Vault entries currently carrying an embedded HMAC watermark.",
        [({}, float(watermarked))],
    )

    # -- sanctions refusals (recorded in operator audit) -----------------
    sanctions_refusals = 0
    for entry in sup.audit_log.entries(action="wallet_error"):
        if "sanctions" in (entry.metadata.get("reason") or ""):
            sanctions_refusals += 1
    metric(
        "darwin_platform_sanctions_refusals_total",
        "counter",
        "Wallet outbound transfers refused by the sanctions screener.",
        [({}, float(sanctions_refusals))],
    )

    # -- resource throttles ----------------------------------------------
    throttles = sup._throttle_service  # noqa: SLF001
    for kind in ("sandbox", "browser", "egress"):
        used_total = 0
        installed_total = 0
        for h in sup._darwins.values():  # noqa: SLF001
            t = throttles.get(darwin_id=h.darwin_id, kind=kind)
            if t is not None:
                installed_total += 1
                used_total += throttles.used_today(
                    darwin_id=h.darwin_id,
                    kind=kind,
                )
        metric(
            "darwin_platform_throttle_installed",
            "gauge",
            "Number of Darwins with an installed throttle for this resource.",
            [({"kind": kind}, float(installed_total))],
        )
        metric(
            "darwin_platform_throttle_used_today",
            "gauge",
            "Aggregate count of throttled calls consumed today by resource.",
            [({"kind": kind}, float(used_total))],
        )

    # -- browser cache ----------------------------------------------------
    bc_stats = sup.browser_stats()
    metric(
        "darwin_platform_browser_sessions_total",
        "counter",
        "Browser sessions recorded across the fleet.",
        [({}, float(bc_stats.get("total_sessions", 0)))],
    )
    metric(
        "darwin_platform_browser_cost_usdc",
        "gauge",
        "Total browser-session cost (USDC) attributed across the fleet.",
        [({}, float(bc_stats.get("total_cost_usdc", 0.0)))],
    )
    promoted = sum(1 for s in bc_stats.get("signatures", []) if s.get("promoted"))
    metric(
        "darwin_platform_browser_cache_promoted",
        "gauge",
        "Browser signatures currently promoted to the cached replay path.",
        [({}, float(promoted))],
    )

    # -- LLM circuit breaker state (only when LiteLLM transport is wired) -
    breaker_states: dict[str, int] = {"closed": 0, "open": 0, "half_open": 0}
    for h in sup._darwins.values():  # noqa: SLF001
        transport = h.runtime.llm._t  # noqa: SLF001
        cb = getattr(transport, "circuit_breaker", None)
        if cb is None:
            continue
        for _model, st in cb.snapshot().items():
            state = st.get("state", "closed")
            breaker_states[state] = breaker_states.get(state, 0) + 1
    metric(
        "darwin_platform_llm_circuit_breaker_models",
        "gauge",
        "Per-model LLM circuit-breaker state counts across the fleet.",
        [({"state": s}, float(c)) for s, c in breaker_states.items()],
    )

    # -- resume events ----------------------------------------------------
    resume_entries = len(sup.audit_log.entries(action="resume"))
    metric(
        "darwin_platform_resume_events_total",
        "counter",
        "Number of Darwin resumes since process start (or restored from audit log).",
        [({}, float(resume_entries))],
    )

    # -- harvest rule fires + scheduler rules ----------------------------
    harvest_rule_fires = len(sup.audit_log.entries(action="harvest_rule_fired"))
    metric(
        "darwin_platform_harvest_rule_fires_total",
        "counter",
        "Recurring-harvest rule fires recorded by the scheduler.",
        [({}, float(harvest_rule_fires))],
    )
    installed_rules = len(sup._harvest_scheduler.list())  # noqa: SLF001
    metric(
        "darwin_platform_harvest_rules_installed",
        "gauge",
        "Currently-installed recurring harvest rules.",
        [({}, float(installed_rules))],
    )

    # -- Phase 1 cost-governance metrics (doc 21 §"Metering ...") --------
    # Per-Darwin daily spend (sum of debits today, in USDC). The
    # wallet's spent_today is authoritative; the platform reads through
    # it for the dashboard.
    daily_spend_samples: list[tuple[dict[str, str], float]] = []
    llm_cost_samples: list[tuple[dict[str, str], float]] = []
    neuron_cost_total: float = 0.0
    revenue_samples: list[tuple[dict[str, str], float]] = []
    for h in sup._darwins.values():  # noqa: SLF001
        try:
            spent = float(h.stack.wallet.spent_today_usdc())
        except Exception:
            spent = 0.0
        daily_spend_samples.append(({"darwin_id": h.darwin_id}, float(spent)))
        # LLM cost: pull from the per-Darwin LLM router's running total
        # when the transport exposes one; otherwise zero. The router's
        # ``cost_today_usdc`` is the canonical genome-side counter.
        llm_cost = 0.0
        try:
            llm_cost = float(getattr(h.runtime.llm, "cost_today_usdc", lambda: 0.0)())
        except Exception:
            llm_cost = 0.0
        llm_cost_samples.append(({"darwin_id": h.darwin_id}, llm_cost))
        # Revenue: read the genome's last analytics ROI output from
        # ``state.metrics`` when present. Best-effort; absence ⇒ 0.
        revenue = 0.0
        try:
            metrics_blob = getattr(h.runtime.state, "metrics", {}) or {}
            revenue = float(metrics_blob.get("revenue_usdc", 0.0))
        except Exception:
            revenue = 0.0
        revenue_samples.append(({"darwin_id": h.darwin_id}, revenue))
    metric(
        "darwin_daily_spend_usdc",
        "gauge",
        "Per-Darwin spend recorded so far today (USDC). Alert if > $20/day.",
        daily_spend_samples or [({}, 0.0)],
    )
    metric(
        "darwin_llm_cost_usdc",
        "gauge",
        "Per-Darwin LLM token spend today (USDC, from the genome's router).",
        llm_cost_samples or [({}, 0.0)],
    )
    # Neuron cost: aggregate count of distinct dispatches recorded on the
    # audit chain (each ``intent:neuron.*`` is one dispatch). Phase 1 uses
    # the chain rather than a per-neuron gauge so a restarted Darwin's
    # historical dispatches are preserved.
    for entry in sup.audit_log.entries(action="spawn"):
        # Spawn entries are not dispatches; we filter by counting via the
        # per-Darwin audit sinks below.
        continue
    for h in sup._darwins.values():  # noqa: SLF001
        try:
            rows = h.stack.audit_sink.read_all()
        except Exception:
            rows = []
        for r in rows:
            k = r.get("kind", "")
            if isinstance(k, str) and k.startswith("intent:neuron."):
                neuron_cost_total += 1.0
    metric(
        "darwin_neuron_dispatch_total",
        "counter",
        "Total neuron dispatches recorded on the genome audit chains.",
        [({}, neuron_cost_total)],
    )
    metric(
        "darwin_revenue_usdc",
        "gauge",
        "Per-Darwin observed revenue (USDC) from the latest analytics measurement.",
        revenue_samples or [({}, 0.0)],
    )

    # -- Phase 1 staging service metrics (doc 21) ------------------------
    try:
        staging_snap = sup.patch_staging.quota.snapshot()
        metric(
            "darwin_platform_staging_quota",
            "gauge",
            "Patch-staging concurrency: active vs. cap.",
            [
                ({"state": "active"}, float(staging_snap["active"])),
                ({"state": "max"}, float(staging_snap["max_concurrent"])),
            ],
        )
        metric(
            "darwin_platform_staging_evaluations_total",
            "counter",
            "Patch-staging evaluations acquired (total).",
            [({}, float(staging_snap["total_acquired"]))],
        )
        metric(
            "darwin_platform_staging_quota_rejections_total",
            "counter",
            "Patch-staging acquires refused due to quota.",
            [({}, float(staging_snap["total_rejected"]))],
        )
        staging_promotions = sum(1 for v in sup.patch_staging.history() if v.promote)
        metric(
            "darwin_platform_staging_promotions_total",
            "counter",
            "Patch-staging verdicts that promoted the candidate.",
            [({}, float(staging_promotions))],
        )
    except Exception:
        pass

    # -- process uptime --------------------------------------------------
    metric(
        "darwin_platform_uptime_seconds",
        "gauge",
        "Seconds since this supervisor process started.",
        [({}, time.time() - started_ts)],
    )

    return "\n".join(lines) + "\n"


def _escape_label(v: Any) -> str:
    """Escape a Prometheus label value per spec: backslash, double-quote,
    newline. Cheap; values here are short."""
    return str(v).replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def _fmt(v: float) -> str:
    """Prometheus prefers integers as ints, floats with limited precision."""
    if isinstance(v, int) or float(v).is_integer():
        return str(int(v))
    return f"{v:.6f}"
