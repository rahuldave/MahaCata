---
title: MahaCata
---

MahaCata holds two Python packages that work together.

- **provcat** resolves catalog URIs. A catalog URI names a dataset, a model, a prompt or some code in words that stay the same when the storage changes. provcat turns it into the URI of a store, such as S3, lakeFS or GitHub, or into a pinned version. It reads the object, or fetches it into a local cache and returns the path. A local file needs no cache. It writes to every store but GitHub. It commits in lakeFS and in versioned buckets, branches in lakeFS and in any bucket, and merges in lakeFS. It makes read-only snapshots of pinned versions.
- **provrun** runs agents and model calls on data that provcat resolves. It asks a broker for each input, stages provcat's read-only snapshots of them and runs the agent. It records the evidence of the run as a chain of hashed events.

provrun depends on provcat, and provcat does not depend on provrun.

## Start here

| To do this | Read |
|---|---|
| Follow catalog URIs through provcat, with commands to run | [A guided tour of provcat](tour.md) |
| Follow one file from a store into a run and its evidence | [A guided tour of provrun](provrun-tour.md) |
| Run agents with provrun | [provrun](provrun.md) |
| Install lakeFS, etcd and the tools that the tests need | [Setup](setup.md) |
| Learn how the parts of provcat fit together | [Design](design.md) |
| Learn what a write and a commit guarantee | [Transactions](transactions.md) |
| Run the shared lakeFS that keeps orgs apart | [The reference server](reference-server.md) |
| Look up a class or a function | [Reference](api/index.md) |
| Look up the `provc` commands, URI forms, stores and configuration | The [README](https://github.com/rahuldave/MahaCata#readme) on GitHub |

## Install

You need Python 3.13 and [uv](https://docs.astral.sh/uv/). From the top folder of the repository:

```
uv sync
uv run provc --help
uv run provr --help
```

## This site

smalldocs builds this site from the folder `docs`.

```
uv run --group docs smalldocs preview docs
```
