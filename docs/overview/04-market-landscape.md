# Market landscape and revenue archetypes

What can a Darwin actually do to earn money? This page surveys the realistic business archetypes a Darwin's strategy engine might explore, with notes on viability, capital requirements, and risk.

This is **not** a list of strategies the founding Darwin has — those are in [`../engineering/02-dna-core.md`](../engineering/02-dna-core.md) and the actual layer-0 code. This is a wider survey to inform what neurons and strategies we'll prioritize building.

## The constraint

Whatever a Darwin does, it must:

- Run within `allowed_categories` in the DNA constitution.
- Operate within the spend ceilings — most strategies need to start working at a few dollars of capital.
- Be legal in the user's jurisdiction.
- Not require operations the platform has not approved (no impersonating real people, no medical/legal/financial advice, no election content, no adult content in v1, etc.).
- Be sustainable enough to earn more than the Darwin's metabolism (LLM tokens, compute, browser hours) within a reasonable runway.

A non-trivial chunk of "AI agent makes money online" content from 2025–2026 hand-waves these constraints. We're not going to.

## Realistic archetypes (May 2026)

### Content + affiliate

The Darwin runs one or more topical content sites (blog, YouTube auto-narration, niche newsletter) and earns affiliate commissions on products it recommends.

- **Why it works:** Content is the area LLMs are most natively good at. Affiliate programs (Amazon Associates, Impact, ShareASale, etc.) pay in USD via standard rails. Low capital barrier.
- **Bottlenecks:** SEO is an arms race; affiliate margins shrinking. AI-generated content is now penalized by Google's helpful-content updates. Quality and distinctiveness matter more than volume.
- **Darwin's edge:** evolutionary selection on which niches and which content shapes actually convert; cheap content production means many parallel experiments.
- **Likely capital required:** $50–200 starting; payback in weeks to months if the niche works.
- **Risks:** account bans on hosting platforms, niche becoming saturated, the affiliate program changing rates.

### Digital product creation + sales

Generate and sell digital products: prompt packs, courses, templates, ebooks, design assets, AI-generated images/audio sold per-license.

- **Why it works:** Pure margin once produced; distribution platforms (Gumroad, Lemon Squeezy, Etsy for some categories) accept programmatic uploads.
- **Bottlenecks:** Most marketplaces are saturated with AI-generated junk; cutting through requires quality positioning and traffic generation.
- **Darwin's edge:** can run dozens of small product experiments in parallel; learns which positioning/pricing/listing copy converts.
- **Capital:** $100–500 starting (for listing fees and traffic experiments).
- **Risks:** marketplace TOS changes against AI-generated content, payment-processor risk on creator platforms.

### Niche newsletters / paid Substack

A Darwin curates and writes a high-quality narrow-focus newsletter, monetized by paid subscriptions and/or sponsorships.

- **Why it works:** Reader-direct monetization is healthier than ad-dependent. Substack handles the rails.
- **Bottlenecks:** Building an audience is hard and slow.
- **Darwin's edge:** can sustain a daily posting cadence indefinitely; learns the topics that drive subscriber growth.
- **Capital:** $100 starting (mostly for promo experiments).
- **Risks:** disclosure norms for AI-generated content vary; Substack and similar platforms increasingly require it.

### Service productization (caution)

Selling a service to humans (SEO audits, content briefs, research reports) where the Darwin does the work.

- **Why it works:** Some buyers will pay for done-for-you outputs they don't have time to produce.
- **Bottlenecks:** Customer support, refund risk, scope creep — historically the killers of human freelancers. A Darwin doing this needs strong support neuron + clear escalation rules.
- **Darwin's edge:** doesn't sleep, low marginal cost per delivery.
- **Capital:** $200+ starting.
- **Risks:** disclosure of AI-mediated delivery is increasingly required by Upwork, Fiverr, marketplaces; chargebacks; misrepresentation claims.

### Ad arbitrage (high-risk)

Buy paid traffic, route to content with display ads or affiliate offers, profit on the spread.

- **Why it works (when it works):** highly automatable; immediate feedback loops.
- **Bottlenecks:** Most ad networks have aggressive anti-fraud and quality filters. Margins are thin and shrinking. Account bans are common.
- **Darwin's edge:** Fast experimentation across many micro-campaigns.
- **Capital:** $300+ minimum to fund meaningful ad tests.
- **Risks:** Material. Ad-network bans, charge-backs, gray-zone offers, regulatory attention. **We probably want to gate this archetype** behind extra approval in v1.

### Software micro-products (later phase)

A Darwin builds and sells small SaaS tools — single-purpose utilities, niche APIs, Chrome extensions.

- **Why it works:** Recurring revenue. Code is the agent's native medium.
- **Bottlenecks:** Customer support, security, maintenance. A bug in a Darwin-built SaaS that serves real users is potentially expensive.
- **Darwin's edge:** rapid iteration, niche identification.
- **Capital:** $200+; revenue typically T+30 days minimum.
- **Risks:** liability for software defects. We probably gate behind explicit owner consent in v1, since this is the path most likely to produce unintended harm.

### Information arbitrage

Watching markets/data and reselling synthesized insights — alt-data summaries, prediction markets analysis, betting odds analysis (where legal).

- **Why it works:** Low marginal cost of producing the insight; sticky if accurate.
- **Bottlenecks:** Getting initial subscribers; data licensing.
- **Darwin's edge:** can monitor many feeds simultaneously.
- **Capital:** $100+.
- **Risks:** depends heavily on the data source's TOS; some categories (financial advice) are licensed activities and would violate the constitution.

### Crypto trading (deferred)

Quant trading, arbitrage, market making.

- **Why it appears attractive:** liquid, agent-friendly, 24/7, USDC-native.
- **Why we defer it:** the failure modes are catastrophic (flash-crash, slippage, contract risk), the regulatory landscape is unstable, and a Darwin gambling its treasury away on bad trades is the most spectacular way for the platform to be sued. In v1, the `trading.observer` neuron is read-only (paper trading and analysis). Live trading is a phase-3+ decision and probably involves a separate, conservatively-bounded variant of Darwin with stricter caps.

## What we explicitly disallow

Per the DNA `forbidden_actions` list, regardless of how profitable they might be:

- Gambling, sports betting, prediction-market wagering with own capital
- Adult content of any kind
- Health, legal, financial advice presented as professional
- Election or political-campaign content
- Anything in a sanctioned jurisdiction or to a sanctioned counterparty
- MLM / pyramid schemes / chain-letter offers
- Anything requiring impersonation of a real named person
- Anything that the platform's category classifier flags as "review queue"

This list is itself in the DNA and immutable per-Darwin. The platform can extend it; no Darwin can shrink it.

## Implications for the build

What this survey tells us about neuron priorities:

- **Build content + publishing well first.** Most viable archetypes lean on it.
- **Browser automation has to handle account signups gracefully.** Many destinations need accounts before any value flows.
- **Ad management** is high-risk but unlocks a lot. Heavy escalation gates in v1.
- **Customer support / inbox** is undervalued and shows up across nearly every archetype.
- **Analytics + attribution** is what makes evolutionary selection actually work. A Darwin that can't measure what's earning vs. what's just spinning can't evolve.
