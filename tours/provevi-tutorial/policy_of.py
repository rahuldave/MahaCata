"""Print the telemetry policy that each run recorded, from its first event.
    policy_of.py RUNS RUN [RUN ...]
RUNS is the folder of the run folders."""
import sys
from pathlib import Path

from provevi import read_events

for run_id in sys.argv[2:]:
    telemetry = read_events(Path(sys.argv[1]) / run_id / "evidence")[0].data["telemetry"]
    print(run_id, telemetry["policy_source"], telemetry["policy"])
