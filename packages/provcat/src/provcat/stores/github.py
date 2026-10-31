"""GitHub repositories: ``github://owner/repo/path?at=branch:main``.

The selector names a git ref with ``branch:``, ``tag:`` or ``commit:``. Without a
selector the store uses the default branch of the repository.

``?at=ts:TIME`` names the commit that the default branch had at TIME, and
``?at=ts:BRANCH@TIME`` the commit that BRANCH had. The store walks the branch's
first parents from its head, and takes the first commit whose committer date
is at or before TIME. First parents are the commits of the branch itself, so a
commit made on another branch and merged later by a merge commit is left out.
Git records when a commit was made, not when it was pushed, so a commit made
on Monday and pushed on Wednesday counts from Monday. docs/design.md lists the
other cases where the answer differs from what the branch showed. A ``branch:`` must
be a branch and a ``tag:`` must be a tag; the store asks the git refs API, so a
tag of the same name cannot stand in for a branch.

Every fetch first pins the ref to a commit. The cache key holds the commit, so a
cached file never goes stale and needs no check::

    <cache>/github/<owner>/<repo>/<commit>/<path>

A URI with ``?at=commit:<40 hex digits>`` needs no network call when the cache
holds it. Pinning a branch or tag records the commit in the cache, so offline
mode can use the last commit it saw. Offline mode also serves a file inside a
folder that was fetched whole.

A single file comes from the contents API, which serves files up to 100 MB. A
folder comes from one tarball of the commit, which costs one API call however
many files it holds. The tarball unpacks only plain files and folders; links,
devices and names that climb with ``..`` are left out. A folder that holds two
names one disk could store as one, for example ``Guide.md`` and ``guide.md``, is
refused.

Git LFS: both the contents API and the tarball give the pointer file of a file
that Git LFS keeps, not its content. So after a download the store looks for
pointer files, which are under 1024 bytes, and asks the Git LFS batch API of
the repository for their content, 100 objects at a time. It checks the size and
the SHA-256 of each object against its pointer. The LFS server is on the web
host of GitHub, ``github.com`` for ``api.github.com``, and the token goes there
as well. The download links that the batch API gives carry their own headers,
and the token never goes to them. A repository that names another LFS server in
``.lfsconfig`` is not supported, and neither is a pointer that names Git LFS
extensions. A file that looks like a pointer but whose object the LFS server
does not have fails the fetch. ``list`` gives the size of the pointer of an LFS
file, because the contents API lists it so; ``stat`` gives the size of the
content.

Options:

- ``api_url``: the API root. The default is ``https://api.github.com``. Set it for
  GitHub Enterprise.
- ``token_env``: the environment variables to read a token from, in order. The
  default is ``[GITHUB_TOKEN, GH_TOKEN]``.
- ``use_gh_cli``: when no variable is set, run ``gh auth token`` for the host of
  ``api_url``. The default is true.
- ``timeout``: seconds for each request. The default is 60.

The token goes only to the host of ``api_url`` and, for Git LFS, to the web host
of the same GitHub. A redirect to another host, for example the tarball download
host, does not carry it.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tarfile
import tempfile
import urllib.error
import urllib.request
from pathlib import Path, PurePosixPath
from urllib.parse import quote, urlsplit

import base64
import functools
import hashlib
from datetime import UTC, datetime

from ..cache import LocalCache, find_twins
from ..errors import ConfigError, NotFound, OfflineMiss, StoreError, UriError
from ..timestamps import parse_ts
from ..uri import Selector, Uri
from . import Entry, Store, register_driver

_SHA = re.compile(r"[0-9a-f]{40}")
_REF_KINDS = ("branch", "tag", "commit")
_PAGE = 100
_MAX_COMMITS = 10_000  # a ts: search reads at most this many commits
_SETTLE = 600  # seconds; a ts: pin closer to now than this is not cached
_LFS_BATCH = 100  # objects in one request to the Git LFS batch API
_POINTER_MAX = 1024  # a Git LFS pointer file is smaller than this
_POINTER = re.compile(rb"version https://git-lfs\.github\.com/spec/v1\n"
                      rb"((?:ext-[0-9]-[a-z0-9]+ sha256:[0-9a-f]{64}\n)*)"
                      rb"oid sha256:([0-9a-f]{64})\nsize ([0-9]{1,19})\n")
_LFS_JSON = "application/vnd.git-lfs+json"
# The cache records this suffix with the commit. Files cached before the store
# read Git LFS content may be pointer files, and they do not have it, so they
# are fetched again.
_CACHE_TAG = "+lfs"


_LOOPBACK = ("127.0.0.1", "::1", "localhost")


def lfs_pointer(data: bytes) -> tuple[str, int, bool] | None:
    """The object ID and the size that a Git LFS pointer file names, and whether
    it names extensions, or None if data is not a pointer."""
    if len(data) >= _POINTER_MAX:
        return None
    m = _POINTER.fullmatch(data)
    return (m.group(2).decode(), int(m.group(3)), bool(m.group(1))) if m else None


class _DropAuthOnHostChange(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if urlsplit(newurl).scheme not in ("http", "https"):
            return None  # urllib would follow ftp: too; the 3xx then stays an error
        new = super().redirect_request(req, fp, code, msg, headers, newurl)
        old_u, new_u = urlsplit(req.full_url), urlsplit(newurl)
        if new is not None and (new_u.scheme, new_u.netloc) != (old_u.scheme, old_u.netloc):
            for name in list(new.headers):
                if name.lower() == "authorization":
                    del new.headers[name]
            new.unredirected_hdrs.pop("Authorization", None)
        return new


_OPENER = urllib.request.build_opener(_DropAuthOnHostChange)


def _reply_guard(fn):
    """Turn a reply of an unexpected shape into a StoreError."""

    @functools.wraps(fn)
    def run(self, uri, *args):
        try:
            return fn(self, uri, *args)
        except (KeyError, TypeError, AttributeError, IndexError) as e:
            raise StoreError(f"{uri}: GitHub sent a reply of an unexpected shape ({e!r})") from e

    return run


@register_driver
class GithubStore(Store):
    driver = "github"
    fetch_pins = True  # fetch pins a branch first, so the catalog pins it before the hooks at fetch
    ts_names_branch = True  # ts:BRANCH@TIME; see Catalog.resolve

    def option_types(self):
        return {"api_url": str, "token_env": list, "use_gh_cli": bool, "timeout": (int, float)}

    def __init__(self, *args, **kw):
        super().__init__(*args, **kw)
        self.api = self.options.get("api_url", "https://api.github.com").rstrip("/")
        self.timeout = float(self.options.get("timeout", 60))
        if self.timeout <= 0:
            raise ConfigError(f"stores.{self.name}.timeout must be more than 0")
        self._token: str | None | bool = False  # False means not looked up yet

    # requests

    def _gh_host(self) -> str:
        host = urlsplit(self.api).hostname or "github.com"
        return "github.com" if host == "api.github.com" else host

    @property
    def token(self) -> str | None:
        if self._token is False:
            names = self.options.get("token_env", ["GITHUB_TOKEN", "GH_TOKEN"])
            self._token = next((os.environ[n] for n in names if os.environ.get(n)), None)
            if self._token is None and self.options.get("use_gh_cli", True) and shutil.which("gh"):
                try:
                    out = subprocess.run(["gh", "auth", "token", "--hostname", self._gh_host()],
                                         capture_output=True, text=True, timeout=10)
                    self._token = (out.stdout.strip() or None) if out.returncode == 0 else None
                except (OSError, subprocess.SubprocessError):
                    self._token = None
        return self._token or None

    def _open(self, uri: Uri, path: str, accept: str):
        headers = {"Accept": accept, "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "provcat"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        req = urllib.request.Request(self.api + path, headers=headers)
        try:
            return _OPENER.open(req, timeout=self.timeout)
        except urllib.error.HTTPError as e:
            if e.code == 404:
                raise NotFound(f"{uri}: not found on GitHub (or no access)") from e
            detail = ""
            try:
                detail = json.loads(e.read() or b"{}").get("message", "")
            except (ValueError, OSError, AttributeError):
                pass
            hint = ""
            if e.code in (401, 403, 429) and "too large" not in detail.lower():
                hint = (". Set GITHUB_TOKEN, or log in with gh, if the repository is private "
                        "or the rate limit is used up")
            raise StoreError(f"{uri}: GitHub said {e.code} {e.reason}" + (f": {detail}" if detail else "") + hint) from e
        except (urllib.error.URLError, OSError) as e:
            raise StoreError(f"{uri}: cannot reach {self.api}: {e}") from e

    def _json(self, uri: Uri, path: str):
        try:
            with self._open(uri, path, "application/vnd.github+json") as r:
                return json.loads(r.read())
        except ValueError as e:
            raise StoreError(f"{uri}: GitHub sent a reply that is not JSON: {e}") from e

    def _web(self) -> str:
        """The web root of this GitHub: https://github.com for api.github.com,
        https://SUB.ghe.com for api.SUB.ghe.com, and https://HOST for GitHub
        Enterprise Server at https://HOST/api/v3."""
        u = urlsplit(self.api)
        netloc = u.netloc[len("api."):] if u.netloc.startswith("api.") else u.netloc
        path = u.path.rstrip("/")
        path = path[: -len("/api/v3")] if path.endswith("/api/v3") else path
        return f"{u.scheme}://{netloc}{path}"

    def _lfs_batch(self, pinned: Uri, owner: str, repo: str, objects: list[dict]) -> list[dict]:
        url = f"{self._web()}/{quote(owner, safe='')}/{quote(repo, safe='')}.git/info/lfs/objects/batch"
        body = json.dumps({"operation": "download", "transfers": ["basic"], "objects": objects,
                           "hash_algo": "sha256"}).encode()
        headers = {"Accept": _LFS_JSON, "Content-Type": _LFS_JSON, "User-Agent": "provcat"}
        if self.token:
            headers["Authorization"] = "Basic " + base64.b64encode(f"x-access-token:{self.token}".encode()).decode()
        req = urllib.request.Request(url, data=body, headers=headers, method="POST")
        try:
            with _OPENER.open(req, timeout=self.timeout) as r:
                out = json.loads(r.read())
        except urllib.error.HTTPError as e:
            hint = ". Set GITHUB_TOKEN, or log in with gh, if the repository is private" if e.code in (401, 403, 404) else ""
            raise StoreError(f"{pinned}: the Git LFS server said {e.code} {e.reason}{hint}") from e
        except (ValueError, RecursionError) as e:
            raise StoreError(f"{pinned}: the Git LFS server sent a reply that is not JSON: {e}") from e
        except (urllib.error.URLError, OSError) as e:
            raise StoreError(f"{pinned}: cannot reach the Git LFS server of {self._web()}: {e}") from e
        if out.get("transfer", "basic") != "basic":
            raise StoreError(f"{pinned}: the Git LFS server chose the transfer {out.get('transfer')!r}, not basic")
        return out["objects"]

    def _lfs_download(self, pinned: Uri, obj: dict, size: int, oid: str, dest: Path) -> None:
        """Write the object to dest, and check its size and SHA-256."""
        action = (obj.get("actions") or {}).get("download")
        if not action or not action.get("href"):
            raise StoreError(f"{pinned}: the Git LFS server gave no download for object {oid}")
        href = action["href"]
        try:
            u = urlsplit(href)
            plain_ok = u.scheme == "http" and u.hostname in _LOOPBACK  # the test servers
        except ValueError as e:
            raise StoreError(f"{pinned}: the Git LFS server gave a download link that is not a URL") from e
        if u.scheme != "https" and not plain_ok:
            raise StoreError(f"{pinned}: the Git LFS server gave a download link that is not HTTPS")
        headers = {str(k): str(v) for k, v in (action.get("header") or {}).items()}
        headers.setdefault("User-Agent", "provcat")
        digest, got = hashlib.sha256(), 0
        try:
            with _OPENER.open(urllib.request.Request(href, headers=headers), timeout=self.timeout) as r, \
                    dest.open("wb") as f:
                while chunk := r.read(1 << 20):
                    got += len(chunk)
                    if got > size:
                        break
                    digest.update(chunk)
                    f.write(chunk)
        except urllib.error.HTTPError as e:
            raise StoreError(f"{pinned}: the Git LFS download of object {oid} failed: {e.code} {e.reason}") from e
        except (urllib.error.URLError, OSError, ValueError) as e:  # ValueError: a bad header or URL
            raise StoreError(f"{pinned}: the Git LFS download of object {oid} failed: {e}") from e
        if got != size or digest.hexdigest() != oid:
            raise StoreError(f"{pinned}: Git LFS object {oid} came back with other content than its pointer names")

    @_reply_guard
    def _lfs_fill(self, pinned: Uri, files: list[Path], scratch: Path) -> None:
        """Replace each Git LFS pointer file among files with its content. Each
        download goes to a temporary file in scratch, which must be outside the
        folder being built and on the same disk."""
        owner, repo, _ = self.locate(pinned)
        wanted: dict[tuple[str, int], list[Path]] = {}
        for p in files:
            try:
                if p.stat().st_size >= _POINTER_MAX:
                    continue
                ptr = lfs_pointer(p.read_bytes())
            except OSError as e:
                raise StoreError(f"{pinned}: cannot read {p.name} in the cache: {e}") from e
            if ptr and ptr[2]:
                raise StoreError(f"{pinned}: {p.name} is a Git LFS pointer that names extensions, "
                                 "which provcat cannot apply")
            if ptr:
                wanted.setdefault(ptr[:2], []).append(p)
        todo = list(wanted)
        for i in range(0, len(todo), _LFS_BATCH):
            part = todo[i: i + _LFS_BATCH]
            replies = self._lfs_batch(pinned, owner, repo, [{"oid": o, "size": n} for o, n in part])
            by_oid = {r.get("oid"): r for r in replies if isinstance(r, dict)}
            for oid, size in part:
                obj = by_oid.get(oid)
                if obj is None:
                    raise StoreError(f"{pinned}: the Git LFS server did not answer for object {oid}")
                if obj.get("error"):
                    err = obj["error"]
                    raise StoreError(f"{pinned}: the Git LFS server has no object {oid} "
                                     f"({err.get('code')}: {err.get('message')})")
                first, *rest = wanted[(oid, size)]
                try:
                    fd, name = tempfile.mkstemp(dir=scratch, prefix="lfs-")
                    os.close(fd)
                except OSError as e:
                    raise StoreError(f"{pinned}: cannot write Git LFS content in the cache: {e}") from e
                part_file = Path(name)
                try:
                    self._lfs_download(pinned, obj, size, oid, part_file)
                    for p in rest:
                        shutil.copyfile(part_file, p)
                    os.replace(part_file, first)
                except OSError as e:
                    raise StoreError(f"{pinned}: cannot write Git LFS content in the cache: {e}") from e
                finally:
                    try:
                        part_file.unlink(missing_ok=True)
                    except OSError:
                        pass

    # URIs

    def locate(self, uri: Uri) -> tuple[str, str, str]:
        if not uri.authority or not uri.segments:
            raise UriError(f"{uri}: write github://owner/repo/path")
        sel = uri.selector
        if sel is not None and sel.kind not in (*_REF_KINDS, "ts"):
            raise UriError(f"{uri}: GitHub selectors are branch:, tag:, commit: or ts:")
        return uri.authority, uri.segments[0], "/".join(uri.segments[1:])

    def _repo_path(self, owner: str, repo: str) -> str:
        return f"/repos/{quote(owner, safe='')}/{quote(repo, safe='')}"

    def _commit_of_ref(self, uri: Uri, base: str, kind: str, name: str) -> str:
        """Follow refs/heads/<name> or refs/tags/<name> to a commit."""
        out = self._json(uri, f"{base}/git/ref/{'heads' if kind == 'branch' else 'tags'}/{quote(name, safe='/')}")
        if isinstance(out, list) or not isinstance(out, dict):
            raise NotFound(f"{uri}: no {kind} named {name}")  # the API lists prefix matches
        obj = out.get("object") or {}
        for _ in range(5):  # an annotated tag points at a tag object
            if obj.get("type") == "commit":
                return obj.get("sha", "")
            if obj.get("type") != "tag":
                raise StoreError(f"{uri}: {kind} {name} points at a {obj.get('type')}, not a commit")
            obj = (self._json(uri, f"{base}/git/tags/{obj.get('sha')}") or {}).get("object") or {}
        raise StoreError(f"{uri}: tag {name} is nested too deep")

    def _commit_at(self, uri: Uri, base: str, head: str, branch: str, moment: datetime) -> str:
        """The first commit on the first-parent line from head whose committer
        date is at or before moment."""
        seen: dict[str, tuple[datetime, str | None]] = {}
        page, more = 1, True

        def learn(c: dict) -> None:
            try:
                made = datetime.fromisoformat(c["commit"]["committer"]["date"].replace("z", "Z"))
            except ValueError:
                raise StoreError(f"{uri}: GitHub gave commit {c.get('sha')} a date that is not a date") from None
            if made.tzinfo is None:
                raise StoreError(f"{uri}: GitHub gave commit {c.get('sha')} a date without a zone")
            parents = c.get("parents") or []
            seen[c["sha"]] = (made, parents[0]["sha"] if parents else None)

        sha: str | None = head
        # Count both the commits read and the steps taken, so a long merged
        # side branch, or a reply whose parents loop, still ends the search.
        for _ in range(_MAX_COMMITS + 1):
            while sha not in seen and more and len(seen) <= _MAX_COMMITS:
                # One listing of the history from head, newest first, page by page.
                out = self._json(uri, f"{base}/commits?sha={head}&per_page={_PAGE}&page={page}")
                for c in out:
                    learn(c)
                more, page = len(out) == _PAGE, page + 1
            if sha not in seen and len(seen) <= _MAX_COMMITS:
                learn(self._json(uri, f"{base}/commits/{sha}"))  # not in the listing; ask for it alone
            if sha not in seen and len(seen) > _MAX_COMMITS:
                break
            if sha not in seen:
                raise StoreError(f"{uri}: GitHub did not return commit {sha}")
            made, parent = seen[sha]
            if made <= moment:
                return sha
            if parent is None:
                raise NotFound(f"{uri}: {branch} has no commit at or before {moment.isoformat()}")
            sha = parent
        raise StoreError(f"{uri}: read {_MAX_COMMITS} commits of {branch} without reaching "
                         f"{moment.isoformat()}; use commit: or a stage")

    @_reply_guard
    def pin(self, uri: Uri, cache: LocalCache) -> Uri:
        owner, repo, _ = self.locate(uri)
        sel = uri.selector
        if sel is not None and sel.kind == "commit" and _SHA.fullmatch(sel.value):
            return uri
        refkey = [owner, repo, str(sel) if sel else "default"]
        remember = True
        if sel is not None and sel.kind == "ts":
            branch, moment = _split_ts(uri, sel.value)
            now = datetime.now(UTC)
            if moment > now:
                raise UriError(f"{uri}: {sel} is in the future, so its commit could still change")
            # A push can still add a commit at or before a recent time, so a
            # recent time is answered but not remembered for --offline.
            remember = (now - moment).total_seconds() > _SETTLE
        if self.offline:
            sha = cache.get_ref(self.name, refkey)
            if sha is None:
                raise OfflineMiss(f"{uri}: offline, and {sel or 'the default branch'} was never pinned")
        else:
            base = self._repo_path(owner, repo)
            if sel is None:
                sha = self._commit_of_ref(uri, base, "branch", self._json(uri, base)["default_branch"])
            elif sel.kind == "ts":
                branch = branch or self._json(uri, base)["default_branch"]
                sha = self._commit_at(uri, base, self._commit_of_ref(uri, base, "branch", branch), branch, moment)
            elif sel.kind == "commit":
                with self._open(uri, f"{base}/commits/{quote(sel.value, safe='')}", "application/vnd.github.sha") as r:
                    sha = r.read().decode(errors="replace").strip()
            else:
                sha = self._commit_of_ref(uri, base, sel.kind, sel.value)
            if not _SHA.fullmatch(sha):
                raise StoreError(f"{uri}: GitHub gave {sha[:60]!r}, not a commit")
            if remember:
                cache.set_ref(self.name, refkey, sha)
        return uri.with_selector(Selector("commit", sha))

    def _key(self, pinned: Uri) -> list[str]:
        return [pinned.authority, *pinned.segments[:1], pinned.selector.value, *pinned.segments[1:]]

    def _cached(self, pinned: Uri, cache: LocalCache) -> Path | None:
        key, version = self._key(pinned), pinned.selector.value + _CACHE_TAG
        return cache.get(self.name, key, version) or cache.covered(self.name, key, version, min_len=3)

    # Store interface

    def stat(self, uri: Uri, cache: LocalCache) -> Entry:
        pinned = self.pin(uri, cache)
        if self.offline:
            hit = self._cached(pinned, cache)
            if hit is None:
                raise OfflineMiss(f"{uri}: commit {pinned.selector.value} is not in the cache")
            sha = pinned.selector.value
            return Entry(pinned, "folder", None, sha) if hit.is_dir() else Entry(pinned, "file", hit.stat().st_size, sha)
        return self.stat_pinned(pinned)

    @_reply_guard
    def stat_pinned(self, pinned: Uri) -> Entry:
        owner, repo, path = self.locate(pinned)
        sha = pinned.selector.value
        if not path:
            return Entry(pinned, "folder", None, sha)
        out = self._json(pinned, f"{self._repo_path(owner, repo)}/contents/{quote(path)}?ref={sha}")
        if isinstance(out, list):
            return Entry(pinned, "folder", None, sha)
        if not isinstance(out, dict) or out.get("type") != "file":
            raise StoreError(f"{pinned}: is a {out.get('type') if isinstance(out, dict) else 'thing'}, not a file or folder")
        return Entry(pinned, "file", out.get("size"), sha)

    def list(self, uri: Uri, cache: LocalCache) -> list[Entry]:
        pinned = self.pin(uri, cache)
        if self.offline:
            raise OfflineMiss(f"{uri}: offline, and only fetch and stat can use the cache")
        return self.list_pinned(pinned)

    @_reply_guard
    def list_pinned(self, pinned: Uri) -> list[Entry]:
        owner, repo, path = self.locate(pinned)
        sha = pinned.selector.value
        out = self._json(pinned, f"{self._repo_path(owner, repo)}/contents/{quote(path)}?ref={sha}")
        if not isinstance(out, list):
            raise NotFound(f"{pinned}: is not a folder")
        base = pinned.without_selector()
        entries = []
        for item in out:
            kind = {"file": "file", "dir": "folder"}.get(item.get("type"))
            if not kind:
                continue
            try:
                child = base.child(item["name"]).with_selector(pinned.selector)
            except (UriError, KeyError):
                continue
            entries.append(Entry(child, kind, item.get("size") if kind == "file" else None, sha))
        return entries

    def fetch(self, uri: Uri, cache: LocalCache) -> Path:
        pinned = self.pin(uri, cache)
        hit = self._cached(pinned, cache)
        if hit is not None:
            return hit
        key, sha = self._key(pinned), pinned.selector.value
        if self.offline:
            raise OfflineMiss(f"{uri}: commit {sha} is not in the cache")
        entry = self.stat_pinned(pinned)
        if entry.kind == "file":
            return self._download_file(pinned, key, cache)

        def fill(tmp: Path) -> None:
            self._extract(pinned, tmp)
            self._lfs_fill(pinned, [p for p in tmp.rglob("*") if p.is_file()], tmp.parent)

        return cache.install_folder(self.name, key, sha + _CACHE_TAG, fill)

    def _download_file(self, pinned: Uri, key: list[str], cache: LocalCache) -> Path:
        owner, repo, path = self.locate(pinned)
        sha = pinned.selector.value

        def fill(tmp: Path) -> str:
            with self._open(pinned, f"{self._repo_path(owner, repo)}/contents/{quote(path)}?ref={sha}",
                            "application/vnd.github.raw") as r, tmp.open("wb") as f:
                shutil.copyfileobj(r, f, 1 << 20)
            self._lfs_fill(pinned, [tmp], tmp.parent)
            return sha + _CACHE_TAG

        return cache.put(self.name, key, fill)

    def _extract(self, pinned: Uri, dest: Path) -> None:
        """Unpack the folder named by pinned from the commit tarball into dest."""
        owner, repo, path = self.locate(pinned)
        sha = pinned.selector.value
        want = tuple(path.split("/")) if path else ()
        with tempfile.TemporaryFile() as raw:
            with self._open(pinned, f"{self._repo_path(owner, repo)}/tarball/{sha}", "application/vnd.github+json") as r:
                shutil.copyfileobj(r, raw, 1 << 20)
            raw.seek(0)
            try:
                with tarfile.open(fileobj=raw, mode="r:*") as tar:
                    members = []
                    for m in tar.getmembers():
                        parts = PurePosixPath(m.name).parts[1:]  # drop the top folder, owner-repo-sha
                        if len(parts) <= len(want) or parts[: len(want)] != want:
                            continue
                        rel = parts[len(want):]
                        if not (m.isfile() or m.isdir()) or any(p in ("..", ".", "") or "\\" in p for p in rel):
                            continue  # links, devices and climbing names
                        m.name = "/".join(rel)
                        members.append(m)
                    twins = find_twins(tuple(m.name.split("/")) for m in members)
                    if twins:
                        raise StoreError(f"{pinned}: {twins[0]!r} and {twins[1]!r} would be one name on a "
                                         "disk that ignores case or Unicode form; fetch the files one by one")
                    tar.extractall(dest, members=members, filter="data")
            except (tarfile.TarError, OSError) as e:
                raise StoreError(f"{pinned}: cannot unpack the tarball: {e}") from e


def _split_ts(uri: Uri, value: str) -> tuple[str, datetime]:
    """The branch and the time of ts:TIME or ts:BRANCH@TIME. A time never holds
    an "@", so the last one ends the branch."""
    branch, _, text = value.rpartition("@")
    if "@" in value and not branch:
        raise UriError(f"{uri}: write ts:BRANCH@TIME, or ts:TIME for the default branch")
    return branch, parse_ts(text)
