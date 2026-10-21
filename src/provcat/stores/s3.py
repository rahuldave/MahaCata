"""S3 and S3-compatible storage: ``s3://bucket/key``.

The same driver serves AWS S3, MinIO and the lakeFS S3 gateway. For lakeFS the
bucket is the repository and the first key segment is the branch or commit, so
``lakefs://repo/main/path`` reads ``path`` on branch ``main``.

Options, all optional:

- ``endpoint_url``: the S3 endpoint, for example ``http://localhost:8000`` for lakeFS.
- ``region``: the region name. The default is ``us-east-1``.
- ``profile``: a profile name in ``~/.aws/config``.
- ``addressing_style``: ``path`` or ``virtual``. The default is ``path`` when
  ``endpoint_url`` is set, because most S3-compatible servers need it.

Credentials come from the usual AWS places: the environment, the profile, or an
instance role. Do not put keys in the provcat configuration.

The cache key is bucket plus object key. The cache keeps the ETag of each file
and downloads the file again when the ETag changes. The ETag recorded is the one
that came with the downloaded bytes, so the record matches the file even if the
object changed between stat and download.

A folder fetch refuses a folder that holds keys no URI can name, for example
``a//b``, and names that one disk could store as one, for example ``Dir/a`` and
``dir/b``. Fetches of one folder run one at a time. It marks the folder done only after
every file is current and every deleted object is gone from the cache.
"""
from __future__ import annotations

from pathlib import Path

from ..cache import LocalCache, find_twins
from ..errors import NotFound, OfflineMiss, StoreError, UriError
from ..uri import Uri
from . import Entry, Store, register_driver

_CHUNK = 1 << 20


