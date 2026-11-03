"""Run agents and model calls on catalog data, and record the evidence of each run.

A program uses provrun as ``provr`` does::

    from provcat import Catalog
    from provrun import RunFile, run_file_sync

    outcome = run_file_sync(RunFile.load("run.yaml"), catalog=Catalog.open())
    print(outcome.to_dict())

A program that shows a run as it goes, such as a web server, starts it with
``start_run`` and follows its live feed; ``provrun.live`` says how.

Every error that provrun raises on purpose is a ``ProvrunError``.
"""
from .context import default_runs_root, find_run
from .errors import ProvrunError, RunFileError, RunNotFound, RunSettingError
from .evidence import read_events
from .live import LiveRun, start_run
from .runner import RunOutcome, run_agent, run_file, run_file_sync
from .spec import AgentSpec, InputSpec, RunFile, RunSettings

__all__ = [
    "AgentSpec", "InputSpec", "LiveRun", "ProvrunError", "RunFile", "RunFileError", "RunNotFound", "RunOutcome",
    "RunSettingError", "RunSettings", "default_runs_root", "find_run", "read_events", "run_agent", "run_file",
    "run_file_sync", "start_run",
]
