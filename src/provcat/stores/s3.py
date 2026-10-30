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

The version of a file is its ETag. The ETag recorded is the one that came with
the downloaded bytes, so the record matches the file even if the object changed
between stat and download. ObjectStore in objects.py holds the rest.
"""
from __future__ import annotations

from pathlib import Path

from ..errors import NotFound, StoreError, UriError
from ..uri import Uri
from . import register_driver
from .objects import ObjectStore

_CHUNK = 1 << 20
_SINGLE = 64 << 20  # files up to this size go in one request


@register_driver
class S3Store(ObjectStore):
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
        session = self._session()
        return session.client(
            "s3",
            endpoint_url=o.get("endpoint_url"),
            region_name=o.get("region", "us-east-1"),
            config=Config(s3={"addressing_style": style}, retries={"max_attempts": 3}),
        )

    def _session(self):
        """A boto3 session with the store's profile, or a clear error."""
        import boto3
        from botocore.exceptions import ProfileNotFound

        try:
            return boto3.session.Session(profile_name=self.options.get("profile"))
        except ProfileNotFound:
            raise StoreError(f"store {self.name}: no AWS profile named {self.options.get('profile')!r}; "
                             "add it to ~/.aws/credentials") from None

    def locate(self, uri: Uri) -> tuple[str, str]:
        if uri.selector is not None:
            self._selector(uri)
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
            if "IfNoneMatch" in kw and code in ("412", "PreconditionFailed", "ConditionalRequestConflict"):
                # Only a conditional write sends a condition. A conflict is
                # another conditional write to the key at the same time.
                from .objects import Exists

                raise Exists(f"{uri}: already there ({code})") from e
            if code in ("404", "NoSuchKey", "NoSuchBucket", "NotFound", "NoSuchVersion"):
                raise NotFound(f"{uri}: not found ({code})") from e
            if code in ("400", "BadRequest") and uri.segments and self.options.get("endpoint_url"):
                # The lakeFS S3 gateway answers a branch or commit that does not
                # exist with a bare 400. Other servers can send 400 too, so hedge.
                raise StoreError(f"{uri}: S3 error {code}; if this store is lakeFS, the branch or commit "
                                 f"{uri.segments[0]!r} may not exist: {e}") from e
            raise StoreError(f"{uri}: S3 error {code}: {e}") from e
        except BotoCoreError as e:
            raise StoreError(f"{uri}: {e}") from e

    def _put(self, uri, bucket, key, src):
        # A small file goes in one request, whose reply gives the ETag of these
        # bytes. upload_file switches to a multipart upload for a large file, so
        # a file over 5 GB works, but it gives no ETag, so a HEAD reads it; a
        # write by someone else in between gives their ETag.
        if Path(src).stat().st_size <= _SINGLE:
            with open(src, "rb") as f:
                return self._call(uri, "put_object", Bucket=bucket, Key=key, Body=f)["ETag"].strip('"')
        self._call(uri, "upload_file", Filename=str(src), Bucket=bucket, Key=key)
        return self._head(uri, bucket, key)[1]

    def _delete(self, uri, bucket, key):
        self._head(uri, bucket, key)  # S3 deletes a missing key without a word
        self._call(uri, "delete_object", Bucket=bucket, Key=key)

    def _versioned(self, uri, bucket):
        status = self._call(uri, "get_bucket_versioning", Bucket=bucket).get("Status")
        if status != "Enabled":
            raise StoreError(f"{uri}: versioning is {(status or 'off').lower()} on bucket {bucket}, so S3 "
                             "does not keep old versions to pin; turn it on")

    def _version_of(self, uri, bucket, key):
        self._versioned(uri, bucket)
        version = self._call(uri, "head_object", Bucket=bucket, Key=key).get("VersionId")
        if not version:
            raise StoreError(f"{uri}: S3 gave no version ID")
        return version

    def _history(self, uri, bucket, prefix):
        self._versioned(uri, bucket)
        out, args = [], dict(Bucket=bucket, Prefix=prefix)
        while True:
            page = self._call(uri, "list_object_versions", **args)
            out += [(v["Key"], v["VersionId"], v["Size"], v["IsLatest"]) for v in page.get("Versions", [])]
            out += [(m["Key"], m["VersionId"], None, m["IsLatest"]) for m in page.get("DeleteMarkers", [])]
            if not page.get("IsTruncated"):
                return out
            args.update(KeyMarker=page["NextKeyMarker"], VersionIdMarker=page["NextVersionIdMarker"])

    def _create(self, uri, bucket, key, src):
        with open(src, "rb") as f:
            self._call(uri, "put_object", Bucket=bucket, Key=key, Body=f, IfNoneMatch="*")

    def _head_version(self, uri, bucket, key, version):
        return self._call(uri, "head_object", Bucket=bucket, Key=key, VersionId=version)["ContentLength"]

    def _get_version(self, uri, bucket, key, version, out):
        obj = self._call(uri, "get_object", Bucket=bucket, Key=key, VersionId=version)
        for chunk in obj["Body"].iter_chunks(_CHUNK):
            out.write(chunk)

    def _copy(self, uri, bucket, key, version, to):
        # copy is the managed copy: one request, or a multipart copy over 5 GB.
        source = {"Bucket": bucket, "Key": key, **({"VersionId": version} if version else {})}
        self._call(uri, "copy", CopySource=source, Bucket=bucket, Key=to)

    def _head(self, uri, bucket, key):
        head = self._call(uri, "head_object", Bucket=bucket, Key=key)
        return head["ContentLength"], head["ETag"].strip('"')

    def _pages(self, uri, bucket, prefix, delimiter, limit=None):
        token = None
        while True:
            args = dict(Bucket=bucket, Prefix=prefix)
            if delimiter:
                args["Delimiter"] = "/"
            if limit or self.page_size:
                args["MaxKeys"] = limit or self.page_size
            if token:
                args["ContinuationToken"] = token
            out = self._call(uri, "list_objects_v2", **args)
            yield ([p["Prefix"] for p in out.get("CommonPrefixes", [])],
                   [(o["Key"], o["Size"], o["ETag"].strip('"')) for o in out.get("Contents", [])])
            if limit or not out.get("IsTruncated"):
                return
            token = out["NextContinuationToken"]

    def _get(self, uri, bucket, key, out):
        obj = self._call(uri, "get_object", Bucket=bucket, Key=key)
        for chunk in obj["Body"].iter_chunks(_CHUNK):
            out.write(chunk)
        return obj["ETag"].strip('"')
