"""Hugging Face repositories: ``hf://datasets/owner/repo/path?at=branch:main``.

It needs the ``hf`` extra: ``pip install provcat[hf]``.

The authority is the kind of repository: ``models``, ``datasets`` or ``spaces``,
as in the paths of ``HfFileSystem`` and the Hub API. The next two segments are the owner and
the name of the repository, and the rest is the path in it.

The selector names a git ref with ``branch:``, ``tag:`` or ``commit:``. Without a
selector the store uses ``main``, the default branch of every Hugging Face
repository. ``?at=ts:TIME`` names the newest commit of ``main`` whose date is at
or before TIME, and ``?at=ts:BRANCH@TIME`` that of BRANCH. The Hub lists the
commits of a branch newest first, but it does not give their parents. So a
commit that someone made on another branch and merged with a merge commit
counts too. The Hub itself merges a pull request as one new commit, so this
matters only for history that someone pushed with git.

Every read first pins the ref to a commit. The cache key holds the commit::

    <cache>/hf/<kind>/<owner>/<repo>/<commit>/<path>

The store downloads each file with the functions that ``hf_hub_download`` of
``huggingface_hub`` uses, which also read files that Xet keeps. It writes the
file only into the provcat cache, not into the cache of ``huggingface_hub``,
and it keeps no listings in memory. It checks each file it downloads: a file
that Xet or Git LFS keeps against the SHA-256 that the Hub gives, and any other
file against its git blob ID. A folder comes file by file, from one listing of
the folder at the commit.

The store cannot write.

Options:

- ``endpoint``: the Hub root. The default comes from ``huggingface_hub``, which
  reads ``HF_ENDPOINT`` and otherwise uses ``https://huggingface.co``. The token
  goes to it, so it must use HTTPS.
- ``token_env``: the environment variables to read a token from, in order. If
  none is set, the store sends no token. Without this option,
  ``huggingface_hub`` finds the token itself: in ``HF_TOKEN``, or in the file
  that ``hf auth login`` writes.
- ``timeout``: seconds for each call to the Hub API. The default is 60. A
  download of a file over plain HTTP waits ``HF_HUB_DOWNLOAD_TIMEOUT`` seconds
  instead, 10 by default, and a Xet download has its own timeouts.
"""
from __future__ import annotations

import contextlib
import hashlib
import math
import os
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import quote, urlsplit

from ..cache import LocalCache, find_twins
from ..errors import ConfigError, NotFound, OfflineMiss, StoreError, UriError
from ..uri import Selector, Uri
from . import Entry, Store, register_driver
from .github import _split_ts

_SHA = re.compile(r"[0-9a-f]{40}")
_SHORT_SHA = re.compile(r"[0-9a-f]{7,40}")
_KINDS = {"models": "model", "datasets": "dataset", "spaces": "space"}
_REF_KINDS = ("branch", "tag", "commit")
_PAGE = 100
_MAX_COMMITS = 10_000  # a ts: search reads at most this many commits
_SETTLE = 600  # seconds; a ts: pin closer to now than this is not cached
_LOOPBACK = ("127.0.0.1", "::1", "localhost")
_NAME = re.compile(r"[A-Za-z0-9_.-]{1,96}")  # an owner or a repository name


@dataclass(frozen=True)
class Item:
    """A file or a folder that the Hub lists. path is the path in the repository."""

    path: str
    kind: str  # "file" or "folder"
    size: int | None = None
    sha256: str | None = None  # of the content, for a file that Xet or Git LFS keeps
    blob_id: str | None = None  # the git blob ID, for any file


def _sdk():
    try:
        import huggingface_hub
    except ImportError:
        raise StoreError("the hf driver needs the hf extra: pip install 'provcat[hf]'") from None
    return huggingface_hub


