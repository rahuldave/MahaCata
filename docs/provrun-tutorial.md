---
title: "Tutorial: run an agent, and read its run folder"
---

In this tutorial, you run an agent on a local file with provrun, and you read what the run left behind. You read the input that the agent got, its answer, and the evidence of each step. You also see a run that the broker stops, a reply in pieces, and a run from Python.

The tutorial takes about 10 minutes. It needs no server, no account and no key, because the agent's answer is fixed in the run file. The last step is optional, and asks a real model.

## Why provrun records a run

An agent that answers a question reads files, calls a model and writes an answer. Later, someone asks what it read and what the model said. provrun keeps the answer to those questions in the run folder. It gives the agent a read-only copy of each input, and records each step of the run as an event. Each event holds the SHA-256 hash of the event before it, so a later change to the record shows up when you check it.

## Terms

| Term | Meaning |
|---|---|
| run file | A YAML file that says what to run: the agent, its question and its inputs. |
| run folder | The folder of one run. It holds the input that the agent read, its output and the evidence. |
| evidence | The events of a run, in order. Each one holds the SHA-256 hash of the one before it. |
| payload | A file in the evidence that holds a large value of an event, such as the prompt. It is named by the SHA-256 hash of its content. |
| broker | The part of provrun that decides whether a run may read an input. This tutorial uses the stub broker, which reads a short list of rules. |

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

   The second line also removes your shell's proxy. If you reach the internet only through a proxy, set it again before you run `uv` for a download, or before Step 10.

3. Choose a scratch folder.

   ```sh
   export TOUR_DIR=/tmp/provrun-tutorial
   mkdir $TOUR_DIR
   ```

A script runs the whole tutorial in one go, with `bash tours/provrun-tutorial/run.sh`. It needs only the first step above, and a folder that does not exist yet.

It deletes its folder at the end. The output below comes from that script. The run IDs, span IDs and times differ on each run.

A run can print a warning from LiteLLM, that the coroutine `Logging.async_success_handler` was never awaited. LiteLLM writes it after the run ends, and it does not change the run. The script hides it with `PYTHONWARNINGS`.

## Step 1: Write the report, and the run file

Write a one-line report. The agent answers a question about it.

```
$ echo "Revenue in the third quarter was 42 million." > $TOUR_DIR/q3.txt
```

The run file has two sections. `run` says who runs the agent, and where the run belongs, by an org, a domain and an environment. `agent` gives the agent's model, its instructions, its question and its inputs:

```yaml
# One question about one local file. mock_response fixes the answer, so the
# tutorial needs no model and no key.
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

The backend `litellm` makes one model call through LiteLLM. `mock_response` makes LiteLLM give that answer without calling the model. The input is a `file://` URI, the address of a local file. The input must name your scratch folder, so `sed` puts the folder in place of the word `TOUR_DIR` as it copies the file:

```
$ sed "s|TOUR_DIR|$TOUR_DIR|" tours/provrun-tutorial/run.yaml > $TOUR_DIR/run.yaml
```

## Step 2: Run the agent

`PROVRUN_RUNS` says where provrun makes the run folders. `PROVCAT_CACHE` says where provcat, which fetches the inputs, keeps its copies. Run the agent:

```
$ export PROVRUN_RUNS=$TOUR_DIR/runs PROVCAT_CACHE=$TOUR_DIR/cache

$ uv run provr run $TOUR_DIR/run.yaml | tee $TOUR_DIR/r1.json
{
  "run_id": "run-20261110T193512Z-c99d8a75",
  "status": "completed",
  "folder": "/tmp/provrun-tutorial/runs/run-20261110T193512Z-c99d8a75",
  "bundle": null,
  "pinned": null,
  "note": "no store for runs is set; the bundle stays in the run folder",
  "error": null
}
```

The summary gives the run ID, the status and the run folder. `bundle` and `pinned` are empty, because the run file names no store for a copy of the run. The note says so. Keep the run ID in `R1`:

```
$ R1=$(uv run python tours/provrun-tutorial/field.py $TOUR_DIR/r1.json run_id)
```

## Step 3: Look at the run folder

The run folder holds four folders. `input/` holds the input that the agent read, and `output/` holds its answer. `scratch/` is the agent's space to work in, and it is empty here, so `find` does not show it.

`evidence/` holds the events in `events.jsonl` and the payloads. It also holds `manifest.json`, which provrun writes as the run ends. The manifest gives the number of events, the hash of the last event and the names of the payloads. So a removed event or payload shows up.

