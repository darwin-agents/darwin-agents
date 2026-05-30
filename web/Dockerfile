# syntax=docker/dockerfile:1
# Darwin web dashboard — Next.js 15, standalone output, pnpm.
# Multi-stage: deps → build → minimal runtime (no full node_modules).

FROM node:22-slim AS base
ENV NEXT_TELEMETRY_DISABLED=1
# Pin pnpm 9 (lockfileVersion 9.0); node 22 satisfies the package "node>=20".
RUN npm install -g pnpm@9

# -- deps: install from the lockfile (cached unless the lockfile changes) --
FROM base AS deps
WORKDIR /app
COPY package.json pnpm-lock.yaml ./
RUN pnpm install --frozen-lockfile

# -- build: compile the standalone server bundle ---------------------------
FROM base AS build
WORKDIR /app
COPY --from=deps /app/node_modules ./node_modules
COPY . .
# NEXT_PUBLIC_* values are inlined at BUILD time — pass them as build args.
ARG NEXT_PUBLIC_API_BASE_URL=http://localhost:8000
ARG NEXT_PUBLIC_CLERK_PUBLISHABLE_KEY=
ENV NEXT_PUBLIC_API_BASE_URL=$NEXT_PUBLIC_API_BASE_URL \
    NEXT_PUBLIC_CLERK_PUBLISHABLE_KEY=$NEXT_PUBLIC_CLERK_PUBLISHABLE_KEY
RUN pnpm build

# -- runtime: copy only the standalone server + static assets --------------
FROM base AS runtime
WORKDIR /app
ENV NODE_ENV=production PORT=3000 HOSTNAME=0.0.0.0
RUN useradd -m -u 1001 nextjs
COPY --from=build /app/public ./public
COPY --from=build --chown=nextjs:nextjs /app/.next/standalone ./
COPY --from=build --chown=nextjs:nextjs /app/.next/static ./.next/static
USER nextjs
EXPOSE 3000
# Runtime-only server vars (CLERK_SECRET_KEY, etc.) are injected by the
# orchestrator; NEXT_PUBLIC_* were baked at build time above.
CMD ["node", "server.js"]
