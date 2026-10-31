---
title: provevi
---

provevi stores, reads and checks the evidence of provrun's runs. Its command is `prove`. Phase 1 of [the plan](provevi-plan.md) is built. It gave the package, the `file` store and the checks. The `sqlite` store, the copy in MLflow, experiments and the span receiver come in later phases.

## Terms

- **Run folder:** the folder of one run, under the runs root. It holds `evidence/`, `output/` and, when the run has one, `bundle.json`.
- **Evidence:** the file `events.jsonl`, the folder `payloads/` and the file `manifest.json`, in `evidence/`.
- **Store:** an object that keeps the evidence of runs. `FileStore` writes the files in each run folder, as provrun always has.
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

- `verify` checks the hash chain, the sequence, the payloads and the manifest of an evidence folder.
- `verify_outputs` checks that `output/` holds exactly the recorded outputs, with their bytes.
- `verify_bundle` reads the events and the manifest from the committed bundle, through a provcat `Catalog`, and compares them with the folder.

Pass `catalog=` as a `Catalog`, or as a function that opens one. A function opens no catalog when the run has no bundle. Pass `bundle=` with the pinned URI of the bundle, when you do not trust the `bundle.json` in the run folder. `require_bundle=True` fails a run that has no bundle to compare with.

[provrun's page](provrun.md) lists what the evidence check covers.

## Write a run through a store

`run_file` and `run_agent` in provrun take `store=`. The default is `FileStore()`, so a caller that passes none gets the same run folder as before.

```python
from provcat import Catalog
from provevi import FileStore
from provrun import RunFile, run_file_sync

outcome = run_file_sync(RunFile.load("run.yaml"), catalog=Catalog.open(), store=FileStore())
```

A store is a subclass of `EvidenceStore`. Its `open_run(run_id, folder)` returns a `RunWriter`, which appends lines, stores payloads by their SHA-256, and seals the run with its manifest.

In Phase 1, a store must also keep `events.jsonl`, `payloads/` and `manifest.json` in `folder`. The checks and the committed bundle read them there. A store that keeps no `events.jsonl` and `manifest.json` fails the run when it closes, in both modes, because nothing could verify the run. A seal that fails also fails the run in both modes. Reading a run back through the store comes in Phase 2.

Before the seal, provrun treats any error from a writer as a lost event. The strict mode stops the run, and the lenient mode records the loss in the manifest.

## The command `prove`

`prove` only reads and checks. provrun writes the runs.

| Command | What it does |
|---|---|
| `prove runs` | Lists the run IDs under the runs root, oldest first. |
| `prove show RUN` | Prints the events of a run. |
| `prove verify RUN` | Checks the evidence, the outputs and the committed bundle. It takes `--config`, `--tenant`, `--bundle` and `--require-bundle`, as `provr verify` does. |

`RUN` is a run ID under the runs root, or the path of a run folder. A run ID that names a link under the runs root is refused, so a link cannot pass off one run as another. The runs root is the same as provrun's. It is `--runs`, or else `PROVRUN_RUNS`, or else `$XDG_DATA_HOME/provrun/runs`, or else `~/.local/share/provrun/runs`.

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
| 1 | The run does not verify, or there is no such run, or the catalog cannot be opened. |
| 2 | `prove show` cannot read the events, or met a line that is not an event. Also a command line that argparse rejects. |

## Errors

Every error that provevi raises on purpose is a `ProveviError`. `RunNotFound` is also a `LookupError`. provrun's own `EvidenceError` and `RunNotFound` are subclasses of provevi's, so code that catches provevi's errors also catches provrun's.
