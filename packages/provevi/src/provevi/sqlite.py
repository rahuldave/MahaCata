"""The ``sqlite`` store: the evidence of many runs in one SQLite file.

Each event becomes a row of ``provevi_events`` as it is written, with its URIs
in ``provevi_event_refs``. A run has a row in ``provevi_runs`` from its start,
and its manifest at the seal. Grants, refusals, inputs and outputs are views
over the events, so a query across runs, such as "which runs read this URI",
is one SELECT.

The store also writes the files of the ``file`` store into each run's
evidence folder: ``events.jsonl``, ``payloads/`` and ``manifest.json``.
provrun commits that folder as the bundle, so a run's bundle is the same in
both stores. The payload bytes live only in that folder; the table records
their hash, size and place.

The tables are provevi's own, and their names start with ``provevi_``, so the
file can also hold MLflow's tables. The file records its schema version, and
opening it to write brings an older schema up to date. Opening it to read,
as ``prove`` does, changes nothing.

The check of a run covers each event's line, and every column that the
events imply: the run's org, domain, environment, actor, mode, experiment,
status and times, its settings, the span IDs and the payload sizes. It does
not cover the run's folder, its sealed_at time, or an experiment's first-run
details (created_by, created_at, spec), which nothing in the events fixes.
"""
from __future__ import annotations

import functools
import json
import os
import sqlite3
import tempfile
import threading
from pathlib import Path
from urllib.parse import quote

from .errors import EvidenceError, ProveviError, RunNotFound
from .checks import ENDINGS
from .events import EvidenceEvent, canonical, now, sha256_bytes
from .store import EvidenceStore, FileRunWriter, RunRecord, RunWriter

#: The schema version that this provevi writes.
SCHEMA_VERSION = 1

_V1 = """
CREATE TABLE provevi_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE provevi_experiments (
    id TEXT PRIMARY KEY, uri TEXT NOT NULL, org TEXT NOT NULL, domain TEXT NOT NULL, env TEXT NOT NULL,
    name TEXT NOT NULL, description TEXT, spec TEXT, spec_sha256 TEXT, created_by TEXT, created_at TEXT,
    mlflow_experiment_id TEXT);
CREATE TABLE provevi_runs (
    run_id TEXT PRIMARY KEY, experiment_id TEXT, org TEXT, domain TEXT, env TEXT, actor TEXT,
    status TEXT NOT NULL, started_at TEXT NOT NULL, ended_at TEXT, mode TEXT, parent_run TEXT,
    folder TEXT NOT NULL, last_hash TEXT, manifest TEXT, manifest_sha256 TEXT, sealed_at TEXT,
    bundle_uri TEXT, pinned_bundle TEXT);
CREATE TABLE provevi_run_params (
    run_id TEXT NOT NULL, key TEXT NOT NULL, value TEXT, PRIMARY KEY (run_id, key));
CREATE TABLE provevi_events (
    run_id TEXT NOT NULL, seq INTEGER NOT NULL, line TEXT NOT NULL, time TEXT NOT NULL, type TEXT NOT NULL,
    component TEXT NOT NULL, actor TEXT NOT NULL, data TEXT NOT NULL, payload TEXT, confidence TEXT NOT NULL,
    prev TEXT, trace_id TEXT, span_id TEXT, PRIMARY KEY (run_id, seq));
CREATE INDEX provevi_events_type ON provevi_events (type);
CREATE TABLE provevi_event_refs (
    run_id TEXT NOT NULL, seq INTEGER NOT NULL, n INTEGER NOT NULL, uri TEXT NOT NULL,
    PRIMARY KEY (run_id, seq, n));
CREATE INDEX provevi_event_refs_uri ON provevi_event_refs (uri);
CREATE TABLE provevi_payloads (
    run_id TEXT NOT NULL, sha256 TEXT NOT NULL, size INTEGER NOT NULL, location TEXT NOT NULL,
    PRIMARY KEY (run_id, sha256));
CREATE TABLE provevi_assessments (
    id TEXT PRIMARY KEY, target_kind TEXT NOT NULL, target_id TEXT NOT NULL, name TEXT NOT NULL,
    value TEXT, rationale TEXT, source TEXT, created_by TEXT, created_at TEXT NOT NULL);
CREATE VIEW provevi_grants AS
    SELECT e.run_id, e.seq, e.time, r.uri, json_extract(e.data, '$.grant_id') AS grant_id,
           json_extract(e.data, '$.mode') AS mode, json_extract(e.data, '$.reason') AS reason
    FROM provevi_events e JOIN provevi_event_refs r ON r.run_id = e.run_id AND r.seq = e.seq AND r.n = 0
    WHERE e.type = 'access_granted';
CREATE VIEW provevi_refusals AS
    SELECT e.run_id, e.seq, e.time, r.uri, json_extract(e.data, '$.mode') AS mode,
           json_extract(e.data, '$.reason') AS reason
    FROM provevi_events e JOIN provevi_event_refs r ON r.run_id = e.run_id AND r.seq = e.seq AND r.n = 0
    WHERE e.type = 'access_denied';
CREATE VIEW provevi_inputs AS
    SELECT e.run_id, e.seq, r.uri, p.uri AS pinned, json_extract(e.data, '$.sha256') AS sha256,
           json_extract(e.data, '$.agent_path') AS agent_path
    FROM provevi_events e
    JOIN provevi_event_refs r ON r.run_id = e.run_id AND r.seq = e.seq AND r.n = 0
    LEFT JOIN provevi_event_refs p ON p.run_id = e.run_id AND p.seq = e.seq AND p.n = 1
    WHERE e.type = 'input_staged';
CREATE VIEW provevi_outputs AS
    SELECT e.run_id, e.seq, r.uri, json_extract(e.data, '$.sha256') AS sha256,
           json_extract(e.data, '$.size') AS size
    FROM provevi_events e JOIN provevi_event_refs r ON r.run_id = e.run_id AND r.seq = e.seq AND r.n = 0
    WHERE e.type = 'output_artifact_created';
"""

