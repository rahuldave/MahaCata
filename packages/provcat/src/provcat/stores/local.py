"""The local file system: ``file:///absolute/path``.

Files are already local, so fetch returns the path itself and the cache is not
used. put writes a file through a temporary file in the same folder, so a
reader never sees half of it, and makes the folders it needs. The file keeps
the mode of the file it replaces. put replaces the file, so a hard link to the
old file keeps the old bytes, and put refuses to write over a link. A crash
can leave a temporary file named .NAME.XXXXXXXX beside it. The option ``root`` confines the store to one folder; a URI path is then
relative to that folder. A path that leaves the root through a symbolic link is
refused.
"""
from __future__ import annotations

import os
import shutil
import tempfile
from pathlib import Path

from ..cache import LocalCache
from ..errors import NotFound, StoreError, UriError
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

    def put(self, uri: Uri, src: Path, cache: LocalCache) -> Entry:
        p = self.locate(uri)
        if not uri.segments:
            raise UriError(f"{uri}: name a file to write")
        if p.is_dir():
            raise StoreError(f"{uri}: {p} is a folder")
        if p.is_symlink():
            raise StoreError(f"{uri}: {p} is a link; write to the file it names")
        try:
            p.parent.mkdir(parents=True, exist_ok=True)
            self.locate(uri)  # a folder made through a link must not leave the root
            # The mode of the file it replaces, or the usual mode of a new file.
            mode = p.stat().st_mode & 0o7777 if p.exists() else 0o666 & ~_umask()
            fd, tmp = tempfile.mkstemp(dir=p.parent, prefix=f".{p.name}.")
            try:
                with os.fdopen(fd, "wb") as out, open(src, "rb") as f:
                    shutil.copyfileobj(f, out)
                    out.flush()
                    os.fsync(out.fileno())
                os.chmod(tmp, mode)
                os.replace(tmp, p)
            except BaseException:
                Path(tmp).unlink(missing_ok=True)
                raise
        except OSError as e:
            raise StoreError(f"{uri}: cannot write {p}: {e.strerror or e}") from e
        return self._entry(uri, p)

    def delete(self, uri: Uri, cache: LocalCache) -> None:
        p = self.locate(uri)
        if p.is_dir() and not p.is_symlink():
            raise StoreError(f"{uri}: {p} is a folder; delete the files in it")
        try:
            p.unlink()
        except FileNotFoundError:
            raise NotFound(f"{uri}: {p} does not exist") from None
        except OSError as e:
            raise StoreError(f"{uri}: cannot delete {p}: {e.strerror or e}") from e


def _umask() -> int:
    mask = os.umask(0)
    os.umask(mask)
    return mask
