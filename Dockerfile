# =============================================================================
# Dockerfile — k8s-troubleshoot-mcp
# =============================================================================
# Multi-stage build (REQ-060). Builder installs deps with uv; runtime is a clean
# slim image with only the venv and source. No uv, no build tools, no apt cache
# in the final image. Structure follows the ambient-weather-mcp precedent.
#
# NO CREDENTIALS ARE BAKED IN. The image contains code only. The server refuses
# to start without KUBECONFIG (REQ-001) and never falls back to ~/.kube/config
# (REQ-003), so an image run without a mounted kubeconfig fails closed rather
# than silently picking up an ambient credential.
#
# BUILD:
#   docker build -t k8s-troubleshoot-mcp .
#
# RUN (stdio — the -i flag is required; JSON-RPC travels on stdin/stdout):
#   docker run -i --rm \
#     -v /path/to/kubeconfig.yaml:/kubeconfig:ro \
#     -e KUBECONFIG=/kubeconfig \
#     -e ALLOWED_NAMESPACES=staging,production \
#     k8s-troubleshoot-mcp
#
# The :ro flag is not decoration. This server performs no writes of any kind,
# so a writable mount grants privilege it has no use for. Note that the
# kubeconfig must be READABLE BY UID 10001 — a file created with mode 600 and
# owned by your host user is not, and the container will fail with a permission
# error rather than a confusing Kubernetes error. Either grant group/other read
# on the file, or run with `--user "$(id -u)"` if your host UID owns it.
#
# For a cluster reachable only on the host's loopback interface (minikube,
# kind), the container additionally needs `--network host` on Linux, or a
# kubeconfig whose server points at host.docker.internal on Docker Desktop.
# =============================================================================

# ---------- Stage 1: builder ----------
FROM python:3.11-slim AS builder

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1

# uv from the official image — only ever exists in the builder stage
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

WORKDIR /app

# Dependency + metadata layer (cached unless these files change).
# README.md is included because pyproject.toml's readme field references it and
# hatchling validates that during the build.
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-dev --no-install-project

# Source layer
COPY src/ ./src/
RUN uv sync --frozen --no-dev

# ---------- Stage 2: runtime ----------
FROM python:3.11-slim AS runtime

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH="/app/.venv/bin:$PATH"

# OCI labels (REQ-067)
LABEL org.opencontainers.image.title="k8s-troubleshoot-mcp" \
      org.opencontainers.image.description="Read-only MCP server for Kubernetes cluster diagnostics" \
      org.opencontainers.image.source="https://github.com/NanaGyamfiPrempeh30/k8s-troubleshoot-mcp" \
      org.opencontainers.image.documentation="https://github.com/NanaGyamfiPrempeh30/k8s-troubleshoot-mcp#readme" \
      org.opencontainers.image.licenses="MIT" \
      org.opencontainers.image.authors="Yaw Nana Gyamfi Prempeh"

# MCP Registry ownership verification. The registry checks this annotation
# against the `name` field in server.json and rejects the publish if they
# differ — it is how the registry proves the image and the listing come from
# the same owner, so the two must be edited together.
#
# The comparison is a plain `mcpName != serverName` string equality, so the
# CASING here is load-bearing: it must match server.json byte-for-byte, and
# both must carry the GitHub login's canonical casing (NanaGyamfiPrempeh30).
LABEL io.modelcontextprotocol.server.name="io.github.NanaGyamfiPrempeh30/k8s-troubleshoot-mcp"

# Non-root user with fixed UID (REQ-060), satisfying Kubernetes
# runAsNonRoot / runAsUser policies without the cluster having to guess.
RUN useradd --create-home --uid 10001 appuser

WORKDIR /app

# Copy venv and source from builder, owned by appuser
COPY --from=builder --chown=appuser:appuser /app/.venv /app/.venv
COPY --from=builder --chown=appuser:appuser /app/src /app/src

USER appuser

# stdio MCP — JSON-RPC on stdout, logs to stderr (REQ-010).
# No HEALTHCHECK: stdio servers are per-session and short-lived, and a health
# probe writing to stdout would corrupt the protocol stream.
ENTRYPOINT ["python", "-m", "k8s_troubleshoot_mcp"]
CMD []
