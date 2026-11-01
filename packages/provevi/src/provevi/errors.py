"""Errors raised by provevi. Every error that provevi raises on purpose is a
ProveviError, so a program that calls provevi can catch them in one place."""


class ProveviError(Exception):
    """Base class for every provevi error."""


class EvidenceError(ProveviError):
    """Evidence could not be written, or does not verify."""


class RunNotFound(ProveviError, LookupError):
    """No run folder has the run ID or path that was given."""


class CannotReadBack(ProveviError):
    """The store keeps no record of its own to read a run back from, so the
    checks read the files in the run folder only."""
