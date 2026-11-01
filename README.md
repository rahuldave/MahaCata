# MahaCata

MahaCata holds three Python packages, provcat, provrun and provevi.

provcat resolves catalog URIs. A catalog URI names a dataset, a model, a prompt or some code in words that stay the same when the storage changes. provcat turns it into the URI of a store, such as S3, lakeFS, GitHub or Hugging Face, or into a pinned version. It reads the object, or fetches it into a local cache and returns the path. A local file needs no cache.

provcat writes to every store but GitHub and Hugging Face. It commits in lakeFS and in versioned buckets, branches in lakeFS and in any bucket, and merges in lakeFS. It makes read-only snapshots of pinned versions.

The documentation is at [https://rahuldave.com/MahaCata/](https://rahuldave.com/MahaCata/). Its sources are in the folder `docs`:

- [The tour of provcat](docs/tour.md) gives commands to run, with their output.
- [The reference](docs/reference.md) lists the commands, the forms of a URI, the stores, the configuration and the cache.
- [Setup](docs/setup.md) shows how to install lakeFS, etcd and the tools that the tests need, and how to run the tests.
- [The design](docs/design.md) explains how the parts fit together, and lists the known limits.
- [Transactions](docs/transactions.md) says what a write and a commit guarantee, in lakeFS and in a bucket.
- [The reference server](docs/reference-server.md) shows how to run the shared lakeFS that keeps orgs apart.

The first commit of this repository is the provcat code as it was found in `~/Catalog/provcat`.

## Layout

This repository is a uv workspace with three Python packages. A uv workspace is one repository with several packages, one lock file and editable installs between them.

| Package | Folder | What it does |
|---|---|---|
| provcat | `packages/provcat` | Resolves catalog URIs, caches files and writes through to stores. Its command is `provc`. |
| provrun | `packages/provrun` | Runs agents and model calls on data that provcat resolves, and records the evidence of each run. Its command is `provr`. Read [docs/provrun.md](docs/provrun.md), and take the tour in [docs/provrun-tour.md](docs/provrun-tour.md). |
| provevi | `packages/provevi` | Stores, reads and checks the evidence of runs. Its command is `prove`. Read [docs/provevi.md](docs/provevi.md). |

provrun depends on provevi and provcat, and provevi depends on provcat. [docs/provrun-plan.md](docs/provrun-plan.md) is the plan that provrun follows. The first two phases of provevi are built; [docs/provevi.md](docs/provevi.md) describes it, and [docs/provevi-design.md](docs/provevi-design.md) and [docs/provevi-plan.md](docs/provevi-plan.md) describe the rest.

The folder `docs` is also [the website](https://rahuldave.com/MahaCata/), with a reference page for each module. [smalldocs](https://github.com/rahuldave/smalldocs) builds it, and it is published on each push to `main`. Run `uv run --group docs smalldocs preview docs`, and open http://127.0.0.1:4200.

## Install and run

provcat needs Python 3.13 and [uv](https://docs.astral.sh/uv/).

```
uv sync
uv run provc --help
uv run pytest
```

The base package reads local files, GitHub, S3 and lakeFS. Google Cloud Storage, Azure and Hugging Face each need an extra, which installs the Python SDK of that service:

provcat is not on PyPI. In another project, install it from GitHub, with the extras that you need in the brackets:

```
pip install 'provcat[hf] @ git+https://github.com/rahuldave/MahaCata#subdirectory=packages/provcat'
```

In a clone, `uv sync` installs all three.
