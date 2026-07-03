"""
zdav_mcp.py — SEARCH side (read). An MCP server exposing semantic + full-text
search over the section-ToC index the worker built. Read-only: it never writes
to R2 or the table. Runs as a remote HTTP MCP server (streamable-http), so a
client like Claude Desktop can connect over the network instead of via stdio.

Tools exposed
-------------
  semantic_search(query, k, level)   — vector similarity over section paths
  fulltext_search(query, k, level)   — BM25/FTS over section paths
  hybrid_search(query, k, level)     — both, fused (default, recommended)
  list_document(zotero_key)          — the full section outline of one doc
  get_document_text(zotero_key)      — the full markdown text of one doc

The search tools return section headers, each tagged with the zotero_key of the
paper it came from, its level, and its breadcrumb path — a semantic table of
contents across the whole library. Once a relevant section is found, fetch the
paper's full text with get_document_text to actually read it.

Run:  uv run zdav_mcp.py            (remote HTTP server at http://HOST:PORT/mcp)
Env:  same ZDAV_STORE/EMBED_ vars as the worker (via zdav_core). No docling needed.
      MCP_TRANSPORT (default streamable-http; set 'stdio' for a local client)
      MCP_HOST (default 0.0.0.0)  MCP_PORT (default 8000)
      MCP_API_KEY (optional; if set, every HTTP request must present it)
pip:  mcp  (plus zdav_core's deps: boto3 lancedb sentence-transformers pyarrow)

Security: set MCP_API_KEY to require a shared key on every request. Send it as
'Authorization: Bearer <key>', 'X-API-Key: <key>', or — for clients that only
take a plain URL — as a query param: http://HOST:PORT/mcp?key=<key>. The URL
param is the least secure (query strings leak into logs, proxies, and browser
history), so prefer a header where you can. Without any key the HTTP transport
is UNAUTHENTICATED and exposes read access to the whole library. Either way, use
TLS (a proxy/tunnel) before exposing it beyond localhost.
"""
from __future__ import annotations
import hmac, os
from urllib.parse import parse_qs

from mcp.server.fastmcp import FastMCP

from zdav_core import get_table, embed, store, STORE, DERIVED, MODEL_ID

MCP_TRANSPORT = os.environ.get("MCP_TRANSPORT", "streamable-http")
MCP_HOST      = os.environ.get("MCP_HOST", "0.0.0.0")
MCP_PORT      = int(os.environ.get("MCP_PORT", "8000"))
MCP_API_KEY   = os.environ.get("MCP_API_KEY")   # if set, required on every request

mcp = FastMCP("zdav-search", host=MCP_HOST, port=MCP_PORT)

# Reader never creates the table — fail loudly if the worker hasn't run.
def _table():
    return get_table(create=False)

def _level_filter(level: int | None) -> str | None:
    return None if level is None else f"level = {int(level)}"

def _rows(results) -> list[dict]:
    """Trim LanceDB rows to the fields a caller cares about (drop the vector)."""
    out = []
    for r in results:
        out.append({
            "zotero_key": r["zotero_key"],
            "section_path": r["section_path"],
            "header": r["header"],
            "level": r["level"],
            "score": r.get("_relevance_score") or r.get("_distance"),
        })
    return out

@mcp.tool()
def semantic_search(query: str, k: int = 10, level: int | None = None) -> list[dict]:
    """
    Semantic (vector) search over section headings across the whole library.
    Finds sections whose meaning matches the query even when wording differs
    (e.g. 'subgroup heterogeneity' matches 'Stratified analyses'). Returns a
    list of matching sections, each with zotero_key, section_path, header, and
    level. Optionally restrict to a heading level (1 = top-level, 2 = subsection).
    """
    tbl = _table()
    q = tbl.search(embed(query))
    if (f := _level_filter(level)): q = q.where(f)
    return _rows(q.limit(k).to_list())

@mcp.tool()
def fulltext_search(query: str, k: int = 10, level: int | None = None) -> list[dict]:
    """
    Full-text (keyword/BM25) search over section headings. Best for exact terms,
    acronyms, or specific phrases that must appear literally in the heading path.
    Returns matching sections with zotero_key, section_path, header, and level.
    Optionally restrict to a heading level.
    """
    tbl = _table()
    q = tbl.search(query, query_type="fts")
    if (f := _level_filter(level)): q = q.where(f)
    return _rows(q.limit(k).to_list())

