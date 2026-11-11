---
title: Reference
---

These pages come from the docstrings and the types of the code. Each build writes them again.

- **provcat** covers catalog URIs, the configuration, the resolver, and `Catalog` with its reads, writes and snapshots. It also covers the hooks, the fsspec file system, the stores and the cache.
- **provrun** covers the run entry for programs, the run context, the evidence, the broker, the resolver and the stager. It also covers the specs, the backends, the tools, the outputter and the runner. Last, it covers the control of library telemetry, where a run's spans go, and `provr`. `provrun.live` gives a run's live feed.
- **provevi** covers the event format, the stores and their writers, and the `sqlite` store and its queries. It also covers the checks of a run, the functions that find runs, the telemetry policy and the copy in MLflow. Last, it covers what a live viewer of a run sees, experiments in the catalog, and `prove`.

The sidebar lists one page for each public module.
