# Secrets, vault, KYC

## Decision

Use **HashiCorp Vault** with one **namespace per Darwin** for per-tenant secrets isolation, leveraging Vault's native AI agent identity model (in public beta during summer 2026). The vault is **shared bidirectionally between the user and the Darwin** — both parties read and write.

## The shapes of secrets

A Darwin's vault namespace holds several distinct classes of secret. The "Written by" column reflects the bidirectional model — both the user and the Darwin write.

| Class | Examples | Sensitivity | Written by | Access pattern |
|---|---|---|---|---|
| **Platform-issued, per-Darwin** | LiteLLM virtual key, E2B project token, OAuth client ID/secret | Medium | Platform (at spawn) | Reusable, long-lived; rotated by platform |
| **User-shared, scoped** | WordPress credentials user gave Darwin, ad account token, Stripe Connect for a specific store, API keys user already had | High | User | Reusable; expires when user revokes |
| **User-shared, KYC** | Passport scan, driving license, utility bill, selfie | Highest | User | Single-use bursts only, attached to a specific named escalation |
| **Darwin-acquired credentials** | OAuth token Darwin obtained by signing up the user for service X, API keys Darwin created at services where it signed up | High | Darwin | Reusable; lives in Darwin's vault namespace |
| **Darwin-provisioned infrastructure** | Connection string for Darwin's own Postgres database, S3 bucket access keys | Medium-High | Platform (at provision time) | Reusable; scoped to specific neurons |
| **Darwin-internal** | Encryption keys for Darwin's own data, signing material | High | Darwin | Reusable; rotation managed by Darwin or platform |
| **Wallet keys** | TEE-secured at Coinbase / Privy; **never in our vault** | N/A | N/A | Used via wallet SDK only |

The wallet keys are explicitly not our problem — they're in the wallet provider's TEE. We hold *spending policy*, not key material.

## Why bidirectional matters

In most multi-tenant SaaS, the "secrets vault" is something the operator/user puts secrets into and the application consumes from. In Darwin, the agent itself writes secrets — credentials for accounts it opened, API keys for services it signed up to, keys for data stores it provisioned. This is a load-bearing property:

- **Account survival across organism death.** When a Darwin dies, the WordPress site it opened, the Medium account it registered, the Stripe Connect account it set up — these all survive in the user's vault. The user inherits accounts they didn't have to manually set up. The next Darwin (or the user manually) can pick up where the dead one left off.
- **User visibility into the agent's footprint.** The user can see, at any time, every account and key their Darwin has minted on their behalf. They can revoke any of them.
- **A clean abstraction for the platform UI.** The dashboard's Vault view is the same view regardless of who wrote each entry; provenance is just metadata.

## Namespace per Darwin

Vault Enterprise's namespaces give us hard per-tenant isolation that's enforced by Vault itself, not by application code. Each Darwin gets:

- A namespace: `darwins/{darwin_id}/`
- An AppRole for the orchestrator's identity
- AppRoles for neurons (one AppRole per neuron class, scoped by capability tag)
- A separate AppRole for the platform UI's read access (more on this below)
- Policies binding tier→AppRole

A neuron requesting `darwins/other_darwin_id/...` gets a syntactic refusal from Vault. Cross-namespace reads are not a policy decision; they're impossible.

## The platform UI's read access

The user's dashboard needs to display what's in the vault — at minimum, the metadata (what kinds of credentials exist, when each was created, by whom). For some classes (Darwin-acquired account credentials, Darwin-provisioned databases) the UI may also display the credentials themselves so the user can copy them out.

