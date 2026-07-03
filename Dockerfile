# zdav — single container running all three long-lived services:
#   1. rclone serve webdav   (:8080  — the WebDAV endpoint Zotero syncs into)
#   2. zdav-worker           (the ingest loop: R2 -> local Docling -> LanceDB)
#   3. zdav MCP server        (:8000  — remote streamable-http search, at /mcp)
FROM python:3.11-slim

# --- system deps -------------------------------------------------------------
#   libgl1 / libglib2.0-0  -> opencv, pulled in by docling
#   tini                   -> proper PID 1 (reaps zombies, forwards signals)
#   curl/unzip             -> fetch the rclone binary
RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates curl unzip tini libgl1 libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*

# --- rclone (official static binary, arch-matched) ---------------------------
RUN ARCH="$(dpkg --print-architecture)" \
    && curl -fsSL "https://downloads.rclone.org/rclone-current-linux-${ARCH}.zip" -o /tmp/rclone.zip \
    && unzip -q /tmp/rclone.zip -d /tmp \
    && install -m 0755 /tmp/rclone-*-linux-${ARCH}/rclone /usr/local/bin/rclone \
    && rm -rf /tmp/rclone*

# --- uv (fast resolver/installer) --------------------------------------------
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

# CPU-only torch — sentence-transformers/docling otherwise pull the multi-GB
# CUDA build. Honored by `uv sync`.
ENV UV_TORCH_BACKEND=cpu

# Model caches (bge embeddings + docling layout/table/OCR models) live here.
# Declared a VOLUME so first-run downloads (~1 GB) persist across restarts.
ENV HF_HOME=/cache/hf \
    TORCH_HOME=/cache/torch \
    EASYOCR_MODULE_PATH=/cache/easyocr
VOLUME ["/cache"]

WORKDIR /app
COPY pyproject.toml ./
COPY zdav_core.py zdav_worker.py zdav_mcp.py ./

# Resolve + install into /app/.venv, including the project's console scripts.
RUN uv sync --no-dev

# Put the venv on PATH so `zdav-worker` / `python` resolve to it (and so the
# docling child subprocess uses the same interpreter that has docling).
ENV PATH="/app/.venv/bin:$PATH"

COPY docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh
RUN chmod +x /usr/local/bin/docker-entrypoint.sh

EXPOSE 8080 8000
ENTRYPOINT ["/usr/bin/tini", "--", "/usr/local/bin/docker-entrypoint.sh"]
