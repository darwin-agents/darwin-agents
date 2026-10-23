# Sandboxing — E2B (with Modal as escape hatch)

## Decision

- **Primary:** E2B (Firecracker microVMs) for all neuron and orchestrator execution.
- **Secondary:** Modal for GPU-heavy or long-batch workloads, if any.
- **Fallback (future):** Northflank Sandboxes for BYO-cloud customers and air-gapped deployments.

> **As built (2026-10):** E2B is the *code-execution* sandbox only. `E2BSandbox.run_code` creates and destroys a microVM per invocation (`src/darwin_platform/adapters/sandbox.py`) for mutation evaluation and code-running neurons; production refuses to boot without `--e2b-api-key` (`src/darwin_platform/__main__.py`). The orchestrator does not run in E2B: under `--driver k8s` it is a per-Darwin pod with a sandboxed RuntimeClass — `gvisor` by default on the Hetzner k3s nodes, `kata` where nodes have nested virtualisation (`infra/terraform/envs/hetzner/main.tf`) — so gVisor, rejected below for untrusted code, is the default boundary for the orchestrator process while generated code still goes to E2B. Neurons run in-process in that pod. Modal and Northflank are not integrated.

## Why microVMs, not containers

Darwin runs untrusted, sometimes self-generated code. The threat model permits a hostile soma. We need kernel-level isolation, not user-space syscall interception.

| Tier | Isolation | Verdict for Darwin |
|---|---|---|
| Linux containers (Docker direct) | Shared host kernel | **Reject.** One container-escape CVE compromises every tenant. |
| gVisor (user-space syscall filter) | Soft kernel boundary | **Reject.** Better than raw containers but explicitly cautioned by its own designers against arbitrary untrusted code in security-critical contexts. |
| Kata Containers | Per-pod microVM | Acceptable if we self-host; operational overhead is meaningful. |
| **Firecracker microVMs** (E2B, AWS Lambda, Fly.io) | Per-VM kernel | **Selected.** Each sandbox has its own kernel; escape requires a hypervisor-level vulnerability. |

## E2B specifics

- **Cold start:** sub-second to a few seconds, well within Darwin's heartbeat tolerance.
- **Languages:** Python, TypeScript, Node, plus arbitrary binaries — we ship our base Ubuntu 24.04 image with the Darwin runtime preinstalled.
- **Lifecycle:** session-scoped. We create a sandbox per orchestrator (long-lived, rotated) and a sandbox per neuron dispatch (ephemeral). Storage is wiped on tear-down except for the per-Darwin soma mount.
- **SDK:** Python and TS clients, simple to drive from LangGraph's `dispatch_neuron` node.
- **Limits:** session max 24 hours in the managed offering. Our orchestrator rotates at a 12-hour cadence regardless to avoid hitting the wall.
- **BYO cloud:** available on enterprise plans; we use the managed service in v1 and migrate when economics demand.

## Modal for the heavy stuff

Modal is the right tool when:
- We need GPUs (A100/H100) — e.g. a future neuron that does on-prem image generation, or a per-Darwin fine-tune (deferred, see stack doc).
- We're running a batch evaluation harness that needs hundreds of parallel mini-sandboxes for a short burst.

We don't use Modal for the day-to-day orchestrator or neuron loops because (a) the isolation tier is gVisor (good but not as strong as Firecracker), and (b) the runtime model (decorator-defined functions) doesn't fit our "long-running orchestrator" shape.

## What the sandboxed environment looks like

A Darwin neuron sandbox boots with:

```
/  (read-write, tmpfs, capped at neuron's quota)
├── /opt/darwin/
│   ├── runtime/          (read-only: our neuron SDK, base libraries)
│   └── credentials/      (read-only mount of scoped vault response)
├── /soma/                (read-only mount of this Darwin's soma, neuron-relevant files only)
├── /dna/                 (read-only mount of DNA core, for validation only)
└── /tmp/work/            (read-write, wiped at tear-down)

Network: only the platform egress proxy is reachable.
Filesystem: no host-share, no /dev access beyond what microVM provides.
```

The orchestrator sandbox is similar but longer-lived and has read-write access to the soma (for self-modification merges).

## Anti-tampering

The DNA mount is read-only at the kernel-mount level. The validator inside the DNA verifies every layer's signature *inside the sandbox* on each boot, so even a sandbox-level mount tampering would be caught. (Belt and braces.)

## Quotas (in sandbox)

Per neuron sandbox (default; adjustable per Darwin per profile):

- CPU: 1 vCPU, soft cap 2
- RAM: 1 GB, hard cap 2 GB
- Disk: 1 GB
- Wall time: 5 minutes default; long-running browser sessions get 30 min; explicit batch tasks get up to 4 hours via a separate sandbox profile
- Network bandwidth: 100 MB/invocation

Per orchestrator sandbox: larger (4 vCPU, 4 GB RAM, 12-hour rotation).

A Darwin that consistently hits these limits is a signal — either the strategy is wrong-shaped, or the limits need adjusting. Either way, the supervisor flags it.

## Cost economics

E2B's pricing model (managed) is roughly $0.06–0.10 per browser-hour and a few cents per CPU-hour. A busy Darwin running 24/7 might cost $20–80/month in sandbox compute. This is a real cost line — we pass it through with margin to the Darwin's treasury, which means a Darwin that can't earn at least its own compute cost gracefully dies. (Which is the point.)

## When we'd switch off E2B

- Volume becomes large enough that self-hosting Firecracker is cheaper. The math depends on average Darwin compute load; estimate is "after a few thousand active Darwins".
- E2B has an extended outage during which we discover we can't tolerate it. Then we add Northflank or self-hosted Kata as a secondary, dual-write our sandbox abstraction, and survive future outages.
- Compliance requires BYO-cloud and E2B's BYOC tier doesn't fit. Then Northflank (which has supported multi-cloud BYOC since inception) becomes attractive.

## Local development

We do not run microVMs locally. Local dev uses plain Docker containers with the same filesystem layout. This is *acceptable for development only* — we run integration tests against real E2B in CI before merging. The platform refuses to start a Darwin against the local-dev sandbox backend in any non-development environment.
