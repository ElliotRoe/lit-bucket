"""
zdav_worker.py — INGEST side (write). Polls R2, parses via Docling, upserts a
section-ToC index into the shared LanceDB table. Search lives elsewhere now
(zdav_mcp.py). This process is the SINGLE WRITER.

Flow
----
1. rclone serves WebDAV to Zotero, backed by the same store. Zotero drops
   <key>.zip / <key>.prop into attachments/.
       rclone serve webdav :8080 --user zotero --pass PW r2:BUCKET/attachments
2. Poll the store; for each new/changed .zip:
       unzip -> PDF -> local Docling -> markdown -> derived/<key>/<etag>.md
       -> embed each header's section_path -> upsert into LanceDB.
3. State is an AUDITABLE JSON MANIFEST at _ingest/manifest.json — a fast-path
   cache, not the source of truth (on a hit we still confirm the .md exists).

Parsing is done on-device with the Docling Python package, run in an isolated
subprocess so a parser crash can't take the poll loop down with it.

Env (in addition to zdav_core's ZDAV_STORE/EMBED vars):
      DOCLING_TIMEOUT (default 900)  POLL_INTERVAL (default 60)
pip:  boto3 lancedb sentence-transformers docling pyarrow
"""
from __future__ import annotations
import io, os, re, json, sys, time, tempfile, zipfile, subprocess, traceback
from datetime import datetime, timezone

from zdav_core import (
    store, STORE, DERIVED, ATTACH, MANIFEST_KEY, MODEL_ID, DIM,
    get_table, embed,
)

POLL            = int(os.environ.get("POLL_INTERVAL", "60"))
DOCLING_TIMEOUT = int(os.environ.get("DOCLING_TIMEOUT", "900"))

# ---------------------------------------------------------------- manifest
def load_manifest() -> dict:
    try: return json.loads(store.get(MANIFEST_KEY))
    except FileNotFoundError: return {}

def save_manifest(m: dict):
    store.put(MANIFEST_KEY, json.dumps(m, indent=2, sort_keys=True).encode(),
              "application/json")

# ---------------------------------------------------------------- docling
# Run in a child interpreter so a native parser crash (segfault/OOM) surfaces as
# a non-zero exit we can catch, instead of taking the poll loop down with it.
DOCLING_CHILD = r"""
import faulthandler
import sys

faulthandler.enable()

from docling.document_converter import DocumentConverter

result = DocumentConverter().convert(sys.argv[1])
md = result.document.export_to_markdown()
if not md:
    raise RuntimeError("docling returned empty markdown")
with open(sys.argv[2], "w", encoding="utf-8") as f:
    f.write(md)
"""

def pdf_from_zip(zb):
    with zipfile.ZipFile(io.BytesIO(zb)) as z:
        for n in z.namelist():
            if n.lower().endswith(".pdf"): return z.read(n)
    return None

def docling_markdown(pdf_bytes: bytes, filename: str) -> str:
    """Convert PDF bytes to markdown with local Docling in an isolated process."""
    suffix = os.path.splitext(filename)[1] or ".pdf"
    with tempfile.NamedTemporaryFile(suffix=suffix) as pdf_f, \
            tempfile.NamedTemporaryFile(suffix=".md") as md_f:
        pdf_f.write(pdf_bytes)
        pdf_f.flush()

        env = os.environ.copy()
        env.setdefault("OMP_NUM_THREADS", "1")
        env.setdefault("OPENBLAS_NUM_THREADS", "1")
        env.setdefault("MKL_NUM_THREADS", "1")
        env.setdefault("NUMEXPR_NUM_THREADS", "1")
        env.setdefault("TOKENIZERS_PARALLELISM", "false")
        try:
            result = subprocess.run(
                [sys.executable, "-c", DOCLING_CHILD, pdf_f.name, md_f.name],
                check=False,
                capture_output=True,
                env=env,
                text=True,
                timeout=DOCLING_TIMEOUT,
            )
        except subprocess.TimeoutExpired as ex:
            raise RuntimeError(
                f"docling timed out for {filename} after {DOCLING_TIMEOUT}s"
            ) from ex

        if result.returncode:
            detail = (result.stderr or result.stdout or "").strip()[-4000:]
            raise RuntimeError(
                f"docling failed for {filename} with exit {result.returncode}\n"
                f"{detail}"
            )

        md_f.seek(0)
        md = md_f.read().decode("utf-8")
        if not md:
            raise RuntimeError("docling returned empty markdown")
        return md