class Hub:
    """The calls that the store makes to the Hub. Tests put a fake in its place.

    Each call goes to the Hub API through ``http_backoff`` of ``huggingface_hub``,
    which tries again after a 429, a 5xx or a network error, with a timeout. A
    download uses the functions under ``hf_hub_download``, Xet when hf_xet is
    there and plain HTTP when not, and writes only to the file that the store
    names.
    """

    def __init__(self, endpoint: str | None, token: str | bool | None, timeout: float = 60):
        _sdk()
        from huggingface_hub import constants

        self.endpoint = (endpoint or constants.ENDPOINT).rstrip("/")
        try:
            u = urlsplit(self.endpoint)
            u.port  # noqa: B018  (raises on a port that is not a number)
        except ValueError as e:
            raise ConfigError(f"the Hugging Face endpoint {self.endpoint} is not a URL: {e}") from e
        if not u.hostname or (u.scheme != "https" and not (u.scheme == "http" and u.hostname in _LOOPBACK)):
            raise ConfigError(f"the Hugging Face endpoint {self.endpoint} must use HTTPS, "
                              "because the token goes to it")
        self._origin = (u.scheme, u.netloc)
        self.token = token
        self.timeout = timeout

    @contextlib.contextmanager
    def errors(self, uri: Uri):
        from huggingface_hub import errors

        try:
            yield
        except errors.GatedRepoError as e:
            raise StoreError(f"{uri}: the repository is gated. Accept its terms on the Hub, "
                             "and set HF_TOKEN or log in with hf auth login") from e
        except (errors.RepositoryNotFoundError, errors.RevisionNotFoundError, errors.EntryNotFoundError) as e:
            raise NotFound(f"{uri}: not found on the Hub (or no access)") from e
        except errors.OfflineModeIsEnabled as e:
            raise StoreError(f"{uri}: HF_HUB_OFFLINE is set, so huggingface_hub makes no calls. "
                             "Unset it, or use provcat's --offline") from e
        except errors.HfHubHTTPError as e:
            code = getattr(getattr(e, "response", None), "status_code", None)
            hint = ". Set HF_TOKEN, or log in with hf auth login, if the repository is private" \
                if code in (401, 403) else ""
            raise StoreError(f"{uri}: the Hub said {code}: {_first_line(e)}{hint}") from e
        except errors.HFValidationError as e:
            raise UriError(f"{uri}: not a Hugging Face repository name: {_first_line(e)}") from e
        # HTTPError: a network error of the HTTP client under huggingface_hub.
        # RuntimeError: what hf_xet raises when a Xet download fails.
        except (OSError, ValueError, errors.HTTPError, RuntimeError) as e:
            raise StoreError(f"{uri}: cannot read from the Hub: {_first_line(e)}") from e
        except (KeyError, TypeError, AttributeError, IndexError) as e:
            raise StoreError(f"{uri}: the Hub sent a reply of an unexpected shape ({e!r})") from e
        except BaseException as e:
            if type(e).__name__ == "PanicException":  # a Rust panic in hf_xet
                raise StoreError(f"{uri}: the Xet download failed: {_first_line(e)}") from e
            raise

    def _api(self, kind: str, repo: str, rest: str) -> str:
        return f"{self.endpoint}/api/{kind}s/{quote(repo)}/{rest}"

    def _call(self, method: str, url: str, **kw):
        from huggingface_hub.utils import build_hf_headers, hf_raise_for_status, http_backoff

        u = urlsplit(url)
        if (u.scheme, u.netloc) != self._origin:  # a page link names its own URL
            raise StoreError(f"the Hub gave the link {url}, which is not on {self.endpoint}")
        r = http_backoff(method, url, headers=build_hf_headers(token=self.token), timeout=self.timeout, **kw)
        hf_raise_for_status(r)
        return r

    def refs(self, kind: str, repo: str) -> tuple[dict[str, str], dict[str, str]]:
        """The branches and the tags, each a map from name to commit."""
        out = self._call("GET", self._api(kind, repo, "refs")).json()
        return ({b["name"]: b["targetCommit"] for b in out["branches"]},
                {t["name"]: t["targetCommit"] for t in out["tags"]})

    def commit_of(self, kind: str, repo: str, revision: str) -> str:
        return self._call("GET", self._api(kind, repo, f"revision/{quote(revision, safe='')}"),
                          params={"expand": "sha"}).json()["sha"]  # not the list of every file

    def commits(self, kind: str, repo: str, branch: str, page: int) -> list[tuple[str, datetime]]:
        """One page of the commits of branch, newest first, with their dates."""
        url = self._api(kind, repo, f"commits/{quote(branch, safe='')}")
        out = self._call("GET", url, params={"p": page, "limit": _PAGE}).json()
        return [(c["id"], _date(c["date"])) for c in out]

    def info(self, kind: str, repo: str, commit: str, path: str) -> Item | None:
        url = self._api(kind, repo, f"paths-info/{commit}")
        got = self._call("POST", url, data={"paths": [path], "expand": False}).json()
        return _item(got[0]) if got else None

    def tree(self, kind: str, repo: str, commit: str, path: str, recursive: bool) -> list[Item]:
        url = self._api(kind, repo, f"tree/{commit}" + (f"/{quote(path)}" if path else ""))
        r = self._call("GET", url, params={"recursive": recursive, "expand": False})
        items = [_item(x) for x in r.json()]
        while (url := r.links.get("next", {}).get("url")):  # the Hub gives pages as GitHub does
            r = self._call("GET", url)
            items += [_item(x) for x in r.json()]
        return items

    def download(self, kind: str, repo: str, commit: str, item: Item, dest: Path) -> None:
        from huggingface_hub import file_download as fd
        from huggingface_hub.utils import build_hf_headers

        url = fd.hf_hub_url(repo, item.path, repo_type=kind, revision=commit, endpoint=self.endpoint)
        meta = fd.get_hf_file_metadata(url, token=self.token, timeout=self.timeout, endpoint=self.endpoint,
                                       retry_on_errors=True)
        headers = build_hf_headers(token=self.token)
        if meta.xet_file_data is not None and fd.is_xet_available():
            fd.xet_get(incomplete_path=dest, xet_file_data=meta.xet_file_data, headers=headers,
                       expected_size=item.size, displayed_filename=item.path)
        else:
            with dest.open("wb") as f:
                fd.http_get(url, f, headers=headers, expected_size=item.size, displayed_filename=item.path)


