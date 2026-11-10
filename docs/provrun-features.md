---
title: What provrun does
---

provrun is a Python library with a command, `provr`. It runs an agent, or one model call, on data that provcat names. It asks a broker for each input, and gives the agent read-only snapshots of pinned versions. It records the run as evidence that can be checked later.

Each section below shows one feature with an example and its real output. [The tour](provrun-tour.md) runs the same commands from a script, on your own machine. [The reference](provrun.md) lists every setting, and [the plan](provrun-plan.md) explains why provrun is built as it is.

## Terms

| Term | Meaning |
|---|---|
| run | One execution of one agent or one model call, with one run ID. |
| run file | A YAML file with the run's settings and its agent spec, the portable description of the agent. |
| broker | The part that decides whether a run may read a URI. provrun has a stub broker now. |
| snapshot | A read-only copy of one pinned version of a file or a folder, in a folder named by its SHA-256. |
| evidence | The events of a run, each holding the SHA-256 of the one before, and the payloads they name, such as prompts. |
| bundle | The evidence and the outputs of a run, committed to a store. |
| telemetry policy | The operator's file that says which libraries may record a run, and where a run's spans may go. |
| span | One timed step of a run, in the OpenTelemetry format that collectors read. |

The examples ran in the shell of [the tour](provrun-tour.md), after `source tours/provrun/start.sh`, in the order of its steps. `$TOUR_DIR` is the tour's scratch folder, `/tmp/provrun-tour`. Its store is moto, a local S3 server, and the agent answers with LiteLLM's `mock_response`, so no example needs a model or a key.

## Run an agent on catalog names

A run file names its inputs by provcat URIs, such as `data://acme/demo/dev/reports/report.txt`. It does not say where the data is. This one asks one question about one report:

```yaml
run:
  org: acme
  domain: demo
  env: dev
  actor: you@example.com
  bundle_scheme: run
agent:
  name: revenue-question
  backend: litellm
  model: openai/gpt-4o-mini
  instructions: Answer from the document only.
  prompt: What was revenue in 2025?
  inputs:
    - data://acme/demo/dev/reports/report.txt
  settings:
    mock_response: Revenue in 2025 was 42 million.
```

`provr run` runs it, and prints a summary:

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

`folder` is the run folder, under `PROVRUN_RUNS`. `pinned` is the commit of the bundle in the store that the scheme `run` names. The backend `litellm` makes one model call. The backend `pydantic-ai` runs an agent with tools, as [the reference](provrun.md) shows.

## Give the agent a read-only snapshot of a pinned version

A run never reads a file that can change under it. provcat pins each input to its version, and copies that version into a snapshot named by its SHA-256. The stager clones the snapshot into the run folder, and makes it read-only:

```
$ uv run python tours/provrun/modes.py $TOUR_DIR/runs/$R1/input $TOUR_DIR/runs/$R1/input/report.txt
dr-xr-xr-x /tmp/provrun-tour/runs/run-20261102T224004Z-d7651699/input
-r--r--r-- /tmp/provrun-tour/runs/run-20261102T224004Z-d7651699/input/report.txt
```

The evidence ties the URI to the version and the bytes:

```
$ uv run python tours/provrun/event.py $R1 snapshot_materialized
{
  "kind": "file",
  "parent_span_id": "dd5aca3e4559ab69",
  "pinned": "local-s3://data/main/acme/demo/dev/reports/report.txt?at=version:ff132f17-7295-4220-8667-de2e7316bae0",
  "sha256": "205cfa28c510eeba46bd4bc49acc2fb13f7645f66bab5ff5f0c3bda90d26f0b2",
  "span_id": "4b14991141c42f4d",
  "started_at": "2026-11-02T22:40:04.463245Z",
  "trace_id": "95d933aadf4041b35d3c2f51143a38b9"
}
```

When the file changes in the store, the next run pins the new version, and makes a new snapshot. The old snapshot keeps the old bytes:

