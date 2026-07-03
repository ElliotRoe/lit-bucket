"""
zdav_core.py — shared config, storage backend, embedding model, and the LanceDB
table.

Imported by BOTH the ingest worker (zdav_worker.py, write side) and the MCP
search server (zdav_mcp.py, read side) so the schema and connection can never
drift between them. This is the single source of truth for "what the table is"
and "where the bytes live."

Storage is pluggable via ZDAV_STORE:
      ZDAV_STORE=s3://my-bucket     -> S3/R2  (creds from the R2_* vars below)
      ZDAV_STORE=/data/lit          -> local filesystem (also file:///data/lit)

Both backends expose the same tiny object interface (list/get/put/exists/delete)
plus the LanceDB location, so the worker and MCP code is storage-agnostic.

Env:  ZDAV_STORE (required)
      EMBED_MODEL (default BAAI/bge-small-en-v1.5)
      # only when ZDAV_STORE is s3://…:
      R2_ACCOUNT_ID R2_ACCESS_KEY_ID R2_SECRET_ACCESS_KEY
"""
from __future__ import annotations
import hashlib, os
from urllib.parse import urlparse

import lancedb
import pyarrow as pa
from sentence_transformers import SentenceTransformer

# ---- config ------------------------------------------------------------------
STORE    = os.environ["ZDAV_STORE"]
MODEL_ID = os.environ.get("EMBED_MODEL", "BAAI/bge-small-en-v1.5")

ATTACH, DERIVED = "attachments/", "derived/"
MANIFEST_KEY = "_ingest/manifest.json"
TABLE = "docs"

# ---- embedding model (module-level singleton) --------------------------------
model = SentenceTransformer(MODEL_ID)
DIM   = model.get_sentence_embedding_dimension()

def embed(text: str):
    """Single-vector embed, normalized (cosine-ready). Shared so query-time and
    index-time embeddings are guaranteed identical."""
    return model.encode(text, normalize_embeddings=True).tolist()

# ---- storage backends --------------------------------------------------------
# Two implementations, one interface:
#   list(prefix) -> [(key, etag)]   get(key) -> bytes (FileNotFoundError if none)
#   put(key, data, content_type)    exists(key) -> bool    delete(key)
# plus `.lance_uri` / `.lance_opts` for the LanceDB connection.

class _S3Store:
    """Objects in an S3-compatible bucket (Cloudflare R2)."""
    def __init__(self, bucket: str):
        import boto3
        self.bucket = bucket
        ak = os.environ["R2_ACCESS_KEY_ID"]
        sk = os.environ["R2_SECRET_ACCESS_KEY"]
        endpoint = f"https://{os.environ['R2_ACCOUNT_ID']}.r2.cloudflarestorage.com"
        self.s3 = boto3.client("s3", endpoint_url=endpoint, aws_access_key_id=ak,
                               aws_secret_access_key=sk, region_name="auto")
        self.lance_uri  = f"s3://{bucket}/lancedb"
        self.lance_opts = {"endpoint": endpoint, "access_key_id": ak,
                           "secret_access_key": sk, "region": "auto"}

    def list(self, prefix):
        tok, out = None, []
        while True:
            kw = {"Bucket": self.bucket, "Prefix": prefix}
            if tok: kw["ContinuationToken"] = tok
            r = self.s3.list_objects_v2(**kw)
            out += [(o["Key"], o["ETag"].strip('"')) for o in r.get("Contents", [])]
            if not r.get("IsTruncated"): return out
            tok = r["NextContinuationToken"]

    def get(self, key):
        try:
            return self.s3.get_object(Bucket=self.bucket, Key=key)["Body"].read()
        except self.s3.exceptions.NoSuchKey:
            raise FileNotFoundError(key)

    def put(self, key, data, content_type="application/octet-stream"):
        self.s3.put_object(Bucket=self.bucket, Key=key, Body=data,
                           ContentType=content_type)

    def exists(self, key):
        try: self.s3.head_object(Bucket=self.bucket, Key=key); return True
        except self.s3.exceptions.ClientError: return False

    def delete(self, key):
        self.s3.delete_object(Bucket=self.bucket, Key=key)


class _LocalStore:
    """Objects as files under a directory. Keys are POSIX-style paths relative to
    `root`; etags are content MD5 (matching rclone's --etag-hash MD5)."""
    def __init__(self, root: str):
        self.root = os.path.abspath(root)
        os.makedirs(self.root, exist_ok=True)
        self.lance_uri  = os.path.join(self.root, "lancedb")
        self.lance_opts = None

    def _path(self, key): return os.path.join(self.root, key)

    def list(self, prefix):
        out = []
        for dirpath, _, files in os.walk(os.path.join(self.root, prefix)):
            for f in files:
                full = os.path.join(dirpath, f)
                key = os.path.relpath(full, self.root).replace(os.sep, "/")
                out.append((key, _md5(full)))
        return out

    def get(self, key):
        with open(self._path(key), "rb") as f:   # raises FileNotFoundError if none
            return f.read()

    def put(self, key, data, content_type="application/octet-stream"):
        path = self._path(key)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = f"{path}.tmp"
        with open(tmp, "wb") as f: f.write(data)
        os.replace(tmp, path)                     # atomic

    def exists(self, key): return os.path.exists(self._path(key))

    def delete(self, key):
        try: os.remove(self._path(key))
        except FileNotFoundError: pass


def _md5(path):
    h = hashlib.md5()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()

def _make_store(spec: str):
    u = urlparse(spec)
    if u.scheme == "s3":
        return _S3Store(u.netloc)                 # s3://BUCKET
    if u.scheme in ("", "file"):
        return _LocalStore(u.path if u.scheme == "file" else spec)
    raise ValueError(f"unsupported ZDAV_STORE {spec!r} (use s3://bucket or a path)")

store = _make_store(STORE)

# ---- the table (schema lives here, once) -------------------------------------
def _schema():
    return pa.schema([
        pa.field("id",           pa.string()),        # key:etag:chunk
        pa.field("zotero_key",   pa.string()),
        pa.field("source_etag",  pa.string()),
        pa.field("chunk_ix",     pa.int32()),
        pa.field("header",       pa.string()),        # the heading text
        pa.field("level",        pa.int32()),         # 1..6 (# count)
        pa.field("section_path", pa.string()),        # "Results > Subgroup analysis"
        pa.field("text",         pa.string()),        # FTS field (= section_path)
        pa.field("vector",       pa.list_(pa.float32(), DIM)),
    ])

def get_table(create: bool = True):
    """
    Open the shared LanceDB table. Writer calls with create=True (default) so a
    fresh deployment bootstraps. Reader (MCP) calls with create=False so it never
    accidentally creates an empty table if pointed at the wrong store — it fails
    loudly instead.
    """
    db = lancedb.connect(store.lance_uri, storage_options=store.lance_opts)
    if TABLE in db.table_names():
        return db.open_table(TABLE)
    if not create:
        raise RuntimeError(
            f"table '{TABLE}' not found at {store.lance_uri} — has the worker "
            f"ingested anything yet? (reader will not create an empty table)")
    tbl = db.create_table(TABLE, schema=_schema())
    tbl.create_fts_index("text", replace=True)
    return tbl
