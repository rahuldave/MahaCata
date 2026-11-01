---
title: provevi
---

provevi stores, reads and checks the evidence of provrun's runs. Its command is `prove`. Phases 1 and 2 of [the plan](provevi-plan.md) are built. Phase 1 gave the package, the `file` store and the checks. Phase 2 gave the `sqlite` store, which answers questions across runs. The copy in MLflow, experiments as catalog objects and the span receiver come in later phases.

## Terms

- **Run folder:** the folder of one run, under the runs root. It holds `evidence/`, `output/` and, when the run has one, `bundle.json`.
- **Evidence:** the file `events.jsonl`, the folder `payloads/` and the file `manifest.json`, in `evidence/`.
- **Store:** an object that keeps the evidence of runs. `FileStore` writes the files in each run folder, as provrun always has. `SqliteStore` also keeps every run in one SQLite file.
- **Store URI:** the name of a store. `file` is the file store, and `sqlite:///evidence.db` is a SQLite file.
- **Experiment:** the runs of one agent in one org, domain and environment, such as `acme/demo/dev/revenue-question`.
- **Writer:** what a store gives for one run. provrun's recorder hands it each event line and each payload, and seals it at the end.
- **Committed bundle:** a copy of the run's evidence that provrun commits through provcat, in another store.

## How the packages depend on each other

provrun depends on provevi, and provevi depends on provcat. provevi never imports provrun.

provrun still decides what each event means, and builds the hash chain. provevi holds the event format, the stores and every check.

## Check a run from a program

Each check is a library function. It returns the problems it finds, and prints nothing. A malformed run folder, such as a `bundle.json` that is not JSON, also gives a problem, not an exception.

```python
from provevi import find_run, verify_run

check = verify_run(find_run("run-20261031T182026Z-f9c3b25f"))
print(check.ok, check.problems)
```

`verify_run` checks the evidence first, then the outputs, then the committed bundle:

- `verify` checks the hash chain, the sequence, the payloads and the manifest of an evidence folder, or of a run that a store reads back.
- `verify_outputs` checks that `output/` holds exactly the recorded outputs, with their bytes.
- `verify_bundle` reads the events and the manifest from the committed bundle, through a provcat `Catalog`, and compares them with the folder.

Pass `store=` to check the run as a store holds it, too. With a `SqliteStore`, `verify_run` checks the rows of the run, and that the files in the run folder are the same as the rows.

Pass `catalog=` as a `Catalog`, or as a function that opens one. A function opens no catalog when the run has no bundle. Pass `bundle=` with the pinned URI of the bundle, when you do not trust the `bundle.json` in the run folder. `require_bundle=True` fails a run that has no bundle to compare with.

