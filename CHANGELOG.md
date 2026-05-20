# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).
Entries are derived from the git commit history (Conventional Commits).

Nothing has been tagged or released on GitHub yet. Work in progress is listed under
**[Unreleased]**; the first release will be **[0.0.1]**.

## [Unreleased]

Work in progress toward the first public release (`0.0.1`): Layer 0 genome
(`darwin_dna`), platform control plane (`darwin_platform`), provider adapters,
Helm chart, and CI/CD.

### 2026-05-18

#### Documentation

- docs: add product overview documentation
- docs: add engineering architecture documentation
- docs: add marketing and landing-page copy
- docs: document trading, markets, and DeFi as in-scope activities

### 2026-05-19

#### Added

- feat(darwin_dna): implement Layer 0 genome package
- feat(fakes): add in-memory test doubles
- feat: add simulated world runner
- feat(orchestrator): stateless LangGraph heartbeats and cross-engine simulation
- feat(constitution): add escalation ticket classes and deadlines
- feat(fakes): add owner response channel to escalation inbox
- feat(orchestrator): pause and resume work on owner escalation (P4)
- feat(evolution): wire fitness counter-pressures to escalation quality
- feat(machinery): add ledger reconciliation and empirical mutation eval harness
- feat(machinery): add episodic and semantic memory
- feat(machinery): add tiered vault access model
- feat(security): add untrusted-text fencing primitive
- feat(neurons): write secrets with explicit vault tiers
- feat(fakes): add owner grant store for sensitive vault reads
- feat(evolution): promote staged semantic lessons at reproduction
- feat(orchestrator): wire memory, vault scoping, and untrusted fencing
- feat(machinery): add pause switch for resumable owner halt
- feat(fakes): add PauseFlag for resumable owner pause simulation
- feat(orchestrator): track owner pause in checkpoint state
- feat(evolution): honor pause at outer loop boundary
- feat(layers): add deserialize_lessons for layer compaction
- feat(evolution): fold oldest layers past generation soft cap
- feat(evolution): pass generation soft cap into reproduction

#### Fixed

- fix(orchestrator): harden dispatch for P7 audit halt and P8 kill-switch
- fix(orchestrator): restore state from checkpoint on sandbox resume
- fix(orchestrator): LangGraph state propagation and engine parity

#### Documentation

- docs: align engineering docs with Layer 0 implementation

#### Tests

- test: add Layer 0 test suite
- test(audit): add tamper and re-anchor regression tests
- test(evolution): add multi-generation reproduction and lineage tests
- test(soma): add self-modification sub-graph integration tests
- test: add escalation pause/resume and fitness counter-pressure tests
- test(kill-switch): add hypothesis property fuzz tests
- test(manifest): add hypothesis property fuzz tests for trust root

#### Build & deploy

- chore: add Python project scaffolding for Layer 0
- chore: add .gitignore for Python build artifacts
- chore: expand gitignore for Python, tooling, and local IDE state

### 2026-05-20

#### Added

