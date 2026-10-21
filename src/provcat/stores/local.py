"""The local file system: ``file:///absolute/path``.

Files are already local, so fetch returns the path itself and the cache is not
used. The option ``root`` confines the store to one folder; a URI path is then
relative to that folder. A path that leaves the root through a symbolic link is
refused.
"""
from __future__ import annotations

from pathlib import Path

from ..cache import LocalCache
from ..errors import NotFound, UriError
from ..uri import Uri
from . import Entry, Store, register_driver


@register_driver
class LocalStore(Store):
    driver = "file"

    def option_types(self):
        return {"root": str}

    def locate(self, uri: Uri) -> Path:
        if uri.authority not in ("", "localhost"):
            raise UriError(f"{uri}: a file URI has no host; write {uri.scheme}:///path")
        if uri.selector is not None:
            raise UriError(f"{uri}: local files have no versions, so ?at= is not allowed")
        root = self.options.get("root")
        if not root:
            return Path("/", *uri.segments)
        base = Path(root).resolve()
        p = base.joinpath(*uri.segments)
        if not p.resolve().is_relative_to(base):
            raise UriError(f"{uri}: leaves the store root {base}")
        return p

    def _entry(self, uri: Uri, p: Path) -> Entry:
        st = p.stat()
        kind = "folder" if p.is_dir() else "file"
        return Entry(uri, kind, None if kind == "folder" else st.st_size, f"{st.st_mtime_ns}-{st.st_size}")

    def stat(self, uri: Uri, cache: LocalCache) -> Entry:
        p = self.locate(uri)
        if not p.exists():
            raise NotFound(f"{uri}: {p} does not exist")
        return self._entry(uri, p)

    def list(self, uri: Uri, cache: LocalCache) -> list[Entry]:
        p = self.locate(uri)
        if not p.is_dir():
            raise NotFound(f"{uri}: {p} is not a folder")
        entries = []
        for c in sorted(p.iterdir()):
            try:
                child = uri.child(c.name)
                self.locate(child)
                entries.append(self._entry(child, c))
            except (UriError, OSError):
                continue  # a broken link, or a link out of the root
        return entries

    def fetch(self, uri: Uri, cache: LocalCache) -> Path:
        p = self.locate(uri)
        if not p.exists():
            raise NotFound(f"{uri}: {p} does not exist")
        return p
