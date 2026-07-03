#!/usr/bin/env bash
# Start the three long-lived services side by side: rclone's WebDAV endpoint,
# the ingest worker, and the remote MCP search server. If ANY of them exits,
# tear the whole container down (so the orchestrator restarts it) rather than
# limping along with part of the stack dead.
set -euo pipefail

: "${ZDAV_STORE:?set ZDAV_STORE (s3://bucket or a local path)}"
: "${WEBDAV_PASS:?set WEBDAV_PASS}"

WEBDAV_USER="${WEBDAV_USER:-zotero}"
WEBDAV_ADDR="${WEBDAV_ADDR:-:8080}"

# rclone serves attachments/ off the SAME store the worker reads, driven by
# ZDAV_STORE. --etag-hash MD5 keeps rclone's etags consistent with both backends
# (R2's native MD5 etag and the local store's content MD5).
extra_flags=(--etag-hash MD5)
case "$ZDAV_STORE" in
  s3://*)
    : "${R2_ACCOUNT_ID:?set R2_ACCOUNT_ID}"
    : "${R2_ACCESS_KEY_ID:?set R2_ACCESS_KEY_ID}"
    : "${R2_SECRET_ACCESS_KEY:?set R2_SECRET_ACCESS_KEY}"
    bucket="${ZDAV_STORE#s3://}"
    # Configure the rclone 'r2' remote from the SAME creds the worker uses.
    export RCLONE_CONFIG_R2_TYPE=s3
    export RCLONE_CONFIG_R2_PROVIDER=Cloudflare
    export RCLONE_CONFIG_R2_ACCESS_KEY_ID="$R2_ACCESS_KEY_ID"
    export RCLONE_CONFIG_R2_SECRET_ACCESS_KEY="$R2_SECRET_ACCESS_KEY"
    export RCLONE_CONFIG_R2_ENDPOINT="https://${R2_ACCOUNT_ID}.r2.cloudflarestorage.com"
    export RCLONE_CONFIG_R2_REGION=auto
    webdav_target="r2:${bucket}/attachments"
    extra_flags+=(--vfs-cache-mode writes --s3-no-check-bucket)
    ;;
  *)
    root="${ZDAV_STORE#file://}"          # local filesystem store
    webdav_target="${root}/attachments"
    mkdir -p "$webdav_target"
    ;;
esac

echo "[entrypoint] starting rclone webdav on ${WEBDAV_ADDR} (${webdav_target})"
rclone serve webdav "$webdav_target" \
    --addr "${WEBDAV_ADDR}" --user "${WEBDAV_USER}" --pass "${WEBDAV_PASS}" \
    "${extra_flags[@]}" &
rclone_pid=$!

echo "[entrypoint] starting zdav-worker"
zdav-worker &
worker_pid=$!

echo "[entrypoint] starting zdav MCP server (${MCP_TRANSPORT:-streamable-http}) on ${MCP_PORT:-8000}"
python /app/zdav_mcp.py &
mcp_pid=$!

# Wait for whichever dies first, then bring the others down and exit non-zero.
wait -n "$rclone_pid" "$worker_pid" "$mcp_pid"
echo "[entrypoint] a service exited — shutting down the container"
kill "$rclone_pid" "$worker_pid" "$mcp_pid" 2>/dev/null || true
wait
exit 1
