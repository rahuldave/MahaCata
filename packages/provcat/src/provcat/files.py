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

__all__ = ["CatalogReader", "CatalogWriter"]


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

    ``size`` is the size at open; a read never goes past it. ``ranges`` lists
    the byte ranges handed out so far, merged, as (start, end). A wrapper that
    reads ahead, such as io.TextIOWrapper, makes them larger than what its
    caller used."""

    _f = None

    def __init__(
        self,
        uri: Uri,  # The URI as opened, logical or physical
        pinned: Uri,  # The pinned physical URI that the handle reads
        path: Path,  # The local copy of that version in the cache
        block: Callable[..., Any],  # Called as block(start, length, read) for each read; it runs the read_block spot around read
        done: Callable[[CatalogReader], None],  # Called with the handle at close; it runs the close spot
    ):
        super().__init__()
        self.uri, self.pinned, self.path = uri, pinned, path
        self._f = path.open("rb")
        self._block, self._done = block, done
        self._ranges: list[tuple[int, int]] = []
        self.size = os.fstat(self._f.fileno()).st_size

    @property
    def ranges(self) -> list[tuple[int, int]]:
        """The byte ranges read so far, merged, as (start, end)."""
        return _merge(self._ranges)

    def readable(self) -> bool:  # Always True
        """A reader can be read."""
        return True

    def seekable(self) -> bool:  # Always True
        """A reader can seek."""
        return True

    def tell(self) -> int:  # The position, in bytes from the start
        """Return the position in the file."""
        return self._f.tell()

    def seek(
        self,
        offset: int,  # The new position, counted from whence
        whence: int = io.SEEK_SET,  # io.SEEK_SET, io.SEEK_CUR or io.SEEK_END
    ) -> int:  # The new position, in bytes from the start
        """Move to a position in the file."""
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

    def readinto(
        self,
        buf: bytearray | memoryview,  # A writable buffer to fill
    ) -> int:  # The number of bytes read; 0 at the end of the file
        """Read into buf, no further than the size at open. It runs the read_block spot."""
        view = memoryview(buf).cast("B")
        return self._read(len(view), lambda n: self._f.readinto(view[:n]))

    def readall(self) -> bytes:  # The bytes up to the size at open
        """Read to the end of the file. It runs the read_block spot."""
        return self._read(self.size, lambda n: self._f.read(n))

    def read(
        self,
        size: int | None = -1,  # The most bytes to read; -1 or None reads to the end
    ) -> bytes:  # The bytes read; empty at the end of the file
        """Read up to size bytes. It runs the read_block spot."""
        if size is None or size < 0:
            return self.readall()
        return self._read(size, lambda n: self._f.read(n))

    def readline(
        self,
        size: int | None = -1,  # The most bytes to read; -1 or None reads to the end of the line
    ) -> bytes:  # One line, with its newline when it has one
        """Read one line. It runs the read_block spot."""
        limit = self.size if size is None or size < 0 else size
        return self._read(limit, lambda n: self._f.readline(n))

    def close(self) -> None:  # Closes the handle; it runs the close spot with the ranges read
        """Close the handle, once; a second close does nothing."""
        if self.closed:
            return
        try:
            if self._f is not None:
                self._done(self)
        finally:
            if self._f is not None:
                self._f.close()
            super().close()

    def abandon(self) -> None:  # Closes the file; no spot runs
        """Close without the close spot, for a handle that its caller never got."""
        if self._f is not None:
            self._f.close()
        io.RawIOBase.close(self)


class CatalogWriter(io.RawIOBase):
    """A handle that writes a new file into a temporary file, and then puts it in the store.

    Close, or commit when autocommit is off, puts it at ``uri``; ``entries``
    then holds what the store wrote."""

    _f = None
    _dir = None

    def __init__(
        self,
        uri: Uri,  # The URI to write, as opened
        done: Callable[[CatalogWriter, Path], None],  # Called with the handle and the temporary file to put it; it runs the close spot and the put
        autocommit: bool = True,  # Put at close; off, put at commit()
    ):
        super().__init__()
        self.uri = uri
        self.autocommit = autocommit
        self._done = done
        self.entries: list = []
        self._dir = tempfile.mkdtemp(prefix="provcat-write-")
        self.path = Path(self._dir) / "data"
        self._f = self.path.open("wb")

    def writable(self) -> bool:  # Always True
        """A writer can be written."""
        return True

    def write(
        self,
        b: bytes | bytearray | memoryview,  # The bytes to add to the temporary file
    ) -> int:  # The number of bytes written
        """Write bytes to the temporary file; nothing reaches the store before the put."""
        self._checkClosed()
        return self._f.write(b)

    def tell(self) -> int:  # The number of bytes written so far
        """Return the position in the temporary file."""
        return self._f.tell()

    def _cleanup(self) -> None:
        if self._f is not None:
            self._f.close()
        if self._dir is not None:
            shutil.rmtree(self._dir, ignore_errors=True)
            self._dir = None

    def close(self) -> None:  # Closes the handle
        """Close the handle. With autocommit it puts the file through the close spot; without, commit() or discard() comes later."""
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

    def commit(self) -> None:  # Puts the file
        """Put the file, when autocommit is off. ValueError when it was already put or discarded."""
        if self._dir is None:
            raise ValueError(f"{self.uri}: this write was already put or discarded")
        try:
            self._f.close()
            self._done(self, self.path)
        finally:
            self._cleanup()
            io.RawIOBase.close(self)

    def discard(self) -> None:  # Deletes the temporary file
        """Close without writing to the store."""
        self._cleanup()
        io.RawIOBase.close(self)

    def abandon(self) -> None:  # Deletes the temporary file
        """Close without writing to the store, for a handle that its caller never got; the same as discard."""
        self.discard()

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
