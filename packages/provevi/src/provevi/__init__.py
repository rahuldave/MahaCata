"""Store, read and check the evidence of provrun's runs.

A program checks a run as ``prove verify`` does::

    from provevi import find_run, verify_run

    check = verify_run(find_run("run-20261031T164032Z-451450d6"))
    print(check.to_dict())

provrun writes runs through a store: ``FileStore``, or ``SqliteStore``, which
also answers queries across runs. ``open_store`` opens one from a URI. Every
error that provevi raises on purpose is a ``ProveviError``.
"""
from .checks import (ENDINGS, RunCheck, bundle_pin, read_events, verify, verify_against, verify_bundle,
                     verify_outputs, verify_run, verify_store_copy)
from .errors import CannotReadBack, EvidenceError, ProveviError, RunNotFound
from .events import CONFIDENCE, EvidenceEvent, canonical, now, sha256_bytes
from .runs import default_runs_root, find_run, list_runs
from .sqlite import SCHEMA_VERSION, SqliteRecord, SqliteRunWriter, SqliteStore
from .store import (EvidenceStore, FileRunWriter, FileStore, FolderRecord, JsonlSink, PayloadStore, RunRecord,
                    RunWriter, Sink, check_store_uri, open_store)

__all__ = [
    "CONFIDENCE", "ENDINGS", "CannotReadBack", "SCHEMA_VERSION", "EvidenceError", "EvidenceEvent", "EvidenceStore", "FileRunWriter",
    "FileStore", "FolderRecord", "JsonlSink", "PayloadStore", "ProveviError", "RunCheck", "RunNotFound", "RunRecord",
    "RunWriter", "Sink", "SqliteRecord", "SqliteRunWriter", "SqliteStore", "bundle_pin", "canonical", "check_store_uri",
    "default_runs_root", "find_run", "list_runs", "now", "open_store", "read_events", "sha256_bytes", "verify",
    "verify_against", "verify_bundle", "verify_outputs", "verify_run", "verify_store_copy",
]
