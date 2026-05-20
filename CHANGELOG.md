# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

Nothing has been tagged or released on GitHub yet. The first release will be **0.0.1**.

## [Unreleased]

### Added

- docs(engineering): add cross-cutting contracts, post-mortem template, and evolution tuning guide (docs 28–30)
- feat(platform): embed PDF watermarks in /Info metadata via pypdf
- feat(platform): add per-Darwin orchestrator entrypoint and control API
- feat(platform): add outbound egress proxy with allowlist enforcement
- feat(platform): add `--driver=k8s` CLI flags for K8sDarwinDriver wiring
- feat(supervisor): delegate spawn to K8sDarwinDriver when k8s_driver is set
- feat(platform): REST endpoints for fiat ramp and cross-pollination inject tests
- feat(supervisor): fiat ramp ops and cross-pollination lesson injection on spawn
- feat(platform): wire Solana and Stellar SDK clients in multichain CLI path
- feat(adapters): add Solana and Stellar SDK chain client builders
- feat(platform): add K8sDarwinDriver and per-Darwin manifest builders
- feat(platform): add DarwinDriver protocol for in-process and K8s lifecycles
- feat(orchestrator): admit Path-C neuron proposals through mutation pipeline
- docs(engineering): add testing strategy and configuration reference (docs 26–27)
- docs(engineering): add data model reference, security threat model, and operational runbook (docs 23–25)
- feat(platform): CLI for multichain wallet, sanctions providers, and sinks
- feat(platform): REST routes for signing rotation and Path-C proposals
- feat(supervisor): cross-pollination, key rotation, and Path-C proposal ops
- feat(platform): schema v2 for retired signing keys and SQLite thread safety
- feat(genome): wire Path-C NeuronAdmissionGate into DarwinRuntime
- feat(sim): add 90-day simulation calibration harness
- feat(platform): add Slack, webhook, and SMTP notification sinks
- feat(platform): add signing key rotation with retired-key drain window
- feat(platform): add signed cross-pollination lesson archive
- feat(adapters): add StagehandReplayClient for cached browser replay path
- feat(adapters): add Solana, Stellar, and multi-chain USDC router
- feat(adapters): add Stripe and MoonPay fiat on/off-ramp adapters
- feat(platform): implement Chainalysis and TRM sanctions screeners
- feat(machinery): add Path-C neuron proposal and interpreted admission gate
- feat(fakes): add support_reply LLM task stub for support responder neuron
- feat(neurons): complete founding catalog with accounts, ads, trading, support
- feat(platform): REST routes for resource throttles and vault metrics
- feat(supervisor): wire persistence, throttles, watermarks, and spread-spend
- feat(platform): persist per-Darwin audit, checkpoints, manifests, signing key
- feat(platform): browser vision-mode fallback for canvas-only failures
- feat(provisioning): add SubprocessProvisioningBackend for external drivers
- feat(platform): throttle sandbox, browser, and egress per Darwin
- feat(platform): embed HMAC watermarks on sensitive vault uploads
- feat(platform): detect spread-spend smear evasion in monitoring
- feat(adapters): add per-model circuit breaker to LiteLLM transport
- feat(fakes): load and export signing key bytes for HA resume
- feat(genome): reconcile reflect beliefs against audit log ground truth
- feat(platform): CLI flags for e2b sandbox and sanctions denylist file
- feat(platform): REST endpoints for harvest rules, vault, browser, resources
- feat(supervisor): wire provisioning, vault admin, harvest, cache, and resume
- feat(platform): persist darwin lifecycle for HA runtime resume
- feat(platform): add browser session cache and cost attribution
- feat(platform): add scheduled harvest rules evaluator
- feat(platform): add vault admin and KYC retention sweep services
- feat(platform): add provisioning service with quotas and scoped port
- feat(adapters): screen RealWallet pay and harvest against sanctions screener
- feat(platform): add OFAC-style sanctions screening for outbound transfers
- feat(genome): grant sandbox substrate to compute.run neurons
- feat(fakes): sanctions screening, vault delete, and new LLM task stubs
- feat(neurons): add publisher, analytics, and research catalog neurons
- feat(platform): add CLI flags for web3, hvac, and browserbase adapters
- feat(supervisor): inject real wallet, vault, browser, and x402 factories
- feat(genome): wire x402 HTTP client into NeuronContext for web neurons
- feat(adapters): export real provider adapter types from package
- feat(adapters): add browser, sandbox, and x402 protocol adapters
- feat(adapters): add HvacVaultClient for HashiCorp KV v2
- feat(adapters): add Web3ChainClient for EVM USDC wallet backend
- feat(platform): wire --leader-election flags on darwin-platform CLI
- feat(platform): add /readyz readiness probe for HA routing
- feat(supervisor): gate spawn on leadership and cancel drives on failover
- feat(platform): add Postgres advisory-lock leader election
- feat(platform): dual SQLite and Postgres persistence backends
- feat(platform): add darwin-platform CLI for serve and signing-key
- feat(platform): add healthz and Prometheus metrics endpoints
- feat(platform): add RealVaultBackend adapter with VaultClient protocol
- feat(platform): add RealWallet adapter with ChainClient protocol
- feat(platform): wire persistence_path and LineageRegistry into supervisor
- feat(platform): add SQLite persistence for platform state
- feat(platform): record operator actions and expose fleet audit API
- feat(platform): add hash-chained operator audit log
- feat(platform): wire llm_transport_factory into supervisor spawn
- feat(fakes): allow injecting real LLM transport on FakeStack
- feat(platform): add LiteLLM transport adapter for real LLM gateway
- feat(platform): wire billing harvest, metered sweep, and REST endpoints
- feat(platform): add billing service for harvest fees and metered markup
- feat(fakes): add harvest_to_owner for profit withdrawals
- feat(platform): throttle endpoints, auto-throttle, and amendment reproduction
- feat(evolution): support amendment layers and forced reproduction
- feat(llm-router): add platform-installed per-tier throttles
- feat(platform): add bearer token auth with owner and operator scopes
- feat(platform): extend wallet policy with per-lifetime cap
- feat(neurons): thread owner soft caps into treasury policy
- feat(fakes): expose owner soft caps and lifetime spend limits
- feat(platform): add quarantine, wallet policy, signing, and span instrumentation
- feat(platform): add OpenTelemetry tracing scaffold
- feat(fakes): add injectable signer and wallet policy caps
- feat(platform): add alert HTTP endpoints and snapshot v2
- feat(platform): wire monitor into supervisor drive loop
- feat(platform): add quota and anomaly monitoring
- feat(platform): enrich escalation HTTP endpoints and tests
- feat(platform): wire escalation router into supervisor drive loop
- feat(platform): add escalation router with notification sink
- feat(constitution): export ticket_timeout_seconds for platform routing
- feat(platform): add FastAPI REST surface for supervisor
- feat(platform): add Darwin Supervisor lifecycle engine
- feat(evolution): pass generation soft cap into reproduction
- feat(evolution): fold oldest layers past generation soft cap
- feat(layers): add deserialize_lessons for layer compaction
- feat(evolution): honor pause at outer loop boundary
- feat(orchestrator): track owner pause in checkpoint state
- feat(fakes): add PauseFlag for resumable owner pause simulation
- feat(machinery): add pause switch for resumable owner halt
- feat(orchestrator): wire memory, vault scoping, and untrusted fencing
- feat(evolution): promote staged semantic lessons at reproduction
- feat(fakes): add owner grant store for sensitive vault reads
- feat(neurons): write secrets with explicit vault tiers
- feat(security): add untrusted-text fencing primitive
- feat(machinery): add tiered vault access model
- feat(machinery): add episodic and semantic memory
- feat(machinery): add ledger reconciliation and empirical mutation eval harness
- feat(evolution): wire fitness counter-pressures to escalation quality
- feat(orchestrator): pause and resume work on owner escalation (P4)
- feat(fakes): add owner response channel to escalation inbox
- feat(constitution): add escalation ticket classes and deadlines
- feat(orchestrator): stateless LangGraph heartbeats and cross-engine simulation
- feat: add simulated world runner
- feat(fakes): add in-memory test doubles
- feat(darwin_dna): implement Layer 0 genome package

