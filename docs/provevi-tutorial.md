---
title: "Tutorial: keep the evidence of runs, and ask questions of it"
---

In this tutorial, you keep the evidence of three runs of an agent in one SQLite file, and you ask that file which runs read a file, which version of the agent ran, and whether the record changed. provevi is the package that keeps, reads and checks the evidence, and its command is `prove`. provrun makes the runs, and provevi does the rest.

The tutorial takes about 10 minutes. It needs no account, no key and no server. The agent's answer is fixed in the run file, and its input is a file on this machine.

## Why keep runs in a SQLite store

provrun writes the evidence of each run into the run's own folder. One folder answers questions about one run. A question across runs, such as "which runs read this file", would mean opening every folder. A SQLite store keeps every run in one file as well, so it answers such questions at once. It also keeps a second copy of the record, and provevi checks each copy against the other.

## Terms

| Term | Meaning |
|---|---|
| evidence | The events of a run, in order. Each one holds the SHA-256 hash of the one before it, so a removed or changed event breaks the chain. |
| payload | A text that an event names by its SHA-256 hash, such as the prompt. It is kept in a file of its own. |
| run folder | The folder of one run. It holds the input that the agent read, its output and the evidence. |
| evidence store | Where provevi keeps the evidence. The SQLite store keeps every run in one file, and the run folders too. |
| store URI | The name of a store, such as `sqlite:///evidence.db`, a file in the current folder. An absolute path takes a fourth slash, as in `sqlite:////tmp/provevi-tutorial/evidence.db`. |
| agent class | An agent's name with the org, the domain and the environment of its runs, such as `acme/finance/dev/revenue-question`. |
| agent version | The SHA-256 hash of the agent's spec, without its prompt and its inputs, which are the task and not the agent. |
| telemetry policy | The operator's file that says which libraries may record data from a run, and where the data may go. |
| destination | A place outside the evidence that data from a run may go to, such as an OpenTelemetry collector or MLflow. |

## Before you start