```
$ find $PROVRUN_RUNS/$R1 -type f | sort
/tmp/provrun-tutorial/runs/run-20261110T193512Z-c99d8a75/bundle.json
/tmp/provrun-tutorial/runs/run-20261110T193512Z-c99d8a75/evidence/events.jsonl
/tmp/provrun-tutorial/runs/run-20261110T193512Z-c99d8a75/evidence/manifest.json
/tmp/provrun-tutorial/runs/run-20261110T193512Z-c99d8a75/evidence/payloads/6ead0d42d7681b5eb0759a1769a13617d6bb7ea14341edd79a59c56aa63fcd25
/tmp/provrun-tutorial/runs/run-20261110T193512Z-c99d8a75/evidence/payloads/d092ca7582c9211ace9c28b2de0ff2da92138c756b2cb2c961ca507c715efb87
/tmp/provrun-tutorial/runs/run-20261110T193512Z-c99d8a75/input/q3.txt
/tmp/provrun-tutorial/runs/run-20261110T193512Z-c99d8a75/output/response.txt
```

The input is a read-only copy, so the agent cannot change it. The answer is an ordinary file:

```
$ uv run python tours/provrun-tutorial/modes.py $PROVRUN_RUNS/$R1/input/q3.txt $PROVRUN_RUNS/$R1/output/response.txt
-r--r--r-- /tmp/provrun-tutorial/runs/run-20261110T193512Z-c99d8a75/input/q3.txt
-rw-r--r-- /tmp/provrun-tutorial/runs/run-20261110T193512Z-c99d8a75/output/response.txt

$ cat $PROVRUN_RUNS/$R1/output/response.txt; echo
Revenue in the third quarter was 42 million.
```

## Step 4: Read the events

`provr show` prints each event of a run. The `awk` keeps three columns, the number of the event, its type and the part of provrun that wrote it.

```
$ uv run provr show $R1 | awk '{print $1, $3, $4}'
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

The events follow the run in order:

- **Events 1 and 2.** provrun asked the broker whether the run may read the file, and the broker said yes.
- **Events 3 to 5.** provcat found the file and made a read-only copy, a snapshot. Then provrun put the copy in `input/`.
- **Events 7 and 8.** These record the prompt and the model's answer.
- **Event 10.** provrun checked that the input had not changed while the agent ran.

Each event also holds data. The snapshot's data gives the SHA-256 hash of the bytes that the agent read. A local file has no versions, so the hash is how the evidence names the version. The span and trace IDs link the event to the run's OpenTelemetry trace.

```
$ uv run python tours/provrun-tutorial/event.py $R1 snapshot_materialized
{
  "kind": "file",
  "parent_span_id": "abe05e726e7a6041",
  "pinned": "file:///tmp/provrun-tutorial/q3.txt",
  "sha256": "6314ca369e7e9f83c0b19536ead44eb549623f81507fec6b3dfab3f9b16a0ada",
  "span_id": "166d783cdbbd6af2",
  "started_at": "2026-11-10T19:35:12.524530Z",
  "trace_id": "24491d08a3932a85c30c6b4a774799e6"
}
```

## Step 5: Read the prompt and the answer

The prompt and the answer can be large, so an event keeps each one in a payload, and holds its hash. The prompt holds the instructions, the input under its name in the run folder, `/input/q3.txt`, and the question:

```
$ uv run python tours/provrun-tutorial/payload.py $R1 prompt_rendered
payload 6ead0d42d7681b5eb0759a1769a13617d6bb7ea14341edd79a59c56aa63fcd25
[{"content":"Answer from the report only.","role":"system"},{"content":[{"text":"[Document: /input/q3.txt]\nRevenue in the third quarter was 42 million.\n","type":"text"},{"text":"What was revenue in the third quarter?","type":"text"}],"role":"user"}]
```

The payload of `model_called` holds the request that went to the model, and its response:

```
$ uv run python tours/provrun-tutorial/payload.py $R1 model_called
payload d092ca7582c9211ace9c28b2de0ff2da92138c756b2cb2c961ca507c715efb87
{"request": [{"role": "system", "content": "Answer from the report only."}, {"role": "user", "content": [{"type": "text", "text": "[Document: /input/q3.txt]\nRevenue in the third quarter was 42 million.\n"}, {"type": "text", "text": "What was revenue in the third quarter?"}]}], "response": "Revenue in the third quarter was 42 million."}
```

## Step 6: Ask for an input that the run may not read

The broker decides whether a run may read each input, before the agent starts. The stub broker allows a local file only in the environment `dev`, because a local path names no environment. Make a copy of the run file for `prod`, and run it. The run fails, and `provr run` exits with status 1.

```
$ sed "s/env: dev/env: prod/" $TOUR_DIR/run.yaml > $TOUR_DIR/prod.yaml

$ uv run provr run $TOUR_DIR/prod.yaml > $TOUR_DIR/r2.json || echo "exit status $?"
exit status 1

