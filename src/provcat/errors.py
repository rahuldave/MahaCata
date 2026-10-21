"""Errors raised by provcat. Every error is a ProvcatError."""


class ProvcatError(Exception):
    """Base class for every provcat error."""


class UriError(ProvcatError):
    """A URI string does not follow the provcat URI rules."""


class ConfigError(ProvcatError):
    """The configuration is missing, unreadable or malformed."""


class ResolveError(ProvcatError):
    """A logical URI cannot be turned into a physical URI."""


class StoreError(ProvcatError):
    """A store failed to read, list or download an object."""


class NotFound(StoreError):
    """The object or folder that a URI names does not exist."""


class OfflineMiss(StoreError):
    """Offline mode is on and the cache does not hold the object."""
