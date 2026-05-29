# LLM routing — self-hosted LiteLLM gateway

## Decision

Use **LiteLLM Proxy** (BerriAI's open-source AI gateway) **self-hosted, from v1**. All Darwin LLM calls — text, embeddings, image generation, video generation, TTS, STT, reranking — route through one platform-operated LiteLLM instance.

We considered OpenRouter and other hosted gateways and rejected them. Rationale below.

## What goes through the gateway

LiteLLM is not just a text-completions proxy. It exposes one OpenAI-compatible interface for every modality Darwin needs:

| Modality         | LiteLLM endpoint           | Example providers behind it                                     |
| ---------------- | -------------------------- | --------------------------------------------------------------- |
| Text completion  | `/v1/chat/completions`     | OpenAI, Anthropic, Gemini, DeepSeek, Llama (Together/Groq), xAI |
| Embeddings       | `/v1/embeddings`           | OpenAI, Voyage, Cohere, Gemini, BGE (self-hosted)               |
| Image generation | `/v1/images/generations`   | DALL-E, Imagen, FLUX (Replicate/fal), Stable Diffusion          |
| Image editing    | `/v1/images/edits`         | DALL-E, FLUX Kontext, gpt-image-1                               |
| Video generation | `/v1/video/generations`    | Sora, Veo, Kling, ModelsLab WAN                                 |
| TTS              | `/v1/audio/speech`         | OpenAI TTS, ElevenLabs, Cartesia                                |
| STT              | `/v1/audio/transcriptions` | Whisper, AssemblyAI, Deepgram                                   |
| Reranking        | `/v1/rerank`               | Cohere, Voyage                                                  |

This is what makes multimodal neurons easy. A `content.video_producer` neuron (when we build one) hits the same proxy, with the same auth, and gets cost-tracked the same way as the `content.writer` neuron. Adding a new video provider in 2027 is a YAML config change on the proxy, not a Darwin-side code change.

## The capability profile abstraction

Per P6 (no provider lock-in), the DNA never references a model name directly. It references a **capability profile**:

| Profile           | Description                                            | Typical models (May 2026)                | Cost band   |
| ----------------- | ------------------------------------------------------ | ---------------------------------------- | ----------- |
| `tier=fast`       | Snap-judgment tasks, classification, routing           | DeepSeek V3, Llama 3.3 70B, Gemini Flash | $           |
| `tier=balanced`   | Day-to-day reasoning, content generation               | Claude Haiku, Gemini Pro, GPT-5.5 mini   | $$          |
| `tier=reasoning`  | Hard planning, strategy selection, mutation evaluation | Claude Sonnet, GPT-5.5, Gemini 2.5 Pro   | $$$         |
| `tier=coding`     | Self-modification — writing patches against the soma   | Claude Opus, GPT-5.5-Codex               | $$$$        |
| `tier=vision`     | Screenshot interpretation when DOM is obscured         | Claude Sonnet (vision), GPT-5.5 vision   | $$$         |
| `tier=embed`      | Embeddings for episodic memory                         | text-embedding-3-small, voyage-3         | $ (trivial) |
| `tier=image_gen`  | Image creation for content                             | FLUX, DALL-E, Imagen                     | $$          |
| `tier=image_edit` | Inpainting, style transfer                             | FLUX Kontext, gpt-image-1                | $$          |
| `tier=video_gen`  | Video creation                                         | Sora, Veo, Kling                         | $$$$        |
| `tier=tts`        | Speech synthesis                                       | OpenAI TTS, ElevenLabs                   | $           |
| `tier=stt`        | Transcription                                          | Whisper, Deepgram                        | $           |

The mapping from profile to model is **a platform-level routing table maintained in LiteLLM's config**, not per-Darwin. When the platform promotes a new cheap-and-smart model into `tier=balanced`, every running Darwin gets the benefit on its next call. No DNA update, no soma migration, no per-Darwin opt-in. This is the single biggest cost lever the platform has.

The Darwin's soma may _additionally_ narrow a request (e.g. "I want extra-long context for this strategy") but cannot widen beyond the tier's allowed model set.

## Why LiteLLM (and why not OpenRouter) for Darwin specifically

Three properties of this platform make hosted gateways the wrong shape:

1. **We meter LLM cost into per-Darwin treasuries.** Every call is an expense entry on a Darwin's ledger, denominated in USDC, charged with margin. We need _per-request cost metadata in the response_. LiteLLM returns this natively in every response (`response.usage.cost`). OpenRouter exposes only aggregate dashboards; per-Darwin attribution would be extra engineering we'd own anyway.

2. **We BYO provider keys at scale.** A platform with hundreds of active Darwins quickly hits volume that justifies direct contracts with Anthropic, OpenAI, Google. OpenRouter's ~5% markup doesn't sound like much until it's $50K/month evaporating because the gateway is in the path. LiteLLM lets us BYO from day one.

3. **One fewer party in the call, identity, and outage chain.** Every Darwin's metabolism depends on LLM access. If OpenRouter has a regional outage, our entire fleet stalls. If our LiteLLM has an outage, it's on us — but it's on us in a way we can debug, page on, and fix.

Other LiteLLM properties we want:

- **Open source (MIT), self-hosted.** No third-party in the call path.
- **OpenAI-compatible.** Drop-in for every Python LLM library and SDK Darwin uses.
- **100+ providers,** including all the multimodal ones above. Adding a provider is one YAML entry.
- **Built-in virtual keys.** One LiteLLM virtual key per Darwin. The Darwin's soma sees a normal OpenAI-shape API; the proxy enforces per-key budgets, rate limits, allowed models, and logs every call against that Darwin's ID.
- **Fallback chains** configured in YAML.
- **Cost tracking in response metadata** → per-Darwin attribution for free.
- **A2A and MCP support** for cross-agent tool use down the line.
- **Backed by YC, ~40K GitHub stars, ~1,300 contributors (March 2026).** Mature, well-maintained.

The trade-off is operational: we run it. That's fine — we're already operating Kubernetes, Postgres, Vault, and a control plane. One more well-understood Docker container is not a meaningful additional burden.

## What we considered and passed on

| Option                               | Verdict                                                                                                                                                                        |
| ------------------------------------ | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| **OpenRouter**                       | Cleanest dev experience, biggest model catalog. Rejected: cloud middleman in every call, per-request attribution requires us to reconstruct, ~5% markup at volume, no BYO-key. |
| **Vercel AI Gateway**                | Strong if we were Vercel-native. Rejected: not enough integration value.                                                                                                       |
| **Cloudflare AI Gateway**            | Good edge observability and caching. Considered as a _complement_ in front of LiteLLM, not a replacement. May add later.                                                       |
| **Direct provider SDKs, no gateway** | Possible. Rejected: re-implements fallback, routing, cost tracking, key management ourselves.                                                                                  |
| **Portkey**                          | Commercial alternative to LiteLLM. Similar features; we prefer the open-source path.                                                                                           |

## Deployment

A single LiteLLM Proxy deployment per environment (dev, staging, prod), backed by Postgres for virtual keys and audit, Redis for rate limits and prompt cache, behind a load balancer.

```
┌────────────────────────────────────────────────┐
│  LiteLLM Proxy (3+ replicas behind a LB)       │
│    - YAML config: model_list, fallbacks, tiers │
│    - Postgres: virtual keys, spend, audit      │
│    - Redis: rate-limit counters, prompt cache  │
│    - Prometheus metrics exporter               │
└────────────────────────────────────────────────┘
       │                          │
       ▼                          ▼
   Provider keys              Per-Darwin virtual keys
   (from platform Vault)      (minted at Darwin spawn,
   - openai                    stored in Darwin's vault)
   - anthropic
   - google
   - replicate
   - elevenlabs
   - ...
```

Each Darwin's virtual key has:

- A budget reconciled hourly against the Darwin's USDC ledger.
- Allowed tiers (default: all; some lockable per platform policy).
- A rate limit (sane defaults; raised per Darwin profile).
- A spend alert threshold (notifies supervisor; doesn't block).
- A hard ceiling (blocks; escalates the Darwin).

When the Darwin's per-key budget runs out, LiteLLM returns 429. The orchestrator interprets this as a treasury check failure and triggers the appropriate response (escalate, or trigger death sequence if treasury is also exhausted).

## Caching

LiteLLM has built-in Redis-backed prompt caching plus support for provider-native caching (Anthropic prompt cache, OpenAI shared-prefix cache). We use both:

- **Provider-native** for the planner's persistent system prompt — constant across heartbeats, hits cache nearly 100% of the time, dramatically reduces per-heartbeat cost.
- **LiteLLM-side Redis cache** for short-window memoization of identical neuron-side prompts (e.g. the research scout asking the same question twice within five minutes).

Cache keys include `darwin_id` to prevent any cross-tenant cache pollution.

## Failover

YAML configures fallback chains per tier:

```yaml
model_list:
  - model_name: tier-reasoning
    litellm_params:
      model: anthropic/claude-sonnet
      api_key: os.environ/CLAUDE_API_KEY
  - model_name: tier-reasoning
    litellm_params:
      model: openai/gpt-5.5
      api_key: os.environ/OPENAI_API_KEY
  - model_name: tier-reasoning
    litellm_params:
      model: gemini/gemini-2.5-pro
      api_key: os.environ/GOOGLE_API_KEY

router_settings:
  routing_strategy: cost-based-routing
  fallbacks:
    - tier-reasoning: [tier-balanced] # degrade rather than fail outright
```

Per-call retry budget is small (one retry). Beyond that the soma decides — usually downgrade tier with a confidence flag rather than block.

## Observability

LiteLLM emits Prometheus metrics out of the box (latency, error rate, token counts per model, cost per virtual key). We scrape into the central metrics store. Every call writes to LiteLLM's Postgres audit log; we mirror to our event log keyed by `darwin_id`.

Cost data flows directly into each Darwin's ledger via a small reconciliation worker reading LiteLLM's per-key spend table and posting ledger entries (`category=expense:llm:<tier>`) every minute and on every heartbeat checkpoint.

## How Darwin pays for its consumption

The platform pays providers (in fiat, or USDC where supported). The Darwin pays the platform's recipient address in USDC for whatever its virtual key consumed in the last reconciliation window. This internal Darwin-to-platform settlement uses x402 — see [`17-payments-providers.md`](17-payments-providers.md). The Darwin's wallet never holds provider API keys; it pays the platform, and the platform pays the providers.

## When we'd reconsider

- A breakthrough hosted gateway provides meaningfully better capabilities our self-hosted LiteLLM cannot match (e.g. globally distributed inference at edge with sub-50ms latency).
- LiteLLM stops being actively maintained (unlikely — YC-backed, growing fast).
- Per-Darwin volume is so low that operating the proxy exceeds the saved markup. Unlikely with any meaningful fleet.
