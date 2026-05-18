# Vision and the metaphor

## The metaphor

Darwin is, deliberately, a biological metaphor pushed as far as it will go in software.

| Biology | Darwin |
|---|---|
| Genome | The immutable DNA core (Python package, signed, never modified by the agent) |
| Soma (body) | The mutable agent code the DNA generates and refines at runtime |
| Cells / organs | Neurons — specialized worker sub-agents (content, ads, accounts, browser, trading, support) |
| Metabolism | LLM tokens, compute, third-party APIs, ad spend, subscriptions — anything that costs money |
| Food | Revenue from the online businesses the neurons run |
| Hunger | Falling treasury balance, approaching minimum viable balance |
| Death | Treasury hits zero before next revenue cycle, or owner pulls the kill-switch |
| Reproduction | A profitable Darwin spawns a child whose DNA is parent's DNA + a new sealed layer of learned code |
| Generation | Each successful reproduction event increments the lineage's generation counter |
| Mutation | Mid-life code rewrites the agent performs on its own soma to adapt |
| Natural selection | Unprofitable Darwins die; only profitable ones reproduce. The fittest lineage propagates |
| Supernatural intervention | The human owner — solves captchas, uploads KYC docs, approves high-stakes actions |

The metaphor is load-bearing, not decorative. Every design decision should be checkable against it: *what is the biological analogue, and does our implementation honor it?* Where the analogy breaks down, we name it (e.g. there is no biological equivalent of a kill-switch; we have one anyway because we're operating in human society).

## Why an organism, not just an agent

Most "autonomous agents" today are scripts with an LLM in the loop. They wake up, perform a task, return, and sleep. They don't persist, don't have a metabolism, don't have a budget, and don't have any reason to care whether they exist tomorrow.

A Darwin is different in four ways:

1. **It has skin in the game.** The treasury is real. Bad decisions cost real money. Good ones earn it. Without a budget that can hit zero, there is no selection pressure, and without selection pressure, there is no meaningful evolution.
2. **It has a body.** Its code is itself, and it can change itself. The architecture is reflexive: the agent's job includes refactoring the agent.
3. **It has children.** Reproduction is the unit of progress, not iteration. A run isn't successful because metrics went up; it's successful because the lineage continued.
4. **It has a lineage.** Generation N inherits the sealed wisdom of generations 1..N-1. We're not training a model; we're growing a family tree of agents whose collective memory is encoded in stacked, immutable DNA layers.

## What problem this is actually solving

Darwin is a research and product bet on two propositions:

- **Proposition A — Compounding code.** AI agents are most useful when they can improve their own scaffolding. The Darwin Gödel Machine proved this on coding benchmarks; we are testing whether the same dynamic produces useful behavior under economic selection pressure. If it does, we'll see emergent strategies the human operator never thought to specify.
- **Proposition B — Owned automation.** Most online income from AI today is captured by the platforms (OpenAI, Anthropic, AWS, the ad networks). A Darwin is a user-owned worker that runs, learns, and earns on the user's behalf. The platform takes a cut for hosting; the user keeps the rest. This is a different business model from selling AI tooling, and it requires a different infrastructure stack.

If both propositions hold, Darwin is a new kind of digital labor: small, owned, self-improving, surviving on its own revenue.

## What this is not

To save discussion later, explicit non-goals:

- **Not AGI.** Darwins are narrow agents — they orchestrate online business operations. They don't reason about physics or invent new science. The "evolution" is over agent code and neuron behavior, not over the underlying foundation model.
- **Not a get-rich-quick scheme.** Most Darwins will die. This is by design — selection pressure requires that failure be real. The platform's value proposition is that the *successful lineages* compound into something useful, not that every individual launch makes money.
- **Not unsupervised.** The owner is part of the system. Darwin escalates captchas, KYC, identity-bound actions, and any spend above a configurable threshold. "Fully autonomous" is a marketing fantasy; "as autonomous as is currently legal and feasible" is the real goal.
- **Not jurisdiction-blind.** The platform will operate under specific terms of service and a defined set of allowed business categories. See [`05-legal-and-tos.md`](05-legal-and-tos.md).
- **Not anonymous.** Every Darwin is bound to a verified human principal. This is required by every payment rail it touches (Coinbase, Stripe, banks) and by anti-money-laundering law everywhere we plan to launch.

## The product

From the user's perspective:

1. **Sign up** on the Darwin platform with email + ID verification.
2. **Fund** the platform wallet — fiat in, USDC out into the user's per-Darwin treasury.
3. **Launch** a Darwin: pick a starting budget, optionally pick a business-category hint, accept the kill-switch terms.
4. **Watch.** The Darwin dashboard has two centerpiece views:
   - The **Anatomy** canvas — a live, animated cell-ramification map of the organism. Core orchestrator in the middle, neurons branching out, active ones pulsing, new ones growing in as the Darwin evolves.
   - The **Timeline** canvas — a horizontal chronological feed of every "movement" the Darwin makes: reasoning, LLM calls, tool/neuron invocations, payments, escalations.

   Around them: treasury balance, current strategy, revenue stream, lineage tree, open escalations, and a data browser into the Darwin's own databases.
5. **Help.** When the Darwin is blocked on a captcha or asks for documents, the user gets a push notification and resolves it from the dashboard or mobile app.
6. **Harvest** (optional). Profit above a configurable reserve can be auto-withdrawn to the user's bank or wallet.
7. **Mourn or celebrate.** When the Darwin dies, the user sees its full lifetime ledger and lineage. If it reproduced before dying, the children continue.

From Darwin's perspective: it wakes up, reads its DNA, surveys the world, picks a strategy, executes, monitors its treasury, and either earns enough to keep going or doesn't.