### Changed

- refactor(adapters): defer browser and e2b import checks to factory
- style: remove unused imports flagged by ruff

### Fixed

- fix(orchestrator): LangGraph state propagation and engine parity
- fix(orchestrator): restore state from checkpoint on sandbox resume
- fix(orchestrator): harden dispatch for P7 audit halt and P8 kill-switch

### Documentation

- docs: refresh README intro, layout, and links to new reference docs
- docs(deploy): add operator runbook for provider enablement and incidents
- docs(engineering): add phase-two multi-tenant build plan
- docs: update phase-one build plan and README license attribution
- docs: add CHANGELOG from conventional commit history
- docs(engineering): add phase-one evolution build plan
- docs(engineering): add phase-zero runtime build plan to index
- docs: refresh README for full catalog and platform adapter surface
- docs: update project and Helm README for platform capabilities
- docs(docker): document CI workflows and GHCR image usage
- docs: align engineering docs with Layer 0 implementation
- docs: document trading, markets, and DeFi as in-scope activities
- docs: add marketing and landing-page copy
- docs: add engineering architecture documentation
- docs: add product overview documentation

### Tests

- test(k8s): cover K8sDarwinDriver, manifests, and supervisor k8s spawn mode
- test(adapters): extend chain SDK and fiat ramp coverage for new extras
- test(orchestrator): cover Path-C proposal flow through propose and apply nodes
- test(supervisor): cover cross-pollination archive wiring
- test(security): red-team adversarial scenarios for doc-10 defences
- test(neurons): cover round-3 catalog neurons and loader validation
- test(supervisor): extend resume and throttle wiring coverage
- test(genome): cover ground-truth drift detection in reflect node
- test(supervisor): cover lifecycle resume and sandbox factory wiring
- test(neurons): cover expanded catalog and loader validation
- test(supervisor): cover real-adapter factory injection and x402 scoping
- test(adapters): stub tests for browser, sandbox, and x402 adapters
- test(leadership): cover NoLeaderElection, Postgres locks, and /readyz
- test(adapters): stub-backed tests for Web3ChainClient and HvacVaultClient
- test(platform): add Postgres persistence integration tests
- test(platform): add monitoring detector and integration tests
- test(manifest): add hypothesis property fuzz tests for trust root
- test(kill-switch): add hypothesis property fuzz tests
- test: add escalation pause/resume and fitness counter-pressure tests
- test(soma): add self-modification sub-graph integration tests
- test(evolution): add multi-generation reproduction and lineage tests
- test(audit): add tamper and re-anchor regression tests
- test: add Layer 0 test suite