#: The steps that bring a schema to each version, from the one before it.
#: Each is a list of statements, so a later step may hold a trigger.
MIGRATIONS = {1: [st.strip() for st in _V1.split(";") if st.strip()]}

#: The settings of a run kept in provevi_run_params, taken from run_created.
#: Never the agent's settings, which may hold secrets.
PARAMS = ("agent", "backend", "model", "mode", "tools", "limits")


def connect(path: Path, *, readonly: bool = False) -> sqlite3.Connection:
    """A connection to the file that waits for another writer instead of
    failing. Transactions are explicit. A read-only connection never writes.

    provevi uses SQLite's WAL mode only. A writer puts the file in WAL mode,
    which stays in the file, and every connection checks it, so a file in
    any other mode is refused. Raises ProveviError."""
    path = Path(path)
    if readonly and not os.access(path.parent, os.W_OK):
        raise ProveviError(f"cannot read {path}: its folder cannot be written, and SQLite writes a side file next "
                           "to a WAL file even to read it")
    if readonly:
        conn = sqlite3.connect(f"file:{quote(str(path))}?mode=ro", uri=True, timeout=60, isolation_level=None,
                               check_same_thread=False)
    else:
        conn = sqlite3.connect(path, timeout=60, isolation_level=None, check_same_thread=False)
    try:
        conn.execute("PRAGMA busy_timeout=60000")
        mode = conn.execute("PRAGMA journal_mode" if readonly else "PRAGMA journal_mode=WAL").fetchone()[0]
        if str(mode).lower() != "wal":
            raise ProveviError(f"{path} is in SQLite's {mode} mode, not WAL; provevi uses WAL only")
    except BaseException:
        conn.close()
        raise
    return conn


