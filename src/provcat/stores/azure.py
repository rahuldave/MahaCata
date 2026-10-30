"""Azure Blob Storage and ADLS Gen2: ``az://account/container/key``.

It needs the ``azure`` extra: ``pip install provcat[azure]``. An ADLS Gen2
account, one with a hierarchical namespace, is read through the same Blob API.

Options, all optional:

- ``account_url``: the Blob endpoint. ``{account}`` is replaced by the account
  in the URI. The default is ``https://{account}.blob.core.windows.net``.
- ``connection_string_env``: the name of an environment variable that holds a
  connection string, for example for a local emulator. The connection string
  then gives the endpoint and the key.

Without a connection string, credentials come from DefaultAzureCredential: the
environment, a managed identity, or ``az login``. The identity needs the role
Storage Blob Data Reader. Do not put keys in the provcat configuration.

The version of a file is its ETag. A download returns the ETag of the bytes it
sent, so the cached bytes always match the version recorded. On an ADLS Gen2
account, the folders are also blobs, marked with the metadata hdi_isfolder.
The listings leave them out. ObjectStore in objects.py holds the rest.
"""
from __future__ import annotations

import os

from ..errors import ConfigError, NotFound, StoreError, UriError
from ..uri import Uri
from . import register_driver
from .objects import ObjectStore

_DEFAULT_URL = "https://{account}.blob.core.windows.net"


def _sdk():
    try:
        from azure.core import exceptions
        from azure.storage.blob import BlobServiceClient
    except ImportError:
        raise StoreError("the azure driver needs the azure extra: pip install 'provcat[azure]'") from None
    return BlobServiceClient, exceptions


def _is_folder(props) -> bool:
    return (props.metadata or {}).get("hdi_isfolder") == "true"


@register_driver
class AzureStore(ObjectStore):
    driver = "azure"

    def option_types(self):
        return {"account_url": str, "connection_string_env": str}

    def __init__(self, *args, **kw):
        super().__init__(*args, **kw)
        self._clients = {}
        if "{account}" not in self.options.get("account_url", _DEFAULT_URL):
            # Without it, every account in a URI would read the one endpoint.
            raise ConfigError(f"stores.{self.name}.account_url must contain {{account}}")

    def _service(self, account: str):
        if account not in self._clients:
            BlobServiceClient, _ = _sdk()
            env = self.options.get("connection_string_env")
            if env:
                conn = os.environ.get(env)
                if not conn:
                    raise ConfigError(f"stores.{self.name}: the environment variable {env} is not set")
                try:
                    client = BlobServiceClient.from_connection_string(conn)
                except ValueError:
                    raise ConfigError(f"stores.{self.name}: the connection string in {env} is not valid") from None
                if client.account_name != account:
                    # The connection string names one account; never serve another one's data.
                    raise ConfigError(f"stores.{self.name}: the connection string in {env} is for account "
                                      f"{client.account_name!r}, not {account!r}")
                self._clients[account] = client
            else:
                try:
                    from azure.identity import DefaultAzureCredential
                except ImportError:
                    raise StoreError("the azure driver needs the azure extra: pip install 'provcat[azure]'") from None
                url = self.options.get("account_url", _DEFAULT_URL).replace("{account}", account)
                self._clients[account] = BlobServiceClient(url, credential=DefaultAzureCredential())
        return self._clients[account]

    def locate(self, uri: Uri) -> tuple[str, str]:
        if uri.selector is not None:
            raise UriError(f"{uri}: Azure has no ?at= selector; put the version in the path")
        if not uri.authority or not uri.segments:
            raise UriError(f"{uri}: write {uri.scheme}://account/container/key")
        return uri.segments[0], "/".join(uri.segments[1:])

    def _call(self, uri: Uri, fn):
        """Run fn(container client), and turn every Azure error into a StoreError."""
        _, exceptions = _sdk()
        container = uri.segments[0]
        try:
            return fn(self._service(uri.authority).get_container_client(container))
        except exceptions.ResourceNotFoundError as e:
            raise NotFound(f"{uri}: not found ({getattr(e, 'error_code', None) or e.reason})") from e
        except exceptions.ClientAuthenticationError as e:
            from azure.identity import CredentialUnavailableError

            if isinstance(e, CredentialUnavailableError):
                raise StoreError(f"{uri}: no Azure credentials; run az login") from e
            first = str(e.message).splitlines()[0] if e.message else type(e).__name__
            raise StoreError(f"{uri}: Azure could not sign in ({first}); run az login again") from e
        except exceptions.HttpResponseError as e:
            raise StoreError(f"{uri}: Azure error {e.status_code} {getattr(e, 'error_code', None)}: {e.message}") from e
        except exceptions.AzureError as e:
            raise StoreError(f"{uri}: {e}") from e

    def _put(self, uri, bucket, key, src):
        with open(src, "rb") as f:
            out = self._call(uri, lambda c: c.get_blob_client(key).upload_blob(f, overwrite=True))
        return str(out["etag"]).strip('"')

    def _delete(self, uri, bucket, key):
        if self._is_marker(uri, bucket, key):
            raise StoreError(f"{uri}: a folder, not a file")
        self._call(uri, lambda c: c.get_blob_client(key).delete_blob())

    def _head(self, uri, bucket, key):
        props = self._call(uri, lambda c: c.get_blob_client(key).get_blob_properties())
        if _is_folder(props):
            raise NotFound(f"{uri}: a folder, not a file")
        return props.size, props.etag.strip('"')

    def _is_marker(self, uri, bucket, key):
        try:
            return _is_folder(self._call(uri, lambda c: c.get_blob_client(key).get_blob_properties()))
        except StoreError:
            return False

    def stat(self, uri, cache):
        # On ADLS Gen2 an empty folder is a marker blob with nothing below it.
        self._online(uri)
        bucket, key = self.locate(uri)
        if key:
            try:
                props = self._call(uri, lambda c: c.get_blob_client(key).get_blob_properties())
                if _is_folder(props):
                    from . import Entry

                    return Entry(uri, "folder")
            except NotFound:
                pass
        return super().stat(uri, cache)

    def _pages(self, uri, bucket, prefix, delimiter, limit=None):
        def pages(c):
            if delimiter:
                it = c.walk_blobs(name_starts_with=prefix or None, delimiter="/", include=["metadata"],
                                  results_per_page=limit or self.page_size)
            else:
                it = c.list_blobs(name_starts_with=prefix or None, include=["metadata"],
                                  results_per_page=limit or self.page_size)
            return it.by_page()

        pages_it = self._call(uri, pages)
        while True:
            page = self._call(uri, lambda c: next(pages_it, None))
            if page is None:
                return
            folders, objects = [], []
            for item in self._call(uri, lambda c: list(page)):
                if not hasattr(item, "size"):  # a BlobPrefix, the folder of a delimited listing
                    folders.append(item.name)
                elif _is_folder(item):
                    # A marker is a folder. In a flat listing it still shows that
                    # something is below the prefix, so stat finds the folder.
                    folders.append(item.name.rstrip("/") + "/")
                else:
                    objects.append((item.name, item.size, item.etag.strip('"')))
            yield sorted(set(folders)), objects
            if limit:
                return

    def _get(self, uri, bucket, key, out):
        stream = self._call(uri, lambda c: c.get_blob_client(key).download_blob())
        self._call(uri, lambda c: stream.readinto(out))
        return stream.properties.etag.strip('"')
