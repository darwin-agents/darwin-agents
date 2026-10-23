# Darwin Agents — documentation

Darwin Agents runs **economically autonomous AI organisms**: each *Darwin* boots from a signed, immutable genome, earns and spends from its own treasury, rewrites its own mutable body under a constitution it cannot change, and reproduces when it is profitable — or dies when it runs out of money. This folder is the design, operation, and product documentation for the whole system; the code lives in [`../src/`](../src/), the dashboard in [`../web/`](../web/), and deployment assets in [`../infra/`](../infra/).

> **Status (2026-10): testnet alpha.** The genome, the platform control plane, the dashboard (with its PWA escalation companion), and the Hetzner k3s infrastructure are built and covered by eight test tiers. Real money is gated on the operator checklist in [`engineering/32-pre-mainnet-gates.md`](engineering/32-pre-mainnet-gates.md) and [`engineering/30-mainnet-cutover.md`](engineering/30-mainnet-cutover.md). Release history: [`../CHANGELOG.md`](../CHANGELOG.md).

## Where to start

| You are… | Read, in order |
| -------- | -------------- |
| **New to the project** | [Vision and metaphor](overview/01-vision-and-metaphor.md) → [Design principles](overview/02-design-principles.md) → [System overview](engineering/01-system-overview.md) → [System diagrams](engineering/19-system-diagrams.md) |
| **An engineer on the genome** | [DNA core](engineering/02-dna-core.md) → [Soma and orchestrator](engineering/03-soma-and-orchestrator.md) → [Neurons](engineering/04-neurons.md) → [Treasury](engineering/05-treasury-and-payments.md) → [Evolution](engineering/06-evolution-and-reproduction.md) → [Soma activation](engineering/33-soma-activation-design.md) |
| **An engineer on the platform** | [Control plane](engineering/08-platform-control-plane.md) → [Isolation](engineering/07-isolation-and-multi-tenancy.md) → [Data model](engineering/23-data-model-reference.md) → [Cross-cutting contracts](engineering/28-cross-cutting-contracts.md) → [Configuration reference](engineering/27-configuration-reference.md) |
| **Operating a deployment** | [Operational runbook](engineering/25-operational-runbook.md) → [Configuration reference](engineering/27-configuration-reference.md) → [Mainnet cutover](engineering/30-mainnet-cutover.md) → [Pre-mainnet gates](engineering/32-pre-mainnet-gates.md) → [`infra/README.md`](../infra/README.md) |
| **Reviewing security** | [Threat model](engineering/24-security-threat-model.md) → [Kill switches](engineering/09-kill-switches-and-owner-controls.md) → [Anti-wireheading](engineering/10-alignment-and-anti-wireheading.md) → [Secrets and Vault](engineering/16-secrets-and-vault.md) → [Testing strategy](engineering/26-testing-strategy.md) (red-team mapping) |
| **Writing or reviewing tests** | [Testing strategy](engineering/26-testing-strategy.md) → [`tests/README.md`](../tests/README.md) |
| **Working on the dashboard** | [`frontend/00-README.md`](frontend/00-README.md) |
| **Product, legal, or go-to-market** | [Market landscape](overview/04-market-landscape.md) → [Legal and ToS](overview/05-legal-and-tos.md) → [Roadmap](overview/06-roadmap-and-phases.md) → [`marketing/`](marketing/00-README.md) |

## Folders

| Folder | Contents | Index |
| ------ | -------- | ----- |
| [`overview/`](overview/) | The idea and its rationale: vision, principles P1–P10, prior art, market, legal, roadmap, infrastructure phases | [`overview/00-README.md`](overview/00-README.md) |
| [`engineering/`](engineering/) | How it is built and run: architecture (01–10), tech stack (11–17), build plans (18–22), reference and hardening (23–34), backlog audit | [`engineering/00-README.md`](engineering/00-README.md) |
| [`frontend/`](frontend/) | The Next.js dashboard and PWA companion: screens, Anatomy/Timeline canvases, realtime, auth, delivery | [`frontend/00-README.md`](frontend/00-README.md) |
| [`marketing/`](marketing/) | Non-technical message source for the landing page and public material | [`marketing/00-README.md`](marketing/00-README.md) |
| [`postmortems/`](postmortems/) | Filled-in incident postmortems, one file per incident | [`postmortems/README.md`](postmortems/README.md) |

Outside this folder: [`../README.md`](../README.md) (quickstart, repository layout, contributing), [`../tests/README.md`](../tests/README.md) (test map), [`../infra/README.md`](../infra/README.md) and [`../infra/OPERATOR_RUNBOOK.md`](../infra/OPERATOR_RUNBOOK.md) (deployment assets), [`../web/README.md`](../web/README.md) (dashboard dev loop).

## Conventions

- **Numbering.** Files in `overview/`, `engineering/`, `frontend/`, and `marketing/` are numbered in reading order, and every folder has a `00-README.md` index. "Doc *NN*" in prose and code comments means `engineering/NN-*.md` unless another folder is named.
- **Kinds of doc.** *Design docs* (engineering 01–17, frontend 01–09) record the design and why. *Build plans* (engineering 18, 20–22; frontend 10) are dated plans with a status banner. *Reference docs* (engineering 23–30, 32) describe the system as it runs and are kept current. *Research notes* (engineering 31) are dated and not updated.
- **As built.** When the implementation diverges from a design doc, the doc keeps its rationale and gains a `> **As built (YYYY-MM):** …` note pointing at the code, rather than being silently rewritten.
- **Generated content.** The `serve` flag tables in [doc 27](engineering/27-configuration-reference.md) are generated from the CLI (`make docs-config`). Don't edit them by hand.
- **Docs are tested.** The infra test tier fails when a CLI flag or environment variable is undocumented, when a relative link breaks, or when an engineering doc is missing from its index — so a docs change goes through the same PR gate as code.
- **Commits.** Use Conventional Commits with the `docs` type (`docs(engineering): …`); docs changes appear in the changelog under *Changed*.
