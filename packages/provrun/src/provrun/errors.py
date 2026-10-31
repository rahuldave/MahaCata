"""Errors raised by provrun. Every error that provrun raises on purpose is a
ProvrunError, so a program that calls provrun can catch them in one place."""


class ProvrunError(Exception):
    """Base class for every provrun error."""


class RunFileError(ProvrunError, ValueError):
    """A run file, or its run section, is missing a part or has a bad one."""


class RunSettingError(ProvrunError, ValueError):
    """A setting passed to a run, such as its environment or evidence mode, is bad."""


class RunNotFound(ProvrunError, LookupError):
    """No run folder has the run ID or path that was given."""
