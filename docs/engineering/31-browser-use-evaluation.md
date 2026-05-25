# Browser Use Cloud — evaluation

*Research note, dated **2026-05-25**. Companion to
[`15-browser-stack.md`](15-browser-stack.md) — that doc defined the
current Browser-Use-on-Browserbase split; this one re-evaluates that
choice now that **browser-use.com** ships an end-to-end cloud
(runtime + agent + model + proxies) that didn't exist when doc 15 was
written.*

## TL;DR

- **Don't migrate. Add an adapter.** The current Browser-Use library on
  Browserbase wiring stays the default. Add a `BrowserUseCloudClient`
  alongside the existing `BrowserUseClient` so the operator can pick at
  deploy time. Matches our provider-neutral stance and the pattern used
  for Stripe/MoonPay, Persona/Onfido, Anthropic/OpenAI/Gemini.
- **Browser Use Cloud is genuinely cheaper** than Browserbase on both
  browser-hours (~40-50%) and proxy bandwidth (~50%), and has a
  materially better free tier. The OSS `browser-use` library is shared
  — it's the *managed runtime + custom model* that's new.
- **The "BU" model is worth a test, not a bet.** Vendor numbers (26×
  cheaper / 4× faster than frontier) are plausible-but-unverified.
  Marketing has no named benchmark and compares against an older Sonnet.
- **The agent self-registration flow is novel and risky.** Beautiful for
  a Darwin to provision its own substrate; gate it behind the spend
  policy engine and platform-mediated provisioning if we ever use it.

## What browser-use.com sells in 2026

Six distinct products under one roof:

| Product | What it is | Maps to today's stack as |
| --- | --- | --- |
| **Browser Harness** | OSS Python library — agent loop that drives a browser via the DOM/accessibility tree. The thing we already `pip install browser-use`. | Already in use ([15-browser-stack.md](15-browser-stack.md), `BrowserUseClient`). |
| **Stealth Browsers** | Managed Chromium runtime with anti-bot patches + CAPTCHA solving + residential proxies. | Direct competitor to **Browserbase**. |
| **Custom Models** ("BU") | LLMs purpose-built for browser automation. | New category — competitor to using Claude/GPT/Gemini as the agent brain. |
| **Web Agents** | Natural-language extract/automate/test/monitor as a managed API. | Higher-level than what we use; we drive the agent ourselves. |
| **Proxies** | Residential IPs, 195+ countries, rotating. | Bundled with Browserbase today. |
| **Browser Use Box** | 24/7 remote Claude Code + Browser Harness, drivable from Telegram/SSH/web. | *Not relevant for Darwin — different problem.* |