- feat(platform): add Darwin Supervisor lifecycle engine
- feat(platform): add FastAPI REST surface for supervisor
- feat(constitution): export ticket_timeout_seconds for platform routing
- feat(platform): add escalation router with notification sink
- feat(platform): wire escalation router into supervisor drive loop
- feat(platform): enrich escalation HTTP endpoints and tests
- feat(platform): add quota and anomaly monitoring
- feat(platform): wire monitor into supervisor drive loop
- feat(platform): add alert HTTP endpoints and snapshot v2
- feat(fakes): add injectable signer and wallet policy caps
- feat(platform): add OpenTelemetry tracing scaffold
- feat(platform): add quarantine, wallet policy, signing, and span instrumentation
- feat(fakes): expose owner soft caps and lifetime spend limits
- feat(neurons): thread owner soft caps into treasury policy
- feat(platform): extend wallet policy with per-lifetime cap
- feat(platform): add bearer token auth with owner and operator scopes
- feat(llm-router): add platform-installed per-tier throttles
- feat(evolution): support amendment layers and forced reproduction
- feat(platform): throttle endpoints, auto-throttle, and amendment reproduction
- feat(fakes): add harvest_to_owner for profit withdrawals
- feat(platform): add billing service for harvest fees and metered markup
- feat(platform): wire billing harvest, metered sweep, and REST endpoints
- feat(platform): add LiteLLM transport adapter for real LLM gateway
- feat(fakes): allow injecting real LLM transport on FakeStack
- feat(platform): wire llm_transport_factory into supervisor spawn
- feat(platform): add hash-chained operator audit log
- feat(platform): record operator actions and expose fleet audit API
- feat(platform): add SQLite persistence for platform state
- feat(platform): wire persistence_path and LineageRegistry into supervisor
- feat(platform): add RealWallet adapter with ChainClient protocol
- feat(platform): add RealVaultBackend adapter with VaultClient protocol
- feat(platform): add healthz and Prometheus metrics endpoints
- feat(platform): add darwin-platform CLI for serve and signing-key
- feat(platform): dual SQLite and Postgres persistence backends
- feat(platform): add Postgres advisory-lock leader election
- feat(supervisor): gate spawn on leadership and cancel drives on failover
- feat(platform): add /readyz readiness probe for HA routing
- feat(platform): wire --leader-election flags on darwin-platform CLI
- feat(adapters): add Web3ChainClient for EVM USDC wallet backend
- feat(adapters): add HvacVaultClient for HashiCorp KV v2
- feat(adapters): add browser, sandbox, and x402 protocol adapters
- feat(adapters): export real provider adapter types from package
- feat(genome): wire x402 HTTP client into NeuronContext for web neurons
- feat(supervisor): inject real wallet, vault, browser, and x402 factories
- feat(platform): add CLI flags for web3, hvac, and browserbase adapters
- feat(neurons): add publisher, analytics, and research catalog neurons
- feat(fakes): sanctions screening, vault delete, and new LLM task stubs
- feat(genome): grant sandbox substrate to compute.run neurons
- feat(platform): add OFAC-style sanctions screening for outbound transfers
- feat(adapters): screen RealWallet pay and harvest against sanctions screener
- feat(platform): add provisioning service with quotas and scoped port
- feat(platform): add vault admin and KYC retention sweep services
- feat(platform): add scheduled harvest rules evaluator
- feat(platform): add browser session cache and cost attribution
- feat(platform): persist darwin lifecycle for HA runtime resume
- feat(supervisor): wire provisioning, vault admin, harvest, cache, and resume
- feat(platform): REST endpoints for harvest rules, vault, browser, resources
- feat(platform): CLI flags for e2b sandbox and sanctions denylist file
- feat(genome): reconcile reflect beliefs against audit log ground truth
- feat(fakes): load and export signing key bytes for HA resume
- feat(adapters): add per-model circuit breaker to LiteLLM transport
- feat(platform): detect spread-spend smear evasion in monitoring
- feat(platform): embed HMAC watermarks on sensitive vault uploads
- feat(platform): throttle sandbox, browser, and egress per Darwin
- feat(provisioning): add SubprocessProvisioningBackend for external drivers
- feat(platform): browser vision-mode fallback for canvas-only failures
- feat(platform): persist per-Darwin audit, checkpoints, manifests, signing key
- feat(supervisor): wire persistence, throttles, watermarks, and spread-spend
- feat(platform): REST routes for resource throttles and vault metrics
- feat(neurons): complete founding catalog with accounts, ads, trading, support
- feat(fakes): add support_reply LLM task stub for support responder neuron
- feat(machinery): add Path-C neuron proposal and interpreted admission gate
- feat(platform): implement Chainalysis and TRM sanctions screeners
- feat(adapters): add Stripe and MoonPay fiat on/off-ramp adapters
- feat(adapters): add Solana, Stellar, and multi-chain USDC router
- feat(adapters): add StagehandReplayClient for cached browser replay path
- feat(platform): add signed cross-pollination lesson archive
- feat(platform): add signing key rotation with retired-key drain window
- feat(platform): add Slack, webhook, and SMTP notification sinks
- feat(sim): add 90-day simulation calibration harness

#### Changed

- style: remove unused imports flagged by ruff
- refactor(adapters): defer browser and e2b import checks to factory

#### Documentation

- docs(docker): document CI workflows and GHCR image usage
- docs: update project and Helm README for platform capabilities
- docs: refresh README for full catalog and platform adapter surface
- docs(engineering): add phase-zero runtime build plan to index
- docs(engineering): add phase-one evolution build plan

#### Tests

- test(platform): add monitoring detector and integration tests
- test(platform): add Postgres persistence integration tests
- test(adapters): stub-backed tests for Web3ChainClient and HvacVaultClient
- test(leadership): cover NoLeaderElection, Postgres locks, and /readyz
- test(adapters): stub tests for browser, sandbox, and x402 adapters
- test(supervisor): cover real-adapter factory injection and x402 scoping
- test(neurons): cover expanded catalog and loader validation
- test(supervisor): cover lifecycle resume and sandbox factory wiring
- test(genome): cover ground-truth drift detection in reflect node
- test(supervisor): extend resume and throttle wiring coverage
- test(neurons): cover round-3 catalog neurons and loader validation
- test(security): red-team adversarial scenarios for doc-10 defences

#### Build & deploy

- build(deps): add darwin_platform package and platform extra
- build(deps): add OpenTelemetry to platform extra
- chore: ignore local docker env, tokens, and SQLite data files
- build(docker): add local compose stack for supervisor and observability
- ci: add GitHub Actions for test, slow sim, lint, and image build
- build: add ruff lint configuration and dev dependency
- deploy: add helm chart skeleton for darwin-platform
- deploy(helm): add ingress, ServiceMonitor, and NetworkPolicy templates
- deploy(helm): add chart README and production values fixture
- ci: add Helm lint and template workflow for darwin-platform chart
- build(deps): add postgres extra for psycopg persistence
- ci: run Postgres persistence tests against service container
- build(docker): install postgres extra and document DSN switch
- deploy(helm): support Postgres via database.url or existingSecret
- ci(helm): validate Postgres-mode chart rendering
- deploy(helm): document leaderElection and multi-replica Postgres HA
- deploy(helm): guard HA settings and use RollingUpdate when elected
- build(deps): add web3 and hvac optional extras
- deploy(helm): point readiness probe at /readyz when HA is enabled
- build(docker): install web3 and hvac extras in default image