$ uv run python tours/provrun-tutorial/field.py $TOUR_DIR/r2.json error
AccessDenied: access to file:///tmp/provrun-tutorial/q3.txt denied: a physical file URI names no environment; the stub grants one only in dev, and the run is in prod
```

The refused run still has a run folder and evidence. The evidence records the request, the refusal and the end of the run, and the agent never started:

```
$ R2=$(uv run python tours/provrun-tutorial/field.py $TOUR_DIR/r2.json run_id)

$ uv run provr show $R2 | awk '{print $1, $3, $4}'
0 run_created RunContext
1 access_requested StubBroker
2 access_denied StubBroker
3 run_failed RunContext

$ uv run python tours/provrun-tutorial/event.py $R2 access_denied reason
a physical file URI names no environment; the stub grants one only in dev, and the run is in prod
```

## Step 7: Check a run

`provr verify` checks the hash chain of the events, the payloads, the manifest and the outputs of a run. Both runs verify. The refused run failed, but its evidence is whole.

```
$ uv run provr verify $R1
run-20261110T193512Z-c99d8a75: the evidence and the outputs verify; no committed bundle to compare with

$ uv run provr verify $R2
run-20261110T193536Z-74895944: the evidence and the outputs verify; no committed bundle to compare with
```

A change to an output, an event or a payload makes the check fail. [The tutorial of all three packages](tutorial.md) breaks a run, and [the provevi tutorial](provevi-tutorial.md) shows more checks.

## Step 8: Ask for the reply in pieces

`stream: true` in the agent spec asks the model for its reply in pieces, as the model makes them. A live viewer can show each piece as it comes. The evidence still records the whole reply once, in `model_called`, with `streamed` set. Add the key, and run again:

```
$ { cat $TOUR_DIR/run.yaml; echo "  stream: true"; } > $TOUR_DIR/stream.yaml

$ uv run provr run $TOUR_DIR/stream.yaml > $TOUR_DIR/r3.json

$ R3=$(uv run python tours/provrun-tutorial/field.py $TOUR_DIR/r3.json run_id)

$ uv run python tours/provrun-tutorial/event.py $R3 model_called streamed output_tokens
true
10
```

`output_tokens` is the number of tokens in the reply, as LiteLLM counts them. The run file ends with the agent spec, so the line that `echo` adds goes into it.

## Step 9: Run it from Python

A program runs a run file as `provr run` does. `run_file_sync` runs it, and gives back the outcome, with the status, the run ID and the run folder. A server that has its own event loop calls `run_file` instead.

```
$ cat tours/provrun-tutorial/ask.py
"""Run a run file from Python, as provr run does, and print the answer.
    ask.py RUN_FILE"""
import sys

from provcat import Catalog
from provrun import RunFile, run_file_sync

outcome = run_file_sync(RunFile.load(sys.argv[1]), catalog=Catalog.open())
print(outcome.status, outcome.run_id)
print((outcome.folder / "output" / "response.txt").read_text())

$ uv run python tours/provrun-tutorial/ask.py $TOUR_DIR/run.yaml
completed run-20261110T193557Z-a2468186
Revenue in the third quarter was 42 million.
```

## Step 10: Ask a real model (needs ANTHROPIC_API_KEY)

This step is optional. It calls Anthropic's API, and each call costs a fraction of a cent. Set `ANTHROPIC_API_KEY`, and make a run file without `mock_response`:

```
$ grep -v mock_response $TOUR_DIR/run.yaml | grep -v "^  settings:" > $TOUR_DIR/real.yaml

$ uv run python tours/provrun-tutorial/ask.py $TOUR_DIR/real.yaml
completed run-20261110T193607Z-e668d207
Revenue in the third quarter was 42 million.
```

The model read the report in the prompt, and its words can differ on each run. `bash tours/provrun-tutorial/run.sh --online` runs this step too.

## What you learned

You ran an agent, and read each part of its run folder:

- The input is a read-only copy, and the evidence holds the SHA-256 hash of its bytes.
- The answer is in `output/`, and the evidence holds the prompt and the answer as payloads.
- The broker decides each input before the agent starts, and a refusal is evidence too.
- `provr verify` checks the whole record, and a program runs a run file with `run_file_sync`.

The tutorial leaves out several features. An input can be a logical URI, which provcat turns into a pinned version in a store. A run can commit a copy of its folder to a store, and keep its evidence in SQLite. A telemetry policy says which libraries may send data from a run, and where.

## Clean up

The inputs in the run folders are read-only, so give write access back before you delete the folder:

```sh
chmod -R u+w $TOUR_DIR
rm -rf $TOUR_DIR
unset PROVRUN_RUNS PROVCAT_CACHE TOUR_DIR
```

## Where to go next

| To do this | Read |
|---|---|
| Run an agent on a named file in a bucket, and keep its evidence in SQLite | [The tutorial of all three packages](tutorial.md) |
| See every feature of provrun on one machine | [The provrun tour](provrun-tour.md) |
| Look up the run file, the library, the telemetry policy and the limits | [provrun](provrun.md) |