### Build & deploy

- build(docker): bundle solana, stellar, stripe, and pdfwatermark in default image
- build(deps): add k8s, solana, stellar, stripe, and pdfwatermark extras
- deploy(grafana): add Darwin platform operations dashboard
- deploy(helm): add K8s driver mode, egress proxy, RBAC, and Kata RuntimeClass
- deploy(helm): wire provider secrets into deployment env vars
- deploy(helm): add providers values for sanctions, notifications, and fiat
- ci(helm): validate provider integration template rendering
- build: set package version to 0.0.1 for pre-release tracking
- chore: add proprietary LICENSE for Specimen Labs, Inc.
- build(docker): install web3 and hvac extras in default image
- deploy(helm): point readiness probe at /readyz when HA is enabled
- build(deps): add web3 and hvac optional extras
- deploy(helm): guard HA settings and use RollingUpdate when elected
- deploy(helm): document leaderElection and multi-replica Postgres HA
- ci(helm): validate Postgres-mode chart rendering
- deploy(helm): support Postgres via database.url or existingSecret
- build(docker): install postgres extra and document DSN switch
- ci: run Postgres persistence tests against service container
- build(deps): add postgres extra for psycopg persistence
- ci: add Helm lint and template workflow for darwin-platform chart
- deploy(helm): add chart README and production values fixture
- deploy(helm): add ingress, ServiceMonitor, and NetworkPolicy templates
- deploy: add helm chart skeleton for darwin-platform
- build: add ruff lint configuration and dev dependency
- ci: add GitHub Actions for test, slow sim, lint, and image build
- build(docker): add local compose stack for supervisor and observability
- chore: ignore local docker env, tokens, and SQLite data files
- build(deps): add OpenTelemetry to platform extra
- build(deps): add darwin_platform package and platform extra
- chore: expand gitignore for Python, tooling, and local IDE state
- chore: add .gitignore for Python build artifacts
- chore: add Python project scaffolding for Layer 0
