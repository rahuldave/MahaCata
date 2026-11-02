---
title: A runnable tour of provevi
---

This tour runs provevi on your own machine. It needs no account, no key, no server and no network. provrun runs a small agent twice, with the model's answer mocked, and keeps the evidence in a SQLite file. Then `prove` asks questions of the evidence, checks it, and copies a run into MLflow. [What provevi does](provevi-features.md) shows the features first. Read it before the tour if provevi is new to you.

A script, `tours/provevi/run.sh`, runs every step and prints each command before its output. The output below is from that script. The run IDs, the times and MLflow's IDs differ on each run.

## Terms

| Term | Meaning |
|---|---|
| run | One run of an agent by provrun, with an ID such as `run-20261102T191955Z-52539489`. |
| evidence | The events of a run, each chained to the one before it by a hash, with their payloads and a manifest. |
| store | Where provevi keeps the evidence. The tour's store is the SQLite file `$TOUR_DIR/evidence.db`, and `$STORE` names it. |
| experiment | The runs of one agent in one org, domain and environment, such as `acme/demo/dev/revenue-question`. |
| telemetry policy | A file that says which libraries may load, and where data from a run may go, besides the evidence. |
| copy in MLflow | A run rebuilt from its evidence in MLflow's tables, for MLflow's viewer. |

## Before you start

