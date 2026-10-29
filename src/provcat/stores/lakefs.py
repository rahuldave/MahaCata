"""lakeFS: ``lakefs://repository/ref/path``.

The lakefs driver is the S3 driver plus the lakeFS REST API. It reads data
through the S3 gateway of lakeFS, where the bucket is the repository and the
first key segment is a ref: a branch, a tag or a commit. It asks the API
three things that the gateway cannot answer:

- **pin:** the commit that a ref points at. ``pin`` turns
  ``lakefs://repo/main/path`` into ``lakefs://repo/<commit>/path``, which never
  changes. A branch also holds uncommitted changes, which the S3 gateway shows
  and a pinned URI does not.
- **ts:** ``lakefs://repo/main/path?at=ts:2026-10-01`` names the last commit of
  ``main`` made at or before that time. stat, ls and fetch pin it first, then
  read that commit.
- **refs:** whether a ref exists, so ``ls`` of an empty branch gives ``[]``, and
  a missing branch says so.

Options are those of the S3 driver, plus ``api_url``. The default for
``api_url`` is ``endpoint_url`` followed by ``/api/v1``. The API takes the same
keys as the gateway, from the same AWS profile or environment.

Pins are kept in the cache, so ``--offline`` pins a branch or a time to the
commit that the last online pin found.
"""
from __future__ import annotations

import base64
import json
import urllib.error
import urllib.request
from datetime import UTC, datetime
from urllib.parse import quote, urlencode

from ..cache import LocalCache
from ..errors import NotFound, OfflineMiss, StoreError, UriError
from ..timestamps import parse_ts
from ..uri import Uri
from . import Entry, register_driver
from .s3 import S3Store

_PAGE = 1000
_MAX_COMMITS = 100_000  # a ts: search reads at most this many commits
_SETTLE = 600  # seconds; a ts: pin closer to now than this is not cached