We solve this with a dedicated read-only AppRole per Darwin, used by the platform's API on behalf of an authenticated user. Crucially:
- This AppRole can read metadata for all classes, but credential *values* only for classes the user has marked "show me".
- The UI access is logged separately and visible to the user in their audit timeline.
- The Darwin's neurons cannot impersonate this AppRole; the credentials live in the platform's vault (not in the Darwin's sandbox).

## The agent identity model (Vault 2026)

HashiCorp announced native AI-agent identity in Vault in 2025 and rolled it out in beta in summer 2026. Key properties for Darwin:

- **Per-action identity.** Each neuron dispatch generates a child token with capability constraints derived from the parent (orchestrator) token. Tokens are short-lived (typically the duration of the dispatch).
- **Runtime delegation.** The orchestrator can delegate a tighter scope to a neuron without ever holding the credential itself — the neuron requests directly from Vault with its child token.
- **Audit per agent action.** Vault logs the chain (orchestrator → neuron → resource), so we can attribute any vault read to a specific neuron invocation.

This replaces what would otherwise be a custom token-vending machine.

## Tiered access

Per [`07-isolation-and-multi-tenancy.md`](07-isolation-and-multi-tenancy.md):

- `tier=public` secrets — any neuron in this Darwin can read.
- `tier=scoped` secrets — only neurons with the matching capability tag can read.
- `tier=sensitive` secrets — only with a fresh owner-issued escalation token.

In Vault terms:

```
darwins/{darwin_id}/
  public/                    # readable by all darwin AppRoles
    display_name
    timezone
    ...
  scoped/
    content_publisher/       # only AppRole "darwin/{id}/neuron/content.publisher"
      wordpress_creds        # written by user
      medium_token           # written by Darwin (signed up itself)
    ads_manager/
      meta_token
      google_ads_token
    infrastructure/          # connection strings, etc.
      postgres_main_dsn      # written by platform when provisioned
      lancedb_path
    ...
  sensitive/                 # readable only with a one-time escalation token
    kyc/
      passport
      drivers_license
      selfie
  platform_managed/          # platform-side AppRoles only
    litellm_virtual_key
    e2b_project_token
    ...
```

The escalation token is itself stored in Vault under a path like `darwins/{id}/escalations/{eid}/grant`. When the owner approves an escalation, the platform writes a one-time-use token at that path. The neuron retrieves it, uses it to unlock the sensitive read, and the token self-destructs after one use.

## KYC documents specifically

KYC documents are the most sensitive thing in the system. We:

- Store them encrypted at rest with a key wrapped by a per-Darwin Vault-managed key
- Never log the actual document content; only log "this document was retrieved by neuron X for purpose Y"
- Set short retention by default (90 days after last use; configurable per user up to applicable legal limit)
- Watermark them invisibly with the Darwin ID and timestamp at retrieval, so leaks can be traced
- Require fresh owner consent for each retrieval; we do not allow blanket consent for KYC docs

A user can revoke a KYC doc at any time from the dashboard. The neuron that depended on it gracefully degrades to escalating the next time it needs it.

## Why not AWS Secrets Manager / GCP Secret Manager

They're great single-cloud answers but:

- No per-tenant namespacing of the strength Vault provides; everything is IAM-policied, which means tenant isolation is application-enforced rather than infrastructure-enforced.
- Less mature dynamic-secrets story.
- No agent-aware identity model.
- Locks us to one cloud at the secrets layer, which is the worst layer to be locked at.
- Don't support the bidirectional shared-namespace model with separate AppRoles for platform/orchestrator/neuron/user-UI as cleanly.

We may use AWS Secrets Manager for *platform-level* secrets (the platform's own database passwords, etc.) but Vault is the per-tenant store.

## Operational notes

- **Vault rotation:** all platform-issued credentials rotate on a 90-day schedule; user-provided credentials rotate at user pace; Darwin-acquired credentials rotate when the Darwin's reflect node decides to (it's a learnable behavior).
- **Sealing:** Vault is auto-unsealed via KMS (cloud HSM-equivalent). Manual unseal keys are split and held by named platform operators.
- **Backups:** Vault is backed up daily; restores have been tested quarterly.
- **Cost:** Vault Enterprise (or HCP Vault Dedicated) is several thousand a month minimum. Acceptable cost-of-doing-business; the alternatives are worse.

If we hit budget pain pre-product-market-fit, we can start on the OSS Vault edition and a single namespace per tenant via paths (`secret/data/darwins/{id}/...`) with RLS-equivalent policies. Namespaces are an enterprise feature. The migration to namespaces later is non-trivial but possible.
