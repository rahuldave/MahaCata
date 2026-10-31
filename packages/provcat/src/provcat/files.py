"""File handles that Catalog.open_file returns.

A reader reads one pinned version that the catalog fetched into the cache.
Each read, readline or readall runs the read_block spot once with its byte
range, and close runs the close spot with every range read. A writer writes
to a temporary file, and close puts it in the store through the put spot. A
writer used in a ``with`` block that ends in an error puts nothing, and so
does a writer that is dropped without a close. A writer opened with
autocommit off puts at ``commit()``, as fsspec transactions expect.
"""
from __future__ import annotations

import io
import os
import shutil
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import Any

from .uri import Uri


def _merge(ranges: list[tuple[int, int]]) -> list[tuple[int, int]]:
    out: list[tuple[int, int]] = []
    for start, end in sorted(ranges):
        if out and start <= out[-1][1]:
            out[-1] = (out[-1][0], max(out[-1][1], end))
        else:
            out.append((start, end))
    return out


class CatalogReader(io.RawIOBase):
    """A read-only handle on one pinned version of a file.

    ``uri`` is the URI as opened, and ``pinned`` the pinned physical URI.
    ``size`` is the size at open; a read never goes past it. ``ranges`` lists
    the byte ranges handed out so far, merged, as (start, end). A wrapper that
    reads ahead, such as io.TextIOWrapper, makes them larger than what its
    caller used."""

    _f = None

    def __init__(self, uri: Uri, pinned: Uri, path: Path, block: Callable[..., Any],
                 done: Callable[[CatalogReader], None]):
        super().__init__()
        self.uri, self.pinned, self.path = uri, pinned, path
        self._f = path.open("rb")
        self._block, self._done = block, done
        self._ranges: list[tuple[int, int]] = []
        self.size = os.fstat(self._f.fileno()).st_size

    @property
    def ranges(self) -> list[tuple[int, int]]:
        return _merge(self._ranges)

    def readable(self) -> bool:
        return True

    def seekable(self) -> bool:
        return True

    def tell(self) -> int:
        return self._f.tell()

    def seek(self, offset: int, whence: int = io.SEEK_SET) -> int:
        return self._f.seek(offset, whence)

    def _read(self, length: int, how: Callable[[int], Any]) -> Any:
        self._checkClosed()
        start = self._f.tell()
        length = max(min(length, self.size - start), 0)
        out = self._block(start, length, lambda _req: how(length))
        n = out if isinstance(out, int) else len(out or b"")
        if n:
            self._ranges.append((start, start + n))
            if len(self._ranges) > 1024:
                self._ranges = _merge(self._ranges)
        return out

    def readinto(self, buf) -> int:
        view = memoryview(buf).cast("B")
        return self._read(len(view), lambda n: self._f.readinto(view[:n]))

    def readall(self) -> bytes:
        return self._read(self.size, lambda n: self._f.read(n))

    def read(self, size: int = -1) -> bytes:
        if size is None or size < 0:
            return self.readall()
        return self._read(size, lambda n: self._f.read(n))

    def readline(self, size: int | None = -1) -> bytes:
        limit = self.size if size is None or size < 0 else size
        return self._read(limit, lambda n: self._f.readline(n))

    def close(self) -> None:
        if self.closed:
            return
        try:
            if self._f is not None:
                self._done(self)
        finally:
            if self._f is not None:
                self._f.close()
            super().close()

    def abandon(self) -> None:
        """Close without the close spot, for a handle that its caller never got."""
        if self._f is not None:
            self._f.close()
        io.RawIOBase.close(self)


class CatalogWriter(io.RawIOBase):
    """A handle that writes a new file. Close, or commit when autocommit is
    off, puts it at ``uri``; ``entries`` then holds what the store wrote."""

    _f = None
    _dir = None

    def __init__(self, uri: Uri, done: Callable[[CatalogWriter, Path], None], autocommit: bool = True):
        super().__init__()
        self.uri = uri
        self.autocommit = autocommit
        self._done = done
        self.entries: list = []
        self._dir = tempfile.mkdtemp(prefix="provcat-write-")
        self.path = Path(self._dir) / "data"
        self._f = self.path.open("wb")

    def writable(self) -> bool:
        return True

    def write(self, b) -> int:
        self._checkClosed()
        return self._f.write(b)

    def tell(self) -> int:
        return self._f.tell()

    def _cleanup(self) -> None:
        if self._f is not None:
            self._f.close()
        if self._dir is not None:
            shutil.rmtree(self._dir, ignore_errors=True)
            self._dir = None

    def close(self) -> None:
        if self.closed:
            return
        if not self.autocommit:
            self._f.close()  # commit or discard comes later
            io.RawIOBase.close(self)
            return
        try:
            self._f.close()
            self._done(self, self.path)
        finally:
            self._cleanup()
            super().close()

    def commit(self) -> None:
        """Put the file, when autocommit is off."""
        if self._dir is None:
            raise ValueError(f"{self.uri}: this write was already put or discarded")
        try:
            self._f.close()
            self._done(self, self.path)
        finally:
            self._cleanup()
            io.RawIOBase.close(self)

    def discard(self) -> None:
        """Close without writing to the store."""
        self._cleanup()
        io.RawIOBase.close(self)

    abandon = discard

    def __exit__(self, kind, err, tb) -> None:
        if kind is None:
            self.close()
        else:
            self.discard()

    def __del__(self) -> None:
        # A writer dropped without a close puts nothing.
        try:
            self.discard()
        except Exception:
            pass