```
$ for f in $TOUR_DIR/cache/.provcat/snapshots/file/*/report.txt; do echo "$f: $(cat $f)"; done
/tmp/provrun-tour/cache/.provcat/snapshots/file/106b090a711c049ade3f3601e56d787f023438b672af9d36aa6112a7f123dc75/report.txt: Revenue in 2025 was 45 million, after a restatement.
/tmp/provrun-tour/cache/.provcat/snapshots/file/205cfa28c510eeba46bd4bc49acc2fb13f7645f66bab5ff5f0c3bda90d26f0b2/report.txt: Revenue in 2025 was 42 million.
```

## Record each run as evidence

Each part of provrun writes events as it works. Each event holds the SHA-256 of the line before it, so the events form a chain:

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

The prompt, and the request and the response of the model call, are payloads, each in a file named by its SHA-256. The events name the payloads by that hash.

## Run again on the same bytes

The evidence names the pinned URI of each input. A run file with that URI as its input reads the old version, even after the file changed:

```
$ echo "$PINNED"
local-s3://data/main/acme/demo/dev/reports/report.txt?at=version:ff132f17-7295-4220-8667-de2e7316bae0
```

```
$ cat $TOUR_DIR/runs/$R3/input/report.txt
Revenue in 2025 was 42 million.
```

## Check a run, and find a change

`provr verify` checks the chain, the payloads and the outputs. It also compares the run folder with the bundle committed in the store. A changed output fails the check:

```
$ uv run provr verify $R1
run-20261102T224004Z-d7651699: the evidence and the outputs verify, and match the committed bundle
```

```
$ echo "Revenue was 99 million." > $TOUR_DIR/runs/$R2/output/response.txt

$ uv run provr verify $R2 || echo "exit status $?"
provr: output response.txt was changed
exit status 1
```

A checker who does not trust the run folder passes the commit from the summary of `provr run`, with `--bundle`. Then a deleted `bundle.json` cannot hide a rewritten chain.

## Ask questions across runs

A run with `--store sqlite:///...` also writes each event as a row of one SQLite file. provevi's command, `prove`, then asks questions across runs:

```
$ uv run prove --store $S runs --reading data://acme/demo/dev/reports/report.txt
run-20261102T224043Z-e6ed9133
run-20261102T224050Z-406f3eaf

$ uv run prove --store $S agents
acme/demo/dev/revenue-question  1 version, 2 runs

$ uv run prove --store $S access $Q
   2 granted  mounted_snapshot   data://acme/demo/dev/reports/report.txt  in the allow list; org and environment match
```

A row deleted from the store is found:

```
$ uv run prove --store $S verify $Q || echo "exit status $?"
prove: in the store: line 3: seq is 4, not 3
prove: in the store: line 3: the chain is broken
prove: in the store: refs of row 3 have no event
prove: in the store: the manifest counts 13 events, and there are 12
exit status 1
```

## Keep libraries' own telemetry out of a run

A library such as LiteLLM can log prompts, call callbacks, or send traces of its own. Under the operator's telemetry policy, provrun turns these off for each run, and records what it turned off. A program that added a LiteLLM callback and set its loggers to `DEBUG` before a run gets this:

```
$ cat $TOUR_DIR/quiet.out
run-20261102T224119Z-b9b5dfdb completed
callbacks after the run: 1
the LiteLLM logger logs at: DEBUG
```

```
$ uv run prove telemetry $PROVRUN_RUNS/$R5
   1 2026-11-02T22:41:19.310981Z blocked litellm.callbacks   removed __main__.<lambda> from litellm.success_callback
   2 2026-11-02T22:41:19.311116Z blocked logging litellm  raised the logger 'LiteLLM' from DEBUG to WARNING
   3 2026-11-02T22:41:19.311216Z blocked logging litellm  raised the logger 'LiteLLM Router' from DEBUG to WARNING
   4 2026-11-02T22:41:19.311297Z blocked logging litellm  raised the logger 'LiteLLM Proxy' from DEBUG to WARNING
   5 2026-11-02T22:41:19.311371Z blocked logging anthropic  raised the logger 'anthropic' from DEBUG to WARNING
   6 2026-11-02T22:41:19.311443Z blocked logging openai  raised the logger 'openai' from DEBUG to WARNING
   7 2026-11-02T22:41:19.311510Z blocked logging botocore  raised the logger 'botocore' from DEBUG to WARNING
```

When the run ends, provrun puts the callback and the logger levels back.