# ---------------------------------------------------------------- chunk
HEADER_RE = re.compile(r"^(#{1,6})\s+(.*)$")

def iter_chunks(md: str):
    """One row PER HEADER; the embedded/searched value is the section_path
    breadcrumb (e.g. 'Results > Subgroup analysis') for high-level discovery."""
    stack, ix = [], 0
    for ln in md.splitlines():
        m = HEADER_RE.match(ln)
        if not m: continue
        level, header = len(m.group(1)), m.group(2).strip()
        if not header: continue
        while stack and stack[-1][0] >= level:
            stack.pop()
        stack.append((level, header))
        path = " > ".join(h for _, h in stack)
        yield ix, {"header": header, "level": level, "path": path}
        ix += 1

# ---------------------------------------------------------------- pipeline
def is_done(key, etag, manifest) -> bool:
    rec = manifest.get(key)
    if not rec or rec.get("etag") != etag: return False
    return store.exists(rec["md_key"])

def process(key_zip, etag, tbl, manifest) -> bool:
    key = os.path.splitext(os.path.basename(key_zip))[0]
    if is_done(key, etag, manifest): return False
    print(f"[start]  {key} etag={etag[:8]}", flush=True)
    pdf = pdf_from_zip(store.get(key_zip))
    if pdf is None:
        print(f"  no pdf in {key_zip}", flush=True); return False
    md = docling_markdown(pdf, f"{key}.pdf")
    md_key = f"{DERIVED}{key}/{etag}.md"
    store.put(md_key, md.encode(), "text/markdown")

    tbl.delete(f"zotero_key = '{key}'")            # idempotent re-ingest
    rows = [{
        "id": f"{key}:{etag}:{ix}", "zotero_key": key, "source_etag": etag,
        "chunk_ix": ix,
        "header": c["header"], "level": c["level"], "section_path": c["path"],
        "text": c["path"],
        "vector": embed(c["path"]),
    } for ix, c in iter_chunks(md)]
    if rows:
        tbl.add(rows)
    for k, _ in store.list(f"{DERIVED}{key}/"):     # drop stale versions
        if not k.endswith(f"{etag}.md"): store.delete(k)

    manifest[key] = {
        "etag": etag, "md_key": md_key, "chars": len(md),
        "processed_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    print(f"[ingest] {key} etag={etag[:8]} ({len(md)} chars)", flush=True)
    return True

def reap_deletes(tbl, manifest, live) -> bool:
    changed = False
    for gone in [k for k in manifest if k not in live]:
        for k, _ in store.list(f"{DERIVED}{gone}/"):
            store.delete(k)
        tbl.delete(f"zotero_key = '{gone}'")
        del manifest[gone]; changed = True
        print(f"[reap] {gone}")
    return changed

# ---------------------------------------------------------------- loop
def poll_once():
    tbl = get_table(create=True)
    manifest = load_manifest()
    dirty = False
    zips = [(k, e) for k, e in store.list(ATTACH) if k.lower().endswith(".zip")]
    live = {os.path.splitext(os.path.basename(k))[0] for k, _ in zips}
    for k, e in zips:
        try:
            changed = process(k, e, tbl, manifest)
            dirty |= changed
            if changed:                            # persist per item so a later
                save_manifest(manifest)            # crash can't lose finished work
        except Exception as ex:
            print(f"  ERR {k}: {ex}", flush=True)
            traceback.print_exc()
    dirty |= reap_deletes(tbl, manifest, live)
    if dirty: save_manifest(manifest)

def main():
    print(f"zdav worker on {STORE} docling=local "
          f"model={MODEL_ID} dim={DIM} every {POLL}s")
    while True:
        try: poll_once()
        except Exception as ex: print(f"[poll] ERR {ex}")
        time.sleep(POLL)

if __name__ == "__main__":
    main()
