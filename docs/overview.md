---
title: What MahaCata does
---

MahaCata follows one file from a store, into a run of an agent, and into the evidence of that run. Three packages share the work. provcat names and fetches the file, provrun runs the agent on it, and provevi keeps the evidence of the run and checks it.

This page shows one example from each package. Each one comes from [the provrun tour](provrun-tour.md), which runs the whole path on your own machine. Each package also has its own page of features and its own tour, listed at the end.

## Terms

| Term | Meaning |
|---|---|
| logical URI | A name that code uses, such as `data://acme/demo/dev/reports/report.txt`. It does not say where the data is. provcat's configuration turns it into a physical URI. |
| physical URI | An address in one store, such as `local-s3://data/main/acme/demo/dev/reports/report.txt`. |
| pinned URI | A URI with a fixed version, such as a physical URI that ends in `?at=version:ff132f17-...`. |
| cache | The folder where provcat keeps the files that it fetched. |
| snapshot | A read-only copy of one pinned version, in a folder named by the SHA-256 of its content. |
| evidence | The events of a run, each holding the SHA-256 of the one before, and the payloads they name, such as the prompt. |
| bundle | The evidence and the outputs of a run. If the run file sets `bundle_scheme`, provrun commits the bundle to a store. |
| telemetry policy | The operator's file that says which libraries may record a run, which hosts it may reach, and where data from it may go. |
| destination | A place outside the evidence that data may go to, such as an OpenTelemetry collector or MLflow. |

## The path of one file

provrun drives every step, and calls provcat for the steps that touch a store. The picture below is a short form of [the tour's picture](provrun-tour.md#the-path-of-one-input).

```text
data://acme/demo/dev/reports/report.txt            the logical URI in the run file
  | provrun asks the broker: may this run read it?
  | provcat resolves it to the store, and pins a version V
  v
local-s3://data/main/.../report.txt?at=version:V   the pinned URI
  | provcat fetches V into the cache, and copies it into a read-only snapshot
  v
cache/.provcat/snapshots/file/<sha256>/report.txt
  | provrun stages a read-only clone in the run folder
  v
runs/<run ID>/input/report.txt                     the agent reads its input here
  | provrun records each step as an event, through a provevi store
  v
the evidence of the run                            provevi keeps it, queries it and checks it
```

provrun depends on provevi and provcat, and provevi depends on provcat. provcat depends on neither, so it works alone.

## provcat names the file and pins it

The run file names its input by a logical URI. provcat turns that name into a physical URI. Here it writes the file to a versioned bucket, and commits the folder.

```
$ uv run provc put data://acme/demo/dev/reports/report.txt $TOUR_DIR/report.txt
[
  {
    "uri": "local-s3://data/main/acme/demo/dev/reports/report.txt",
    "kind": "file",
    "size": 32,
    "version": "de5877cfcc0dfd578914982c36e261dc"
  }
]

$ uv run provc commit data://acme/demo/dev/reports -m 'first report'
local-s3://data/main/acme/demo/dev/reports?at=commit:370305b1b05729828309544ccacf185983325762c9e9f54a063fb21a5e347ee2
```

The `version` that `put` prints is the ETag of the object, a hash that S3 gives its content. A pin shows the version ID instead, which a versioned bucket gives each write.

```
$ uv run provc pin data://acme/demo/dev/reports/report.txt
local-s3://data/main/acme/demo/dev/reports/report.txt?at=version:ff132f17-7295-4220-8667-de2e7316bae0
```

The logical URI in the run file stays the same when the data moves to another store. Only provcat's configuration changes. A pinned URI names one store and one version, so it does not follow the move.

## provrun runs the agent on it

provrun asks a broker whether the run may read each input. It stages a read-only snapshot of the pinned version, and runs the agent. The tour's run file sets `bundle_scheme`, so provrun then commits the bundle to a store.

```
$ uv run provr run $TOUR_DIR/run.yaml | tee $TOUR_DIR/r1.json
{
  "run_id": "run-20261102T224004Z-d7651699",
  "status": "completed",
  "folder": "/tmp/provrun-tour/runs/run-20261102T224004Z-d7651699",
  "bundle": "run://acme/demo/dev/run-20261102T224004Z-d7651699",
  "pinned": "local-s3://runs/main/acme/demo/dev/run-20261102T224004Z-d7651699?at=commit:2c12c5e026ede88e9f12a86a480f06d1a772b97cd475329e0dce9fe801f62f5d",
  "note": "",
  "error": null
}
```

`pinned` is the URI of the commit that holds the bundle. A checker can read the bundle back from it later.

## provevi keeps the evidence and checks it

provrun hands each event to a provevi store as it happens. A `sqlite` store keeps every run in one SQLite file, so you can ask questions across runs. Only runs started with `provr --store` go into that file. The tour put two runs in it. The command below asks which of them read the report.

```
$ uv run prove --store $S runs --reading data://acme/demo/dev/reports/report.txt
run-20261102T224043Z-e6ed9133
run-20261102T224050Z-406f3eaf
```

provevi also finds a change to the evidence. Here the tour deleted the third event of the run `$Q` from the SQLite file, and left the run folder as it was.

```
$ uv run python tours/provrun/drop_event.py $TOUR_DIR/evidence.db $Q 3

$ uv run prove --store $S verify $Q || echo "exit status $?"
prove: in the store: line 3: seq is 4, not 3
prove: in the store: line 3: the chain is broken
prove: in the store: refs of row 3 have no event
prove: in the store: the manifest counts 13 events, and there are 12
exit status 1
```

Without `--store`, `prove verify` checks only the run folder, and that check still passes.

When the telemetry policy approves an MLflow destination, provrun has provevi copy each sealed run into MLflow, for MLflow's viewer. The policy says what each destination receives, from names only up to the prompts and answers.

## Where to go next

| To do this | Read |
|---|---|
| Learn the whole path step by step, with an explanation of each step | [The tutorial](tutorial.md) |
| Run the whole path on your own machine, from one file in a store to the evidence of its runs | [A runnable tour of provrun](provrun-tour.md) |
| See what provcat does, with examples | [What provcat does](provcat-features.md), and [its tour](tour.md) |
| See what provrun does, with examples | [What provrun does](provrun-features.md), and [provrun's reference](provrun.md) |
| See what provevi does, with examples | [What provevi does](provevi-features.md), and [its tour](provevi-tour.md) |
| Look up a class or a function | [Reference](api/index.md) |

The examples ran in the provrun tour's shell, after `source tours/provrun/start.sh`. `$S` is the SQLite store of the tour's Step 9, and `$Q` is the first of its two runs. The run IDs, version IDs and hashes differ on each run.
