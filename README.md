<p align="center">
  <img src="Bucket%20-%20Logo.svg" alt="lit-bucket logo" width="140" height="140">
</p>

<h1 align="center">Lit Bucket</h1>

<p align="center">
  <em>Infinitely scalable research library storage & search for AI agents, backed by S3.</em>
</p>

<p align="center">
  <a href="LICENSE"><img alt="License: Apache 2.0" src="https://img.shields.io/badge/License-Apache_2.0-blue.svg"></a>
  <img alt="Python 3.11+" src="https://img.shields.io/badge/python-3.11%2B-blue.svg">
</p>

---

**Lit Bucket** turns the papers in your Zotero library into a searchable knowledge base for AI assistants. It watches the attachments Zotero syncs over WebDAV, parses each PDF to Markdown **on-device** with [Docling](https://github.com/DS4SD/docling), and builds a section-level table-of-contents index (vector + full-text) in [LanceDB](https://lancedb.com/). A remote **MCP server** then lets a client like Claude search that index and pull the full text of any paper.

Everything runs in **one Docker container**, backed by either **Cloudflare R2** or a **local filesystem**. 

> [!NOTE]
> If you're university-affiliated / non-commercial and looking for a turnkey free hosted storage & search solution, submit your email [here](https://litbucket.dev), and I can provision you a reasonable amount of storage on my homelab infrastructure. If you're looking to self-host something more scalable yourself, just reach out to help@litbucket.dev (this is just an alias to my personal email) and happy to help walk you through it.

## How it works

```
                 ┌──────────────────────── one container ──────────────────────────┐
   Zotero ──WebDAV──▶  rclone (:8080)                                              │
                 │        │  writes .zip/.prop                                     │
                 │        ▼                                                        │
                 │   ┌─────────┐   poll    ┌───────────────┐   embed   ┌────────┐  │
                 │   │  STORE  │◀──────────│ ingest worker │──────────▶│ LanceDB│  │
                 │   │ (R2 or  │  PDF ───▶ │  Docling → md │  upsert   │  index │  │
                 │   │  local) │           └───────────────┘           └────────┘  │
                 │   └─────────┘                                            ▲      │
                 │        ▲                                                 │read  │
   MCP client ──HTTP──▶ MCP server (:8000) ─────────────────────────────────┘      │
   (Claude, …)   │      semantic / fulltext / hybrid search + full-text fetch      │
                 └─────────────────────────────────────────────────────────────────┘
```

1. **rclone** serves a WebDAV endpoint that Zotero syncs its file attachments into (`attachments/<key>.zip`).
2. The **ingest worker** (single writer) polls the store, and for each new/changed `.zip`: unzips the PDF → parses it with Docling in an isolated subprocess → writes `derived/<key>/<etag>.md` → embeds each section heading's breadcrumb path → upserts rows into LanceDB. State is an auditable JSON manifest at `_ingest/manifest.json`.
3. The **MCP server** (read-only) exposes search + retrieval tools over the index.

The index is a *semantic table of contents*: one row per section heading, embedding the breadcrumb path (e.g. `Results > Subgroup analysis`). Search finds relevant sections across the whole library; `get_document_text` then returns the full Markdown of a paper to read.

## Quick start (Docker)

```bash
git clone https://github.com/ElliotRoe/lit-bucket.git
cd lit-bucket
cp .env.example .env      # then edit — see Configuration below
docker compose up --build -d
docker compose logs -f
```

First run downloads ~1 GB of models (embeddings + Docling) into a persistent volume. Give the container **≥4 GB RAM** — Docling + torch parsing is the heavy part. When healthy the logs show all three services starting:

```
[entrypoint] starting rclone webdav on :8080 ...
zdav worker on <store> docling=local ...
zdav-search MCP (streamable-http, ...) on http://0.0.0.0:8000/mcp ...
```

## Storage: R2 or local

Pick a backend with a single variable, `ZDAV_STORE`:

| `ZDAV_STORE`          | Backend            | Notes                                                        |
| --------------------- | ------------------ | ------------------------------------------------------------ |
| `s3://my-literature`  | Cloudflare R2      | Needs the `R2_*` credentials.                                |
| `/data/lit`           | Local filesystem   | Kept as plain files on the host via a bind mount (`LIT_DATA_DIR`). |

Both hold everything — `attachments/`, `derived/`, `lancedb/`, and the manifest. In local mode, that's browsable on disk:

```
$LIT_DATA_DIR/
├── attachments/          <key>.zip / <key>.prop   (synced by Zotero)
├── derived/<key>/<etag>.md                          (parsed markdown)
├── lancedb/                                          (vector + FTS index)
└── _ingest/manifest.json
```

## Configuration

Set these in `.env` (see [`.env.example`](.env.example)):

| Variable               | Required            | Default                    | Description                                                        |
| ---------------------- | ------------------- | -------------------------- | ------------------------------------------------------------------ |
| `ZDAV_STORE`           | **yes**             | —                          | `s3://bucket` or a local path.                                     |
| `R2_ACCOUNT_ID`        | if `s3://`          | —                          | Cloudflare account ID.                                            |
| `R2_ACCESS_KEY_ID`     | if `s3://`          | —                          | R2 access key.                                                    |
| `R2_SECRET_ACCESS_KEY` | if `s3://`          | —                          | R2 secret key.                                                    |
| `LIT_DATA_DIR`         | local mode          | `./lit-data`               | Host directory bind-mounted to the container's local store.       |
| `WEBDAV_USER`          | no                  | `zotero`                   | WebDAV username Zotero connects with.                             |
| `WEBDAV_PASS`          | **yes**             | —                          | WebDAV password.                                                  |
| `MCP_TRANSPORT`        | no                  | `streamable-http`          | `stdio` for a local (non-remote) client.                         |
| `MCP_HOST` / `MCP_PORT`| no                  | `0.0.0.0` / `8000`         | MCP server bind address.                                         |
| `MCP_API_KEY`          | no (recommended)    | —                          | If set, every MCP request must present it (see [Security](#security)). |
| `EMBED_MODEL`          | no                  | `BAAI/bge-small-en-v1.5`   | sentence-transformers model (used at index and query time).       |
| `POLL_INTERVAL`        | no                  | `60`                       | Seconds between store polls.                                      |
| `DOCLING_TIMEOUT`      | no                  | `900`                      | Seconds before a PDF parse is abandoned.                         |

## Connect Zotero

Zotero → **Settings → Sync → File Syncing → WebDAV**:

- **URL:** `http://<host>:8080/`
- **User / Password:** `WEBDAV_USER` / `WEBDAV_PASS`

Click *Verify Server*, then sync. New attachments are picked up within `POLL_INTERVAL`.

## Connect an MCP client

The search server speaks **streamable-HTTP** at `http://<host>:8000/mcp` (note the `/mcp` path). Add it as a custom/remote MCP server in your client. If `MCP_API_KEY` is set, present it as any of:

- `Authorization: Bearer <key>` (best)
- `X-API-Key: <key>`
- `?key=<key>` in the URL — `http://<host>:8000/mcp?key=<key>` — for clients that only accept a plain URL (weakest; the key leaks into logs/proxies/history).

### Claude.ai web (custom connector)

To reach the server from Claude.ai on the public internet, expose **only** the MCP port with a TLS tunnel — [Tailscale](https://tailscale.com/kb/1223/funnel) Funnel is the easiest:

```bash
tailscale funnel --bg --https=443 8000     # public HTTPS 443 -> container :8000
tailscale funnel status                    # prints https://<host>.<tailnet>.ts.net
```

Then in **Claude → Settings → Connectors → Add custom connector**, use the full URL **including `/mcp` and the key**:

```
https://<host>.<tailnet>.ts.net/mcp?key=<MCP_API_KEY>
```

Gotchas we hit, so you don't:

- **The `/mcp` path is required.** Pointing at the bare host makes Claude POST to `/`, which 404s (`POST / … 404` in the logs). Add `/mcp` before the `?`.
- **`404` vs `401`:** a `404` means the URL/path is wrong; a `401` means the key is wrong. A working connection logs `POST /mcp … 200`.
- **OAuth caveat:** Claude.ai's web connector is built around the MCP **OAuth 2.1** flow for private data, so depending on the version it may attempt OAuth discovery (`GET /.well-known/oauth-protected-resource`) and refuse the plain URL-key approach. The `?key=` method is a pragmatic stopgap; the durable fix is real OAuth (e.g. a DCR-capable IdP like WorkOS AuthKit, or a Cloudflare Access MCP portal). See the [MCP authorization spec](https://modelcontextprotocol.io/specification/2025-06-18/basic/authorization).
- **Don't Funnel the WebDAV port (8080).** Keep Zotero sync on your LAN/tailnet; only the MCP port needs to be public.

### Tools

| Tool                              | Purpose                                                              |
| --------------------------------- | ------------------------------------------------------------------- |
| `semantic_search(query, k, level)`| Vector similarity over section headings — meaning, not exact words. |
| `fulltext_search(query, k, level)`| BM25/FTS over section headings — exact terms, acronyms.             |
| `hybrid_search(query, k, level)`  | Both, fused. The recommended default.                              |
| `list_document(zotero_key)`       | The full section outline (table of contents) of one paper.          |
| `get_document_text(zotero_key)`   | The full Markdown text of one paper — call this to actually read it.|

## Security

- The MCP HTTP transport is **unauthenticated unless you set `MCP_API_KEY`.** Without it, anyone who can reach `:8000` gets read access to your whole library.
- The key is only as private as the channel it rides on — especially via the `?key=` URL param, which lands in logs and history. Always put **TLS** in front before exposing the server beyond localhost/LAN (a tunnel like Tailscale/`cloudflared`, or a reverse proxy). The server is **read-only**, which caps the blast radius, but treat the key as low-value and rotate it freely.
- The worker is the **single writer**; the MCP server never writes. Re-ingest is idempotent (etag-keyed), so duplicate or out-of-order events can't corrupt the index.

## Running without Docker

The three entry points are plain Python (managed with [uv](https://docs.astral.sh/uv/)):

```bash
uv sync
# ingest loop:
ZDAV_STORE=/data/lit WEBDAV_PASS=… uv run zdav-worker
# MCP server:
ZDAV_STORE=/data/lit uv run zdav_mcp.py
# rclone WebDAV (separately): rclone serve webdav /data/lit/attachments --addr :8080 ...
```

`zdav_core.py` is the shared source of truth (config, storage backend, schema, embeddings) imported by both the worker (write) and the MCP server (read), so they can never drift.

## License

[Apache License 2.0](LICENSE) © 2026 Elliot Roe.