def _first_line(e: BaseException) -> str:
    return (str(e).strip().splitlines() or [type(e).__name__])[0]


def _date(text: str) -> datetime:
    made = datetime.fromisoformat(text.replace("z", "Z"))
    if made.tzinfo is None:
        raise ValueError(f"a date without a zone: {text!r}")
    return made


def _item(x: dict) -> Item:
    """An Item from one entry of a Hub listing."""
    if x["type"] == "directory":
        return Item(x["path"], "folder")
    if x["type"] != "file":
        raise StoreError(f"the Hub listed {x['path']!r} as a {x['type']!r}, not a file or a folder")
    lfs = x.get("lfs") or {}
    return Item(x["path"], "file", x["size"], lfs.get("oid"), x["oid"])


def _check(pinned: Uri, item: Item, p: Path) -> None:
    """Refuse a downloaded file whose bytes are not the ones that the Hub listed."""
    try:
        size = p.stat().st_size
        if item.size is not None and size != item.size:
            raise StoreError(f"{pinned}: {item.path} came back with {size} bytes, not {item.size}")
        if item.sha256:
            want, got = item.sha256, _hash(p, hashlib.sha256())
        elif item.blob_id:
            want, got = item.blob_id, _hash(p, hashlib.sha1(f"blob {size}\0".encode()))
        else:
            raise StoreError(f"{pinned}: the Hub gave no hash for {item.path}, so it cannot be checked")
    except OSError as e:
        raise StoreError(f"{pinned}: cannot read {item.path} in the cache: {e}") from e
    if got != want:
        raise StoreError(f"{pinned}: {item.path} came back with other content than the Hub lists")


def _hash(p: Path, h) -> str:
    with p.open("rb") as f:
        while chunk := f.read(1 << 20):
            h.update(chunk)
    return h.hexdigest()