You need Python 3.13, [uv](https://docs.astral.sh/uv/) and `bash`.

1. Clone the repository, and go into its top folder. Run every command of the tour from there.

   ```
   git clone https://github.com/rahuldave/MahaCata
   cd MahaCata
   ```

2. Install the packages. This installs MLflow too.

   ```
   uv sync
   ```

## Run the whole tour

Run the script. It takes about a minute, most of it for starting Python and MLflow.

```
bash tours/provevi/run.sh
```

The script makes its scratch folder, runs the steps, and deletes the folder.

## Or run it step by step

1. Start the tour in your shell.

   ```
   source tours/provevi/start.sh
   ```

   The script does these things:

   - It makes the scratch folder `/tmp/provevi-tour`, or the folder in `TOUR_DIR`. It refuses a folder that is already there, unless an earlier tour made it.
   - It points `PROVRUN_RUNS` and `PROVCAT_CACHE` at that folder, so the tour does not touch your own runs or cache. It sets `STORE` to the tour's SQLite file.
   - It unsets `PROVRUN_TELEMETRY_POLICY`, the provcat variables, and every `MLFLOW_`, `OTEL_`, `LOGFIRE_` and `LITELLM_` variable, because a run records the telemetry variables of the shell.
   - It writes the document `report.txt` and the run file `run.yaml` into the folder.
   - It refuses to start a second tour in a shell that runs one, and to run outside the top folder of the repository. If it fails after its checks, it undoes what it did.

2. Copy each command of the steps below into the same shell, in order. Copy only the text after `$ `.

3. Stop the tour when you finish, in the same shell.

   ```
   source tours/provevi/stop.sh
   ```

   It deletes the scratch folder, and gives back each variable that `start.sh` changed its old value.

## The tour's files

| File | What it is |
|---|---|
| `tours/provevi/run.yaml` | The run file. The agent reads `report.txt` and answers one question. Its answer is mocked, so the run needs no key. |
| `tours/provevi/policy.yaml` | The telemetry policy. It approves LiteLLM at the version that the workspace pins, and a copy in MLflow in the store's own file, at the level `usage`. |
| `tours/provevi/run_id.py` | Prints the run ID from what `provr run` printed. |
| `tours/provevi/break_row.py` | Deletes one event from the evidence file, for Step 4. |
| `tours/provevi/query.py` | Asks the evidence file questions from Python, for Step 7. |

The run file names its input by its path in the tour's folder:

```yaml
agent:
  name: revenue-question
  backend: litellm
  model: openai/gpt-4o-mini
  prompt: What was revenue in 2025?
  inputs:
    - file:///tmp/provevi-tour/report.txt
  settings:
    mock_response: Revenue in 2025 was 42 million.
```

## Step 1: Run an agent, and keep its evidence in SQLite

`provr run` runs the agent, and `--store` keeps its evidence in the tour's SQLite file. Run it twice, and change the document between the runs.

```
$ uv run provr --store $STORE run $TOUR_DIR/run.yaml 2> /dev/null > $TOUR_DIR/first.json

$ RUN1=$(uv run python tours/provevi/run_id.py $TOUR_DIR/first.json)

$ printf 'Revenue in 2025 was 45 million.\n' > $TOUR_DIR/report.txt

$ uv run provr --store $STORE run $TOUR_DIR/run.yaml 2> /dev/null > $TOUR_DIR/second.json

$ RUN2=$(uv run python tours/provevi/run_id.py $TOUR_DIR/second.json)

$ echo $RUN1 $RUN2
run-20261102T191955Z-52539489 run-20261102T192005Z-c93d67ab
```

**What happened:**
- Each run wrote each event as a row of the file, as it happened. Each run folder under `$TOUR_DIR/runs` holds the same events as files.
- `RUN1` read the first version of the document, and `RUN2` read the changed one.

## Step 2: Ask which runs read a file

The SQLite store answers questions across runs.

```
$ uv run prove --store $STORE runs
run-20261102T191955Z-52539489
run-20261102T192005Z-c93d67ab

$ uv run prove --store $STORE experiments
acme/demo/dev/revenue-question  created by you@example.com at 2026-11-02T19:19:55.358841Z

$ uv run prove --store $STORE runs --reading file://$TOUR_DIR/report.txt
run-20261102T191955Z-52539489
run-20261102T192005Z-c93d67ab

$ uv run prove --store $STORE runs --experiment acme/demo/dev/revenue-question
run-20261102T191955Z-52539489
run-20261102T192005Z-c93d67ab
```

**What happened:**
- The experiment is the agent's name in its org, domain and environment. The first run made it.
- `runs --reading` found both runs, because each staged the document as an input.

## Step 3: Read the events of a run

`prove show` prints one line for each event. The `awk` keeps the sequence number, the type and the component, and drops the time, the URIs and the data. `prove access` prints what the run was allowed to read.

```
$ uv run prove --store $STORE show $RUN1 | awk '{print $1, $3, $4}'
0 run_created RunContext
1 access_requested StubBroker
2 access_granted StubBroker
3 context_resolved ProvcatResolver
4 snapshot_materialized ProvcatResolver
5 input_staged InProcessStager
6 sandbox_started LiteLLMBackend
7 prompt_rendered LiteLLMBackend
8 model_called LiteLLMBackend
9 sandbox_completed LiteLLMBackend
10 input_verified InProcessStager
11 output_artifact_created Outputter
12 run_completed RunContext

$ uv run prove --store $STORE access $RUN1
   2 granted  mounted_snapshot   file:///tmp/provevi-tour/report.txt  in the allow list; physical URI in dev
```

**What happened:**
- The run asked for the document, was granted it, and staged a read-only snapshot.
- It called the model, checked that the input had not changed, and wrote its answer.

## Step 4: Check a run, and break one

`prove verify` checks the chain of hashes, the payloads, the manifest and the outputs. Delete one event from the file, and check again.

```
$ uv run prove --store $STORE verify $RUN1
run-20261102T191955Z-52539489: the evidence and the outputs verify; no committed bundle to compare with

$ uv run python tours/provevi/break_row.py $TOUR_DIR/evidence.db $RUN1 3

$ uv run prove --store $STORE verify $RUN1 || echo exit $?
prove: in the store: line 3: seq is 4, not 3
prove: in the store: line 3: the chain is broken
prove: in the store: refs of row 3 have no event
prove: in the store: the manifest counts 13 events, and there are 12
exit 1

$ uv run prove verify $RUN1
run-20261102T191955Z-52539489: the evidence and the outputs verify; no committed bundle to compare with
```

**What happened:**
- With the row gone, the chain no longer joins, and the manifest counts one event more than the file holds. `prove verify` exits with 1.
- Without `--store`, `prove verify` reads the run folder, which still verifies. A check of the store and a check of the folder are separate, so changing one of them shows.

## Step 5: Check a telemetry policy

`prove policy` checks a policy file, and the installed version of each library that it approves. `prove telemetry` prints what the policy blocked in a run.

```
$ uv run prove policy tours/provevi/policy.yaml
tours/provevi/policy.yaml: sha256 718093cad9c0bf0ece06403cc43572baa7ac849978a81bbbfc181a540d83a02a
  library litellm ==1.83.0 feeds nothing
  destination viewer (mlflow) receives usage
  hosts: none

$ uv run prove --store $STORE telemetry $RUN2
run-20261102T192005Z-c93d67ab: no blocked attempts and no problems
```

**What happened:** the installed LiteLLM is the version that the policy approves, so the check passes. The run ran under no policy, and nothing tried to send telemetry, so it has nothing to show.

## Step 6: Copy a run into MLflow

`prove mlflow` copies a run into MLflow, at the place that the policy approves. With no policy, nothing is copied. The tour's policy approves the store's own file at the level `usage`.

```
$ uv run prove --store $STORE mlflow $RUN2 || echo exit $?
prove: the telemetry policy approves no mlflow destination for the store's own file (MLFLOW_TRACKING_URI is unset)
exit 1

$ uv run prove --store $STORE mlflow $RUN2 --policy tours/provevi/policy.yaml
run-20261102T192005Z-c93d67ab: copied to MLflow at sqlite:////private/tmp/provevi-tour/evidence.db, destination viewer, level usage
  experiment 1, MLflow run adc3b893aa3d4213948fc0e6bff7dc18, trace tr-df0f3042c464ccd694ba527b07383e09 with 7 spans

$ uv run prove --store $STORE mlflow $RUN2 --policy tours/provevi/policy.yaml
run-20261102T192005Z-c93d67ab: copied to MLflow at sqlite:////private/tmp/provevi-tour/evidence.db, destination viewer, level usage
  experiment 1, MLflow run 256ad36c0bf94e758ddf07c65dba6f7b, trace tr-df0f3042c464ccd694ba527b07383e09 with 7 spans
  deleted the earlier copy, MLflow run adc3b893aa3d4213948fc0e6bff7dc18

$ uv run prove --store $STORE mlflow $RUN1 --policy tours/provevi/policy.yaml || echo exit $?
prove: run-20261102T191955Z-52539489 does not verify, so it is not copied: line 3: seq is 4, not 3; line 3: the chain is broken; refs of row 3 have no event
exit 1
```

**What happened:**
- With no policy, the copy was refused, and the command exited with 1.
- With the policy, the run went into MLflow's tables, in the same file as provevi's. The trace keeps provrun's trace ID after `tr-`.
- The second copy deleted the first and wrote it again, so the copy can be rebuilt at any time.
- `RUN1` no longer verifies after Step 4, so it is not copied.

## Step 7: Ask the same questions from Python

Each command calls a library function, so a program asks the same questions. `tours/provevi/query.py` finds the runs that read the document, and checks each one:

```python
store = SqliteStore(os.environ["TOUR_DIR"] + "/evidence.db", create=False)
report = "file://" + os.environ["TOUR_DIR"] + "/report.txt"
for run_id in store.runs_reading(report):
    run = store.run(run_id)
    [staged] = store.inputs(run_id)
    problems = verify(store.read_run(run_id), run_id)
    print(run_id, run["status"], staged["sha256"][:12], "verifies" if not problems else f"{len(problems)} problems")
```

```
$ uv run python tours/provevi/query.py
run-20261102T191955Z-52539489 completed 205cfa28c510 4 problems
run-20261102T192005Z-c93d67ab completed 81c0f05189eb verifies
```

**What happened:** each run staged a different version of the document, so the SHA-256 of their inputs differs. The run broken in Step 4 has 4 problems.

## Open MLflow's viewer

This step is not in the script, because the viewer runs until you stop it. Run it before `stop.sh`, after Step 6:

```
uv run prove --store $STORE ui
```

Open `http://127.0.0.1:5000`. The experiment `acme/demo/dev/revenue-question` holds the copy of `RUN2`, with its settings, its token counts and its trace. Press Ctrl-C to stop the viewer.

## Clean up

If you ran the steps by hand, delete the scratch folder:

```
source tours/provevi/stop.sh
```

`run.sh` does this itself.

## Where to go next

- [What provevi does](provevi-features.md) shows each feature, with more examples.
- [provevi's reference](provevi.md) lists the commands, the store's tables, the policy's keys and the exit codes.
- [The provrun tour](provrun-tour.md) runs the agent against a bucket, and commits each run's bundle.
- [The design](provevi-design.md) and [the plan](provevi-plan.md) say how provevi works and what comes next.
