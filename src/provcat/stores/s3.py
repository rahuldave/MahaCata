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

from ..errors import NotFound, StoreError, UriError
from ..uri import Uri
from . import register_driver
from .objects import ObjectStore

_CHUNK = 1 << 20


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
            if code in ("400", "BadRequest") and uri.segments and self.options.get("endpoint_url"):
                # The lakeFS S3 gateway answers a branch or commit that does not
                # exist with a bare 400. Other servers can send 400 too, so hedge.
                raise StoreError(f"{uri}: S3 error {code}; if this store is lakeFS, the branch or commit "
                                 f"{uri.segments[0]!r} may not exist: {e}") from e
            raise StoreError(f"{uri}: S3 error {code}: {e}") from e
        except BotoCoreError as e:
            raise StoreError(f"{uri}: {e}") from e

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