You need Python 3.13, [uv](https://docs.astral.sh/uv/) and `bash`.

1. Clone the repository, install the packages, and stay in the top folder of the repository for every command.

   ```sh
   git clone https://github.com/rahuldave/MahaCata
   cd MahaCata
   uv sync
   ```

2. Clear the variables that your shell may hold from your own work or from an earlier tutorial. The first line clears those of provcat and provrun. The tutorial sets each one that it needs. The second line clears those of OpenTelemetry, Logfire, MLflow, LiteLLM, Postgres and proxies. Each of them would change what a run records, and so the output.

   ```sh
   unset PROVCAT_CONFIG PROVCAT_CACHE PROVCAT_HOOKS PROVCAT_APP PROVCAT_TENANT PROVRUN_RUNS PROVRUN_TELEMETRY_POLICY PYDANTIC_AI_NO_BANNER
   for v in $(env | command grep -oiE '^((OTEL|LOGFIRE|MLFLOW|LITELLM)_|PG|[A-Z_]*PROXY)[A-Z0-9_]*'); do unset $v; done
   ```

   The second line also removes your shell's proxy. If you reach the internet only through a proxy, set it again before you run `uv` for a download.

3. Choose a scratch folder.

   ```sh
   export TOUR_DIR=/tmp/provevi-tutorial
   export PROVCAT_CACHE=$TOUR_DIR/cache
   mkdir $TOUR_DIR
   ```

A script runs the whole tutorial in one go, with `bash tours/provevi-tutorial/run.sh`. It needs only the first step above, and a folder that does not exist yet.

It deletes its folder at the end. The output below comes from that script. The run IDs differ on each run, and so do the hashes that cover them.

A run can print a warning from LiteLLM, that the coroutine `Logging.async_success_handler` was never awaited. LiteLLM writes it after the run ends, and it does not change the run. The script hides it with `PYTHONWARNINGS`.

## Step 1: Make the evidence of two runs

The run file asks one question about one file. Its input is a physical URI, the address of a file on this machine. The stub broker, which decides what a run may read, lets a run in `dev` read a physical URI.

```yaml
# One question about one local file. sed copies it into the tutorial's
# folder, and puts the folder's path in the input's URI. mock_response fixes the
# answer, so the runs need no model and no key.
run:
  org: acme
  domain: finance
  env: dev
  actor: you@example.com
agent:
  name: revenue-question
  backend: litellm
  model: anthropic/claude-haiku-5-5
  instructions: Answer from the report only.
  prompt: What was revenue in the third quarter?
  inputs:
    - file://TOUR_DIR/q3.txt
  settings:
    mock_response: Revenue in the third quarter was 42 million.
```

Write the file with your folder in place of `TOUR_DIR`. `PROVRUN_RUNS` says where provrun makes the run folders, and `S` holds the URI of the SQLite store.

```
$ sed "s|TOUR_DIR|$TOUR_DIR|" tours/provevi-tutorial/run.yaml > $TOUR_DIR/run.yaml

$ export PROVRUN_RUNS=$TOUR_DIR/runs S=sqlite:///$TOUR_DIR/evidence.db
```

Write the report, and run the agent with `--store`, so that the evidence goes into the SQLite file too. Then change the report, and run the agent again.

```
$ echo "Revenue in the third quarter was 42 million." > $TOUR_DIR/q3.txt

$ uv run provr --store $S run $TOUR_DIR/run.yaml > $TOUR_DIR/r1.json

$ echo "Revenue in the third quarter was 45 million, after a restatement." > $TOUR_DIR/q3.txt

$ uv run provr --store $S run $TOUR_DIR/run.yaml > $TOUR_DIR/r2.json
```

Each run prints a summary in JSON. Keep the two run IDs:

```
$ R1=$(uv run python tours/provevi-tutorial/field.py $TOUR_DIR/r1.json run_id)

$ R2=$(uv run python tours/provevi-tutorial/field.py $TOUR_DIR/r2.json run_id)
```

## Step 2: List the runs and the agents

`prove runs` lists the runs in the store, in the order of their IDs, which start with the time. `prove agents` lists the agent classes, with their versions and their runs.

```
$ uv run prove --store $S runs
run-20261110T193639Z-3de5d577
run-20261110T193659Z-09f923ef

$ uv run prove --store $S agents
acme/finance/dev/revenue-question  1 version, 2 runs
```

Both runs used one version of the agent, because the report changed and the agent did not. The version is the SHA-256 hash of the agent's spec, shown by its first 12 digits. `prove agent show` prints each version, with its backend, model, tools and limits:

```
$ uv run prove --store $S agent show acme/finance/dev/revenue-question
acme/finance/dev/revenue-question  first run run-20261110T193639Z-3de5d577
  version 95ac5cc7af91  2 runs  {"backend": "litellm", "limits": {}, "model": "anthropic/claude-haiku-5-5", "tools": []}
    run-20261110T193639Z-3de5d577
    run-20261110T193659Z-09f923ef
```

A change to the model, the instructions or a setting makes a new version, but a change to the prompt or the inputs does not. The summary leaves out the instructions and the settings, because a setting may hold a secret. So two versions can show the same summary.

## Step 3: Find the runs that read a file

`--reading` lists the runs that read a URI. Here it is the report, so both runs show.

```
$ uv run prove --store $S runs --reading file://$TOUR_DIR/q3.txt
run-20261110T193639Z-3de5d577
run-20261110T193659Z-09f923ef
```

The question takes one query in the SQLite file, for any number of runs in the store. Without the store, provevi would have to open the evidence of every run folder.

## Step 4: Read the events of a run

`prove show` prints each event of a run, with its time, the part of provrun that wrote it, the URIs that it names and its data. The `awk` keeps three columns, the number of the event, its type and the part that wrote it.

```
$ uv run prove --store $S show $R1 | awk '{print $1, $3, $4}'
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
```

Event 0 records the agent's spec and the settings of the run. Events 1 and 2 record the broker's decision on the input. Events 3 to 5 record the copy of the input that the agent read. Events 7 and 8 record the prompt and the answer. `prove access` prints only the broker's decisions:

```
$ uv run prove --store $S access $R1
   2 granted  mounted_snapshot   file:///tmp/provevi-tutorial/q3.txt  in the allow list; physical URI in dev
```

## Step 5: Check a run

`prove verify` checks the hash chain of the events, their numbers, the payloads and the outputs of a run. With `--store`, it also checks the store's copy against the run folder.

```
$ uv run prove --store $S verify $R1
run-20261110T193639Z-3de5d577: the evidence and the outputs verify; no committed bundle to compare with
```

The last words of the line say that the run has no copy in a store for runs. A run file can name such a store, and `prove verify` then compares the run folder with the copy there too.

## Step 6: Break the store's copy, and check again

The helper deletes event 3 of the first run from the SQLite file, as a careless edit of the database would. It leaves the run folder as it was.

```
$ uv run python tours/provevi-tutorial/break_row.py $TOUR_DIR/evidence.db $R1 3
```

The check of the store's copy now fails, and exits with status 1. Each line names one problem:

- The numbers of the events skip from 2 to 4.
- The hash chain breaks at the gap.
- The store keeps the URIs that each event names in a table of their own. The URIs of event 3 are still there, with no event.
- The store holds one event fewer than the manifest counts.

```
$ uv run prove --store $S verify $R1 || echo exit status $?
prove: in the store: line 3: seq is 4, not 3
prove: in the store: line 3: the chain is broken
prove: in the store: refs of row 3 have no event
prove: in the store: the manifest counts 13 events, and there are 12
exit status 1
```

Without `--store`, `prove verify` checks only the run folder, which still verifies. So the damage is in the store's copy only.

```
$ uv run prove verify $TOUR_DIR/runs/$R1
run-20261110T193639Z-3de5d577: the evidence and the outputs verify; no committed bundle to compare with
```

## Step 7: Ask the same questions from Python

Each command of `prove` is also a function of provevi's library. The helper `tours/provevi-tutorial/query.py` opens the store by its URI, finds the runs that read the report, and checks each one:

```python
store = open_store(sys.argv[1])
for run_id in store.runs_reading(sys.argv[2]):
    run = store.run(run_id)
    [staged] = store.inputs(run_id)
    problems = verify(store.read_run(run_id), run_id)
    print(run_id, run["status"], staged["sha256"][:12], "verifies" if not problems else f"{len(problems)} problems")
```

It prints each run's status, the first digits of the SHA-256 hash of the input that it read, and the result of the check:

```
$ uv run python tours/provevi-tutorial/query.py $S file://$TOUR_DIR/q3.txt
run-20261110T193639Z-3de5d577 completed 6314ca369e7e 4 problems
run-20261110T193659Z-09f923ef completed d9936f11a8ac verifies
```

The two hashes differ, because the two runs read two versions of the report. The first run still has the four problems of Step 6.

## Step 8: Run under a telemetry policy

A library that an agent uses, such as LiteLLM, can send data about a run to other places. The operator's telemetry policy says which libraries are approved, at which versions, and which destinations each one may feed. It also lists the hosts that destinations may send to. The policy does not limit where a run connects, because that is the job of the firewall or the container. This policy approves LiteLLM at one version, and lets it feed no destination:

```yaml
# A telemetry policy. LiteLLM is approved at the version that the workspace
# pins, and its telemetry feeds no destination. The host list names where a
# destination may send. Nothing from a run goes anywhere but the evidence.
version: 1
libraries:
  litellm:
    versions: "==1.83.0"
hosts:
  - api.anthropic.com
```

`prove policy` checks the file, and the installed version of each library that it names. It prints the SHA-256 hash of the file first. `feeds nothing` means that LiteLLM's own telemetry goes to no destination.

```
$ cp tours/provevi-tutorial/policy.yaml $TOUR_DIR/

$ uv run prove policy $TOUR_DIR/policy.yaml
/tmp/provevi-tutorial/policy.yaml: sha256 43e1cf3a0641334f3dcf4e5338967a141336918fead55a20a7304912a24f4d9d
  library litellm ==1.83.0 feeds nothing
  hosts: api.anthropic.com
```

Run the agent a third time, under the policy:

```
$ uv run provr --store $S run --policy $TOUR_DIR/policy.yaml $TOUR_DIR/run.yaml > $TOUR_DIR/r3.json

$ R3=$(uv run python tours/provevi-tutorial/field.py $TOUR_DIR/r3.json run_id)
```

The first event of each run records the policy that the run used, and its hash. The second run had no policy, so it ran under the policy that approves nothing, `<deny all>`. The third run names the file, with the hash that `prove policy` printed.

```
$ uv run python tours/provevi-tutorial/policy_of.py $PROVRUN_RUNS $R2 $R3
run-20261110T193659Z-09f923ef <deny all> 85581f4b999976c811c5a2edff37732ba6d34e4dbad9a66f44d6c7022bf14b5e
run-20261110T193727Z-4952d420 /tmp/provevi-tutorial/policy.yaml 43e1cf3a0641334f3dcf4e5338967a141336918fead55a20a7304912a24f4d9d
```

`prove telemetry` lists what the policy blocked in a run, and the problems that the run found. This run has neither.

```
$ uv run prove --store $S telemetry $R3
run-20261110T193727Z-4952d420: no blocked attempts and no problems
```

## The questions that the store answered

You kept the evidence of three runs in one SQLite file, and you asked it questions:

- **Which runs read a file**, with `prove runs --reading`, and which version of the file each one read, from Python.
- **Which version of the agent ran**, with `prove agents` and `prove agent show`.
- **What happened in a run**, with `prove show` and `prove access`.
- **Whether the record changed**, with `prove verify`, which found the event that you deleted from the store.
- **Which telemetry policy a run used**, from its first event.

provevi does more than this tutorial shows. It copies a sealed run into MLflow, for MLflow's viewer. It shows a run to a live viewer. It keeps experiments as versioned objects in the catalog.

## Clean up

The inputs in the run folders are read-only, so give write access back before you delete the folder:

```sh
chmod -R u+w $TOUR_DIR
rm -rf $TOUR_DIR
unset PROVRUN_RUNS PROVCAT_CACHE S TOUR_DIR
```

## Where to go next

| To do this | Read |
|---|---|
| Follow one report through all three packages | [The tutorial](tutorial.md) |
| See each feature of provevi, with MLflow, a live viewer and experiments | [The provevi tour](provevi-tour.md) |
| Look up the `prove` commands, the stores and the policy's keys | [The provevi reference](provevi.md) |
| See what provevi does, with examples | [What provevi does](provevi-features.md) |