@register_driver
class LakefsStore(S3Store):
    driver = "lakefs"

    def option_types(self):
        return {**super().option_types(), "api_url": str}

    def __init__(self, *args, **kw):
        super().__init__(*args, **kw)
        if not self.options.get("endpoint_url") and not self.options.get("api_url"):
            from ..errors import ConfigError

            raise ConfigError(f"stores.{self.name}: the lakefs driver needs endpoint_url")

    # URIs

    def locate(self, uri: Uri) -> tuple[str, str]:
        if uri.selector is not None:
            raise UriError(f"{uri}: pin a ?at= selector before reading")
        if not uri.authority or not uri.segments:
            raise UriError(f"{uri}: write {uri.scheme}://repository/ref/path")
        return uri.authority, uri.path

    def _check_selector(self, uri: Uri) -> None:
        if uri.selector is not None and uri.selector.kind != "ts":
            raise UriError(f"{uri}: lakeFS URIs put the branch, tag or commit in the path; "
                           "the only selector is ?at=ts:TIME")
        if not uri.authority or not uri.segments:
            raise UriError(f"{uri}: write {uri.scheme}://repository/ref/path")

    def _call(self, uri: Uri, method: str, **kw):
        """The S3 call. With access control, the gateway answers a repository
        that does not exist with 403, as it answers one the keys may not read,
        so on a 403 the API says which."""
        try:
            return super()._call(uri, method, **kw)
        except StoreError as e:
            response = getattr(e.__cause__, "response", None) or {}
            code = str(response.get("Error", {}).get("Code", ""))
            if self.offline or code not in ("403", "AccessDenied") or not uri.authority:
                raise
            try:
                self._api(uri, f"/repositories/{quote(uri.authority, safe='')}")
            except NotFound:
                raise NotFound(f"{uri}: lakeFS at {self._api_url()} has no repository {uri.authority!r}") from None
            except StoreError:
                pass  # the API refuses too, so the 403 stands
            raise e

    # The REST API

    def _api_url(self) -> str:
        return self.options.get("api_url") or self.options["endpoint_url"].rstrip("/") + "/api/v1"

    def _authorization(self, uri: Uri) -> str:
        """Basic auth with the keys of the profile or the environment, read
        each time, so keys that refresh stay current."""
        creds = self._session().get_credentials()
        if creds is None:
            raise StoreError(f"{uri}: no keys for lakeFS; set a profile or AWS_ACCESS_KEY_ID")
        frozen = creds.get_frozen_credentials()
        return "Basic " + base64.b64encode(f"{frozen.access_key}:{frozen.secret_key}".encode()).decode()

    def _api(self, uri: Uri, path: str, query: dict | None = None) -> dict:
        url = self._api_url() + path + ("?" + urlencode(query) if query else "")
        req = urllib.request.Request(url, headers={"Authorization": self._authorization(uri),
                                                   "Accept": "application/json"})
        try:
            with _NO_REDIRECTS.open(req, timeout=30) as resp:
                return json.loads(resp.read() or b"{}")
        except urllib.error.HTTPError as e:
            body = e.read()[:300].decode(errors="replace").strip()
            if e.code in (301, 302, 303, 307, 308):
                # Following it would send the keys to whatever host it names.
                raise StoreError(f"{uri}: the lakeFS API at {self._api_url()} answered with a redirect; "
                                 "set api_url to the real API address") from None
            if e.code == 404:
                raise NotFound(f"{uri}: lakeFS found nothing at {path} ({body})") from None
            if e.code in (401, 403):
                # With access control, lakeFS knows the keys but their policies
                # do not allow the action; its message names the action.
                try:
                    message = str(json.loads(body).get("message", ""))
                except (ValueError, AttributeError):
                    message = ""
                if message.startswith("insufficient permissions"):
                    raise StoreError(f"{uri}: lakeFS does not allow these keys to do this ({e.code}): "
                                     f"{message[:200]}") from None
                raise StoreError(f"{uri}: lakeFS refused the keys ({e.code})") from None
            raise StoreError(f"{uri}: lakeFS API error {e.code}: {body}") from None
        except (urllib.error.URLError, OSError) as e:
            raise StoreError(f"{uri}: cannot reach the lakeFS API at {self._api_url()}: {e}") from None
        except ValueError:
            raise StoreError(f"{uri}: the lakeFS API at {self._api_url()} sent a reply that is not JSON") from None

    def _missing(self, uri: Uri, repo: str, ref: str) -> NotFound:
        """The right NotFound: no repository, or no ref in it."""
        try:
            self._api(uri, f"/repositories/{quote(repo, safe='')}")
        except NotFound:
            return NotFound(f"{uri}: lakeFS at {self._api_url()} has no repository {repo!r}")
        return NotFound(f"{uri}: repository {repo} has no branch, tag or commit {ref!r}")

    def _commits(self, uri: Uri, repo: str, ref: str):
        """The first-parent commit log of a ref, newest first, page by page.

        First parent means the commits of the ref itself. A commit of a branch
        that was merged in is left out, because the ref did not point at it."""
        after = ""
        path = f"/repositories/{quote(repo, safe='')}/refs/{quote(ref, safe='')}/commits"
        while True:
            query = {"amount": _PAGE, "first_parent": "true"}
            if after:
                query["after"] = after
            out = self._api(uri, path, query)
            yield from out.get("results") or []
            pagination = out.get("pagination") or {}
            if not pagination.get("has_more"):
                return
            after = pagination.get("next_offset") or ""
            if not after:
                return

    def _commit_of(self, uri: Uri, repo: str, ref: str) -> str:
        try:
            for commit in self._commits(uri, repo, ref):
                return commit["id"]
        except NotFound:
            raise self._missing(uri, repo, ref) from None
        raise NotFound(f"{uri}: {ref} has no commits")

    def _commit_at(self, uri: Uri, repo: str, ref: str, moment: datetime) -> str:
        """The newest first-parent commit of ref made at or before moment."""
        limit = moment.timestamp()
        try:
            for n, commit in enumerate(self._commits(uri, repo, ref)):
                made = commit.get("creation_date")
                if not isinstance(made, (int, float)):
                    raise StoreError(f"{uri}: lakeFS gave commit {commit.get('id')} no creation date")
                if made <= limit:
                    return commit["id"]
                if n + 1 >= _MAX_COMMITS:
                    raise StoreError(f"{uri}: searched {_MAX_COMMITS} commits of {ref} without reaching "
                                     f"{moment.isoformat()}; use commit: or a stage")
        except NotFound:
            raise self._missing(uri, repo, ref) from None
        raise NotFound(f"{uri}: {ref} has no commit at or before {moment.isoformat()}")

    def ref_exists(self, uri: Uri, repo: str, ref: str) -> bool:
        try:
            self._commit_of(uri, repo, ref)
            return True
        except NotFound:
            return False

    # Pinning

    def pin(self, uri: Uri, cache: LocalCache) -> Uri:
        self._check_selector(uri)
        repo, ref = uri.authority, uri.segments[0]
        sel = uri.selector
        name = [repo, ref, str(sel) if sel else "head"]
        if self.offline:
            commit = cache.get_ref(self.name, name)
            if commit is None:
                raise OfflineMiss(f"{uri}: offline, and {ref}{' at ' + sel.value if sel else ''} was never pinned")
            return Uri(uri.scheme, repo, (commit, *uri.segments[1:]))
        remember = True
        if sel is None:
            commit = self._commit_of(uri, repo, ref)
        else:
            moment = parse_ts(sel.value)
            now = datetime.now(UTC)
            if moment > now:
                raise UriError(f"{uri}: {sel} is in the future, so its commit could still change")
            # A commit that is under way, or a server clock that runs behind,
            # can still add a commit at or before a recent time. So a recent
            # time is answered but not remembered for --offline.
            remember = (now - moment).total_seconds() > _SETTLE
            commit = self._commit_at(uri, repo, ref, moment)
        if not isinstance(commit, str) or not commit:
            raise StoreError(f"{uri}: lakeFS gave no commit id")
        if remember:
            cache.set_ref(self.name, name, commit)
        return Uri(uri.scheme, repo, (commit, *uri.segments[1:]))

    def _plain(self, uri: Uri, cache: LocalCache) -> Uri:
        """The URI to read: pinned when it has a ts: selector."""
        self._check_selector(uri)
        return self.pin(uri, cache) if uri.selector is not None else uri

    # The Store interface

    def _branches(self, uri: Uri) -> list[Entry]:
        """The branches of a repository, for ls of lakefs://repo."""
        if uri.selector is not None:
            raise UriError(f"{uri}: a repository has no time; put ?at=ts: on a branch")
        out, after, path = [], "", f"/repositories/{quote(uri.authority, safe='')}/branches"
        while True:
            query = {"amount": _PAGE}
            if after:
                query["after"] = after
            try:
                page = self._api(uri, path, query)
            except NotFound:
                raise NotFound(f"{uri}: lakeFS at {self._api_url()} has no repository {uri.authority!r}") from None
            out += [Entry(uri.child(b["id"]), "folder") for b in page.get("results") or []]
            pagination = page.get("pagination") or {}
            after = pagination.get("next_offset") or ""
            if not pagination.get("has_more") or not after:
                return out

    def stat(self, uri: Uri, cache: LocalCache) -> Entry:
        if uri.authority and not uri.segments and not self.offline:
            self._branches(uri)
            return Entry(uri, "folder")
        plain = self._plain(uri, cache)
        if len(plain.segments) == 1 and not self.offline:
            # The root of a ref is a folder, even when the branch is empty.
            if self.ref_exists(plain, plain.authority, plain.segments[0]):
                return Entry(plain, "folder")
            raise self._missing(plain, plain.authority, plain.segments[0])
        return super().stat(plain, cache)

    def list(self, uri: Uri, cache: LocalCache) -> list[Entry]:
        if uri.authority and not uri.segments:
            self._online(uri)
            return self._branches(uri)
        plain = self._plain(uri, cache)
        try:
            return super().list(plain, cache)
        except OfflineMiss:
            raise
        except StoreError as e:
            if self.offline:
                raise
            ref = plain.segments[0]
            if not self.ref_exists(plain, plain.authority, ref):
                raise self._missing(plain, plain.authority, ref) from None
            if isinstance(e, NotFound) and len(plain.segments) == 1:
                return []  # an empty branch
            raise

    def fetch(self, uri: Uri, cache: LocalCache):
        return super().fetch(self._plain(uri, cache), cache)


class _Refuse(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None  # urllib then raises the redirect as an HTTPError


_NO_REDIRECTS = urllib.request.build_opener(_Refuse)