`run_created` records the telemetry variables that were set, without what can hold a secret, and the policy by its SHA-256. A run file can narrow the policy, but never widen it:

```
$ uv run provr run $TOUR_DIR/wide.yaml || echo "exit status $?"
provr: the run file: host 'paste.example' is not allowed by the policy
exit status 1
```

## Send the spans only where the policy allows

A run is one OpenTelemetry trace, with a span for each step. The variables that name a collector, such as `OTEL_EXPORTER_OTLP_ENDPOINT`, or Logfire, say where the spans may go. provrun sends them only to a place that the policy approves, cut to the level that the place receives. A place it does not approve gets nothing:

```
$ uv run prove telemetry $PROVRUN_RUNS/$R4
   1 2026-11-02T22:41:00.519122Z blocked destination   OTEL_EXPORTER_OTLP_ENDPOINT: matches no otlp destination of the policy; nothing is sent
   2 2026-11-02T22:41:00.519258Z blocked destination   MLFLOW_TRACKING_URI: matches no mlflow destination of the policy; nothing is copied
```

A place it approves gets the run's spans. This collector receives names only, so `model_called` comes without its token counts:

```
$ cat $TOUR_DIR/collector.log
/v1/traces context_resolved ['provrun.component', 'provrun.run_id', 'provrun.step']
/v1/traces snapshot_materialized ['provrun.component', 'provrun.run_id', 'provrun.step']
/v1/traces input_staged ['provrun.component', 'provrun.run_id', 'provrun.step']
/v1/traces model_called ['gen_ai.operation.name', 'gen_ai.request.model', 'provrun.component', 'provrun.run_id', 'provrun.step']
/v1/traces input_verified ['provrun.component', 'provrun.run_id', 'provrun.step']
/v1/traces output_artifact_created ['provrun.component', 'provrun.run_id', 'provrun.step']
/v1/traces run ['gen_ai.agent.name', 'gen_ai.operation.name', 'provrun.component', 'provrun.run_id', 'provrun.step']
```

provrun's sender ignores the proxy variables, so no variable sends the spans elsewhere. `run_created` records which proxy variables were set, by name only.

## Ask for the reply in pieces

A run file can ask for the reply in pieces with `stream: true` in its agent spec. The evidence still records the whole response once:

```
$ uv run python tours/provrun/event.py $R11 model_called streamed input_tokens output_tokens finish_reason
true
48
10
stop
```