@register_driver
class S3Store(Store):
    driver = "s3"

    def option_types(self):
        return {"endpoint_url": str, "region": str, "profile": str, "addressing_style": str}

    def __init__(self, *args, **kw):
        super().__init__(*args, **kw)
        self._client = None

    def _make_client(self):
        import boto3
        from botocore.config import Config

        o = self.options
        style = o.get("addressing_style") or ("path" if o.get("endpoint_url") else "auto")
        session = boto3.session.Session(profile_name=o.get("profile"))
        return session.client(
            "s3",
            endpoint_url=o.get("endpoint_url"),
            region_name=o.get("region", "us-east-1"),
            config=Config(s3={"addressing_style": style}, retries={"max_attempts": 3}),
        )

    @staticmethod
    def locate(uri: Uri) -> tuple[str, str]:
        if uri.selector is not None:
            raise UriError(f"{uri}: S3 has no ?at= selector; put the version in the path")
        if not uri.authority:
            raise UriError(f"{uri}: write {uri.scheme}://bucket/key")
        return uri.authority, uri.path

    def _call(self, uri: Uri, method: str, **kw):
        """Call one S3 client method, and turn every boto error into a StoreError."""
        from botocore.exceptions import BotoCoreError, ClientError

        try:
            if self._client is None:
                self._client = self._make_client()
            return getattr(self._client, method)(**kw)
        except ClientError as e:
            code = str(e.response.get("Error", {}).get("Code", ""))
            if code in ("404", "NoSuchKey", "NoSuchBucket", "NotFound"):
                raise NotFound(f"{uri}: not found ({code})") from e
            raise StoreError(f"{uri}: S3 error {code}: {e}") from e
        except BotoCoreError as e:
            raise StoreError(f"{uri}: {e}") from e

    def _online(self, uri: Uri) -> None:
        if self.offline:
            raise OfflineMiss(f"{uri}: offline, and only fetch can use the cache")

    def stat(self, uri: Uri, cache: LocalCache) -> Entry:
        self._online(uri)
        bucket, key = self.locate(uri)
        if key:
            try:
                head = self._call(uri, "head_object", Bucket=bucket, Key=key)
                return Entry(uri, "file", head["ContentLength"], head["ETag"].strip('"'))
            except NotFound:
                pass
        prefix = key + "/" if key else ""
        out = self._call(uri, "list_objects_v2", Bucket=bucket, Prefix=prefix, MaxKeys=1)
        if out.get("KeyCount", 0) or not key:
            return Entry(uri, "folder")
        raise NotFound(f"{uri}: no object and no folder")

    def _pages(self, uri: Uri, **kw):
        bucket, key = self.locate(uri)
        prefix = key + "/" if key else ""
        token = None
        while True:
            args = dict(Bucket=bucket, Prefix=prefix, **kw)
            if token:
                args["ContinuationToken"] = token
            out = self._call(uri, "list_objects_v2", **args)
            yield prefix, out
            if not out.get("IsTruncated"):
                return
            token = out["NextContinuationToken"]

    @staticmethod
    def _child(uri: Uri, rel: str) -> Uri | None:
        try:
            for seg in rel.split("/"):
                uri = uri.child(seg)
            return uri
        except UriError:
            return None  # a key that no URI can name, for example one with "//"

    def list(self, uri: Uri, cache: LocalCache) -> list[Entry]:
        self._online(uri)
        entries = []
        for prefix, out in self._pages(uri, Delimiter="/"):
            for p in out.get("CommonPrefixes", []):
                child = self._child(uri, p["Prefix"][len(prefix):].rstrip("/"))
                if child:
                    entries.append(Entry(child, "folder"))
            for o in out.get("Contents", []):
                rel = o["Key"][len(prefix):]
                child = self._child(uri, rel) if rel else None
                if child:
                    entries.append(Entry(child, "file", o["Size"], o["ETag"].strip('"')))
        return entries

    def walk(self, uri: Uri) -> list[Entry]:
        """Every file below a folder, at any depth.

        Keys that end in "/" are folder markers and are left out. Any other key
        that no URI can name is an error, so a fetched folder is never short.
        """
        files, bad = [], []
        for prefix, out in self._pages(uri):
            for o in out.get("Contents", []):
                rel = o["Key"][len(prefix):]
                if not rel or rel.endswith("/"):
                    continue
                child = self._child(uri, rel)
                if child is None:
                    bad.append(o["Key"])
                else:
                    files.append(Entry(child, "file", o["Size"], o["ETag"].strip('"')))
        if bad:
            raise StoreError(f"{uri}: keys that no URI can name, so the folder cannot be cached: {bad[:5]}")
        twins = find_twins(f.uri.segments[len(uri.segments):] for f in files)
        if twins:
            raise StoreError(f"{uri}: {twins[0]!r} and {twins[1]!r} would be one name on a disk "
                             "that ignores case or Unicode form")
        return files

    def _key(self, uri: Uri) -> list[str]:
        return [uri.authority, *uri.segments]

    def _download(self, entry: Entry, cache: LocalCache) -> Path:
        bucket, key = self.locate(entry.uri)

        def fill(tmp: Path) -> str:
            obj = self._call(entry.uri, "get_object", Bucket=bucket, Key=key)
            with tmp.open("wb") as f:
                for chunk in obj["Body"].iter_chunks(_CHUNK):
                    f.write(chunk)
            return obj["ETag"].strip('"')

        return cache.put(self.name, self._key(entry.uri), fill)

    def fetch(self, uri: Uri, cache: LocalCache) -> Path:
        key = self._key(uri)
        self.locate(uri)
        if self.offline:
            hit = cache.get(self.name, key, None) or cache.folder_done(self.name, key, None)
            if hit is None:
                raise OfflineMiss(f"{uri}: not in the cache")
            return hit
        entry = self.stat(uri, cache)
        if entry.kind == "file":
            return cache.get(self.name, key, entry.version) or self._download(entry, cache)
        try:
            with cache.lock(self.name, key):
                return self._fetch_folder(uri, key, cache)
        except OSError as e:
            raise StoreError(f"cannot lock {cache.path(self.name, key)} in the cache: {e}") from e

    def _fetch_folder(self, uri: Uri, key: list[str], cache: LocalCache) -> Path:
        cache.unmark_folder(self.name, key)
        files = self.walk(uri)
        keep = set()
        for f in files:
            keep.add(tuple(f.uri.segments[len(uri.segments):]))
            if cache.get(self.name, self._key(f.uri), f.version) is None:
                self._download(f, cache)
        cache.prune(self.name, key, keep)
        folder = cache.path(self.name, key)
        try:
            folder.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            raise StoreError(f"cannot write {folder} in the cache: {e}") from e
        cache.mark_folder(self.name, key, "listed")
        return folder
