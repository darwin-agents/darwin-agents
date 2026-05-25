# Browser automation stack

## Decision

- **Primary framework:** **Browser-Use** (Python, autonomous agent loop), driving the page via the DOM/accessibility tree.
- **Browser runtime:** **Browserbase** for managed Chromium with stealth, residential proxies, session recording.
- **Optimization layer for repetitive flows:** **Stagehand** with cached actions (or `workflow-use` to convert successful Browser-Use traces into cached replays).
- **Last-resort autonomous attempt:** **Browser-Use's own vision mode** (screenshots + DOM hybrid, on the same Browserbase session) for the rare page the DOM can't express — capped on cost and steps.
- **True last resort:** **escalate to the owner** for a live human takeover of the Browserbase session — the same mechanism used to resolve captchas.

This is a deliberate layered stack: an autonomous DOM-driven agent that figures out new sites, a cached deterministic replay for sites it's mastered, a capped vision-mode attempt for pages the DOM genuinely can't express, and human takeover for the truly hostile cases. The browser always runs remotely on Browserbase — the agent only drives it over CDP/API, so nothing in the stack needs a local display, whether the agent runs in a microVM, a VPS container, or serverless.

## Why Browser-Use primary

Browser-Use is the right primary for Darwin specifically:

1. **Python-native.** Browser-Use is `pip install browser-use`. Our orchestrator and neurons are Python; LangGraph is Python; LiteLLM is Python. Stagehand is TypeScript-native and runs naturally on Node — using it from Python means subprocess invocation or HTTP wrapping, which is operational friction we'd rather avoid. The Python fit alone is decisive.

2. **Autonomous mode matches Darwin's workload.** A young Darwin doesn't know what works yet — discovering effective strategies is the whole point of evolution. Most browser tasks early in a Darwin's life are exploratory: poking at unfamiliar sites, trying new platforms, signing up for new services. Browser-Use's agent-first model ("give me a goal in natural language, I'll figure out the steps") is the right abstraction for this. Stagehand's hybrid model assumes a human (or in our case a Darwin's planner) has already authored the flow — premature for exploratory work.

3. **Ecosystem and maturity.** 50K+ GitHub stars, one of the fastest-growing open-source AI projects of 2025–2026. LangGraph integration is first-class. LiteLLM is supported. Big enough community that bugs get fixed fast and integrations exist for most things we'll want.

4. **Reliability is competitive.** On WebVoyager-style benchmarks, Browser-Use scores ~89% and Stagehand ~89%. They are within a percentage point. Reliability is not the decisive axis; cost structure is.

5. **Cost is solvable.** The biggest concern with Browser-Use is that the LLM is in the loop on every step, so cost scales linearly with workflow length. We mitigate this three ways (see below), and what's left becomes a normal Darwin economic decision — does the metabolism of this neuron exceed the revenue from its work?

## Where Stagehand fits

Stagehand's killer feature is **action caching with self-healing replay**. First run of a workflow costs LLM tokens; subsequent runs hit the cache and execute at near-zero LLM cost. The cache is "write-once, run-forever" — when a cached selector eventually breaks, Stagehand re-invokes the LLM, updates the cache, and continues.

This is a perfect fit for the _repetitive_ half of Darwin's workload:

- `content.publisher` posting a daily blog to WordPress
- `analytics.measurer` reading the same dashboard every morning
- `ads.manager` adjusting bids on configured campaigns
- Any flow the Darwin has run successfully N times and is now stable

The pattern we adopt: Darwin discovers a workflow exploratively with Browser-Use, observes that it succeeds repeatedly, and the reflect node decides to _promote_ the workflow to a cached Stagehand or `workflow-use` replay. This is a learnable, evolvable behavior — a Darwin discovering when to switch tools to save money. From Layer 0 we set the gate (e.g. ≥ 5 consecutive successes on the same target) and the cost-savings calculation; lessons can tune them.