def schema_version(conn: sqlite3.Connection) -> int:
    """The schema version of the file; 0 for a file with no provevi tables."""
    if not conn.execute("SELECT 1 FROM sqlite_master WHERE name = 'provevi_meta'").fetchone():
        return 0
    row = conn.execute("SELECT value FROM provevi_meta WHERE key = 'schema_version'").fetchone()
    return int(row[0]) if row else 0


def _newer(path, version: int) -> ProveviError:
    return ProveviError(f"{path} has schema version {version}; this provevi reads up to "
                        f"{SCHEMA_VERSION}. Use a newer provevi.")


def _rollback(conn: sqlite3.Connection) -> None:
    # After a failed COMMIT, SQLite may have rolled back already.
    if conn.in_transaction:
        conn.execute("ROLLBACK")


def migrate(conn: sqlite3.Connection, path: str | Path = "the evidence file") -> int:
    """Bring the file to SCHEMA_VERSION, one step at a time, in one
    transaction. Raises ProveviError for a file of a newer provevi."""
    if schema_version(conn) == SCHEMA_VERSION:
        return SCHEMA_VERSION
    conn.execute("BEGIN IMMEDIATE")
    try:
        version = schema_version(conn)
        if version > SCHEMA_VERSION:
            raise _newer(path, version)
        for v in range(version + 1, SCHEMA_VERSION + 1):
            for statement in MIGRATIONS[v]:
                conn.execute(statement)
            conn.execute("INSERT OR REPLACE INTO provevi_meta VALUES ('schema_version', ?)", (str(v),))
        conn.execute("COMMIT")
    except BaseException:
        _rollback(conn)
        raise
    return SCHEMA_VERSION


def _provevi_objects(conn: sqlite3.Connection) -> list[tuple[str, str, str]]:
    return conn.execute(
        "SELECT type, name, sql FROM sqlite_master WHERE sql IS NOT NULL "
        "AND (name LIKE 'provevi\\_%' ESCAPE '\\' OR tbl_name LIKE 'provevi\\_%' ESCAPE '\\' "
        "OR (type = 'trigger' AND sql LIKE '%provevi\\_%' ESCAPE '\\'))").fetchall()


@functools.lru_cache(maxsize=4)
def _expected(version: int, steps: tuple[tuple[str, ...], ...]) -> frozenset[tuple[str, str, str]]:
    """The schema as SQLite stores it after every step, which can differ from
    the statements once a step alters a table. It is read from a scratch file
    that the steps are applied to, in WAL mode like every provevi file."""
    with tempfile.TemporaryDirectory() as tmp:
        conn = connect(Path(tmp) / "schema.db")
        try:
            for step in steps:
                for statement in step:
                    conn.execute(statement)
            return frozenset(_provevi_objects(conn))
        finally:
            conn.close()


def schema_problems(conn: sqlite3.Connection) -> list[str]:
    """provevi's tables and views must be exactly its schema's, and no
    trigger may touch them, since the queries read the views and a trigger
    could change rows as they are written. An extra index is allowed, as it
    cannot change what a query returns."""
    want = _expected(SCHEMA_VERSION, tuple(tuple(MIGRATIONS[v]) for v in range(1, SCHEMA_VERSION + 1)))
    found = set(_provevi_objects(conn))
    problems = [f"the trigger {name} touches provevi's tables" for kind, name, _ in found if kind == "trigger"]
    problems += [f"the {kind} {name} is not provevi's" for kind, name, _ in sorted(found - want)
                 if kind not in ("trigger", "index")]
    problems += [f"the {kind} {name} is missing or was changed" for kind, name, _ in sorted(want - found)]
    return problems


