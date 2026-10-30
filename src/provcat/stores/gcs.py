"""Google Cloud Storage: ``gs://bucket/key``.

It needs the ``gcs`` extra: ``pip install provcat[gcs]``.

Options, all optional:

- ``project``: the Google Cloud project. The default comes from the credentials.
- ``endpoint_url``: another API endpoint, for example a local emulator.
- ``anonymous``: true to send no credentials, for public buckets and emulators.

Credentials come from Application Default Credentials: the environment variable
GOOGLE_APPLICATION_CREDENTIALS, ``gcloud auth application-default login``, or
the service account of the machine. Do not put keys in the provcat configuration.

The version of a file is its generation, a number that GCS changes on every
write. The download reads the generation of the bytes it got from the reply,
so the cached bytes always match the version recorded. ObjectStore in objects.py holds the rest.
"""
from __future__ import annotations

from ..errors import NotFound, StoreError, UriError
from ..uri import Uri
from . import register_driver
from .objects import ObjectStore


def _sdk():
    try:
        from google.api_core import exceptions
        from google.cloud import storage
    except ImportError:
        raise StoreError("the gcs driver needs the gcs extra: pip install 'provcat[gcs]'") from None
    return storage, exceptions


@register_driver
class GcsStore(ObjectStore):
    driver = "gcs"

    def option_types(self):
        return {"project": str, "endpoint_url": str, "anonymous": bool}

    def __init__(self, *args, **kw):
        super().__init__(*args, **kw)
        self._client = None

    def _make_client(self):
        storage, _ = _sdk()
        o = self.options
        kw = {"project": o.get("project")}
        if o.get("endpoint_url"):
            kw["client_options"] = {"api_endpoint": o["endpoint_url"]}
        if o.get("anonymous"):
            from google.auth.credentials import AnonymousCredentials

            kw["credentials"] = AnonymousCredentials()
            kw["project"] = kw["project"] or "anonymous"
        return storage.Client(**kw)

    def locate(self, uri: Uri) -> tuple[str, str]:
        if uri.selector is not None:
            raise UriError(f"{uri}: GCS has no ?at= selector; put the version in the path")
        if not uri.authority:
            raise UriError(f"{uri}: write {uri.scheme}://bucket/key")
        return uri.authority, uri.path

    def _call(self, uri: Uri, fn):
        """Run fn(client), and turn every Google error into a StoreError."""
        _, exceptions = _sdk()
        from google.auth.exceptions import GoogleAuthError

        try:
            if self._client is None:
                self._client = self._make_client()
            return fn(self._client)
        except exceptions.NotFound as e:
            raise NotFound(f"{uri}: not found ({e.message})") from e
        except exceptions.GoogleAPICallError as e:
            raise StoreError(f"{uri}: GCS error {e.code}: {e.message}") from e
        except GoogleAuthError as e:
            raise StoreError(f"{uri}: no Google credentials ({e}); run gcloud auth application-default login") from e
        except (exceptions.GoogleAPIError, OSError) as e:
            raise StoreError(f"{uri}: {e}") from e
        except Exception as e:  # DataCorruption, a failed checksum, derives only from Exception
            from google.cloud.storage.exceptions import DataCorruption

            if isinstance(e, DataCorruption):
                raise StoreError(f"{uri}: the download failed its checksum: {e}") from e
            raise

    def _put(self, uri, bucket, key, src):
        blob = self._call(uri, lambda c: c.bucket(bucket).blob(key))
        self._call(uri, lambda c: blob.upload_from_filename(str(src)))
        if blob.generation is None:
            raise StoreError(f"{uri}: GCS sent no generation with the upload")
        return str(blob.generation)

    def _delete(self, uri, bucket, key):
        self._call(uri, lambda c: c.bucket(bucket).blob(key).delete())

    def _head(self, uri, bucket, key):
        blob = self._call(uri, lambda c: c.bucket(bucket).get_blob(key))
        if blob is None:
            raise NotFound(f"{uri}: not found")
        return blob.size, str(blob.generation)

    def _pages(self, uri, bucket, prefix, delimiter, limit=None):
        def pages(c):
            it = c.list_blobs(bucket, prefix=prefix, delimiter="/" if delimiter else None,
                              max_results=limit, page_size=limit or self.page_size)
            return it.pages

        it = self._call(uri, pages)
        while True:
            page = self._call(uri, lambda c: next(it, None))
            if page is None:
                return
            objects = [(b.name, b.size, str(b.generation)) for b in page]
            yield sorted(page.prefixes), objects
            if limit:
                return

    def _get(self, uri, bucket, key, out):
        # One request. The SDK reads the generation of the bytes it sent from
        # the x-goog-generation header, so the version always matches the file.
        blob = self._call(uri, lambda c: c.bucket(bucket).blob(key))
        self._call(uri, lambda c: blob.download_to_file(out))
        if blob.generation is None:
            raise StoreError(f"{uri}: GCS sent no generation with the download")
        return str(blob.generation)
