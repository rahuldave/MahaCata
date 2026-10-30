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
            self._selector(uri)
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
        except exceptions.PreconditionFailed as e:
            from .objects import Exists  # only a conditional write sends a precondition

            raise Exists(f"{uri}: already there") from e
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

    def _versioned(self, uri, bucket):
        # Without versioning a generation is gone once the object is written again.
        if not self._call(uri, lambda c: c.get_bucket(bucket)).versioning_enabled:
            raise StoreError(f"{uri}: versioning is off on bucket {bucket}, so GCS does not keep "
                             "old generations to pin; turn it on")

    def _version_of(self, uri, bucket, key):
        self._versioned(uri, bucket)
        return self._head(uri, bucket, key)[1]

    def _history(self, uri, bucket, prefix):
        # GCS has no delete markers: a delete makes the live generation old,
        # which sets its time_deleted, so a listing still shows the change.
        self._versioned(uri, bucket)

        def history(c):
            return [(b.name, str(b.generation), b.size, b.time_deleted is None)
                    for b in c.list_blobs(bucket, prefix=prefix, versions=True)]

        return self._call(uri, history)

    def _create(self, uri, bucket, key, src):
        blob = self._call(uri, lambda c: c.bucket(bucket).blob(key))
        self._call(uri, lambda c: blob.upload_from_filename(str(src), if_generation_match=0))

    @staticmethod
    def _generation(uri, version):
        if not version.isdigit():
            raise UriError(f"{uri}: a GCS version is a generation, a number")
        return int(version)

    def _head_version(self, uri, bucket, key, version):
        g = self._generation(uri, version)
        blob = self._call(uri, lambda c: c.bucket(bucket).get_blob(key, generation=g))
        if blob is None:
            raise NotFound(f"{uri}: no generation {version}")
        return blob.size

    def _get_version(self, uri, bucket, key, version, out):
        g = self._generation(uri, version)
        blob = self._call(uri, lambda c: c.bucket(bucket).blob(key, generation=g))
        self._call(uri, lambda c: blob.download_to_file(out))

    def _copy(self, uri, bucket, key, version, to):
        g = self._generation(uri, version) if version else None

        def copy(c):
            b = c.bucket(bucket)
            b.copy_blob(b.blob(key), b, to, source_generation=g)

        self._call(uri, copy)

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
