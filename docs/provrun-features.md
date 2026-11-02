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
  "run_id": "run-20261102T195556Z-88d8d7ec",
  "status": "completed",
  "folder": "/tmp/provrun-tour/runs/run-20261102T195556Z-88d8d7ec",
  "bundle": "run://acme/demo/dev/run-20261102T195556Z-88d8d7ec",
  "pinned": "local-s3://runs/main/acme/demo/dev/run-20261102T195556Z-88d8d7ec?at=commit:8f206c3924a18eaa16044cc1c55f55b9d3f595ace09b1d8cae242974cd4e7df9",
  "note": "",
  "error": null
}
```

`folder` is the run folder, under `PROVRUN_RUNS`. `pinned` is the commit of the bundle in the store that the scheme `run` names. The backend `litellm` makes one model call. The backend `pydantic-ai` runs an agent with tools, as [the reference](provrun.md) shows.

## Give the agent a read-only snapshot of a pinned version

A run never reads a file that can change under it. provcat pins each input to its version, and copies that version into a snapshot named by its SHA-256. The stager clones the snapshot into the run folder, and makes it read-only:

```
$ uv run python tours/provrun/modes.py $TOUR_DIR/runs/$R1/input $TOUR_DIR/runs/$R1/input/report.txt
dr-xr-xr-x /tmp/provrun-tour/runs/run-20261102T195556Z-88d8d7ec/input
-r--r--r-- /tmp/provrun-tour/runs/run-20261102T195556Z-88d8d7ec/input/report.txt
```

The evidence ties the URI to the version and the bytes:

```
$ uv run python tours/provrun/event.py $R1 snapshot_materialized
{
  "kind": "file",
  "parent_span_id": "c9fca02628b8d4aa",
  "pinned": "local-s3://data/main/acme/demo/dev/reports/report.txt?at=version:331f10c3-64f3-43f3-966a-75fb58398960",
  "sha256": "205cfa28c510eeba46bd4bc49acc2fb13f7645f66bab5ff5f0c3bda90d26f0b2",
  "span_id": "ce0da8a7d4e73d5c",
  "started_at": "2026-11-02T19:55:56.301512Z",
  "trace_id": "13a2ee8f025050d4cb2cefd70147b8cd"
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
local-s3://data/main/acme/demo/dev/reports/report.txt?at=version:331f10c3-64f3-43f3-966a-75fb58398960
```

```
$ cat $TOUR_DIR/runs/$R3/input/report.txt
Revenue in 2025 was 42 million.
```

## Check a run, and find a change

`provr verify` checks the chain, the payloads and the outputs. It also compares the run folder with the bundle committed in the store. A changed output fails the check:

```
$ uv run provr verify $R1
run-20261102T195556Z-88d8d7ec: the evidence and the outputs verify, and match the committed bundle
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
run-20261102T195619Z-143ab2e0
run-20261102T195624Z-4f8fba83

$ uv run prove --store $S experiments
acme/demo/dev/revenue-question  created by you@example.com at 2026-11-02T19:56:19.325242Z

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
run-20261102T195647Z-e039d455 completed
callbacks after the run: 1
the LiteLLM logger logs at: DEBUG
```

```
$ uv run prove telemetry $PROVRUN_RUNS/$R5
   1 2026-11-02T19:56:47.740780Z blocked litellm.callbacks   removed __main__.<lambda> from litellm.success_callback
   2 2026-11-02T19:56:47.740872Z blocked logging litellm  raised the logger 'LiteLLM' from DEBUG to WARNING
   3 2026-11-02T19:56:47.740940Z blocked logging litellm  raised the logger 'LiteLLM Router' from DEBUG to WARNING
   4 2026-11-02T19:56:47.740991Z blocked logging litellm  raised the logger 'LiteLLM Proxy' from DEBUG to WARNING
   5 2026-11-02T19:56:47.741041Z blocked logging anthropic  raised the logger 'anthropic' from DEBUG to WARNING
   6 2026-11-02T19:56:47.741089Z blocked logging openai  raised the logger 'openai' from DEBUG to WARNING
   7 2026-11-02T19:56:47.741133Z blocked logging botocore  raised the logger 'botocore' from DEBUG to WARNING
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
   1 2026-11-02T19:56:33.087145Z blocked destination   OTEL_EXPORTER_OTLP_ENDPOINT: matches no otlp destination of the policy; nothing is sent
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

## Use it from a program

`provr` only reads its arguments, calls provrun's library and prints. A program can call the same functions, such as `RunFile.load` and `run_file_sync`, and gets the outcome back:

```python
outcome = run_file_sync(RunFile.load(f"{tour}/run.yaml"), catalog=Catalog.open())
print(outcome.run_id, outcome.status)
```

```
$ cat $TOUR_DIR/quiet.out
run-20261102T195647Z-e039d455 completed
callbacks after the run: 1
the LiteLLM logger logs at: DEBUG
```

[The reference](provrun.md) lists the functions, and the errors that they raise.

## Where the examples come from

| Examples | Where they ran |
|---|---|
| Every example on this page | The tour's script, `bash tours/provrun/run.sh`, with moto and a small collector. |
| A real model | The tour's script with `--online`, or `packages/provrun/examples/claude-run.yaml` and `claude-agent.yaml`. They need `ANTHROPIC_API_KEY`. |

The run IDs, times, version IDs and hashes differ on each run.
