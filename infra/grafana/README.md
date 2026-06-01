# Grafana dashboards for the Darwin platform

This directory ships a ready-to-import dashboard for the supervisor's
`/metrics` endpoint.

## What's here

- `darwin-platform-dashboard.json` — a 16-panel dashboard covering
  every metric the supervisor exposes:

  | Section | Panels |
  |--|--|
  | **Fleet** | Darwins by state, audit entries, open escalations, uptime |
  | **Alerts + safety** | Alerts by severity (rate), sanctions refusals |
  | **Throttles** | Installed throttles, throttle usage today (by kind) |
  | **Browser** | Sessions, cost USDC, cached signatures promoted |
  | **LLM** | Circuit-breaker state (closed/open/half_open) |
  | **Continuity** | Resume events |
  | **Vault** | Watermarked entries, total entries |
  | **Reproduction** | Harvest rule fires, lineage edges |
  | **Billing** | Platform revenue, harvest fees |

## Import

### Manual (one dashboard, ad hoc)

In Grafana: **Dashboards → New → Import**. Upload
`darwin-platform-dashboard.json`. Pick the Prometheus datasource that
scrapes the supervisor's `/metrics` endpoint.

### Provisioning (declarative, recommended for production)

Mount the JSON file at `/etc/grafana/provisioning/dashboards/` and add
a provisioning entry, e.g.:

```yaml
# /etc/grafana/provisioning/dashboards/darwin.yaml
apiVersion: 1
providers:
  - name: 'darwin-platform'
    folder: 'Darwin'
    type: file
    options:
      path: /var/lib/grafana/dashboards/darwin
```

Then drop `darwin-platform-dashboard.json` in
`/var/lib/grafana/dashboards/darwin/` (via configmap, sidecar, or your
GitOps tool).

### kube-prometheus-stack

When using the kube-prometheus-stack chart, the dashboard can be
distributed via a ConfigMap labelled
`grafana_dashboard: "1"`. Helm chart values:

```yaml
darwin-platform:
  serviceMonitor:
    enabled: true   # so Prometheus scrapes /metrics
```

Then provide the dashboard ConfigMap separately (the Darwin platform
chart doesn't render it; the dashboard is consumer-side).

## Where the metrics come from

Every panel queries a metric emitted by
`darwin_platform.api._render_metrics`. The Prometheus scrape config
needs the supervisor's pod with the `/metrics` path. The Darwin
platform Helm chart's `serviceMonitor.enabled: true` value handles
this for `kube-prometheus-stack`; for manual scrape, see
`docker/prometheus.yml` for the docker-compose pattern.

## Alerting

The dashboard panels include color thresholds where appropriate
(escalations, alert severities). For prod-grade alerting, configure
PromQL alerts in your Prometheus instance and wire them to the
supervisor's `--pagerduty-routing-key` so the same escalations also
arrive in PagerDuty.

Example alert rules:

```yaml
# alerts/darwin.yaml
groups:
  - name: darwin
    rules:
      - alert: DarwinCriticalAlertsRising
        expr: |
          increase(darwin_platform_alerts_total{severity="critical"}[10m]) > 0
        for: 5m
        labels: {severity: page}
        annotations:
          summary: "Critical alert from Darwin supervisor"

      - alert: DarwinSanctionsRefusal
        expr: |
          increase(darwin_platform_sanctions_refusals_total[1h]) > 0
        labels: {severity: page}
        annotations:
          summary: "A wallet transfer was refused by the sanctions screener"
```