`workflow-use` (the Browser-Use team's own extension for the same purpose) is an alternative to Stagehand for this caching layer — same idea, different framework. We start with Stagehand and revisit once `workflow-use` matures. Both can coexist; the promotion target is "cached replay", not specifically "Stagehand".

## Cost economics — the actual numbers

This is the part that determines whether Browser-Use works at our scale. Concretely:

**Per-step LLM cost.** A Browser-Use step is one LLM round-trip to interpret the page and decide the next action. On a balanced-tier model (Claude Haiku, Gemini Flash, GPT-5.5 mini), per-step input is ~5K tokens (DOM-derived accessibility tree + history), output ~200 tokens. Cost roughly $0.005–0.02 per step.

**A typical workflow.**

- Publishing a blog post: ~10 steps → $0.05–0.20 per run
- Signing up for a new service: ~15-30 steps → $0.08–0.60 per attempt (heavy because captchas/KYC introduce extra exploration)
- Reading an analytics dashboard: ~5 steps → $0.03–0.10 per run
- Filling out a sign-up form: ~8 steps → $0.04–0.16 per run

**Scale impact.** A content-publishing Darwin doing 3 publishes/day on 2 platforms (6 sessions, ~10 steps each) burns $0.30–$1.20/day in Browser-Use LLM tokens, _before caching_. With cached replay for stable workflows, this drops to near-zero amortized cost after the first successful run. Affordable for any Darwin earning even modest affiliate revenue.

**Three cost-mitigation levers we build in:**

1. **Tier-down per step.** Most per-step decisions are simple ("click the next button"). We configure Browser-Use to use `tier=fast` (DeepSeek V3, Gemini Flash, Llama 3.3 70B) for routine steps, escalating to `tier=balanced` or `tier=reasoning` only when the planner reports uncertainty. Via LiteLLM this is a routing-table decision, not a Darwin code change.

2. **Cached replay promotion.** As above — successful repeated workflows promote to Stagehand or `workflow-use` cached replays. The Darwin's reflect node learns when to promote.

3. **Per-Darwin LiteLLM virtual key cap.** Browser-neuron LLM spend is metered against the same per-Darwin budget as everything else. A runaway browser neuron hits its cap and the orchestrator either escalates or downgrades. We won't be surprised by a $500/day bill from a single misbehaving Darwin.

The cost story is real but bounded. Without caching, Browser-Use is too expensive for high-volume repetitive workloads. With caching, it's competitive with anything else available.

## Browserbase as the runtime

Browser-Use and Stagehand both work against any CDP-compatible cloud browser provider. We use **Browserbase** for production browser sessions because:

- Managed Chromium fleet — we don't operate browsers ourselves.
- Stealth profiles and anti-bot evasion baked in.
- Residential proxy rotation included.
- Session recording for debugging and the dashboard's Anatomy/Timeline live-view (when a Darwin operator wants to watch a browser session in real time, especially for resolving a captcha escalation).
- Live takeover — the user can take direct control of a Browserbase session via the dashboard, which is how captchas get resolved.

Alternative cloud-browser providers (Steel, Scrapfly, Bright Data Agent Browser) are reasonable backups; the framework choice (Browser-Use) is decoupled from the runtime choice. We standardize on Browserbase for v1 and reassess at scale.

## The browser neuron's decision tree

```
incoming task with target URL
       │
       ▼
1) Has this Darwin run a successful cached workflow (Stagehand/workflow-use)
   for this URL recently?
       │ yes ── run cached actions; replay deterministically; near-zero cost
       │ no
       ▼
2) Does this Darwin have ≥ 5 successful Browser-Use traces for this URL?
       │ yes ── promote one to a cached workflow; run that
       │ no
       ▼
3) Run Browser-Use (DOM/accessibility tree) with tier=fast per-step
       │ on success ── log the trace; reflect node may flag for promotion
       │ on uncertainty ── escalate per-step tier to balanced or reasoning
       │ on hard block (captcha, KYC, 2FA) ── escalate to owner
       │ on DOM unavailable (canvas-only app, hard anti-bot wall)
       ▼
4) Browser-Use vision mode on the SAME Browserbase session
   (screenshots + DOM hybrid), strict cost + step cap
       │ on success ── log the trace; reflect node may flag for promotion
       │ no progress within the cap
       ▼
5) Escalate to owner — live human takeover of the Browserbase
   session (the captcha-resolution mechanism, reused)
```

The tree is itself learnable. The Darwin's reflect node records which approach worked per target and adjusts routing accordingly. The promotion gate (5 successes), the per-step tier defaults, and the vision-mode cost and step caps are all hyperparameters that start in Layer 0 and can be tuned by lessons. Vision-mode steps are markedly pricier than DOM steps — a full-frame screenshot is far more tokens than a trimmed accessibility tree — which is why the cap is strict and the path is last-resort, not routine.

## Anti-bot, KYC, captchas, 2FA

The Darwin operates on the open web like a real user where possible. Where it can't, we escalate cleanly:

- **Captchas.** Always escalate. We don't try to solve them programmatically (legally fraught, technically an arms race we lose) and we don't use paid captcha-solving services (TOS violation almost everywhere). The owner solves them in the dashboard via Browserbase's live-takeover UI — the session streams to the user's browser, they click, and control returns to the Darwin.

- **KYC / identity verification.** Always escalate. The owner uploads documents to the vault under a fresh one-time-use token; the neuron retrieves them via that token, presents them, and tears down the token. See [`16-secrets-and-vault.md`](16-secrets-and-vault.md) for the vault flow.

- **2FA / SMS verification.** Two patterns supported:
  - High-trust: the platform owns a phone number (Twilio etc.) that the owner has authorized, codes route into the platform inbox, the user approves their release to the neuron.
  - Low-trust: every code release is an escalation; the owner manually approves each one.

- **Hostile pages (canvas-only apps, hard anti-bot walls).** The DOM path can't express these. Darwin makes one capped Browser-Use vision-mode attempt on the same Browserbase session; if it makes no progress within the cap, it escalates to a live human takeover — the same path as captchas.

The general principle: a captcha or human-verification gate is **not a failure** — it's a control-flow primitive that paused the neuron and notified the owner. Same model as everywhere else in Darwin's architecture.

## When we'd revisit this choice

We'd revisit if:

- Stagehand ships a first-class Python SDK that matches its TypeScript ergonomics. The Python fit is the strongest argument for Browser-Use; if Stagehand removes it, the calculus changes.
- Browser-Use's autonomous-loop costs prove unmanageable even with our three mitigations. Real production data from Phase 0–1 will tell us.
- A new framework emerges that combines Browser-Use's autonomy with Stagehand's caching primitives natively. The space is moving fast; this is plausible within a year.
- Browserbase has a sustained outage during which we discover our threshold of pain. Then we add Steel or Scrapfly as a secondary runtime; Browser-Use abstracts the runtime, so the switch is config-level.

## Practices that fall out of this

A few things the Darwin code has to handle that aren't immediately obvious:

- **Trace logging.** Every Browser-Use session writes its trace (page → action sequence) to the per-Darwin organism database. The reflect node reads these to decide promotion.
- **Cache invalidation.** Cached Stagehand workflows can break when the target site changes. We rely on Stagehand's self-healing for routine drift, and the reflect node demotes a workflow back to Browser-Use exploration if the cache produces N consecutive failures.
- **Cost attribution per session.** LiteLLM returns per-request cost; we tag each call with the neuron, target URL, and trace ID so the dashboard's Timeline can show "this browser session cost $0.14".
- **The reflect node's promotion logic** is one of the most concrete learnable behaviors a Darwin has. Phase 1's evolution-loop demonstrations should show this working — a Darwin self-modifying its routing to prefer cached replays for stable workflows.

## Browser Use Cloud

Browser Use Cloud didn't exist when this doc's approach was set — see [`31-browser-use-evaluation.md`](31-browser-use-evaluation.md) for a deep-dive and current head-to-head eval.

**Summary:**

- **Current default:** Browser-Use OSS library as orchestration (agent loop, DOM interface), running on Browserbase as the browser/runtime/proxy substrate.
- **Alternative:** Browser Use Cloud now offers a fully managed runtime (browser, agent, proxies, custom LLM, API) at a significant cost advantage with a generous free tier.
- **Decision (as of 2026-05-25):**
  - Don't migrate outright yet; results are promising but we haven't fully production-tested it.
  - Add a provider adapter. Support both Browserbase and Browser Use Cloud as drop-in backends (see provider-neutral client pattern in `31-browser-use-evaluation.md`). Operator picks via a flag/env.
  - Test the BU model (Browser Use's custom automation-focused LLM) for real tasks; only commit once data supports the claimed cost/performance gains.
  - Keep all provisioning behind the existing treasury policy and capability-approval flows (see §Agent self-registration in [`31-browser-use-evaluation.md`](31-browser-use-evaluation.md)) — never let agents sign up/account-spend unmediated.

**If you're evolving this stack:**

- Ship an MVP on the existing Browserbase/Browser-Use path.
- Add `BrowserUseCloudClient` against the same protocol as the current client, configurable at deploy time.
- Route costs through the spend-policy engine, never direct vendor-side account creation by agents.
- Escalate any agent self-registration to an owner for explicit sign-off.

**For worked numbers, migration step-by-step, and risk notes:**  
→ See [`31-browser-use-evaluation.md`](31-browser-use-evaluation.md) for pricing, reliability, security, and next-steps recommendations.
