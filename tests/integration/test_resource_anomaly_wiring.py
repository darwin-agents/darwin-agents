"""Resource-anomaly detector wired into the supervisor (doc 22; audit #26).

Covers: the serve flag builds the detector; a maintenance observation feeds the
detector per-interval deltas; and a sustained over-consumption spike produces an
alert AND installs an auto-throttle on the offending resource.
"""

from __future__ import annotations

import pytest

from darwin_platform.__main__ import _build_platform_services, build_parser
from darwin_platform.observability.resource_anomaly import ResourceAnomalyDetector
from darwin_platform.runtime.supervisor import Supervisor


def test_flag_builds_resource_anomaly_detector():
    args = build_parser().parse_args(
        ["serve", "--enable-resource-anomaly-detection"]
    )
    kwargs = _build_platform_services(args)
    assert isinstance(
        kwargs.get("resource_anomaly_detector"), ResourceAnomalyDetector
    )


def test_no_detector_without_flag():
    args = build_parser().parse_args(["serve"])
    assert "resource_anomaly_detector" not in _build_platform_services(args)


class _RecordingSink:
    def __init__(self):
        self.alerts = []

    def emit(self, alert):
        self.alerts.append(alert)


async def test_observation_feeds_detector_with_deltas():
    det = ResourceAnomalyDetector()
    sup = Supervisor(heartbeat_seconds=0.0, resource_anomaly_detector=det)
    try:
        status = await sup.spawn(opening_usdc=100.0)
        handle = sup._darwins[status.darwin_id]  # noqa: SLF001
        sup._run_resource_observation(handle)  # noqa: SLF001
        # The detector now holds rolling state for this Darwin, and the
        # cumulative snapshot was latched for the next delta.
        assert det.state_for(status.darwin_id) is not None
        assert handle.resource_runs == 1
        assert "llm_spend_usdc" in handle.resource_last_cumulative
    finally:
        await sup.shutdown(kill_running=True)


async def test_sustained_spike_alerts_and_auto_throttles_sandbox():
    det = ResourceAnomalyDetector(min_observations=4, warn_sigma=2.0,
                                  critical_sigma=3.0)
    sink = _RecordingSink()
    sup = Supervisor(heartbeat_seconds=0.0, resource_anomaly_detector=det,
                     alert_sink=sink)
    try:
        status = await sup.spawn(opening_usdc=100.0)
        did = status.darwin_id
        handle = sup._darwins[did]  # noqa: SLF001
        # Feed a flat sandbox baseline then a big spike via the gather seam.
        # Cumulative counts → deltas [0,1,1,1,1,96]: a quiet baseline then a
        # 96-call burst in one interval.
        for cumulative in (0, 1, 2, 3, 4, 100):
            sup._gather_resource_cumulative = (  # noqa: SLF001
                lambda _h, _c=cumulative: {
                    "llm_spend_usdc": 0.0, "egress_calls": 0.0,
                    "sandbox_calls": float(_c), "browser_calls": 0.0,
                }
            )
            sup._run_resource_observation(handle)  # noqa: SLF001
        # An alert reached the sink for the sandbox dimension ...
        assert any(
            a.kind == "resource_anomaly:sandbox_calls" for a in sink.alerts
        )
        # ... and the supervisor auto-throttled the sandbox resource.
        throttles = sup._throttle_service.list_for(did)  # noqa: SLF001
        assert any(t.kind == "sandbox" for t in throttles)
    finally:
        await sup.shutdown(kill_running=True)
