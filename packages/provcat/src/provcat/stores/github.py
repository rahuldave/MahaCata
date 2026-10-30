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

Options:

- ``api_url``: the API root. The default is ``https://api.github.com``. Set it for
  GitHub Enterprise.
- ``token_env``: the environment variables to read a token from, in order. The
  default is ``[GITHUB_TOKEN, GH_TOKEN]``.
- ``use_gh_cli``: when no variable is set, run ``gh auth token`` for the host of
  ``api_url``. The default is true.
- ``timeout``: seconds for each request. The default is 60.

The token goes only to the host of ``api_url``. A redirect to another host, for
example the tarball download host, does not carry it.
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

import functools
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


class _DropAuthOnHostChange(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
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
        key, sha = self._key(pinned), pinned.selector.value
        return cache.get(self.name, key, sha) or cache.covered(self.name, key, sha, min_len=3)

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
        return cache.install_folder(self.name, key, sha, lambda tmp: self._extract(pinned, tmp))

    def _download_file(self, pinned: Uri, key: list[str], cache: LocalCache) -> Path:
        owner, repo, path = self.locate(pinned)
        sha = pinned.selector.value

        def fill(tmp: Path) -> str:
            with self._open(pinned, f"{self._repo_path(owner, repo)}/contents/{quote(path)}?ref={sha}",
                            "application/vnd.github.raw") as r, tmp.open("wb") as f:
                shutil.copyfileobj(r, f, 1 << 20)
            return sha

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
