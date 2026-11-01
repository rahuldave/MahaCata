---
title: MahaCata
---

MahaCata holds three Python packages that work together.

- **provcat** resolves catalog URIs. A catalog URI names a dataset, a model, a prompt or some code in words that stay the same when the storage changes. provcat turns it into the URI of a store, such as S3, lakeFS, GitHub or Hugging Face, or into a pinned version. It reads the object, or fetches it into a local cache and returns the path. A local file needs no cache. It writes to every store but GitHub and Hugging Face. It commits in lakeFS and in versioned buckets, branches in lakeFS and in any bucket, and merges in lakeFS. It makes read-only snapshots of pinned versions.
- **provrun** runs agents and model calls on data that provcat resolves. It asks a broker for each input, stages provcat's read-only snapshots of them and runs the agent. It records the evidence of the run as a chain of hashed events.
- **provevi** stores, reads and checks the evidence of runs. Its command is `prove`. The first of its five phases is built; [its page](provevi.md) says what it does now, and [the design](provevi-design.md) and [the plan](provevi-plan.md) say what comes next.

provrun depends on provevi and provcat, and provevi depends on provcat. provcat depends on neither.

## Start here

| To do this | Read |
|---|---|
| Follow catalog URIs through provcat, with commands to run | [A guided tour of provcat](tour.md) |
| Follow one file from a store into a run and its evidence | [A guided tour of provrun](provrun-tour.md) |
| Run agents with provrun | [provrun](provrun.md) |
| List, show and check the evidence of runs | [provevi](provevi.md) |
| Install lakeFS, etcd and the tools that the tests need, and run the tests | [Setup](setup.md) |
| Learn how the parts of provcat fit together | [Design](design.md) |
| Learn what a write and a commit guarantee | [Transactions](transactions.md) |
| Run the shared lakeFS that keeps orgs apart | [The reference server](reference-server.md) |
| Read why provrun is built as it is | [The provrun plan](provrun-plan.md) |
| Read how the evidence store will be built | [The provevi design](provevi-design.md) and [plan](provevi-plan.md) |
| Look up a class or a function | [Reference](api/index.md) |
| Look up the `provc` commands, URI forms, stores and configuration | [The provcat reference](reference.md) |

## Install

You need Python 3.13 and [uv](https://docs.astral.sh/uv/). provcat and provrun are not on PyPI, so clone the repository:

```
git clone https://github.com/rahuldave/MahaCata
cd MahaCata
uv sync
uv run provc --help
uv run provr --help
```

## This site

smalldocs builds this site from the folder `docs`.

```
uv run --group docs smalldocs preview docs
```
