"""A program that switches on a library's own telemetry before it starts a
run: a LiteLLM callback, and the root logger at DEBUG, writing to a file."""
import logging
import os

import litellm
from provcat import Catalog
from provrun import RunFile, run_file_sync

tour = os.environ["TOUR_DIR"]
logging.basicConfig(level=logging.DEBUG, filename=f"{tour}/debug.log")
litellm.success_callback.append(lambda *args, **kwargs: None)

outcome = run_file_sync(RunFile.load(f"{tour}/run.yaml"), catalog=Catalog.open())
print(outcome.run_id, outcome.status)
print("callbacks after the run:", len(litellm.success_callback))
print("the LiteLLM logger logs at:", logging.getLevelName(logging.getLogger("LiteLLM").getEffectiveLevel()))
