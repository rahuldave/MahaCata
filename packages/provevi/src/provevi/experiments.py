"""An experiment: a question a team asks again and again in a project.

An experiment is a folder in the catalog, at a logical URI such as
``experiment://acme/risk/dev/credit/q``, holding one file, ``experiment.yaml``.
A commit of the folder is a version, so the folder must be in a store with
commits. The file says what every run of the experiment shares::

    kind: agent-eval
    description: Does a retrieval step raise accuracy on the revenue questions?
    inputs:
      - data+table://acme/risk/dev/evals/revenue
    metrics: [accuracy]

``kind`` is ``agent-eval``, an agentic experiment for evals, or ``ml``, a
machine-learning experiment. The file holds no agent spec, so one experiment
can try many agents. ``write_experiment`` writes and commits the file, and
``read_experiment`` reads it at a pin. A run names its experiment beside its
agent, and records the pin and the file's hash in ``run_created``.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import yaml

from .errors import ProveviError
from .events import sha256_bytes

#: The kinds of experiment.
KINDS = ("agent-eval", "ml")

#: The name of the experiment's file in its folder.
FILE = "experiment.yaml"

_KEYS = ("kind", "description", "inputs", "metrics")


@dataclass(frozen=True)
class Experiment:
    """What every run of an experiment shares: its kind, a description, the
    inputs that each run reads, such as an eval set, and its metrics."""
    kind: str
    description: str
    inputs: tuple[str, ...] = field(default=())
    metrics: tuple[str, ...] = field(default=())

    def __post_init__(self):
        if self.kind not in KINDS:
            raise ProveviError(f"an experiment's kind is {' or '.join(KINDS)}, not {self.kind!r}")
        if not isinstance(self.description, str) or not self.description.strip():
            raise ProveviError("an experiment needs a description")
        for name, values in (("inputs", self.inputs), ("metrics", self.metrics)):
            if isinstance(values, str) or not all(isinstance(v, str) and v for v in values):
                raise ProveviError(f"an experiment's {name} must be a list of texts")
        object.__setattr__(self, "inputs", tuple(self.inputs))
        object.__setattr__(self, "metrics", tuple(self.metrics))

    def to_dict(self) -> dict[str, Any]:
        return {"kind": self.kind, "description": self.description, "inputs": list(self.inputs),
                "metrics": list(self.metrics)}

    def to_bytes(self) -> bytes:
        """The file, as YAML with its keys in a fixed order."""
        return yaml.safe_dump(self.to_dict(), sort_keys=False, allow_unicode=True).encode()

    @classmethod
    def from_bytes(cls, raw: bytes, where: str = "the experiment") -> Experiment:
        """The experiment in a file. Raises ProveviError for a file that is
        not an experiment, or has a key it does not know."""
        try:
            data = yaml.safe_load(raw.decode("utf-8"))
        except (UnicodeDecodeError, yaml.YAMLError) as e:
            raise ProveviError(f"{where} is not YAML: {e}") from e
        if not isinstance(data, dict):
            raise ProveviError(f"{where} is not a mapping")
        unknown = sorted(set(data) - set(_KEYS), key=str)
        if unknown:
            raise ProveviError(f"{where} has keys an experiment does not have: {', '.join(map(str, unknown))}")
        for name in ("inputs", "metrics"):
            if data.get(name) is not None and not isinstance(data[name], list):
                raise ProveviError(f"{where}: an experiment's {name} must be a list of texts")
        try:
            return cls(data.get("kind"), data.get("description"), tuple(data.get("inputs") or ()),
                       tuple(data.get("metrics") or ()))
        except ProveviError as e:
            raise ProveviError(f"{where}: {e}") from None
        except TypeError as e:
            raise ProveviError(f"{where}: an experiment's inputs and metrics must be lists of texts") from e


@dataclass(frozen=True)
class PinnedExperiment:
    """An experiment as one version holds it: its URI, the URI pinned to that
    version, and the SHA-256 of its file there."""
    uri: str
    pin: str
    sha256: str
    experiment: Experiment


def _folder(uri: str) -> str:
    """The experiment's URI with no selector. Raises ProveviError for one
    that is not an experiment's."""
    if not isinstance(uri, str) or not uri.startswith("experiment://"):
        raise ProveviError(f"{uri!r} is not an experiment's URI, which starts with experiment://")
    base = uri.split("?", 1)[0]
    if base.endswith("/" + FILE):
        raise ProveviError(f"{uri}: name the experiment's folder, not its file")
    return base


def write_experiment(catalog, uri: str, experiment: Experiment, message: str) -> PinnedExperiment:
    """Write the experiment's file in its folder at uri, commit the folder
    with message, and return the new version. The catalog's hooks see the
    write and the commit. Raises ProveviError, and provcat's errors, as for
    a store with no commits."""
    base = _folder(uri)
    if "?" in uri:
        raise ProveviError(f"{uri}: write an experiment at its URI, with no ?at=")
    raw = experiment.to_bytes()
    catalog.put(f"{base}/{FILE}", raw)
    catalog.commit(base, message)
    return read_experiment(catalog, base)


def read_experiment(catalog, uri: str) -> PinnedExperiment:
    """The experiment at uri, pinned: its newest version, or the version
    that uri's ``?at=`` names. Raises ProveviError for a store whose pin
    names no version, as a file store's does."""
    base = _folder(uri)
    pinned = str(catalog.pin(uri))
    if "?at=commit:" not in pinned:
        raise ProveviError(f"{uri}: its store has no commits, so the experiment cannot be pinned")
    selector = pinned.split("?at=", 1)[1]
    pin = f"{base}?at={selector}"
    raw = catalog.read(f"{base}/{FILE}?at={selector}")
    return PinnedExperiment(base, pin, sha256_bytes(raw), Experiment.from_bytes(raw, f"{pin}/{FILE}"))


def experiment_problems(catalog, created_data: dict) -> list[str]:
    """What is wrong with the experiment that a run's run_created data names:
    a pin that cannot be read, or a file whose hash differs from the one the
    run recorded. An empty list for a run that names no experiment."""
    uri, pin, digest = (created_data.get(k) for k in ("experiment", "experiment_pin", "experiment_sha256"))
    if uri is None and pin is None and digest is None:
        return []
    if not all(isinstance(x, str) and x for x in (uri, pin, digest)):
        return ["the run names an experiment without its URI, its pin and its hash"]
    try:
        base = _folder(pin)
        if base != _folder(uri):
            return [f"the experiment's pin {pin} is not of {uri}"]
        if "?at=commit:" not in pin or "&" in pin or "#" in pin:
            return [f"the experiment's pin {pin} names no commit"]
        if "?at=commit:" in uri and uri != pin:
            return [f"the run named {uri}, but its pin is {pin}"]
        raw = catalog.read(f"{base}/{FILE}?at={pin.split('?at=', 1)[1]}")
    except Exception as e:  # provcat raises its own errors, and the store may be unreachable
        return [f"cannot read the experiment at {pin}: {e}"]
    if sha256_bytes(raw) != digest:
        return [f"the experiment at {pin} has changed: its hash is not {digest}"]
    return []