def derive(created: EvidenceEvent | None, ending: EvidenceEvent | None, sealed: bool) -> dict:
    """What a run's row, settings and experiment must be, from its run_created
    event and its ending event. The writer writes these, and the check
    compares the stored rows with them."""
    d = created.data if created is not None else {}
    spec = d.get("spec") if isinstance(d.get("spec"), dict) else {}
    agent = d.get("agent") or spec.get("name")
    org, domain, env = d.get("org"), d.get("domain"), d.get("env")
    experiment = (f"{org}/{domain}/{env}/{agent}"
                  if all(isinstance(x, str) and x for x in (org, domain, env, agent)) else None)
    values = {"agent": agent, "mode": d.get("mode"), **{k: spec.get(k) for k in PARAMS if k in spec}}
    if ending is not None:
        status, ended_at = ending.type.removeprefix("run_"), ending.time
    else:
        status, ended_at = ("incomplete" if sealed else "open"), None
    return {
        "run": {"experiment_id": experiment, "org": org, "domain": domain, "env": env,
                "actor": created.actor if created is not None else None, "mode": d.get("mode"),
                "parent_run": d.get("parent_run"), "started_at": created.time if created is not None else None,
                "status": status, "ended_at": ended_at},
        "params": {k: canonical(v) for k, v in values.items() if k in PARAMS and v is not None},
        "experiment": None if experiment is None else {
            "id": experiment, "uri": f"experiment://{experiment}", "org": org, "domain": domain, "env": env,
            "name": agent},
        "spec": spec,
    }


class SqliteStore(EvidenceStore):
    """The evidence of many runs in one SQLite file, with the files of each
    run in its evidence folder as well. Two runs, in one process or in two,
    can write the same file at once.

    With create=False the store only reads: a missing file, a file that is not
    provevi's, or one of another schema version raises ProveviError, and
    nothing is written. Every SQLite error comes out as a ProveviError."""

    def __init__(self, path: str | Path, *, create: bool = True):
        self.path = Path(path).expanduser().resolve()
        self.create = create
        try:
            if create:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                conn = connect(self.path)
                try:
                    migrate(conn, self.path)
                    version, wrong = SCHEMA_VERSION, schema_problems(conn)
                finally:
                    conn.close()
            else:
                if not self.path.is_file():
                    raise ProveviError(f"no evidence file at {self.path}")
                conn = connect(self.path, readonly=True)
                try:
                    version = schema_version(conn)
                    wrong = schema_problems(conn) if version == SCHEMA_VERSION else []
                finally:
                    conn.close()
        except sqlite3.Error as e:
            raise ProveviError(f"cannot open the evidence file {self.path}: {e}") from e
        if wrong:
            raise ProveviError(f"the provevi schema in {self.path} was changed: " + "; ".join(wrong))
        if version == 0:
            raise ProveviError(f"{self.path} holds no provevi evidence")
        if version > SCHEMA_VERSION:
            raise _newer(self.path, version)
        if version < SCHEMA_VERSION:
            raise ProveviError(f"the evidence file {self.path} has schema version {version}; open it to write, "
                               f"as provr run does, to bring it to {SCHEMA_VERSION}")

    def __repr__(self) -> str:
        return f"SqliteStore({str(self.path)!r})"

    def _query(self, sql: str, args: tuple = ()) -> list[sqlite3.Row]:
        try:
            conn = connect(self.path, readonly=not self.create)
            conn.row_factory = sqlite3.Row
            try:
                return conn.execute(sql, args).fetchall()
            finally:
                conn.close()
        except sqlite3.Error as e:
            raise ProveviError(f"cannot read the evidence file {self.path}: {e}") from e

    def open_run(self, run_id: str, folder: Path) -> SqliteRunWriter:
        if not self.create:
            raise ProveviError(f"{self!r} was opened to read; open it with create=True to write runs")
        return SqliteRunWriter(self, run_id, folder)

    def read_run(self, run_id: str, folder: Path | None = None) -> SqliteRecord:
        """The run as the file holds it. folder is the evidence folder to read
        the payloads from; without it, the place the file records is used."""
        if not self._query("SELECT 1 FROM provevi_runs WHERE run_id = ?", (run_id,)):
            raise RunNotFound(f"no run {run_id!r} in {self.path}")
        return SqliteRecord(self, run_id, folder)

    def list_runs(self) -> list[str]:
        return [r[0] for r in self._query("SELECT run_id FROM provevi_runs ORDER BY run_id")]

    def run_folder(self, run_id: str) -> Path | None:
        """The run folder that the file records. The check does not cover it,
        so prove looks under the runs root first."""
        rows = self._query("SELECT folder FROM provevi_runs WHERE run_id = ?", (run_id,))
        return Path(rows[0][0]).parent if rows else None

    # Queries across runs. Each returns plain values.

    def runs_reading(self, uri: str) -> list[str]:
        """The runs that staged uri as an input, by its URI or by a pinned
        version of it (the URI followed by ``?``)."""
        rows = self._query(
            "SELECT DISTINCT run_id FROM provevi_inputs WHERE uri = ? OR pinned = ? "
            "OR substr(uri, 1, length(?) + 1) = ? || '?' OR substr(pinned, 1, length(?) + 1) = ? || '?' "
            "ORDER BY run_id", (uri, uri, uri, uri, uri, uri))
        return [r[0] for r in rows]

    def runs_of_experiment(self, experiment: str) -> list[str]:
        """The runs of an experiment, by its name, such as acme/demo/dev/revenue-agent."""
        rows = self._query("SELECT run_id FROM provevi_runs WHERE experiment_id = ? ORDER BY run_id",
                           (experiment.removeprefix("experiment://"),))
        return [r[0] for r in rows]

    def experiments(self) -> list[dict]:
        """The experiments. Each row was made by the experiment's first run."""
        rows = self._query("SELECT id, uri, created_by, created_at, spec_sha256 FROM provevi_experiments "
                           "ORDER BY id")
        return [dict(r) for r in rows]

    def run(self, run_id: str) -> dict | None:
        """The run's row, and its settings as ``params``."""
        rows = self._query("SELECT run_id, experiment_id, org, domain, env, actor, status, started_at, ended_at, "
                           "mode, folder, last_hash, manifest_sha256, sealed_at, bundle_uri, pinned_bundle "
                           "FROM provevi_runs WHERE run_id = ?", (run_id,))
        if not rows:
            return None
        params = self._query("SELECT key, value FROM provevi_run_params WHERE run_id = ? ORDER BY key", (run_id,))
        return {**dict(rows[0]), "params": {k: json.loads(v) for k, v in params}}

    def access(self, run_id: str) -> list[dict]:
        """The grants and refusals of a run, in the order they happened."""
        rows = self._query(
            "SELECT seq, time, 'granted' AS decision, uri, mode, reason FROM provevi_grants WHERE run_id = ? "
            "UNION ALL SELECT seq, time, 'denied', uri, mode, reason FROM provevi_refusals WHERE run_id = ? "
            "ORDER BY seq", (run_id, run_id))
        return [dict(r) for r in rows]

    def inputs(self, run_id: str) -> list[dict]:
        rows = self._query("SELECT seq, uri, pinned, sha256, agent_path FROM provevi_inputs WHERE run_id = ? "
                           "ORDER BY seq", (run_id,))
        return [dict(r) for r in rows]