@mcp.tool()
def hybrid_search(query: str, k: int = 10, level: int | None = None) -> list[dict]:
    """
    Hybrid search: combines semantic (vector) and full-text (BM25) ranking for
    the best of both — semantic recall plus exact-term precision. This is the
    recommended default for most section-discovery queries. Returns matching
    sections with zotero_key, section_path, header, and level. Optionally
    restrict to a heading level.
    """
    tbl = _table()
    q = tbl.search(query, query_type="hybrid").vector(embed(query))
    if (f := _level_filter(level)): q = q.where(f)
    return _rows(q.limit(k).to_list())

@mcp.tool()
def list_document(zotero_key: str) -> list[dict]:
    """
    Return the full section outline (table of contents) for a single document,
    given its Zotero item key. Sections come back ordered as they appear in the
    paper, each with its level, header, and breadcrumb section_path.
    """
    tbl = _table()
    rows = tbl.search().where(f"zotero_key = '{zotero_key}'").limit(10000).to_list()
    rows.sort(key=lambda r: r["chunk_ix"])
    return [{"level": r["level"], "header": r["header"],
             "section_path": r["section_path"]} for r in rows]

@mcp.tool()
def get_document_text(zotero_key: str) -> str:
    """
    Return the FULL markdown text of a single document, given its Zotero item
    key. Use this after a search surfaces a relevant section to actually read
    the paper's content (the search tools only return section headings, not
    body text). The worker keeps exactly one current markdown per document, so
    this returns the latest ingested version.
    """
    mds = [k for k, _ in store.list(f"{DERIVED}{zotero_key}/") if k.endswith(".md")]
    if not mds:
        raise ValueError(
            f"no ingested text for zotero_key '{zotero_key}' — has the worker "
            f"processed it yet?")
    return store.get(sorted(mds)[-1]).decode("utf-8")

class ApiKeyMiddleware:
    """Reject requests that don't present the shared key. Accepts it three ways,
    in order of preference:
        Authorization: Bearer <key>   (best)
        X-API-Key: <key>
        ?key=<key>  or  ?api_key=<key>   (URL param — convenient but weaker:
            query strings land in access logs, proxies, and browser history)
    Pure ASGI so it wraps the FastMCP app with no extra machinery. Constant-time
    compare avoids leaking the key via response timing."""
    def __init__(self, app, api_key: str):
        self.app, self.api_key = app, api_key

    def _token(self, scope) -> str:
        h = dict(scope.get("headers", []))
        auth = h.get(b"authorization", b"").decode()
        if auth[:7].lower() == "bearer ":
            return auth[7:]
        if b"x-api-key" in h:
            return h[b"x-api-key"].decode()
        qs = parse_qs(scope.get("query_string", b"").decode())
        return (qs.get("key") or qs.get("api_key") or [""])[0]

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            if not hmac.compare_digest(self._token(scope), self.api_key):
                from starlette.responses import PlainTextResponse
                await PlainTextResponse("unauthorized", status_code=401)(scope, receive, send)
                return
        await self.app(scope, receive, send)

if __name__ == "__main__":
    import sys
    if MCP_TRANSPORT == "stdio":
        print(f"zdav-search MCP (stdio) — {STORE} model={MODEL_ID}", file=sys.stderr)
        mcp.run(transport="stdio")
    else:
        import uvicorn
        app = mcp.streamable_http_app()
        if MCP_API_KEY:
            app.add_middleware(ApiKeyMiddleware, api_key=MCP_API_KEY)
            auth = "api-key required"
        else:
            auth = "NO AUTH — set MCP_API_KEY to require a key"
        print(f"zdav-search MCP (streamable-http, {auth}) on "
              f"http://{MCP_HOST}:{MCP_PORT}/mcp — {STORE} model={MODEL_ID}",
              file=sys.stderr)
        uvicorn.run(app, host=MCP_HOST, port=MCP_PORT)
