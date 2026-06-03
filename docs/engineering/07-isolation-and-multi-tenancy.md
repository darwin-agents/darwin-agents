# Isolation and multi-tenancy

The platform hosts many Darwins, each one autonomous and untrusted. The isolation story has to be airtight, because a bug here is a class-action lawsuit.

## Threat model

We design assuming any one of these is true:

- The Darwin's soma contains code we didn't write and haven't audited (true after generation 1).
- A neuron might be malicious — bug, prompt injection from a website it scraped, evolved-into pathology.
- An owner might try to manipulate their own Darwin via the vault or escalation responses.
- A bad actor might sign up, fund a Darwin, and try to use the platform as a launchpad.
- The platform's own code has bugs.

The job is to ensure that none of these can:

- Touch another tenant's data, treasury, vault, or running soma.
- Escape the sandbox onto the platform's hosts.
- Corrupt the platform's audit log or signing infrastructure.
- Exceed declared resource quotas.

## The four isolation boundaries

1. **Network.** Per-Darwin namespace + per-Darwin egress proxy.
2. **Compute.** Per-Darwin microVM sandbox (E2B for neuron execution; firecracker-grade isolation).
3. **Data.** Per-Darwin database schema, per-Darwin object-store prefix, per-Darwin vault namespace.
4. **Identity.** Per-Darwin signing key, per-Darwin wallet, per-Darwin OAuth identities.

We follow the standard multi-tenant SaaS data-isolation pattern (silo-pooled hybrid), augmented with hardware-level isolation for code execution. Reference architectures: HashiCorp Vault's namespace model, the EKS-tenant pattern, Coinbase Agentic Wallets' per-agent TEE.

## Compute isolation: why microVMs

We considered three sandboxing tiers:

| Tier | Examples | Verdict |
|---|---|---|
| Process-level (Python venvs, nsjail) | Cheap, fast | **Reject.** Shared kernel; one container-escape vulnerability and the whole platform is compromised. |
| gVisor / user-space syscall interception | Modal | Acceptable for some workloads. **Reject for Darwin** — the threat surface (untrusted self-generated code) is exactly what gVisor's design-time warnings caution about. |
| MicroVM (Firecracker / Kata) | E2B, Northflank, AWS Lambda | **Accept.** Each sandbox gets its own kernel; escape requires hypervisor-level vulnerability, which is rare and headline-news when found. |

Each neuron invocation runs in a fresh E2B microVM. The orchestrator itself runs in a longer-lived per-Darwin microVM (replaced on soma mutation or at scheduled rotation). Cold start ~100–500ms, well within Darwin's heartbeat budget.

For execution-sandbox comparison see [`14-sandboxing-e2b.md`](14-sandboxing-e2b.md).

## Network isolation

Inside the platform Kubernetes cluster:

- Each Darwin gets a dedicated namespace.
- A per-namespace NetworkPolicy blocks all pod-to-pod traffic except to the platform's egress proxy and the LangGraph checkpoint store.
- Outbound traffic from a Darwin's sandboxes is routed through an **egress proxy** that:
  - Logs every connection (URL, response code, payload size) to the audit log
  - Blocks destinations on the platform's blocklist (sanctioned-list IPs, known phishing domains, internal cluster IPs)
  - Enforces per-Darwin bandwidth quotas
  - Strips and replaces any platform-internal headers
- Inbound traffic to a Darwin's sandboxes is not allowed. Darwins are clients, not servers.

This means Darwins can't talk to each other directly even if they wanted to. If we ever want cross-Darwin communication (e.g. for cross-lineage learning), it goes through a platform-mediated channel that we control.

## Data isolation

There are two distinct classes of data, with different ownership:

**Platform-level data** — billing, audit log, control-plane state, ledger entries, escalation tickets, lineage metadata, supervisor state. The platform owns this. It lives in the **platform Postgres** with shared tables + RLS policies enforcing `darwin_id = current_setting('app.current_darwin_id')`. Every database connection sets the GUC at session start; a query that forgets returns zero rows. *(Implemented in schema v12 — `_migrate_v12_enable_rls` + `PersistenceStore.tenant_connection` / `ensure_tenant_role`; see [`23-data-model-reference.md`](23-data-model-reference.md) §"RLS policies".)*

**Organism-level data** — whatever the Darwin's business is actually about. Customer records, content drafts, subscriber emails, product inventory, support ticket history. The Darwin owns this; it provisions its own databases on demand. See "The Darwin's own databases" below.