class SqliteRunWriter(RunWriter):
    """Writes one run to the file and to the run's evidence folder. An event
    counts as written when both hold it: the row is committed only after the
    line is in events.jsonl, and if the commit fails, the line is cut from
    the file again. If the process dies between the two, the file holds one
    more line than the rows, and the check against the store reports it."""

    def __init__(self, store: SqliteStore, run_id: str, folder: Path):
        self.store = store
        self.run_id = run_id
        self.folder = Path(folder)
        self._lock = threading.Lock()
        try:
            conn = connect(store.path)
        except sqlite3.Error as e:
            raise ProveviError(f"cannot open the evidence file {store.path}: {e}") from e
        try:
            conn.execute("INSERT INTO provevi_runs (run_id, status, started_at, folder) VALUES (?, ?, ?, ?)",
                         (run_id, "open", now(), str(self.folder.resolve())))
        except sqlite3.IntegrityError:
            conn.close()
            raise EvidenceError(f"the run {run_id} is already in {store.path}") from None
        except BaseException:
            conn.close()
            raise
        self._conn: sqlite3.Connection | None = conn
        self._sealed = False
        self.files = FileRunWriter(self.folder)

    def _reconnect(self, conn: sqlite3.Connection) -> None:
        """The rollback failed, so the connection is still in a transaction.
        Closing it ends the transaction; a new one starts clean. If that
        fails, the next write tries again."""
        self._conn = None
        try:
            conn.close()
        except sqlite3.Error:
            pass
        try:
            self._conn = connect(self.store.path)
        except (sqlite3.Error, ProveviError):
            pass

    @property
    def payloads(self):
        """The payload store in the run folder, as a file store's writer has."""
        return self.files.payloads

    def _write(self, statements: list[tuple[str, tuple]], then=None):
        """Run statements and then(undo) in one transaction. then writes the
        files, and adds to undo what takes its writes back."""
        undo: list = []
        with self._lock:
            if self._sealed:
                raise EvidenceError(f"the evidence of {self.run_id} is sealed")
            if self._conn is None:  # a reconnect failed before; try once more
                self._conn = connect(self.store.path)
            conn = self._conn
            conn.execute("BEGIN IMMEDIATE")
            try:
                for sql, args in statements:
                    conn.execute(sql, args)
                result = then(undo) if then else None
                conn.execute("COMMIT")
            except BaseException:
                try:
                    _rollback(conn)
                except BaseException:
                    pass  # the first error is the one to raise
                finally:
                    for u in reversed(undo):
                        u()
                    if conn.in_transaction:
                        self._reconnect(conn)
                raise
            return result

    def append(self, line: str) -> None:
        event = EvidenceEvent.from_line(line)
        data = event.data
        statements = [(
            "INSERT INTO provevi_events VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (event.run_id, event.seq, line, event.time, event.type, event.component, event.actor, canonical(data),
             event.payload, event.confidence, event.prev, data.get("trace_id"), data.get("span_id")))]
        statements += [("INSERT INTO provevi_event_refs VALUES (?, ?, ?, ?)", (event.run_id, event.seq, n, uri))
                       for n, uri in enumerate(event.refs)]
        if event.type == "run_created":
            statements += self._created(event)
        elif event.type in ENDINGS:
            statements.append(("UPDATE provevi_runs SET status = ?, ended_at = ? WHERE run_id = ?",
                               (event.type.removeprefix("run_"), event.time, self.run_id)))

        def write_line(undo):
            path = self.folder / "events.jsonl"
            size = path.stat().st_size if path.exists() else 0
            undo.append(lambda: os.truncate(path, size))
            self.files.append(line)

        self._write(statements, then=write_line)

    def _created(self, event: EvidenceEvent) -> list[tuple[str, tuple]]:
        want = derive(event, None, False)
        run, out = want["run"], []
        if want["experiment"] is not None:
            e = want["experiment"]
            kept = {k: v for k, v in want["spec"].items() if k in PARAMS}
            out.append(("INSERT OR IGNORE INTO provevi_experiments (id, uri, org, domain, env, name, spec, "
                        "spec_sha256, created_by, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (e["id"], e["uri"], e["org"], e["domain"], e["env"], e["name"], canonical(kept),
                         sha256_bytes(canonical(want["spec"]).encode()), event.actor, event.time)))
        out.append(("UPDATE provevi_runs SET experiment_id = ?, org = ?, domain = ?, env = ?, actor = ?, mode = ?, "
                    "parent_run = ?, started_at = ? WHERE run_id = ?",
                    (run["experiment_id"], run["org"], run["domain"], run["env"], run["actor"], run["mode"],
                     run["parent_run"], run["started_at"], self.run_id)))
        out += [("INSERT OR REPLACE INTO provevi_run_params VALUES (?, ?, ?)", (self.run_id, k, v))
                for k, v in want["params"].items()]
        return out

    def close(self) -> None:
        self.files.close()

    def put_payload(self, data: bytes | str) -> str:
        raw = data.encode() if isinstance(data, str) else data
        digest = sha256_bytes(raw)
        dest = self.folder / "payloads" / digest

        def write_file(undo):
            if not dest.exists():
                undo.append(lambda: dest.unlink(missing_ok=True))
            self.files.put_payload(raw)

        self._write([("INSERT OR IGNORE INTO provevi_payloads VALUES (?, ?, ?, ?)",
                      (self.run_id, digest, len(raw), str(dest.resolve())))], then=write_file)
        return digest

    def get_payload(self, digest: str) -> bytes:
        return self.files.get_payload(digest)

    def payload_names(self) -> list[str]:
        rows = self.store._query("SELECT sha256 FROM provevi_payloads WHERE run_id = ? ORDER BY sha256",
                                 (self.run_id,))
        return [r[0] for r in rows]

    def seal(self, manifest: dict) -> None:
        path = self.folder / "manifest.json"

        def write_files(undo):
            if not path.exists():
                undo.append(lambda: path.unlink(missing_ok=True))
            self.files.seal(manifest)
            raw = path.read_bytes()
            self._conn.execute(
                "UPDATE provevi_runs SET last_hash = ?, manifest = ?, manifest_sha256 = ?, sealed_at = ?, "
                "status = CASE WHEN status = 'open' THEN 'incomplete' ELSE status END WHERE run_id = ?",
                (manifest.get("last"), raw.decode(), sha256_bytes(raw), now(), self.run_id))

        self._write([], then=write_files)
        with self._lock:
            conn, self._conn, self._sealed = self._conn, None, True
        if conn is not None:
            conn.close()

    def manifest(self) -> dict:
        rows = self.store._query("SELECT manifest FROM provevi_runs WHERE run_id = ?", (self.run_id,))
        if not rows or rows[0][0] is None:
            raise EvidenceError(f"the evidence of {self.run_id} is not sealed")
        return json.loads(rows[0][0])

    def record_bundle(self, uri: str | None, pinned: str | None) -> None:
        conn = connect(self.store.path)
        try:
            conn.execute("UPDATE provevi_runs SET bundle_uri = ?, pinned_bundle = ? WHERE run_id = ?",
                         (uri, pinned, self.run_id))
        finally:
            conn.close()


