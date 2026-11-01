"""Store, read and check the evidence of provrun's runs.

A program checks a run as ``prove verify`` does::

    from provevi import find_run, verify_run

    check = verify_run(find_run("run-20261031T164032Z-451450d6"))
    print(check.to_dict())

provrun writes runs through a store, such as ``FileStore``. Every error that
provevi raises on purpose is a ``ProveviError``.
"""
from .checks import (ENDINGS, RunCheck, bundle_pin, read_events, verify, verify_against, verify_bundle,
                     verify_outputs, verify_run)
from .errors import EvidenceError, ProveviError, RunNotFound
from .events import CONFIDENCE, EvidenceEvent, canonical, now, sha256_bytes
from .runs import default_runs_root, find_run, list_runs
from .store import EvidenceStore, FileRunWriter, FileStore, JsonlSink, PayloadStore, RunWriter, Sink

__all__ = [
    "CONFIDENCE", "ENDINGS", "EvidenceError", "EvidenceEvent", "EvidenceStore", "FileRunWriter", "FileStore",
    "JsonlSink", "PayloadStore", "ProveviError", "RunCheck", "RunNotFound", "RunWriter", "Sink", "bundle_pin",
    "canonical", "default_runs_root", "find_run", "list_runs", "now", "read_events", "sha256_bytes", "verify",
    "verify_against", "verify_bundle", "verify_outputs", "verify_run",
]