The data also holds `first_token_ms`, the milliseconds to the first piece. A stream that fails partway records the text it had handed on. [provrun's page](provrun.md#streamed-replies) says more.

## Watch a run as it goes

A program, such as a web server, can show a run as it goes. `start_run` starts the run and gives its live feed, one item per event and a last item for the end. The telemetry policy names the viewer as a destination of kind `live`, and its level decides what each item shows. Here the viewer receives `names`, so each event shows only the keys of its data that hold names:

```
$ PROVRUN_TELEMETRY_POLICY=$TOUR_DIR/live.yaml uv run python tours/provrun/follow.py $TOUR_DIR/stream.yaml
1 event run_created agent backend domain env mode org span_id trace_id
2 event access_requested mode
3 event access_granted decision grant_id mode policy
4 event context_resolved grant_id parent_span_id span_id started_at trace_id
5 event snapshot_materialized kind parent_span_id sha256 span_id started_at trace_id
6 event input_staged kind parent_span_id sha256 span_id started_at trace_id
7 event sandbox_started agent backend model
8 event prompt_rendered
9 event model_called finish_reason model parent_span_id span_id started_at streamed trace_id
10 event sandbox_completed status
11 event input_verified parent_span_id sha256 span_id started_at trace_id unchanged
12 event output_artifact_created parent_span_id sha256 span_id started_at trace_id
13 event run_completed span_id trace_id
14 done completed
```

[provrun's page](provrun.md#watch-a-run-as-it-goes) shows the code, and what each item holds. It also shows `live_server.py`, a small FastAPI server in the examples that sends the feed to a browser.

## Copy a run into MLflow

MLflow gives a viewer for runs. A policy can approve an MLflow place, here the SQLite evidence store's own file, which receives names only. MLflow gets nothing while the run goes on. When the run is sealed, provrun copies it from the evidence, cut to that level, and the summary of `provr run` says where it went:

```
$ PROVRUN_TELEMETRY_POLICY=$TOUR_DIR/mlflow.yaml uv run provr --store $M run $TOUR_DIR/run.yaml > $TOUR_DIR/r7.json

$ uv run python tours/provrun/field.py $TOUR_DIR/r7.json mlflow
{
  "destination": "own",
  "receives": "names",
  "copied": true,
  "tracking_uri": "sqlite:////tmp/provrun-tour/mlflow-evidence.db",
  "experiment_id": "1",
  "mlflow_run_id": "8b98d520a6fc4602a123be383e8b9a8d",
  "trace_id": "tr-264afdee126d9782ae0a97a967b374cc",
  "spans": 7
}
```

The copy's trace has a span for each step. `model_called` has the model's name, and no token counts:

```
$ uv run python tours/provrun/mlflow_spans.py $TOUR_DIR/r7.json
run gen_ai.agent.name gen_ai.operation.name mlflow.spanType mlflow.traceRequestId provrun.component provrun.run_id provrun.step
context_resolved mlflow.traceRequestId provrun.component provrun.run_id provrun.step
snapshot_materialized mlflow.traceRequestId provrun.component provrun.run_id provrun.step
input_staged mlflow.traceRequestId provrun.component provrun.run_id provrun.step
model_called gen_ai.operation.name gen_ai.request.model mlflow.llm.model mlflow.spanType mlflow.traceRequestId provrun.component provrun.run_id provrun.step
input_verified mlflow.traceRequestId provrun.component provrun.run_id provrun.step
output_artifact_created mlflow.traceRequestId provrun.component provrun.run_id provrun.step
```

A place that the policy does not approve gets no copy, and the run records that, without the file's path:

```
$ uv run python tours/provrun/event.py $R8 telemetry_blocked detail
MLFLOW_TRACKING_URI: matches no mlflow destination of the policy; nothing is copied

$ test -e $TOUR_DIR/elsewhere.db || echo "no copy"
no copy
```

## Name the experiment a run belongs to

An experiment is a versioned folder in the catalog, such as `experiment://acme/demo/dev/revenue/retrieval`. A run file names it with `experiment:`, beside `agent:`. As the run starts, provrun pins the experiment through the catalog, and `run_created` records the URI, the pin and the SHA-256 of its file:

```
$ uv run python tours/provrun/event.py $R12 run_created experiment experiment_pin experiment_sha256
experiment://acme/demo/dev/revenue/retrieval
experiment://acme/demo/dev/revenue/retrieval?at=commit:da23bf672a844fa165113a8a24ae5ba22cc10a7565b79611efca7047e15fcd76
8386baf7befc41bccec929be03e8a1dec4c9f37b2422a5d5eb11f4333fa3faef
```

After a second version is written, the same run file names the new pin:

```
$ uv run python tours/provrun/event.py $R13 run_created experiment_pin
experiment://acme/demo/dev/revenue/retrieval?at=commit:7311e8221f7268c01fb243ef177028736e0595bf8425a5276a126dbc7ea71a7f
```

`prove` lists the experiment, and checks a run's experiment at that run's own pin:

```
$ uv run prove --store $E experiments
experiment://acme/demo/dev/revenue/retrieval  2 versions, 2 runs

$ uv run prove --store $E verify --config $PROVCAT_CONFIG $R12
run-20261110T021826Z-5d709442: the evidence and the outputs verify, and match the committed bundle; its experiment matches experiment://acme/demo/dev/revenue/retrieval?at=commit:da23bf672a844fa165113a8a24ae5ba22cc10a7565b79611efca7047e15fcd76
```

An experiment that cannot be pinned stops the run before the run folder is made. A run that another run starts records `parent_run`. [provrun's page](provrun.md#name-the-experiment) says more.

## Use it from a program

`provr` only reads its arguments, calls provrun's library and prints. A program can call the same functions, such as `RunFile.load` and `run_file_sync`, and gets the outcome back:

```python
outcome = run_file_sync(RunFile.load(f"{tour}/run.yaml"), catalog=Catalog.open())
print(outcome.run_id, outcome.status)
```

```
$ cat $TOUR_DIR/quiet.out
run-20261102T224119Z-b9b5dfdb completed
callbacks after the run: 1
the LiteLLM logger logs at: DEBUG
```

[The reference](provrun.md) lists the functions, and the errors that they raise.

## Run a real model

The examples above use a mock answer. These ran with Claude Haiku, under a policy that approves a collector and a copy in MLflow, with token counts. A Logfire token was set too, which the policy does not approve. One model call with LiteLLM read the report:

```text
$ cat $PROVRUN_RUNS/$R9/output/response.txt; echo
Revenue in 2025 was 45 million, after a restatement.

$ uv run python tours/provrun/event.py $R9 model_called model input_tokens output_tokens finish_reason
anthropic/claude-haiku-5-5
62
23
stop

$ uv run prove telemetry $PROVRUN_RUNS/$R9
   1 2026-11-02T22:42:06.909446Z blocked destination   LOGFIRE_TOKEN: matches no logfire destination of the policy; nothing is sent

$ tail -n +$((N + 1)) $TOUR_DIR/collector.log
/v1/traces context_resolved ['provrun.component', 'provrun.run_id', 'provrun.step']
/v1/traces snapshot_materialized ['provrun.component', 'provrun.run_id', 'provrun.step']
/v1/traces input_staged ['provrun.component', 'provrun.run_id', 'provrun.step']
/v1/traces model_called ['gen_ai.operation.name', 'gen_ai.request.model', 'gen_ai.usage.input_tokens', 'gen_ai.usage.output_tokens', 'provrun.component', 'provrun.run_id', 'provrun.step']
/v1/traces input_verified ['provrun.component', 'provrun.run_id', 'provrun.step']
/v1/traces output_artifact_created ['provrun.component', 'provrun.run_id', 'provrun.step']
/v1/traces run ['gen_ai.agent.name', 'gen_ai.operation.name', 'provrun.component', 'provrun.run_id', 'provrun.step']
```

The collector got the run's spans with their token counts. MLflow got a copy, and Logfire got nothing.

A PydanticAI agent with tools found the fact with `grep_file`, noted the line it used, and wrote its answer:

```text
$ cat $PROVRUN_RUNS/$R10/output/answer.txt
Revenue in 2025 was 45 million (after a restatement). Source: /input/report.txt, line 1.

$ uv run python tours/provrun/event_types.py $R10
0 run_created 
1 telemetry_blocked 
2 access_requested 
3 access_granted 
4 context_resolved 
5 snapshot_materialized 
6 input_staged 
7 sandbox_started 
8 prompt_rendered 
9 telemetry_blocked 
10 model_called 
11 tool_called grep_file
12 model_called 
13 note_added 
14 tool_called add_note
15 tool_called write_file
16 model_called 
17 trace_imported 
18 sandbox_completed 
19 input_verified 
20 output_artifact_created 
21 output_artifact_created 
22 run_completed 
```

The Anthropic SDK made a span of its own, which the policy does not approve. provrun kept it out, and recorded that:

```text
$ uv run prove telemetry $PROVRUN_RUNS/$R10
   1 2026-11-02T22:42:25.064639Z blocked destination   LOGFIRE_TOKEN: matches no logfire destination of the policy; nothing is sent
   9 2026-11-02T22:42:31.803022Z blocked opentelemetry.span com-anthropic-sdk-python  a span from com.anthropic.sdk.python, which the policy does not approve
```

Both runs verify:

```text
$ uv run provr --store $O verify $R9
run-20261102T224206Z-45187a05: the evidence and the outputs verify, and match the committed bundle
```

```text
$ uv run provr --store $O verify $R10
run-20261102T224225Z-8e0c9702: the evidence and the outputs verify, and match the committed bundle
```

## Where the examples come from

| Examples | Where they ran |
|---|---|
| Every example on this page but "Run a real model" | The tour's script, `bash tours/provrun/run.sh`, with moto and a small collector. |
| "Run a real model" | Step 16 of the tour's script, `bash tours/provrun/run.sh --online`, with Claude Haiku. It needs `ANTHROPIC_API_KEY`, and the tests do not run it. `packages/provrun/examples/claude-run.yaml` and `claude-agent.yaml` run the same on a local file. |

The run IDs, times, version IDs and hashes differ on each run.