class SqliteRecord(RunRecord):
    """A run as the file holds it, for the checks. Besides the chain, it
    checks every column that the events imply, so a changed column fails as
    a changed line does. A file it cannot read raises EvidenceError."""

    def __init__(self, store: SqliteStore, run_id: str, folder: Path | None = None):
        self.store = store
        self.run_id = run_id
        self.folder = Path(folder) if folder is not None else None
        self.where = f"{store.path} run {run_id}"

    def _q(self, sql: str, args: tuple = ()) -> list[sqlite3.Row]:
        try:
            return self.store._query(sql, args)
        except ProveviError as e:
            raise EvidenceError(f"cannot read {self.where}: {e.__cause__ or e}") from e

    def lines(self) -> list[str]:
        rows = self._q("SELECT seq, line FROM provevi_events WHERE run_id = ? ORDER BY seq", (self.run_id,))
        for seq, line in rows:
            if not isinstance(line, str):
                raise EvidenceError(f"row {seq} of {self.where}: the line is not text")
        return [r[1] for r in rows]

    def manifest_bytes(self) -> bytes | None:
        rows = self._q("SELECT manifest FROM provevi_runs WHERE run_id = ?", (self.run_id,))
        if not rows or rows[0][0] is None:
            return None
        if not isinstance(rows[0][0], str):
            raise EvidenceError(f"the manifest of {self.where} is not text")
        return rows[0][0].encode()

    def _payload_path(self, name: str) -> Path | None:
        if self.folder is not None:
            return self.folder / "payloads" / name
        rows = self._q("SELECT location FROM provevi_payloads WHERE run_id = ? AND sha256 = ?", (self.run_id, name))
        return Path(rows[0][0]) if rows and isinstance(rows[0][0], str) else None

    def payload_digest(self, name: str) -> str | None:
        if not self._q("SELECT 1 FROM provevi_payloads WHERE run_id = ? AND sha256 = ?", (self.run_id, name)):
            return None
        p = self._payload_path(name)
        if p is None or not p.exists():
            return None
        return sha256_bytes(p.read_bytes())

    def payload_files(self) -> tuple[set[str], list[str]]:
        rows = self._q("SELECT sha256 FROM provevi_payloads WHERE run_id = ?", (self.run_id,))
        return {str(r[0]) for r in rows}, []

    def problems(self) -> list[str]:
        """Rows whose columns do not match their line, and stored values that
        differ from what the events imply."""
        try:
            conn = connect(self.store.path, readonly=not self.store.create)
            try:
                problems = [f"the schema: {p}" for p in schema_problems(conn)]
            finally:
                conn.close()
        except (sqlite3.Error, ProveviError) as e:
            raise EvidenceError(f"cannot read {self.where}: {e}") from e
        rows = self._q("SELECT seq, line, time, type, component, actor, data, payload, confidence, prev, trace_id, "
                       "span_id FROM provevi_events WHERE run_id = ? ORDER BY seq", (self.run_id,))
        refs: dict[int, list[str]] = {}
        for seq, uri in self._q("SELECT seq, uri FROM provevi_event_refs WHERE run_id = ? ORDER BY seq, n",
                                (self.run_id,)):
            refs.setdefault(seq, []).append(uri)
        events = []
        for row in rows:
            try:
                e = EvidenceEvent.from_line(row["line"])
            except (ValueError, TypeError):
                continue  # the chain check reports the line
            events.append(e)
            columns = tuple(row)[2:]
            if columns != (e.time, e.type, e.component, e.actor, canonical(e.data), e.payload, e.confidence, e.prev,
                           e.data.get("trace_id"), e.data.get("span_id")) or row["seq"] != e.seq:
                problems.append(f"row {row['seq']}: its columns differ from its line")
            if refs.pop(row["seq"], []) != list(e.refs):
                problems.append(f"row {row['seq']}: its refs differ from its line")
        problems += [f"refs of row {seq} have no event" for seq in sorted(refs)]
        run = self._q("SELECT * FROM provevi_runs WHERE run_id = ?", (self.run_id,))
        if not run:
            return problems + [f"no run {self.run_id} in the runs table"]
        run = dict(run[0])
        created = next((e for e in events if e.type == "run_created"), None)
        ending = events[-1] if events and events[-1].type in ENDINGS else None
        want = derive(created, ending, run["manifest"] is not None)
        for col, value in want["run"].items():
            if run[col] != value:
                problems.append(f"the run's {col} is {run[col]!r}, and its events say {value!r}")
        params = {k: v for k, v in self._q("SELECT key, value FROM provevi_run_params WHERE run_id = ?",
                                            (self.run_id,))}
        if params != want["params"]:
            problems.append("the run's settings differ from its run_created event")
        if want["experiment"] is not None:
            exp = self._q("SELECT id, uri, org, domain, env, name FROM provevi_experiments WHERE id = ?",
                          (want["experiment"]["id"],))
            if not exp or dict(exp[0]) != want["experiment"]:
                problems.append(f"the experiment {want['experiment']['id']} is missing or differs from the run's")
        for digest, size in self._q("SELECT sha256, size FROM provevi_payloads WHERE run_id = ?", (self.run_id,)):
            p = self._payload_path(str(digest))
            if p is not None and p.is_file() and p.stat().st_size != size:
                problems.append(f"payload {digest}: its size is not {size}")
        if run["manifest"] is not None and isinstance(run["manifest"], str):
            if run["manifest_sha256"] != sha256_bytes(run["manifest"].encode()):
                problems.append("the run's manifest hash differs from its manifest")
            try:
                if json.loads(run["manifest"]).get("last") != run["last_hash"]:
                    problems.append("the run's last hash differs from its manifest")
            except (ValueError, AttributeError, RecursionError):
                pass  # the manifest check reports it
        return problems