Plus an unusual **AI self-registration endpoint** (see [§Agent
self-registration](#agent-self-registration)) — a `POST /cloud/signup`
flow with an LLM-solved math challenge, explicitly designed for an
autonomous agent to provision its own account.

## Pricing — verified head-to-head

Both providers' published pricing as of **2026-05-25**.

### Subscription plans

| Tier | **Browser Use Cloud** | **Browserbase** |
| --- | --- | --- |
| **Free** | 3 concurrent · "free browser access" · 1 team member · 1 profile | 3 concurrent · **1 hr/mo** · no CAPTCHA · no proxies |
| **Entry** | **$29/mo** Dev — 25 concurrent · $29 credits · BYO key/proxy · auto-recharge · scheduled jobs | **$20/mo** Developer — 25 concurrent · 100 hrs included · 1 GB proxy · auto-CAPTCHA |
| **Mid** | **$299/mo** Business — 200 concurrent · $400 credits · unlimited team/profiles | **$99/mo** Startup — 100 concurrent · 500 hrs included · 5 GB proxy |
| **Top published** | **$999/mo** Scaleup — 500 concurrent · $1,400 credits · dedicated support | Scale — custom · 250+ concurrent · HIPAA/DPA/SSO |

### Pay-as-you-go rates

| Resource | **Browser Use Cloud** | **Browserbase** | Browser Use vs Browserbase |
| --- | --- | --- | --- |
| Browser session | **$0.06/hr** | $0.10/hr (Startup overage) · $0.12/hr (Developer overage) | **~40-50% cheaper** |
| Proxy bandwidth | **$5/GB** | $10/GB (Startup) · $12/GB (Developer) | **~50% cheaper** |
| CAPTCHA solving | Bundled in Stealth Browsers | Bundled on Developer+ | Comparable |
| Pass-through LLM tokens | **1.2× provider rates** (V3 Agent) | N/A — you bring your own LLM | Browser Use takes 20% markup if you route through them; BYO LLM at Dev tier+ avoids it |

### "BU" custom-model pricing

Browser Use publishes **only** these figures:

- "**53 tasks per dollar vs 2 for Sonnet 4.5**" (≈26× cheaper).
- "**68 seconds per task vs 225-330s for Gemini, Claude, and OpenAI
  computer use models**" (≈3-5× faster).
- Per-token pricing: **not disclosed**.
- API availability outside their runtime: **not disclosed**.

### Worked example: 10 concurrent × 8 h/day × 30 days ≈ 2,400 browser-hrs/mo

| Path | Browser cost | Notes |
| --- | --- | --- |
| **Browserbase Startup + own LLM** | $99 base + 1,900 hrs overage × $0.10 = **~$289/mo** | + your LLM bill (model-agnostic, $100-1,000+/mo) |
| **Browser Use Business + BYO LLM** | $299 base — 2,400 hrs × $0.06 = $144 fits inside the $400 credits = **~$299/mo** | + your LLM bill (BYO at Dev+ tiers) |
| **Browser Use Business + V3 Agent** | Same $299, agent LLM at 1.2× provider rates | 20% markup on every token vs going direct |
| **Browser Use + BU model** *(if claims hold)* | $299 base + ~$45/mo at 1,000 tasks/day (vendor math: 53 tasks/$1) | + ~$1,500/mo same workload on Sonnet 4.6 direct (vendor's own comparison; treat with skepticism) |

The browser-runtime cost gap is real and ~30% in Browser Use's favour at
this volume. The agent-LLM gap (Sonnet 4.6 direct vs BU model) **depends
entirely on whether you trust unverified vendor numbers** — could be
order-of-magnitude transformative, could be 2-3× cheaper, could not
work for your workload at all.

## Stealth Browsers vs Browserbase (runtime layer)

Feature-parity is essentially complete; differentiation is on price and
incumbency.

**Browser Use Stealth Browsers**
- Custom Chromium fork with patches to bypass **Cloudflare, DataDome,
  Kasada, Akamai, PerimeterX**.
- Auto-solves **Cloudflare Turnstile, reCAPTCHA, PerimeterX** challenges.
- Residential proxies in **195+ countries**.
- 1Password integration for auto-login with 2FA.
- No published SLA / regions / per-CAPTCHA pricing on the marketing
  page; see their docs.

**Browserbase**
- Mature managed Chromium with comparable stealth + CAPTCHA on
  Developer tier and up.
- Stagehand cached-replay library is first-party — useful for
  repetitive Darwin flows (see [`15-browser-stack.md`](15-browser-stack.md)
  §Where Stagehand fits).
- Longer in market → more production hours and known failure modes.
- HIPAA / DPA / SSO available on the Scale tier.

For our use case the runtime is largely a commodity. Browser Use wins
on price; Browserbase wins on track record and Stagehand integration.

## The "BU" custom model — be skeptical

Vendor claims that demand scrutiny:

| Claim | Status |
| --- | --- |
| "Outperforms general-purpose frontier models" | **Unfalsifiable** — no named benchmark cited |
| "68s/task vs 225-330s for Gemini, Claude, OpenAI" | **Vendor-published, no benchmark named** — task complexity dominates this metric |
| "53 tasks per dollar vs 2 for Sonnet 4.5" | Plausible direction (a fine-tune *can* be much cheaper), magnitude (~26×) suspicious without an independent benchmark |
| Comparison against **Sonnet 4.5** | Mild red flag — Sonnet 4.6 has shipped (Sept 2025); choosing an older comparison point inflates the gap |
| Architecture (fine-tune? custom?) | Page says only "trained specifically for browser automation" |
| Per-token pricing | **Not disclosed** |
| Available outside Browser Use Cloud runtime? | **Not disclosed** |

**Plausible reality:** a domain-specific fine-tune on browser
trajectories is genuinely faster and cheaper than running a general
frontier model for the same task — that part is mechanically sound.
The *magnitude* of the improvement is the unverified part. A 2-3×
improvement is expected; 26× is not.

**Test, don't trust.** Run a representative Darwin workload
(content publish, affiliate dashboard, form fill) on the BU model and
on Sonnet 4.6 direct, measure cost-per-successful-task. Make the
decision on **your** numbers, not their landing page.

## Agent self-registration

Browser Use ships a flow explicitly for autonomous-agent onboarding:

```
POST /cloud/signup           → challenge (LLM-solvable math problem)
POST /cloud/signup/verify    → api_key
[optional] /cloud/signup/claim → claim_url for a human to attach later
```

**Why this is interesting for Darwin:** doc 04 phase 3 / doc 07 already
contemplate a Darwin requesting capabilities the platform provisions on
its behalf. An agent that can provision its own browser substrate at
boot — without the supervisor intermediating — fits the same shape.

**Why this is risky:** any agent that can self-register can rack up
costs without owner sign-off. We have a treasury policy engine and a
spend-cap stack ([`05-treasury-and-payments.md`](05-treasury-and-payments.md))
for exactly this; bypassing it via direct vendor-side signup defeats
the point.

**If/when we use it:**
- Route through `payments.facilitator` so spend hits the policy engine.
- Cap monthly spend at the Darwin's strategy envelope.
- Owner-escalate before the first signup (the first call should be a
  human-approved capability request, not unattended).
- Treat the returned API key as a **sensitive-tier** vault credential
  ([`16-secrets-and-vault.md`](16-secrets-and-vault.md)) and rotate via
  the existing rotation path.

## Three options weighed

### Option A — Stay current (Browser-Use OSS lib + Browserbase)
- **Pros.** Zero migration. Browserbase is battle-tested. Stagehand
  caching is first-party. LLM choice stays ours (LiteLLM proxy).
- **Cons.** Paying Browserbase's premium (~30-50% above Browser Use on
  hours + proxies). No access to the BU model.
- **Cost @ worked example.** ~$289/mo browser + your LLM bill.

### Option B — Switch wholesale to Browser Use Cloud
- **Pros.** Cheaper hours + proxies. One vendor. Bundled stealth/CAPTCHA/
  proxies. Access to BU model (if it actually works for our tasks).
- **Cons.** Vendor concentration on agent + runtime + (optionally)
  model — single outage takes everything down. Less production track
  record than Browserbase. Stagehand-style caching not first-party.
  Unclear whether our existing OSS `browser_use.Agent` works against
  Browser Use Cloud's runtime or if we'd be moving to their managed
  V3 Agent API (different abstraction).
- **Cost @ worked example.** ~$299/mo browser + your LLM bill (or
  potentially dramatically less if BU model holds up).

### Option C — Hybrid (Browserbase runtime + BU model brain)
- **Pros.** Keep battle-tested infra; capture the cost win on the agent.
- **Cons.** BU model standalone availability is unconfirmed. Adds a
  third vendor. Operational complexity for marginal benefit.
- **Verdict.** Only viable if BU model has a standalone API. Not worth
  pursuing until that's verified.

## Recommendation

**Do option A today. Build the adapter for option B. Test the BU model.**

Concretely, for an MVP-ASAP solo-dev launch:

1. **Ship the MVP on the current Browserbase wiring.** It works, it's
   tested, no churn. Status quo for launch.

2. **~~Round-7 task: add `BrowserUseCloudClient`.~~ Shipped 2026-05-25.**
   `src/darwin_platform/adapters/browser_use_cloud.py` implements the
   same `BrowserClient` Protocol as `BrowserUseClient(Browserbase)`.
   Picked via `--browser-provider {browserbase,browser-use-cloud}` and
   `BROWSER_USE_API_KEY`; spend-capped via `--browser-use-max-cost-usdc`
   (pre-debited to the wallet *and* passed to the SDK as `max_cost_usd`
   so cloud billing matches the ledger debit). Optional
   `--browser-use-model` for the BU custom model. `[browser-use-cloud]`
   extra. +11 tests.

3. **Sign up for Browser Use Free** (3 concurrent, "free browser
   access" — verify this means unmetered hours at 3 concurrent). Run
   2-3 real Darwin flows through it end-to-end. Compare reliability and
   per-flow cost against Browserbase on the same flows.

4. **Test the BU model on one representative task.** Publish a content
   piece, fill an affiliate signup form, log into a dashboard — both
   via BU model and via Sonnet 4.6 direct. Record successful-task rate
   and cost-per-successful-task. **Decide on your data, not their
   landing page.**

5. **Hold off on the agent self-registration flow** until the
   capability-request + spend-policy plumbing is end-to-end (it
   essentially already is — see
   [`04-neurons.md`](04-neurons.md) phase 3 + the existing
   `payments.facilitator`). When we use it, route through that, not
   direct.

**Bottom line.** Browser Use Cloud is **probably worth it as a
price-and-stealth alternative**; the BU model is **worth testing, not
worth committing to** until we have our own numbers. The provider-
neutral adapter pattern lets us keep both available and switch on
data instead of marketing copy.

## Sources

- Browser Use pricing: <https://browser-use.com/pricing>
- Browser Use custom models: <https://browser-use.com/custom-models>
- Browser Use stealth browsers: <https://browser-use.com/stealth-browsers>
- Browser Use cloud docs: <https://docs.cloud.browser-use.com>
- Browser Use OSS library: <https://github.com/browser-use/browser-use>
  · docs: <https://docs.browser-use.com>
- Browserbase pricing: <https://www.browserbase.com/pricing>
- AI self-registration flow: documented at
  `https://api.browser-use.com` per Browser Use's landing page
  instructions to agents.

All vendor pricing and product claims verified **2026-05-25**. Both
products are moving fast; revisit before any production commitment that
extends beyond ~3 months.