**Object storage.** Per-Darwin S3 prefix with an IAM role scoped to that prefix. Sandboxes get role-assumed temporary credentials, not long-lived keys. Used for both platform artifacts (e.g. event-log Merkle anchors) and Darwin assets (generated images, content uploads, etc.).

**Vector store.** LanceDB per Darwin (lightweight enough to make this practical). Holds episodic memory and semantic-search indexes.

**Vault (KYC docs, account credentials, payment methods).** HashiCorp Vault with one **Vault namespace per Darwin**. The orchestrator and neurons authenticate via short-lived tokens scoped to their namespace; cross-namespace reads are syntactically impossible. See the dedicated section below.

**Event log.** Shared append-only Postgres table partitioned by `darwin_id` and `month`. Reads are RLS-gated. Writes are append-only via a stored procedure that hash-chains the entry; direct INSERTs are revoked.

## The owner-agent shared vault

The vault is **bidirectional**: both the user and the Darwin write to it. It's not just a one-way credential drop-box; it's the collaborative memory between the human and the organism.

**What the user writes into the vault:**
- KYC documents (passport, driving license, utility bill, selfie) — written once at signup or per-Darwin if granted
- Credentials for accounts the user wants the Darwin to operate (existing WordPress, an ad account, a Shopify store)
- Payment methods (card tokens for fiat top-up, bank/wallet addresses for harvest withdrawals)
- API keys the user already has for services they want the Darwin to use
- Personal data the Darwin needs to act on the user's behalf (brand voice samples, contact info, time zone, preferences)

**What the Darwin writes into the vault:**
- **Account credentials** for accounts the Darwin creates itself (the Medium account it opened, the WordPress site it provisioned, the email address it registered)
- **API keys it earns** (API keys for services where the Darwin signed up; OAuth tokens it acquired)
- **Database connection strings** for the databases the Darwin provisions (more on this below)
- **Encryption keys and signing material** for things only the Darwin should be able to decrypt
- **Lesson artifacts** that don't go into the next DNA layer but should survive death (e.g. "the IDs of accounts I'm running that the user will inherit")

Both halves are visible to the user through the dashboard's **Vault** view. Items written by the Darwin are tagged as such, with provenance: which neuron created it, when, for what purpose. The user can revoke any item at any time; if it's something the Darwin depends on, the Darwin gracefully escalates or dies depending on how essential it is.

This bidirectional model has consequences:

- **Survivability of accounts.** When a Darwin dies, the user retains the accounts the Darwin opened on their behalf — they're already in the user's vault. They are not orphaned with the dead organism.
- **Forensic value.** If a Darwin behaved badly, the audit trail includes every secret it minted, and the user has access to revoke them all.
- **A new failure mode.** The user can manipulate their own Darwin by writing misleading vault entries. We accept this — the user is the principal; if they want to lie to their own Darwin they're within rights. We just log it.

## Tiered access on the vault

Per the design from [`../overview/02-design-principles.md`](../overview/02-design-principles.md), vault entries are classified:

- `tier=public` — readable by any neuron with the right capability tag (e.g. "the user's preferred display name for their content")
- `tier=scoped` — readable only by named neurons (e.g. "the WordPress credentials are readable only by content.publisher")
- `tier=sensitive` — readable only when the orchestrator presents a fresh owner-approved escalation token (e.g. "the passport scan is readable only when an account neuron is responding to a specific KYC request the owner approved")

A neuron requesting a sensitive credential without a token gets refused. A neuron requesting a scoped credential it doesn't have the tag for gets refused. The vault never logs the *value* of what it returns, only that it returned something.

We use HashiCorp Vault for the implementation, with its **native AI agent support** (available 2026) for the agent-side identity flow. Each Darwin authenticates to its vault namespace via a per-Darwin AppRole; each neuron invocation gets a child token with capability constraints. See [`16-secrets-and-vault.md`](16-secrets-and-vault.md) for the full vault design.

## The Darwin's own databases

A Darwin needs to store data that isn't an LLM call or a single secret: customer records, content drafts, business state, subscriber lists, transaction history specific to whatever it's doing. We treat this as **the Darwin's own infrastructure**, not the platform's.

