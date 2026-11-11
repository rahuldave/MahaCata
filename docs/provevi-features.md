---
title: What provevi does
---

provevi is a Python library with a command, `prove`. It keeps the evidence of the runs that provrun makes, and answers questions about it. It checks that the evidence was not changed. It reads the telemetry policy that says where data from a run may go, and it copies runs into MLflow for MLflow's viewer.

Each section below shows one feature with examples and their real output. [The tour](provevi-tour.md) runs the same commands from a script, on your own machine. [provevi's reference](provevi.md) lists every command, table and key, and [the design](provevi-design.md) explains how the parts work.

## Terms

| Term | Meaning |
|---|---|
| run | One run of an agent by provrun, with an ID such as `run-20261102T191955Z-52539489`. |
| evidence | The events of a run, its payloads and its manifest. Each event holds the hash of the event before it. |
| payload | A body that an event refers to by its SHA-256, such as the request and response of a model call. |
| manifest | The last record of a run: how many events it has, the hash of the last one, and its payloads. |
| store | Where provevi keeps the evidence. The `file` store keeps files in each run folder. The `sqlite` store also keeps every run in one SQLite file. |
| experiment | A question a team asks again and again in a project. It is a folder in the catalog, such as `experiment://acme/risk/dev/credit/revenue`, and each commit of it is a version. |
| agent class | An agent's name in its org, domain and environment, such as `acme/demo/dev/revenue-question`. The store finds it in the runs. |
| telemetry policy | The operator's file that says which libraries may load, and where data from a run may go, besides the evidence. |
| destination | A place outside the evidence that data may go to, such as an OpenTelemetry collector, MLflow or a live viewer. |
| live feed | The events of a run as it goes, cut to a level, which a program streams to a viewer such as a browser. |
| level | What a destination receives: `names`, `usage` or `bodies`, from least to most. |

The examples that use `$STORE`, `$RUN1` or `$RUN2` ran in the shell of [the tour](provevi-tour.md), after `source tours/provevi/start.sh`, in the order of its steps. The examples that use `packages/provevi/tests/data` read a run that the repository holds.

## Keep the evidence of each run

provrun hands each event to a provevi store as it happens. Every store keeps the same three files in the run folder. They are `events.jsonl`, a `payloads/` folder and `manifest.json`. provrun commits those files as the run's bundle.

```
$ ls $TOUR_DIR/runs/$RUN1 $TOUR_DIR/runs/$RUN1/evidence
/tmp/provevi-tour/runs/run-20261102T192146Z-8adb7612:
bundle.json
evidence
input
output
scratch

/tmp/provevi-tour/runs/run-20261102T192146Z-8adb7612/evidence:
events.jsonl
manifest.json
payloads
```

Each event is one line of JSON. The first is `run_created`, with the run's org, domain, environment, mode and agent:

```
$ head -1 $TOUR_DIR/runs/$RUN1/evidence/events.jsonl | uv run python -m json.tool | head -12
{
    "actor": "you@example.com",
    "component": "RunContext",
    "confidence": "observed",
    "data": {
        "agent": "revenue-question",
        "backend": "litellm",
        "domain": "demo",
        "env": "dev",
        "mode": "lenient",
        "org": "acme",
        "span_id": "9863e8c089b19bf3",
```

The manifest seals the run. It counts the events, holds the hash of the last one, and lists the payloads by their SHA-256:

```
$ uv run python -m json.tool $TOUR_DIR/runs/$RUN1/evidence/manifest.json
{
    "run_id": "run-20261102T192146Z-8adb7612",
    "events": 13,
    "last": "bcfd8799cd567a6dfa18a03a0a5267fc927422386e490c86620e6dce6e9ccadb",
    "payloads": [
        "169a2eac7d7f094a88744ea5c1500e3a7ccb706952cdd6d5fa67eef58ff6c601",
        "f4503948841cb1db460bbdec8f9b4178f81cc3254f1c17a27487d62495373fcd"
    ],
    "complete": true,
    "problems": []
}
```

`provr --store sqlite:///path run` keeps every run in one SQLite file as well, one row for each event. The file is in SQLite's WAL mode, so many runs can write it at once:

```
$ uv run provr --store $STORE run $TOUR_DIR/run.yaml 2> /dev/null > $TOUR_DIR/first.json
```

## Ask questions across runs

The `sqlite` store answers questions about many runs at once. These commands ask which runs read a file, and which agents ran:

```
$ uv run prove --store $STORE runs --reading file://$TOUR_DIR/report.txt
run-20261102T191955Z-52539489
run-20261102T192005Z-c93d67ab

$ uv run prove --store $STORE agents
acme/demo/dev/revenue-question  1 version, 2 runs

$ uv run prove --store $STORE agent show acme/demo/dev/revenue-question
acme/demo/dev/revenue-question  first run run-20261102T191955Z-52539489
  version fe7bd27a6da9  2 runs  {"backend": "litellm", "limits": {}, "model": "openai/gpt-4o-mini", "tools": []}
    run-20261102T191955Z-52539489
    run-20261102T192005Z-c93d67ab
```

Both runs are one version of the agent class. A version is the start of the SHA-256 of the agent's spec, without its prompt and its inputs. Changed instructions or a new model make a new version.

`prove access` shows what a run was allowed to read, and why:

```
$ uv run prove --store $STORE access $RUN1
   2 granted  mounted_snapshot   file:///tmp/provevi-tour/report.txt  in the allow list; physical URI in dev
```

`runs --reading` also finds a run that read a pinned version of the URI, such as the URI followed by `?at=commit:...`. The file store has no index of runs, so these questions need a `sqlite` store.

## Read the events of a run

`prove show` prints one line for each event, with its sequence number, time, type, component, URIs and data. This line is cut short at the right:

```
$ uv run prove --store $STORE show $RUN1 | sed -n 9p | cut -c1-160
   8 2026-11-02T19:21:50.516834Z model_called             LiteLLMBackend            {"finish_reason": "stop", "input_tokens": 10, "model": "openai/gpt-4o-mini",
```

Keep only the types, to see the steps of a run in order:

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
```

## Check that the evidence was not changed

`prove verify` checks the chain of hashes, the sequence, the payloads, the manifest and the outputs. With `--config`, it also compares the run with the bundle that provrun committed through provcat.

A run that was not changed verifies:

```
$ uv run prove --store $STORE verify $RUN1
run-20261102T191955Z-52539489: the evidence and the outputs verify; no committed bundle to compare with
```

Delete one event from the store, and the check finds it:

```
$ uv run python tours/provevi/break_row.py $TOUR_DIR/evidence.db $RUN1 3

$ uv run prove --store $STORE verify $RUN1 || echo exit $?
prove: in the store: line 3: seq is 4, not 3
prove: in the store: line 3: the chain is broken
prove: in the store: refs of row 3 have no event
prove: in the store: the manifest counts 13 events, and there are 12
exit 1
```

A run with no committed bundle fails when you require one. This run is in the repository:

```
$ uv run prove --runs packages/provevi/tests/data verify run-20261031T182026Z-f9c3b25f --require-bundle
prove: no committed bundle to compare with, and a bundle is required
```

Someone who can write the run folder can still write a false record that is consistent. The committed bundle rules that out, because it sits in another store. Pass `--bundle` with its pinned URI, taken from outside the run folder.

## Check a telemetry policy

A library can record or send data by itself, such as MLflow's autolog or LiteLLM's callbacks. provrun turns each of these off during a run. The operator approves exceptions in a telemetry policy, and provevi reads and checks it.

`prove policy` checks a policy file, and the installed version of each library that it approves. The tour's policy approves LiteLLM, and a copy in MLflow:

```
$ uv run prove policy tours/provevi/policy.yaml
tours/provevi/policy.yaml: sha256 718093cad9c0bf0ece06403cc43572baa7ac849978a81bbbfc181a540d83a02a
  library litellm ==1.83.0 feeds nothing
  destination viewer (mlflow) receives usage
  hosts: none
```

A policy in the repository lets PydanticAI feed Logfire:

```
$ uv run prove policy packages/provevi/tests/data/telemetry-policy.yaml
packages/provevi/tests/data/telemetry-policy.yaml: sha256 927ccec8f74bf254aaae11282df1f64dfc73f6291e6dd50b3c83faa22e313b03
  library litellm ==1.83.0 feeds nothing
  library pydantic-ai >=2.64,<2.65 feeds logfire
  destination logfire (logfire) receives usage at https://logfire-us.pydantic.dev
  hosts: api.anthropic.com, .amazonaws.com, logfire-us.pydantic.dev
```

A library installed at a version that the policy does not name fails the check. With `versions: ">=2.65"` for pydantic-ai, it printed:

```
prove: pydantic-ai 2.64.0 is installed, but the policy approves >=2.65
```

`prove telemetry RUN` prints what the policy blocked in a run. It also prints the problems that the run found, such as a library that set up its own telemetry:

```
$ uv run prove --store $STORE telemetry $RUN2
run-20261102T192005Z-c93d67ab: no blocked attempts and no problems
```

## Limit what each destination receives

Each destination receives one level. `names` is the names, models and IDs of each span. `usage` adds the token counts and the cost. `bodies` adds everything else, such as the messages. An attribute that provevi does not know counts as a body. So does a name that holds a space, because it may be text from a prompt.

This program asks what each level receives of one span, and narrows the policy for one run:

```python
from provevi import ProveviError, TelemetryPolicy, attributes_for

span = {
    "gen_ai.operation.name": "chat",
    "gen_ai.request.model": "gpt-4o-mini",
    "gen_ai.usage.input_tokens": 10,
    "gen_ai.input.messages": '[{"role": "user", "content": "What was revenue?"}]',
    "gen_ai.response.id": "the answer is 42",
}
for level in ("names", "usage", "bodies"):
    print(level, sorted(attributes_for(span, level)))

policy = TelemetryPolicy.from_dict({"version": 1, "destinations": {
    "collector": {"kind": "otlp", "receives": "usage", "endpoint": "https://otel.example.com/v1/traces"}},
    "hosts": ["otel.example.com"]})
print(policy.narrow({"destinations": {"collector": {"receives": "names"}}}).destinations["collector"].receives)
try:
    policy.narrow({"destinations": {"collector": {"receives": "bodies"}}})
except ProveviError as e:
    print("refused:", e)
```
It prints:
```
names ['gen_ai.operation.name', 'gen_ai.request.model']
usage ['gen_ai.operation.name', 'gen_ai.request.model', 'gen_ai.usage.input_tokens']
bodies ['gen_ai.input.messages', 'gen_ai.operation.name', 'gen_ai.request.model', 'gen_ai.response.id', 'gen_ai.usage.input_tokens']
names
refused: the run file: destination 'collector' would receive bodies, more than the policy's usage
```
A run file can narrow the policy, but never widen it.

## Match a variable to a destination

A run finds its destinations in the user's variables, such as `OTEL_EXPORTER_OTLP_ENDPOINT` and `MLFLOW_TRACKING_URI`. `policy.match_destination(kind, url)` gives the destination of that kind whose endpoint is the same place, or `None`. A place with no match gets nothing.

This program matches three URLs against a policy with two destinations:

```python
from provevi import TelemetryPolicy, otlp_traces_target

policy = TelemetryPolicy.from_dict({
    "version": 1,
    "destinations": {
        "collector": {"kind": "otlp", "receives": "usage", "endpoint": "https://otel.example.com/v1/traces"},
        "tracking": {"kind": "mlflow", "receives": "bodies", "endpoint": "postgresql://db.example.com/mlflow"},
    },
    "hosts": ["otel.example.com", "db.example.com"],
})
protocol, url = otlp_traces_target({"OTEL_EXPORTER_OTLP_ENDPOINT": "https://OTEL.example.com:443/"})
print(protocol, url, "->", policy.match_destination("otlp", url).name)
print(policy.match_destination("mlflow", "postgresql+psycopg://u:pw@db.example.com:5432/mlflow").name)
print(policy.match_destination("otlp", "https://evil.example@otel.example.com/v1/traces"))
```

It prints:

```
http/protobuf https://OTEL.example.com:443/v1/traces -> collector
tracking
None
```

The first URL is written another way, but it matches. The second matches with its user, password and driver. The third reads as `otel.example.com` to one URL parser, but a client could connect to `evil.example`, so it matches nothing. [The reference](provevi.md#check-a-telemetry-policy) lists every URL that is refused.

## Copy a run into MLflow

The evidence is the record of a run. MLflow holds a copy, so that its viewer can show the run, search runs and add judgements. `prove mlflow` makes the copy at the place that `MLFLOW_TRACKING_URI` names, and only if the policy approves it. With the variable unset, the copy goes into the store's own file. provrun makes the same copy itself when a run is sealed, as [its features page](provrun-features.md#copy-a-run-into-mlflow) shows.

With no policy, nothing is copied:

```
$ uv run prove --store $STORE mlflow $RUN2 || echo exit $?
prove: the telemetry policy approves no mlflow destination for the store's own file (MLFLOW_TRACKING_URI is unset)
exit 1
```

With the tour's policy, the run is copied at the level `usage`. A second copy replaces the first, so the copy can be rebuilt at any time:

```
$ uv run prove --store $STORE mlflow $RUN2 --policy tours/provevi/policy.yaml
run-20261102T192005Z-c93d67ab: copied to MLflow at sqlite:////private/tmp/provevi-tour/evidence.db, destination viewer, level usage
  experiment 1, MLflow run adc3b893aa3d4213948fc0e6bff7dc18, trace tr-df0f3042c464ccd694ba527b07383e09 with 7 spans

$ uv run prove --store $STORE mlflow $RUN2 --policy tours/provevi/policy.yaml
run-20261102T192005Z-c93d67ab: copied to MLflow at sqlite:////private/tmp/provevi-tour/evidence.db, destination viewer, level usage
  experiment 1, MLflow run 256ad36c0bf94e758ddf07c65dba6f7b, trace tr-df0f3042c464ccd694ba527b07383e09 with 7 spans
  deleted the earlier copy, MLflow run adc3b893aa3d4213948fc0e6bff7dc18
```

A run that does not verify is not copied:

```
$ uv run prove --store $STORE mlflow $RUN1 --policy tours/provevi/policy.yaml || echo exit $?
prove: run-20261102T191955Z-52539489 does not verify, so it is not copied: line 3: seq is 4, not 3; line 3: the chain is broken; refs of row 3 have no event
exit 1
```

A place that the policy does not name is refused, and a password in the URL never shows:

```
$ MLFLOW_TRACKING_URI=https://mlflow.example.com uv run prove --store $STORE mlflow $RUN2 --policy tours/provevi/policy.yaml
prove: the telemetry policy approves no mlflow destination for MLFLOW_TRACKING_URI https://mlflow.example.com:443
```

The copy can also go to an MLflow tracking server, or to MLflow's tables in Postgres. The level limits what it holds, as for any destination.

The copy refuses to run while a proxy variable or a Postgres variable such as `PGHOST` could send it elsewhere. A copy that fails part way deletes what it wrote. An approved library's spans, such as PydanticAI's, go in when the policy lets that library feed MLflow. [The reference](provevi.md#copy-a-run-into-mlflow) gives the rules. `prove ui` opens MLflow's viewer on the store's own file, on 127.0.0.1. On macOS, `/tmp` is a link to `/private/tmp`, so the store's path shows as `/private/tmp`.

## Show a run to a live viewer

provrun can give a live feed of a run, which a program such as a web app streams to a browser. The policy names the viewer as a destination of kind `live`, with a level. provevi cuts each event to what the viewer may see. `replay` gives the same feed for a run that has ended, so a browser that connects late sees the same items. This program, `tours/provevi/replay.py`, prints each event as its type and the keys of its data:

```
$ uv run python tours/provevi/replay.py $RUN2 names
run_created              agent backend domain env mode org span_id trace_id
access_requested         mode
access_granted           decision grant_id mode policy
context_resolved         grant_id parent_span_id span_id started_at trace_id
snapshot_materialized    kind parent_span_id sha256 span_id started_at trace_id
input_staged             kind parent_span_id sha256 span_id started_at trace_id
sandbox_started          agent backend model
prompt_rendered
model_called             finish_reason model parent_span_id span_id started_at streamed trace_id
sandbox_completed        status
input_verified           parent_span_id sha256 span_id started_at trace_id unchanged
output_artifact_created  parent_span_id sha256 span_id started_at trace_id
run_completed            span_id trace_id
{"n": 14, "run_id": "run-20261102T192005Z-c93d67ab", "status": "completed", "type": "done"}
```

At `names`, the feed kept only the keys that hold names, such as `model` and `status`. The purpose, the file's name, the paths, the spec and the token counts stayed out. At `bodies`, it adds them, and the model's answer as a `text` item. [The reference](provevi.md#show-a-run-to-a-live-viewer) says what each level shows.

A run's experiment and its parent run show at `names` too, when they are plain. An experiment's URI shows with no selector, or pinned to a commit. One that names a branch stays out, because a branch's name may hold any text:

```python
from provevi import EvidenceEvent, shown_at

pin = "experiment://acme/risk/dev/credit/revenue?at=commit:7311e8221f7268c0"
for data in ({"agent": "q", "experiment": "experiment://acme/risk/dev/credit/revenue", "experiment_pin": pin,
              "parent_run": "run-20261109T221005Z-0a1b2c3d"},
             {"agent": "q", "experiment": "experiment://acme/risk/dev/credit/revenue?at=branch:jane-review"}):
    event = EvidenceEvent("run-20261109T221010Z-4e5f6a7b", 0, "2026-11-09T22:10:10.000000Z", "run_created",
                          "RunContext", "you", (), data)
    print(shown_at(event, "names")["data"])
```

```
{'agent': 'q', 'experiment': 'experiment://acme/risk/dev/credit/revenue', 'experiment_pin': 'experiment://acme/risk/dev/credit/revenue?at=commit:7311e8221f7268c0', 'parent_run': 'run-20261109T221005Z-0a1b2c3d'}
{'agent': 'q'}
```

## Write an experiment in the catalog

An experiment is a folder in the catalog that holds `experiment.yaml`, and each commit of the folder is a version. It says what every run shares, such as the eval set and the metrics. It holds no agent spec, so one experiment can try many agents. This program writes one twice, in an S3 bucket that moto mocks, and reads both versions:

```
$ uv run python tours/provevi/experiment.py
version 1: sha256 bdf38674b8fe, metrics accuracy
version 2: sha256 5a27db5abdff, metrics accuracy recall
the pins differ: True
the newest version is version 2: True
version 1 at its pin: Does a retrieval step raise accuracy on the revenue questions?
the file at experiment://acme/risk/dev/credit/revenue:
  kind: agent-eval
  description: Does a retrieval step raise accuracy and recall on the revenue questions?
  inputs:
  - data+table://acme/risk/dev/evals/revenue
  metrics:
  - accuracy
  - recall
refused: experiment://acme/risk/dev/credit/revenue/experiment.yaml: name the experiment's folder, not its file
```

A run names its experiment beside its agent, and provrun records the pin. `prove experiments` lists the experiments that runs named, and `prove experiment show` lists each version with its runs. `prove verify` reads the experiment at the run's pin, and checks its hash. [The reference](provevi.md#experiments-and-agent-classes) gives the file's keys.

## Use it from Python

Each command calls a library function, which returns what it finds and prints nothing. This program, `tours/provevi/query.py`, finds the runs that read the tour's document, and checks each one:

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

`verify_run(folder)` checks a run folder as `prove verify` does, and `copy_run(store, run_id, policy)` makes the copy in MLflow. [The reference](provevi.md) lists the rest.

## Where the examples come from

| Examples | Where they ran |
|---|---|
| `$STORE`, `$RUN1`, `$RUN2`, `$TOUR_DIR` | The tour's shell, after `source tours/provevi/start.sh`, in the order of [the tour's](provevi-tour.md) steps. The model's answer is mocked, so they need no key and no network. |
| The run folder, the first event, the manifest and the one line of `prove show` | Another run of the tour, so their run ID differs from the rest. |
| `packages/provevi/tests/data` | A run and a policy that the repository holds. |
| `tours/provevi/experiment.py` | Step 9 of the tour. Its bucket is mocked in its own process. |
| The Python programs on levels and matching | Any shell in the repository, with `uv run python`. |
| The version that fails the policy | The repository's policy, with `versions: ">=2.65"` for pydantic-ai. |
| `MLFLOW_TRACKING_URI=https://mlflow.example.com` | The tour's shell. The name is refused before anything connects to it. |

The run IDs, the times, the hashes of the events and MLflow's IDs differ on each run.
