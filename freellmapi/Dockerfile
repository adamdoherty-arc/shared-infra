# shared-freellmapi — self-hosted OpenAI-compatible proxy that aggregates
# free-tier keys from ~14 LLM providers (Google Gemini, Groq, Cerebras,
# SambaNova, Mistral, OpenRouter, GitHub Models, Cloudflare, Cohere,
# Z.ai/Zhipu, NVIDIA, Ollama Cloud) behind a single /v1/chat/completions
# endpoint with priority-ordered fallback chain + per-key RPM/RPD/TPM/TPD
# rate-limit tracking.
#
# Upstream: https://github.com/tashfeenahmed/freellmapi (MIT, ~2.6k stars).
# No upstream Dockerfile ships — this is our reproducible build.
#
# Pinned commit:
#   18eb04be990bcdaca842574bf6e00a6968308761  (main HEAD as of 2026-05-20)
# Bump deliberately + redeploy when upstream lands fixes; don't float to main.
#
# Build deps required by better-sqlite3 (native module) at runtime arch.
# We use bookworm-slim (glibc) not alpine (musl) so prebuilt better-sqlite3
# binaries resolve cleanly.

FROM node:20-bookworm-slim AS builder

ARG FREELLMAPI_COMMIT=18eb04be990bcdaca842574bf6e00a6968308761

RUN apt-get update \
 && apt-get install -y --no-install-recommends \
        git ca-certificates python3 make g++ \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /build

# Pinned shallow clone — Dockerfile rebuild is deterministic for a given SHA.
RUN git clone https://github.com/tashfeenahmed/freellmapi.git . \
 && git checkout "${FREELLMAPI_COMMIT}"

# Workspace install (root package.json declares shared/server/client workspaces).
# better-sqlite3 compiles natively here; prebuilt binaries are picked up when
# the runtime arch matches (linux/amd64 on Docker Desktop / amd64 hosts).
RUN npm ci --no-audit --no-fund || npm install --no-audit --no-fund

# Builds both server (dist/index.js) and client (Vite static bundle).
RUN npm run build

# Trim devDependencies after the build to shrink the runtime layer.
RUN npm prune --omit=dev


FROM node:20-bookworm-slim AS runtime

# tini = PID 1; sqlite3 = optional CLI for debugging the DB inside the
# container.
RUN apt-get update \
 && apt-get install -y --no-install-recommends tini sqlite3 \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Bring over only the runtime artifacts. The monorepo layout means we keep
# the workspace tree intact so node's workspace-aware resolution still works.
COPY --from=builder /build/package.json        /app/package.json
COPY --from=builder /build/node_modules        /app/node_modules
COPY --from=builder /build/shared              /app/shared
COPY --from=builder /build/server/package.json /app/server/package.json
COPY --from=builder /build/server/dist         /app/server/dist
COPY --from=builder /build/server/node_modules /app/server/node_modules
COPY --from=builder /build/client/package.json /app/client/package.json
COPY --from=builder /build/client/dist         /app/client/dist

# Non-root runtime user.
RUN groupadd -r freellm && useradd -r -g freellm -d /app -s /usr/sbin/nologin freellm \
 && mkdir -p /data && chown -R freellm:freellm /data /app

USER freellm

# SQLite database path — bind-mount /data via docker-compose for persistence.
ENV DATABASE_PATH=/data/freellmapi.db \
    PORT=3001 \
    NODE_ENV=production \
    HOST=0.0.0.0

EXPOSE 3001

# Lightweight liveness probe — / serves the SPA, /v1/models requires keys.
HEALTHCHECK --interval=60s --timeout=10s --start-period=20s --retries=3 \
  CMD node -e "require('http').get('http://127.0.0.1:3001/', r => process.exit(r.statusCode >= 200 && r.statusCode < 500 ? 0 : 1)).on('error', () => process.exit(1))"

ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["node", "server/dist/index.js"]
