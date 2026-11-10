"""Write the tour's experiment in the catalog, as a new version, and print its
pin and the SHA-256 of its file.
    experiment.py DESCRIPTION METRIC [METRIC...]"""
import sys

from provcat import Catalog
from provevi import Experiment, write_experiment

URI = "experiment://acme/demo/dev/revenue/retrieval"

pinned = write_experiment(Catalog.open(), URI, Experiment("agent-eval", sys.argv[1], (), tuple(sys.argv[2:])),
                          sys.argv[1])
print(pinned.pin)
print(pinned.sha256)