[provrun's page](provrun.md) lists what the evidence check covers.

## Write a run through a store

`run_file` and `run_agent` in provrun take `store=`. Without it, `run_file` opens the store that the run file names in `run: store:`, and without that it uses `FileStore()`. `provr --store URI run` takes the place of the run file's store.

```python
from provcat import Catalog
from provevi import SqliteStore
from provrun import RunFile, run_file_sync

outcome = run_file_sync(RunFile.load("run.yaml"), catalog=Catalog.open(), store=SqliteStore("evidence.db"))
```

A store is a subclass of `EvidenceStore`. Its `open_run(run_id, folder)` returns a `RunWriter`, which appends lines, stores payloads by their SHA-256, and seals the run with its manifest.

`open_store(uri)` gives the store of a store URI. A store can also read a run back, with `read_run(run_id)`, which returns a `RunRecord`. The checks read a run through a `RunRecord`.

Every store must also keep `events.jsonl`, `payloads/` and `manifest.json` in `folder`. provrun commits that folder as the bundle. A store that keeps no `events.jsonl` and `manifest.json` fails the run when it closes, in both modes, because nothing could verify the run. A seal that fails also fails the run in both modes.

Before the seal, provrun treats any error from a writer as a lost event. The strict mode stops the run, and the lenient mode records the loss in the manifest.

## Keep the evidence in SQLite

`SqliteStore(path)` keeps the evidence of many runs in one SQLite file. Its store URI is `sqlite:///relative/path.db`, or `sqlite:////absolute/path.db`, as SQLAlchemy and MLflow write them.

- A relative path is fixed against the current folder when the store opens. So a run file's `store:` is relative to the folder you run `provr` in, not to the run file.
- `~` is your home folder.
- A URI with a query or a fragment, such as `?mode=ro`, is refused.

How the store writes:

- **Each event is a row as it happens.** The row is committed only once the line is also in the run's `events.jsonl`. If the file write or the commit fails, both are taken back, and the recorder counts the event as lost. If the process dies between the two, the file holds one more line than the rows, and the check against the store reports it.
- **The run folder holds the same files as the `file` store.** So the bundle that provrun commits is the same in both stores. The payload bytes live only in the run's `payloads/` folder, and the table records their hash, size and place.
- **Many runs can write one file at once,** from threads or from processes. A writer waits up to a minute for another one.
- **The file records its schema version.** Opening a file to write brings an older version up to date, one step at a time. A file of a newer provevi is refused.
- **`SqliteStore(path, create=False)` only reads.** It writes nothing, and refuses a missing file, a file that holds no provevi evidence, and a file of another schema version. `prove` opens the store this way.
- **provevi uses SQLite's WAL mode only.** A writer puts the file in WAL mode, which stays in the file. Every connection checks it, and a file in any other mode is refused. Even to read, SQLite writes a side file next to a WAL file, so the file's folder must be writable.
- **A copy may leave WAL mode.** A copy made with `VACUUM INTO` is in another mode, so `prove` refuses it. Open the copy once to write, as `SqliteStore(path)` or `provr run` does, and it is in WAL mode again. A copy in a folder you cannot write must first be copied to one you can.
- **The schema is checked when the store opens.** provevi's tables, indexes and views must be exactly what its upgrade steps make. A file whose schema was changed is refused, because the queries read the views.
  - An extra index on a provevi table is allowed, since it cannot change what a query returns.
  - A trigger is refused if it is on a provevi table, or if its text names one.
- **A run sealed without its ending event** has the status `incomplete`.
- **The table names start with `provevi_`,** so the same file can also hold MLflow's tables.

| Table or view | What it holds |
|---|---|
| `provevi_runs` | One row for each run: its experiment, actor, status, times, mode, folder, last hash, manifest, and where its bundle was committed. |
| `provevi_run_params` | The agent, backend, model, mode, tools and limits of a run. |
| `provevi_experiments` | One row for each experiment, made by its first run. It holds that run's backend, model, tools and limits, and the SHA-256 of its whole agent spec. |
| `provevi_events` | Each event: its line, and its fields as columns, with the trace and span IDs. |
| `provevi_event_refs` | Each URI that an event names. |
| `provevi_payloads` | The SHA-256, size and place of each payload. |
| `provevi_assessments` | Judgements on a run or an event. Nothing writes this table yet. |
| `provevi_grants`, `provevi_refusals`, `provevi_inputs`, `provevi_outputs` | Views over the events. |

The agent's `settings` may hold secrets, so the run's settings and the experiment leave them out. The `run_created` event still holds the whole spec, in the file as in `events.jsonl`.

A changed or deleted row fails the check:

- the chain covers each event's line;
- each row's columns and URIs must match its line;
- the run's manifest must match its hash;
- the run's org, domain, environment, actor, mode, experiment, status and times must be what its events say;
- its settings, its experiment's name and parts, and its payload sizes must match its events and files;
- where its bundle was committed must match the run's `bundle.json`, if the store recorded it;
- the schema must be provevi's own, with no trigger on its tables.

The check does not cover the run's folder, the time of its seal, or who made an experiment and when. Nothing in the events fixes those.

The store answers questions across runs:

| Method | Returns |
|---|---|
| `runs_reading(uri)` | The runs that staged `uri` as an input, by its URI or by a pinned version of it. |
| `runs_of_experiment(name)` | The runs of an experiment, by its name or its `experiment://` URI. |
| `experiments()` | The experiments in the file. |
| `run(run_id)` | The run's row, with its settings as `params`. |
| `access(run_id)` | The grants and refusals of a run, in order. |
| `inputs(run_id)` | The inputs that a run staged, with their pinned URIs. |

## The command `prove`

`prove` only reads and checks. provrun writes the runs.

| Command | What it does |
|---|---|
| `prove runs` | Lists the run IDs in the store, oldest first. |
| `prove runs --reading URI` | Lists the runs that staged the URI as an input. |
| `prove runs --experiment NAME` | Lists the runs of an experiment. |
| `prove experiments` | Lists the experiments. |
| `prove access RUN` | Prints the grants and refusals of a run. |
| `prove show RUN` | Prints the events of a run. |
| `prove verify RUN` | Checks the evidence, the outputs and the committed bundle. It takes `--config`, `--tenant`, `--bundle` and `--require-bundle`, as `provr verify` does. |

`--store URI` chooses the store, before the command, as in `prove --store sqlite:///evidence.db runs`. The default is the file store under the runs root. The file store has no index of runs, so `--reading`, `--experiment`, `experiments` and `access` need a `sqlite` store.

`RUN` is a run ID under the runs root, or the path of a run folder. With a `sqlite` store, a run ID that is not under the runs root is found at the folder that the store records. `show` reads the events from the store when the run is in it. A run ID that names a link under the runs root is refused, so a link cannot pass off one run as another. The runs root is the same as provrun's. It is `--runs`, or else `PROVRUN_RUNS`, or else `$XDG_DATA_HOME/provrun/runs`, or else `~/.local/share/provrun/runs`.

`provr verify` calls the same code, so the two commands give the same answer.

The repository holds a run that provrun wrote before provevi existed, in `packages/provevi/tests/data`. `prove` reads and verifies it:

```
$ uv run prove --runs packages/provevi/tests/data runs
run-20261031T182026Z-f9c3b25f

$ uv run prove --runs packages/provevi/tests/data verify run-20261031T182026Z-f9c3b25f
run-20261031T182026Z-f9c3b25f: the evidence and the outputs verify; no committed bundle to compare with

$ uv run prove --runs packages/provevi/tests/data verify run-20261031T182026Z-f9c3b25f --require-bundle
prove: no committed bundle to compare with, and a bundle is required
```

`prove show` prints one line for each event, with its sequence number, time, type, component, references and data. The two lines below are cut short at the right:

```
$ uv run prove --runs packages/provevi/tests/data show run-20261031T182026Z-f9c3b25f
   0 2026-10-31T18:20:26.242767Z run_created              RunContext               run://acme/demo/dev/run-20261031T182026Z-f9c3b25f {"agent": "q", ...
   ...
   6 2026-10-31T18:20:35.505766Z run_completed            RunContext               run://acme/demo/dev/run-20261031T182026Z-f9c3b25f {}
```

## Exit codes

| Code | Meaning |
|---|---|
| 0 | The command worked, and the run verifies. |
| 1 | The run does not verify, or there is no such run, or the catalog cannot be opened. Also a store URI that names no store, a store file that cannot be opened or was changed, and a question that the file store cannot answer. |
| 2 | `prove show` cannot read the events, or met a line that is not an event. Also a command line that argparse rejects. |

## Errors

Every error that provevi raises on purpose is a `ProveviError`. `RunNotFound` is also a `LookupError`. provrun's own `EvidenceError` and `RunNotFound` are subclasses of provevi's, so code that catches provevi's errors also catches provrun's.
