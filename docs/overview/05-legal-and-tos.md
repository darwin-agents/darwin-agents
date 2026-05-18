# Legal, terms of service, jurisdictional posture

This is an *operational outline*, not legal advice. The actual ToS, privacy policy, and AML procedures will be drafted with legal counsel before phase 2. This doc captures what we know we need to handle so legal counsel doesn't have to start from a blank page.

## The shape of the legal entity

For phase 0–1 we operate as a small company in our home jurisdiction. For phase 2 (real users, real money) we need:

- **A formal corporate entity** in a jurisdiction friendly to the business — likely a US Delaware C-corp with an EU subsidiary if we serve EU users, or a structure recommended by counsel. Given the founder is in Spain (Barcelona), a EU entity is the natural primary, with a US subsidiary if we want US market access.
- **A regulatory posture for money movement.** Three structural options:
  1. **Non-custodial.** The platform never custodies user funds; users self-custody via their Coinbase Agentic Wallet, and we are merely a software layer. This may sidestep MTL/MSB requirements but is hard to engineer cleanly (the omnibus model in [`../engineering/05-treasury-and-payments.md`](../engineering/05-treasury-and-payments.md) is partially custodial).
  2. **Custodial via partnership.** We partner with a chartered MSB/MTL (e.g. Stripe's regulated entities, Coinbase, MoonPay) and ride their compliance. We are the platform; the chartered partner is the money-mover of record.
  3. **Chartered ourselves.** We acquire MTLs state-by-state in the US and the equivalent in the EU. Expensive (~$1M+ and 12+ months); not appropriate for phase 2.

We expect to start with structure 2 (custodial via partnership), evaluate structure 1 as we learn the engineering reality, and reach for structure 3 only at meaningful scale.

- **Insurance.** General liability, technology errors-and-omissions (E&O), cyber liability. Quotes obtained pre-phase-2.

## Key regulatory regimes we touch

A non-exhaustive map. Each warrants counsel review when it applies.

### Money services (US)

- **MSB registration** with FinCEN if we move money. Triggered by stablecoin transmission in most interpretations.
- **State-level MTLs** in the US (NY BitLicense, California, Texas, etc.) for state-by-state operation. Partnership with a chartered partner sidesteps most of this.
- **AML/KYC obligations** (CIP, ongoing monitoring, SARs) — flow through to the user via KYC at signup and through to outbound counterparties via sanctions screening.

### EU

- **MiCA (Markets in Crypto-Assets Regulation)** — applies to crypto-asset service providers; we hold USDC and route USDC payments, which puts us in scope under most interpretations.
- **PSD2** — payment services directive; depending on structure, may apply.
- **GDPR** — comprehensive data protection. Applies to all EU users regardless of where we're based.
- **DSA / DMA** — services and markets acts; primarily for large platforms but the disclosure requirements (especially around recommender systems and ad transparency) are worth tracking.
- **AI Act** — the EU AI Act applies to deployers and providers of certain AI systems. Darwin's autonomous agents likely classify as a *high-risk* AI system in some interpretations (depending on the business they conduct on the user's behalf). Compliance is significant — risk management system, transparency to users, human oversight, etc. We design with this in mind from day one.

### UK

- **FCA's crypto regime** (registration as a cryptoasset firm) likely required.
- **UK GDPR** — substantially equivalent to EU GDPR.

### Spain specifically (founder's jurisdiction)

- **Bank of Spain registration** for crypto-asset providers under EU MiCA's transitional arrangements.
- **CNMV** if any activity crosses into securities-adjacent territory.
- **AEPD** for data protection oversight.

### Cross-cutting

- **Sanctions** (OFAC, EU, UK HMT). Every payment counterparty screened.
- **Tax** — we provide year-end statements but do not give tax advice. The user is responsible for their own tax filings; we are responsible for accurate transaction history.
- **Consumer protection** law (FTC in the US, EU consumer-rights directives) — applies to disclosures, refunds, account closures.

## What the ToS must explicitly cover

A non-exhaustive checklist for the legal draft:

### User responsibilities and acknowledgments
- The user is the principal; the Darwin acts on their behalf within DNA-imposed limits.
- The user accepts that most Darwins will die, and that losses are not refunded.
- The user is responsible for tax obligations on any revenue the Darwin generates.
- The user is responsible for the legality of the activities they direct the Darwin to perform (within the platform's allowed categories).
- The user grants the Darwin permission to operate accounts and sign up for services on their behalf.
- The user provides KYC documentation and accepts that retention follows applicable law.

### Platform responsibilities and commitments
- Kill-switch latency commitment.
- Audit-log retention.
- Data-survival-on-death policy.
- Notice period for forced reproduction events.
- Notice period for platform wind-down.

### Limitations of liability
- Standard cap (12 months of fees, or stated dollar amount, etc.) for our liability to the user.
- Explicit disclaimer of liability for the *outcome* of the Darwin's autonomous actions within constitutional limits.
- Indemnity from the user for activities they specifically direct that violate third-party TOS or law.

### Allowed and forbidden activities
- The list from the constitution, exposed as ToS-level prohibitions on what the user can direct the Darwin to do.
- A "platform may modify the forbidden list" clause with notice requirement.

### Data and privacy
- What we collect, why, how long.
- Sub-processors (Stripe, MoonPay, Coinbase, AWS, LiteLLM providers, etc.).
- Cross-border data transfer mechanisms (SCCs for EU→US).
- User rights (access, deletion, portability).
- Special handling for KYC documents.

### Dispute resolution
- Governing law (TBD by counsel).
- Arbitration clause where permitted; carve-outs for jurisdictions where mandatory arbitration is unenforceable.
- Class-action waiver where permitted.

### Termination
- User can terminate at any time; we return any residual treasury balance to their verified wallet/bank.
- Platform can terminate for cause (TOS violation, fraud, abuse) with documented reason.
- Platform can terminate without cause with notice (30 days minimum) and full refund of residuals.

## Disclosure requirements

Where the Darwin's autonomous activity touches third parties (publishing content, sending messages, paying counterparties), disclosure may be legally or contractually required. Examples:

- **AI-content disclosure.** Many jurisdictions and platforms are moving toward mandatory labeling of AI-generated content. The Darwin's content neurons must support adding required disclosures, and the platform's policy must default to over-disclosing rather than under-disclosing.
- **Sponsored/affiliate disclosure.** FTC requires clear disclosure of affiliate relationships. Our `content.writer` neuron must include the required disclosures in any affiliate-bearing content.
- **Automation disclosure on platforms that require it.** Some platforms forbid automation; the Darwin should not operate there at all (per the allowed-categories model in [`../engineering/16-secrets-and-vault.md`](../engineering/16-secrets-and-vault.md)). Other platforms allow automation with disclosure; the Darwin discloses.
- **Identity disclosure.** A Darwin should never claim to be a specific human. It can use a brand or pseudonym, but it cannot impersonate a real named individual.

The constitution enforces these at the action level; the legal layer enforces them at the policy level.

## Liability surface — known risks

Risks we know exist and how we plan to bound them:

| Risk | Mitigation |
|---|---|
| User claims the Darwin made decisions they didn't authorize | The constitution and DNA hard caps define authorization scope; every action is in the audit log with rationale |
| User claims platform took unauthorized action | All platform actions on a Darwin are logged with operator identity |
| Darwin defrauds a third party (e.g. takes payment, doesn't deliver) | Allowed categories exclude service-provision categories where this risk is highest in v1; we add `support.responder` with refund authority in phase 3 |
| Darwin's content infringes IP | Content neurons trained to avoid; DMCA takedown procedure; user is principal for their Darwin's content (per ToS) |
| Darwin violates a third-party platform's TOS | Allowed-platforms whitelist in the constitution; the Darwin operates only on platforms we've vetted |
| Darwin handles personal data of third parties (e.g. customers of the user's business) | The Darwin's databases are subject to GDPR/CCPA; the user is the controller, we are the processor; DPA in place |
| Sanctions violation through a Darwin payment | Real-time screening at the payment layer; refused payments go to operator review |
| Money laundering through Darwin businesses | KYC at user signup; transaction-pattern monitoring on Darwin treasuries; SAR filing where required |
| Tax evasion | Per-user year-end statements; we comply with information-reporting requirements (1099 in US, EU equivalents) |
| Underage user signs up | KYC requires 18+; we honor age-verification at the KYC provider level |
| Security breach exposing KYC documents | Encrypted at rest with per-Darwin keys; access audited; breach notification per law |

## Things we will not do (legally / ethically)

A line in the sand, equivalent to the constitution's forbidden actions but at the platform level:

- We will not sell user data.
- We will not operate in sanctioned jurisdictions.
- We will not provide service to users we cannot KYC.
- We will not enable Darwins to participate in categories we've forbidden, regardless of user request.
- We will not assist law enforcement beyond what's legally compelled (and we will publish a transparency report when we exit small-platform exemptions).
- We will not silently change the constitution. Every change goes through a published changelog.

## Open legal questions

Tagged for counsel review:

- **Is the platform a money transmitter, a money services business, both, or neither?** Depends on structure. Counsel input required before phase 2.
- **Does the platform need a virtual asset service provider (VASP) registration in EU member states under MiCA's transitional rules?** Likely yes; need to register correctly.
- **What's the right ToS structure for Darwin's autonomous actions?** Standard agency law works for actions within explicit authorization; less clear for actions within "general business authorization" that are still novel under existing case law. Counsel review.
- **Are Darwin-acquired accounts (e.g. a WordPress site the Darwin opened on behalf of the user) the user's property at law?** The vault holds the credentials and the user is identifiable as the account-holder of record (KYC was theirs), but the *novelty* of having an agent open an account is something the courts haven't litigated.
- **Are children Darwins their parents' "products" or independent agents at law?** Currently we treat them as independent agents owned by the same user. Counsel review for edge cases (parent dies, child continues).
- **How does the EU AI Act classify a Darwin?** If high-risk, full compliance regime applies — significant. If limited-risk, transparency obligations only.

These questions are pre-launch homework. They are not phase-0 or phase-1 blockers; they are phase-2 blockers.

## How we operationalize legal across phases

| Phase | Legal activity |
|---|---|
| 0 | None publicly. Operate as R&D. Don't take real users' money. |
| 1 | Engage counsel; structure the entity; draft v0 ToS, privacy policy, AML/KYC procedures; obtain insurance quotes. |
| 2 | Execute the structure. Live ToS in place; live AML procedures; KYC provider integrated; sanctions screening live; SOC 2 readiness assessment. |
| 3 | Public launch. Bug bounty live. Transparency reporting begins. Expand to additional jurisdictions per priority. |
