"""Print the version of each input that each run read, from the store.
    pins.py STORE RUN [RUN ...]
STORE is a store URI, such as sqlite:///evidence.db."""
import sys

from provevi import open_store

store = open_store(sys.argv[1])
for run_id in sys.argv[2:]:
    for row in store.inputs(run_id):
        print(f"{run_id}  {row['pinned']}")
        print(f"{' ' * len(run_id)}  sha256 {row['sha256']}")
