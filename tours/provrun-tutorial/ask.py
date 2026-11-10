"""Run a run file from Python, as provr run does, and print the answer.
    ask.py RUN_FILE"""
import sys

from provcat import Catalog
from provrun import RunFile, run_file_sync

outcome = run_file_sync(RunFile.load(sys.argv[1]), catalog=Catalog.open())
print(outcome.status, outcome.run_id)
print((outcome.folder / "output" / "response.txt").read_text())