How it works:
- A Darwin requests "I need a Postgres database for storing X" via a `infrastructure.provisioner` neuron (platform-mediated).
- The platform provisions an isolated database (a per-Darwin Postgres database in our managed Postgres fleet, or a new schema with strict RLS).
- The platform writes the connection string into the Darwin's vault under `tier=scoped`, tagged with the neuron name allowed to use it.
- The Darwin uses it via that vault entry. The neuron sandbox sees only the credentials it needs.
- The platform's API has read-only credentials for the same database, used to render the Darwin's data in the user's dashboard.

This makes the platform's dashboard a **window into the Darwin's data**, not a copy of it. The Darwin is the source of truth for its own business; the platform observes. When the Darwin dies, its databases survive — they belong to the user. The user can browse them, export them, hand them to the next Darwin, or wind them down.

This also clarifies a subtle architectural point: the platform's Postgres (with per-Darwin schemas, RLS) holds **platform-level** data — billing, audit log, control-plane state. The Darwin's own databases hold **organism-level** data — what the Darwin built. These are separate concerns and should be in different Postgres instances at scale (a multi-tenant platform DB vs. a fleet of per-Darwin DBs). In v1 we may colocate them on the same physical Postgres with schema-level separation; we plan for the split.

## Identity isolation

Every Darwin has its own:

- **Platform signing identity.** Signs the Darwin's DNA layers (the signature is the platform's, but the per-Darwin identity is encoded in the signed manifest).
- **Wallet.** A separate Coinbase Agentic Wallet, with its key material in a TEE.
- **OAuth identities.** Where the Darwin signs up for a third-party service (WordPress, Medium, an ad account), the resulting OAuth credential lives in the Darwin's vault namespace and is unique to that Darwin. A second Darwin owned by the same human is a separate principal with separate accounts.

This is partly for security and partly because some platforms will rate-limit or banhammer agent-like behavior — and we'd rather lose one Darwin's WordPress account than all of them.

## The platform's view

The platform's control plane can:

- Spawn, pause, resume, kill any Darwin
- Read the event log (for compliance and dashboards)
- Read the ledger (for billing reconciliation)
- Force a reproduction event (rare, only for forced DNA migrations)
- Rotate signing keys
- Update the blocklist

The platform's control plane **cannot**:

- Read a Darwin's vault entries (the platform has the namespace, but not the unsealing capability; only the Darwin's runtime identity can read)
- Read a Darwin's wallet's private key (TEE-secured at the wallet provider)
- Modify the Darwin's DNA after spawn (only the signing service can sign new layers, and only at reproduction events)
- Read the user's KYC documents in plaintext (encrypted at rest with a key the user has consent-gated)

The principle: the platform has *control* over running Darwins (can stop them) but not *secrets* (can't impersonate them or read their owners' private data). This is the same model regulated payment processors use.

## Quotas and noisy-neighbor mitigation

Per-Darwin caps the platform enforces:

| Resource | Default cap | Burst | Notes |
|---|---|---|---|
| LLM tokens / hour | 1M input + 100K output | 2× | Above cap, throttled, not refused. Above 5× hard ceiling: paused for owner review. |
| Egress bandwidth | 10 GB/day | 2× | Same throttle/pause pattern. |
| Neuron concurrent dispatches | 8 | 16 | Excess queue. |
| Disk in sandboxes | 5 GB | n/a | Hard cap. |
| Sandboxes alive | 16 | 32 | Excess queue. |
| Heartbeat rate | 1/sec | 2/sec | Cooperative; enforced at the orchestrator main-loop level, not network. |
| Spend rate | $50/hour | 2× | Soft. Hard cap is DNA's spend ceiling. |

A Darwin running into these is a signal — the strategy might be too eager or there's a bug. Repeated hits route to the user dashboard.

## What we don't do

- **No cross-Darwin RPC.** Even if it'd be useful (Darwin A wants to ask Darwin B "how's your conversion rate on this ad creative?"), we don't allow it. Any cross-Darwin information flow goes through the platform's curated pool, where consent and privacy can be enforced.
- **No shared model fine-tunes.** Each Darwin uses the same foundation models (via the platform LiteLLM gateway); we do not maintain per-Darwin fine-tunes. The economics don't work and the entanglement would be ugly.
- **No mutual TLS between Darwins and the platform.** Just bearer tokens, audited, rotated. Less elegant than mTLS but vastly easier to operate and reason about, and the threat model doesn't require it (the egress proxy already terminates and re-originates the outbound).