@register_driver
class HfStore(Store):
    driver = "hf"
    ts_names_branch = True  # ts:BRANCH@TIME; see Catalog.resolve

    def option_types(self):
        return {"endpoint": str, "token_env": list, "timeout": (int, float)}

    def __init__(self, *args, **kw):
        super().__init__(*args, **kw)
        self.timeout = float(self.options.get("timeout", 60))
        if not (math.isfinite(self.timeout) and self.timeout > 0):
            raise ConfigError(f"stores.{self.name}.timeout must be a number more than 0")
        self._hub: Hub | None = None

    @property
    def hub(self) -> Hub:
        if self._hub is None:
            token: str | bool | None = None
            if "token_env" in self.options:
                token = next((os.environ[n] for n in self.options["token_env"] if os.environ.get(n)), False)
            self._hub = Hub(self.options.get("endpoint"), token, self.timeout)
        return self._hub

    @hub.setter
    def hub(self, value) -> None:
        self._hub = value

    # URIs

    def locate(self, uri: Uri) -> tuple[str, str, str]:
        """The kind of repository, owner/name, and the path in it."""
        kind = _KINDS.get(uri.authority)
        if kind is None or len(uri.segments) < 2:
            raise UriError(f"{uri}: write hf://KIND/owner/repo/path, where KIND is models, datasets or spaces")
        if not all(_NAME.fullmatch(n) for n in uri.segments[:2]):
            raise UriError(f"{uri}: an owner or a repository name on the Hub has only letters, digits, -, _ and .")
        sel = uri.selector
        if sel is not None and sel.kind not in (*_REF_KINDS, "ts"):
            raise UriError(f"{uri}: Hugging Face selectors are branch:, tag:, commit: or ts:")
        return kind, f"{uri.segments[0]}/{uri.segments[1]}", "/".join(uri.segments[2:])

    def pin(self, uri: Uri, cache: LocalCache) -> Uri:
        kind, repo, _ = self.locate(uri)
        sel = uri.selector
        if sel is not None and sel.kind == "commit" and _SHA.fullmatch(sel.value):
            return uri
        refkey = [kind, repo, str(sel) if sel else "default"]
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
            with self.hub.errors(uri):
                sha = self._resolve(uri, kind, repo, sel)
            if not isinstance(sha, str) or not _SHA.fullmatch(sha):
                raise StoreError(f"{uri}: the Hub gave {str(sha)[:60]!r}, not a commit")
            if remember:
                cache.set_ref(self.name, refkey, sha)
        return uri.with_selector(Selector("commit", sha))

    def _resolve(self, uri: Uri, kind: str, repo: str, sel: Selector | None) -> str:
        if sel is None:
            sel = Selector("branch", "main")
        if sel.kind == "commit":
            if not _SHORT_SHA.fullmatch(sel.value):
                raise UriError(f"{uri}: a commit is 7 to 40 hex digits")
            # The Hub would also take a branch or a tag here, so check the answer.
            sha = self.hub.commit_of(kind, repo, sel.value)
            if not str(sha).startswith(sel.value):
                raise NotFound(f"{uri}: no commit {sel.value}")
            return sha
        if sel.kind == "ts":
            branch, moment = _split_ts(uri, sel.value)
            return self._commit_at(uri, kind, repo, branch or "main", moment)
        branches, tags = self.hub.refs(kind, repo)
        sha = (branches if sel.kind == "branch" else tags).get(sel.value)
        if sha is None:
            raise NotFound(f"{uri}: no {sel.kind} named {sel.value}")
        return sha

    def _commit_at(self, uri: Uri, kind: str, repo: str, branch: str, moment: datetime) -> str:
        """The newest commit of branch whose date is at or before moment."""
        branches, _ = self.hub.refs(kind, repo)
        if branch not in branches:
            raise NotFound(f"{uri}: no branch named {branch}")
        read, page = 0, 0
        while read < _MAX_COMMITS:
            commits = self.hub.commits(kind, repo, branch, page)
            if not commits:
                raise NotFound(f"{uri}: {branch} has no commit at or before {moment.isoformat()}")
            for sha, made in commits:
                if made <= moment:
                    return sha
            read, page = read + len(commits), page + 1
        raise StoreError(f"{uri}: read {read} commits of {branch} without reaching "
                         f"{moment.isoformat()}; use commit: or a stage")

    def _key(self, pinned: Uri) -> list[str]:
        return [pinned.authority, *pinned.segments[:2], pinned.selector.value, *pinned.segments[2:]]

    def _cached(self, pinned: Uri, cache: LocalCache) -> Path | None:
        key, version = self._key(pinned), pinned.selector.value
        return cache.get(self.name, key, version) or cache.covered(self.name, key, version, min_len=4)

    # Store interface

    def stat(self, uri: Uri, cache: LocalCache) -> Entry:
        pinned = self.pin(uri, cache)
        sha = pinned.selector.value
        if self.offline:
            hit = self._cached(pinned, cache)
            if hit is None:
                raise OfflineMiss(f"{uri}: commit {sha} is not in the cache")
            return Entry(pinned, "folder", None, sha) if hit.is_dir() else Entry(pinned, "file", hit.stat().st_size, sha)
        item = self._info(pinned)
        return Entry(pinned, item.kind, item.size, sha)

    def _info(self, pinned: Uri) -> Item:
        kind, repo, path = self.locate(pinned)
        sha = pinned.selector.value
        with self.hub.errors(pinned):
            if not path:
                # A pin to a full commit asks the Hub nothing, so ask here
                # whether the repository has that commit.
                if self.hub.commit_of(kind, repo, sha) != sha:
                    raise NotFound(f"{pinned}: no commit {sha}")
                return Item("", "folder")
            item = self.hub.info(kind, repo, sha, path)
        if item is None:
            raise NotFound(f"{pinned}: not found on the Hub")
        if item.path != path:
            raise StoreError(f"{pinned}: the Hub answered for {item.path!r}, not {path!r}")
        return item

    def list(self, uri: Uri, cache: LocalCache) -> list[Entry]:
        pinned = self.pin(uri, cache)
        if self.offline:
            raise OfflineMiss(f"{uri}: offline, and only fetch and stat can use the cache")
        if self._info(pinned).kind != "folder":
            raise NotFound(f"{pinned}: is not a folder")
        kind, repo, path = self.locate(pinned)
        with self.hub.errors(pinned):
            items = self.hub.tree(kind, repo, pinned.selector.value, path, recursive=False)
        base = pinned.without_selector()
        entries = []
        for item in items:
            name = item.path.rpartition("/")[2]
            try:
                child = base.child(name).with_selector(pinned.selector)
            except UriError:
                continue  # a name that a URI cannot hold
            entries.append(Entry(child, item.kind, item.size, pinned.selector.value))
        return entries

    def fetch(self, uri: Uri, cache: LocalCache) -> Path:
        pinned = self.pin(uri, cache)
        hit = self._cached(pinned, cache)
        if hit is not None:
            return hit
        key, sha = self._key(pinned), pinned.selector.value
        if self.offline:
            raise OfflineMiss(f"{uri}: commit {sha} is not in the cache")
        item = self._info(pinned)
        kind, repo, path = self.locate(pinned)
        if item.kind == "file":
            def fill_file(tmp: Path) -> str:
                self._download(pinned, kind, repo, item, tmp)
                return sha

            return cache.put(self.name, key, fill_file)

        with self.hub.errors(pinned):
            items = [i for i in self.hub.tree(kind, repo, sha, path, recursive=True) if i.kind == "file"]
        prefix = f"{path}/" if path else ""
        rels = []
        for item in items:
            rel = tuple(item.path[len(prefix):].split("/")) if item.path.startswith(prefix) else ()
            if not rel or any(p in ("", ".", "..") or "\\" in p or "\0" in p for p in rel):
                raise StoreError(f"{pinned}: the Hub listed {item.path!r}, which is not a path in the folder")
            rels.append(rel)
        twins = find_twins(rels)
        if twins:
            raise StoreError(f"{pinned}: {twins[0]!r} and {twins[1]!r} would be one name on a "
                             "disk that ignores case or Unicode form; fetch the files one by one")

        def fill_folder(tmp: Path) -> None:
            for item, rel in zip(items, rels):
                dest = tmp.joinpath(*rel)
                try:
                    dest.parent.mkdir(parents=True, exist_ok=True)
                except OSError as e:
                    raise StoreError(f"{pinned}: cannot write {item.path} in the cache: {e}") from e
                self._download(pinned, kind, repo, item, dest)

        return cache.install_folder(self.name, key, sha, fill_folder)

    def _download(self, pinned: Uri, kind: str, repo: str, item: Item, dest: Path) -> None:
        with self.hub.errors(pinned):
            self.hub.download(kind, repo, pinned.selector.value, item, dest)
        _check(pinned, item, dest)
