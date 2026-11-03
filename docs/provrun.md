# provrun

provrun runs agents and model calls on data that provcat resolves, and records the evidence of each run. provrun is the caller of record. It makes the run ID, asks the broker for each input, makes read-only snapshots of the inputs through provcat and runs the agent. Then it writes the evidence and the outputs as one bundle. [provrun-plan.md](provrun-plan.md) has the design and the reasons for it. [What provrun does](provrun-features.md) shows each feature with real output, and [the tour](provrun-tour.md) runs the same commands from a script, `tours/provrun/run.sh`.

## Terms

| Term | Meaning |
|---|---|
| run | One execution of one agent or one model call, with one run ID. |
| agent spec | The portable description of an agent: model, instructions, prompt, inputs and tools. |
| broker | The part that decides whether a run may use a URI. provrun has a stub broker now. |
| grant | The answer of the broker to one request. |
| snapshot | A read-only copy of one pinned version of a file or a folder, in a folder named by its SHA-256. |
| stager | The part that makes the snapshots visible to the agent. |
| backend | The part that runs an agent spec, for example a LiteLLM call or a PydanticAI agent. |
| evidence event | One line in the evidence of a run, for example `model_called`. |
| bundle | The evidence and the outputs of a run. |

## Run an example

The examples `file-run.yaml` and `lakefs-run.yaml` use the LiteLLM setting `mock_response`, so they call no model and need no key.

1. Make the input file.

   ```
   mkdir -p /tmp/provrun-example && echo "Revenue in 2025 was 42 million." > /tmp/provrun-example/report.txt
   ```

2. Run the example.

   ```
   uv run provr run packages/provrun/examples/file-run.yaml
   ```

   The command prints the run ID, the status and the run folder as JSON.

3. Show the events, and check them.

   ```
   uv run provr show <run ID>
   uv run provr verify <run ID>
   ```

`packages/provrun/examples/lakefs-run.yaml` reads `lakefs://quickstart/main/README.md`. It needs the local lakeFS that [setup.md](setup.md) describes, and the example provcat configuration:

```
uv run provr run packages/provrun/examples/lakefs-run.yaml --config packages/provcat/config/provcat.example.yaml
```

Two examples call a real model, `claude-haiku-5-5`. They read the same input file as `file-run.yaml`, so make it first, as in step 1. They need `ANTHROPIC_API_KEY` in the environment.

- `packages/provrun/examples/claude-run.yaml` makes one LiteLLM call.
- `packages/provrun/examples/claude-agent.yaml` runs a PydanticAI agent. The agent searches the input with `grep_file` and records its source with `add_note`. The instructions ask it to write `/output/answer.txt`. The final answer is always in `/output/response.txt` as well.

```
uv run provr run packages/provrun/examples/claude-agent.yaml
```

`grep_file` needs ripgrep, the `rg` command. Without it, the tool tells the agent that ripgrep is not installed.

To call another model, change `model` in the run file, and set the key of its provider, for example `OPENAI_API_KEY`. The evidence holds the prompts and the responses, but not the key. An agent spec refuses a setting that can carry a key, such as `api_key` or `extra_headers`, because the spec goes into the evidence.

## Use provrun from a program

An orchestrator, or any other program, can do all that `provr run` and `provr show` do by calling provrun. `provr` only reads its arguments, calls these functions and prints. The library prints nothing, and it takes its catalog, runs root and settings as arguments.

```python
from provcat import Catalog
from provrun import RunFile, find_run, run_file_sync

rf = RunFile.load("packages/provrun/examples/file-run.yaml")
outcome = run_file_sync(rf, catalog=Catalog.open())
print(outcome.status, outcome.run_id)
print(find_run(outcome.run_id))
```

`Catalog.open()` reads the provcat configuration as `provr run` does. With the input file of step 1 above and `PROVRUN_RUNS=/tmp/provrun-example/runs`, it prints:

```
completed run-20261031T173013Z-38c86007
/private/tmp/provrun-example/runs/run-20261031T173013Z-38c86007
```

On macOS `/tmp` is a link to `/private/tmp`, and `find_run` gives the real path.

| Function | What it does |
|---|---|
| `RunFile.load(path)` | Reads and checks a run file. A missing or bad part raises `RunFileError`. |
| `run_file(rf, catalog=..., runs_root=..., mode=..., bundle_scheme=...)` | Runs it, as a coroutine. It also takes `broker`, `resolver`, `stager`, `backend` and `store`, which replace the defaults, and `policy`, the telemetry policy or its file. Without `store`, it opens the run file's store. `bundle_scheme=False` writes no bundle through the catalog, even if the run file sets a scheme. |
| `run_file_sync(rf, ...)` | The same, for a program with no event loop running. |
| `outcome.to_dict()` | The summary that `provr run` prints. It has `mlflow` only when the policy approves an MLflow place, as [below](#copy-a-sealed-run-into-mlflow). |
| `find_run(run, root, ids_only=False)` | The absolute folder of a run, from its ID or its path. A run ID under `root` comes before a folder in the working directory. With `ids_only=True`, only a run ID is taken, so a name from a user cannot reach another folder. A missing run raises `RunNotFound`. |
| `read_events(folder)` | The events in the evidence folder of a run, as `provr show` prints them. |
| `run_agent(agent, catalog=..., org=..., ...)` | Runs an agent spec with the run settings given one by one. |

`run_file` can run inside a server's event loop, such as FastAPI's, and several runs can go at once. A run does its slow plain-Python steps in threads. These steps fetch and stage the inputs, check them, collect the outputs, seal the run, send the last spans and copy the run into MLflow. So the server goes on answering other requests meanwhile. Two quick steps stay on the loop. One opens the run, making its folder and writing its first event, and the other is the end scan for patches.

A cancel waits for the step in hand to end. Then the run ends with `run_failed`, whose error is `CancelledError`, and the cancel goes on to the caller. A cancel that comes while the run is being sealed waits for the seal. That run keeps the `run_completed` or `run_failed` it was given, and then the cancel goes on to the caller.

A run checks its settings before it makes the run folder. Each of these raises `RunSettingError` and leaves no folder behind:

- a mode that is not `strict` or `lenient`;
- an org, domain or environment that is not one part of a URI;
- a bundle scheme that is not a plain scheme name, or that names a store;
- a telemetry policy file that cannot be read, or a run file's `telemetry` section that would widen the policy;
- in strict mode, a telemetry problem, which "Control of library telemetry" below lists;
- an unknown backend.

An error after that fails the run. The outcome holds the error, and the evidence records it.

Every error that provrun raises on purpose is a `ProvrunError`, so a program can catch them in one place. An error from the system, such as a runs root that cannot be written, comes through as it is. The checks behind `provr verify` are in provevi's library, and [provevi's page](provevi.md) shows how a program calls them.

These settings in provrun affect the whole process:

- During a run, provrun sets a few environment variables, as "Control of library telemetry" below says. It puts them back when the last run that is going on ends.
- The first run sets OpenTelemetry's global tracer, meter and logger providers to provrun's own. They stay for the rest of the process.
- When the LiteLLM backend first calls LiteLLM, it sets `litellm.suppress_debug_info`, so LiteLLM prints nothing on stdout. This also applies to the program's own LiteLLM calls.
- Before each LiteLLM call, the backend sets the environment variable `USE_OTEL_LITELLM_REQUEST_SPAN` to `true`, so that LiteLLM's tracing does not write onto provrun's span. A value that is already set is kept, with a warning if it is not `true`.

## The run file

A run file has two sections:

- **`run`** names the org, the domain, the environment and the actor of the run. It can also hold `allow`, a list of more URIs that the stub broker allows, `bundle_scheme`, and `store`, the evidence store, such as `sqlite:///evidence.db`. [provevi's page](provevi.md) describes the stores. Any other key is refused. The org, the domain and the environment become parts of the run URI. So each must be one part, with no `/`, `?`, `#` or space. Each entry of `allow` must be a URI.
- **`agent`** is an agent spec.
- **`telemetry`**, which is optional, narrows the operator's telemetry policy for this run. "Control of library telemetry" below describes it.

| Key of the agent spec | Meaning |
|---|---|
| `name` | The name of the agent. |
| `backend` | `litellm` for one model call, or `pydantic-ai` for an agent with tools. |
| `model` | The model. LiteLLM takes `provider/model`, for example `openai/gpt-4o-mini`. PydanticAI takes `provider:model`, for example `openai:gpt-4o-mini`. |
| `instructions` | The system instructions. |
| `prompt` | The task. |
| `inputs` | URIs, or objects with `uri` and `name`. Each input is staged at `/input/<name>`. An input must be pinnable: a file or folder in lakeFS or GitHub, a file in a versioned bucket, a bucket folder with a provcat commit, or a local file or folder. An input in a bucket without versioning fails, because provcat cannot pin it. |
| `tools` | Tool names, for the `pydantic-ai` backend. `grep_file` and `add_note` are built in. |
| `settings` | Model settings, for example `temperature`. |
| `limits` | `timeout` in seconds, `request_limit` and `retries`. |

## What a run does

Each step writes an evidence event:

1. `RunContext` makes the run ID and the run folder, and writes `run_created`.
2. The broker writes `access_requested`, and then `access_granted` or `access_denied`, for each input.
3. The resolver writes `context_resolved` and `snapshot_materialized`. The snapshot event holds the pinned URI and the SHA-256.
4. The stager copies each snapshot into the input folder of the run, and writes `input_staged`. It hashes each copy, and a copy that differs from its snapshot fails the run before the agent starts.
5. The backend writes `sandbox_started`, `prompt_rendered`, each `model_called`, each `tool_called` and `sandbox_completed`.
6. The stager hashes the inputs again, and writes `input_verified`. A changed input fails the run.
7. The outputter writes `output_artifact_created` for each file in the output folder. It does so for a failed run too, so the bundle holds no file that the evidence does not name.
8. `RunContext` writes `run_completed` or `run_failed`, closes the evidence and writes the bundle.

provrun reads and writes only through the `Catalog` that it is given. So every lookup, pin, fetch, snapshot, write and commit of a run goes through the hooks of that catalog. An orchestrator passes its hooks to that catalog, as "Hooks from a program" in [the reference](reference.md#hooks-from-a-program) shows.

If a step raises an error, provrun writes `step_failed` with the error, and the run fails. The evidence of a failed run is still complete, and it verifies.

In the PydanticAI backend, one PydanticAI capability writes `model_called` and `tool_called`. A capability is PydanticAI's hook on each step of an agent run. It records:

- each model request, streamed or not, as `model_called`, with `streamed` and `answered_by` in its data. Behind a fallback model, `answered_by` names the model that answered;
- each call of a function tool as `tool_called`, also for a tool that another capability adds;
- a call that waits for approval as `tool_called` with `deferred` set, and again when it runs;
- the output tool, when the agent answers through one, as `tool_called` with `output_tool` set;
- each tool that the provider ran itself, such as a web search, as `tool_called` with `provider_run` set and confidence `parsed`. provrun sees only the provider's account of such a call.

The backend gives text output and no provider tools, so its runs have neither of the last two. Other code can pass the capability, `evidence_capability`, to its own agent, and stream; Step 15 of the [tour](provrun-tour.md) streams Claude Haiku this way. A run file cannot ask for a streamed reply. In a streamed request, the output tool's event comes before the `model_called` that asked for it.

Each `model_called` event lists the tool calls that the model asked for, under `tool_calls`. The list includes a call to an unknown tool, or a call with bad arguments. PydanticAI refuses such a call before the tool runs, so it has no `tool_called` event.

The PydanticAI agent sees `/input` read-only, `/output` and `/scratch`. If the agent tries to write to `/input`, the sandbox refuses. The refusal is in the evidence as `step_failed`, and the model gets the refusal as a message, so it can try another way.

## The evidence

The run folder holds the evidence:

```
<run folder>/evidence/events.jsonl       one event on each line
<run folder>/evidence/payloads/<sha256>  prompts, responses and tool calls
<run folder>/evidence/manifest.json      the hash of the last event, written at the end
<run folder>/output/                     the outputs
<run folder>/input/                      the staged inputs, not in the bundle
<run folder>/scratch/                    working space, not in the bundle
```

Each event holds the SHA-256 of the line before it. `provr verify` checks these things, by calling provevi, which also has the command `prove verify`:

- the chain, and the sequence number of each event
- that the first event is `run_created`, and that the last event is `run_completed` or `run_failed`
- that the manifest counts every event and ends at the last one
- that every event and the manifest belong to the run of the folder
- the payloads, by their SHA-256, and that the payload folder holds exactly the payloads that the manifest lists
- that the output folder holds exactly the recorded outputs, with the recorded SHA-256
- that each output is a regular file, recorded once, and named by the reference of its event

If someone changes, removes, adds or reorders an event, or changes a payload or an output, the check fails.

The local check proves that the folder is consistent. But someone who can write the folder can write a consistent false record. If the run has a committed bundle, `provr verify RUN --config CONFIG` also reads the events and the manifest from the commit, and compares them with the folder. The file `bundle.json` in the run folder names the commit.

Someone who can write the folder can delete that file. So a checker who does not trust the folder passes the commit with `--bundle URI`. `--require-bundle` fails a run that has no commit to compare with.

The runs root is `~/.local/share/provrun/runs`, or `$XDG_DATA_HOME/provrun/runs` if that variable is set. The environment variable `PROVRUN_RUNS` or the option `--runs` changes it. The option `--store` chooses the evidence store, as in `provr --store sqlite:///evidence.db run run.yaml`. For `provr run`, it takes the place of the run file's store. `provr show` reads the events from it, and `provr verify` also checks the run as the store holds it.

provrun also opens an OpenTelemetry span for each step, with the names of the GenAI semantic conventions. Each step's event holds two kinds of value in its data:

- `started_at`, the time the step began, in the same form as the event's `time`, which is the time it ended. It is there with or without tracing, and in `step_failed` too.
- `trace_id`, `span_id` and `parent_span_id`. provrun's spans come from its own tracer provider, which "Control of library telemetry" below describes, so a run's events hold them. `parent_span_id` names the span of the step that holds this one. A recorder used outside a run, with no tracer provider, gives none of the three. So does a span that a provider's sampler drops. The recorder sets these four keys, and drops any value a step gives for them.

Each run is one trace. `run_agent` opens a root span named `run` before it writes `run_created`, and ends it after the event that ends the run. Its attributes name the run and the agent. Its status is an error when the run fails.

Each top-level step is the root span's child, so its `parent_span_id` is the root span's ID. `run_created`, and `run_completed` or `run_failed`, hold the root span's `trace_id` and `span_id`, so a reader finds the root without a guess. Their times are the run's start and end. The root span starts a new trace even when the program that calls `run_agent` has a span open.

A tool that the provider ran gets a span too, as a child of the `model_called` span whose response reported it. provrun did not see the tool run, so the span marks only where the call sits, and the event has no `started_at`. Its event still comes after that `model_called`.

The names are those of release 1.37.0 of the semantic conventions. The GenAI conventions have since moved to their own repository, `open-telemetry/semantic-conventions-genai`, which has no release and no schema URL yet. So provrun claims no schema URL. `provrun/otel.py` keeps every name in one place.

LiteLLM's OpenTelemetry loggers, such as `otel`, `logfire` and `arize`, make no span of their own when a span is active. They write onto the active span, which is provrun's `model_called` span, often after that span ended. So the LiteLLM backend sets `USE_OTEL_LITELLM_REQUEST_SPAN=true` before each call, and those loggers make their own span. The setting stays for the rest of the process. If you set that variable yourself to another value, provrun keeps it and warns. A run empties LiteLLM's callback lists, as "Control of library telemetry" below says, so this matters only for a logger added during a call.

The evidence has two modes. In the strict mode, a lost event stops the run. provrun still closes the evidence, writes the manifest and the bundle, and then raises the error. In the lenient mode, provrun warns and goes on. In both modes, the manifest marks the evidence as incomplete and names each lost event.

A run in the `dev` environment is lenient by default, and a run in any other environment is strict. The option `--mode` changes it.

## Control of library telemetry

The libraries that a run loads can record or send data by themselves, such as LiteLLM's callbacks or MLflow's autolog. In a controlled run, the evidence must be the only record, unless the operator approves another. The operator writes a telemetry policy, which [provevi's page](provevi.md#check-a-telemetry-policy) describes. Phase 3 of [provevi's plan](provevi-plan.md) gives all the steps.

provrun does steps 2 to 10. The steps record the telemetry variables and own the providers. They turn off MLflow, LiteLLM's callbacks, PydanticAI's instrumentation and the loggers' debug levels. They find what cannot be turned off, and check again during the run. Step 9 keeps a run's spans and sends them to the places the policy approves. It copies a sealed run into MLflow, through the copy of Phase 4.

**Where the policy comes from.** In order:

1. the `policy` argument of `run_agent`, `run_file` or `run_file_sync`, or `provr run --policy FILE`;
2. the file that the environment variable `PROVRUN_TELEMETRY_POLICY` names;
3. otherwise, a policy that approves nothing.

A run file cannot name the policy file, because the operator owns it. The run file's `telemetry` section can only narrow the policy. A section that would widen it fails the run before the run folder is made.

**The environment.** The telemetry variables are the user's configuration. `OTEL_EXPORTER_OTLP_ENDPOINT` and its headers name a collector. `MLFLOW_TRACKING_URI` names a tracking server or a database. `LOGFIRE_TOKEN` names a Logfire project.

So a run leaves them as they are, credentials included. provrun sends the run's spans where they point, when the policy approves, as "Where a run's spans go" below says. Libraries do not act on them, because the steps below turn the libraries' own telemetry off.

Before the backend loads its library, provrun sets only these settings. None of them names a destination:

- `LITELLM_LOCAL_MODEL_COST_MAP=True`, `LITELLM_LOCAL_ANTHROPIC_BETA_HEADERS=True` and `LITELLM_MODE=PRODUCTION`. With these, LiteLLM fetches no price table and reads no `.env` file when it is imported. A user who keeps keys in a `.env` file for LiteLLM must load it before the run.
- `LITELLM_LOG=ERROR`, because with no value LiteLLM's log handler takes every level, and `PYDANTIC_AI_NO_BANNER=1`.
- `MLFLOW_USE_DEFAULT_TRACER_PROVIDER=true` and `MLFLOW_DISABLE_TELEMETRY=true`, so that MLflow keeps off provrun's provider and sends no usage reports.
- `LOGFIRE_SEND_TO_LOGFIRE=false` and `LOGFIRE_CONSOLE=false`, the off switches of a Logfire that a library configures for itself.

provrun sets them again as each run starts, so a value changed during one run does not reach a run that starts later. It puts back what the user had when the last run that is going on ends.

`OTEL_SDK_DISABLED` stays as the user set it. provrun makes its own providers live even when it is set, so that its spans still record.

**What a run records about the variables.** `run_created` lists each variable of the `OTEL_*`, `LOGFIRE_*`, `MLFLOW_*` and `LITELLM_*` families that is set. A variable that names a place is recorded by where it points. Such a variable's name ends in `_ENDPOINT`, `_URI` or `_URL`, or is `OTEL_SERVICE_NAME` or `OTEL_EXPORTER_OTLP_PROTOCOL`.

The value of `OTEL_SERVICE_NAME` or `OTEL_EXPORTER_OTLP_PROTOCOL`, such as `http/protobuf`, is kept if it holds only letters, digits and the marks `.`, `_`, `/` and `-`. Otherwise it is recorded as `<not recorded>`.

The value of a name ending in `_ENDPOINT`, `_URI` or `_URL` is recorded in one of these forms:

- A URL with a host gives its scheme, host and port only. The user, password, path, query and fragment are left out, because a token, a password or a local path can sit in each.
- A URL without a host, such as `sqlite:///data/mlflow.db`, gives its scheme only, as `sqlite:<path not recorded>`.
- A bare name of letters and hyphens, such as `databricks`, is kept.
- A URL with a port that is not a number is recorded as `<not recorded: not a valid URL>`.
- Any other value is recorded as `<not recorded: not a URL>`. This includes a value with an `@` outside its user part, and a value such as `localhost:5000`, which has no `//`.

Every other variable, such as a token, a header or a key, is recorded by its name only. For example:

```json
"variables": {
  "LOGFIRE_TOKEN": null,
  "MLFLOW_TRACKING_URI": "postgresql://db.example:5432",
  "OTEL_EXPORTER_OTLP_ENDPOINT": "http://collector.example:4318",
  "OTEL_EXPORTER_OTLP_HEADERS": null
}
```

`overridden` holds the user's own value of each setting above that the run changed, such as `"LITELLM_MODE": "DEV"`.

A library that the program imported before the run read the environment before provrun set it. The run records such libraries; this is not a failure, because a program that passes in a model has imported its library already.

**The providers.** OpenTelemetry has one global tracer provider, meter provider and logger provider in a process, and each can be set once. The first run sets all three to provrun's own. provrun's spans come from its own provider, so the step events of a run hold span IDs. A step event is one that a step writes when it ends, such as `model_called`; `run_created` is not one.

provrun's tracer and logger providers refuse a processor that provrun did not add through their `add_span_processor` and `add_log_record_processor`. MLflow does this when `MLFLOW_USE_DEFAULT_TRACER_PROVIDER` is false. Code that reaches into a provider's private parts can still add one. The checks below find a global provider that was replaced.

Each refusal is written to each open run's evidence as `telemetry_blocked`. Outside a run, it is a warning. `prove telemetry RUN` lists a run's refusals.

**The libraries.** As each run starts, and again before and after each model call, provrun turns off what a library has switched on. It writes a `telemetry_blocked` event for each thing it turns off.

The event's `library` is the distribution that installed the code, the name pip knows it by, such as `opentelemetry-sdk` or `pydantic-ai-slim`. Code that no distribution installed, such as a program's own package, is named by its top module. The name takes the form that pip uses, so `my_tools` becomes `my-tools`. A program's own script, `__main__`, gets no library.

- **MLflow.** Only when the program has loaded MLflow, because the calls would load it. provrun calls `mlflow.autolog(disable=True)`. Then it calls `autolog(disable=True)` for each MLflow flavor that is on. It does so for the flavors of LiteLLM, PydanticAI, OpenAI and Anthropic too, when their library is loaded. The general call does not undo a flavor's own call, so each goes by name. Then it calls `mlflow.tracing.disable()`. As each run starts, it also calls `set_telemetry_client()`, so that an MLflow loaded before the run reads `MLFLOW_DISABLE_TELEMETRY`. The event names each flavor that was on.
- **LiteLLM's callbacks.** provrun empties all nine of LiteLLM's callback lists, such as `success_callback` and `_async_success_callback`, and the logger that a call's `logger_fn` argument leaves behind. A call's `callbacks=` argument stays in the global lists, so provrun empties them after each call too. A policy cannot keep a LiteLLM logger yet. Each sends to its own service, past the policy's levels. The one that writes to provrun's tracer provider, `otel`, does not start without LiteLLM's proxy package.
- **PydanticAI's instrumentation.** provrun sets `instrument` on the agent it builds. The agent's own value wins over `Agent.instrument_all`, so provrun leaves that class setting alone. The value is `False`, unless the policy gives PydanticAI a destination, as `pydantic-ai` or `pydantic-ai-slim`. Then it is `InstrumentationSettings` with provrun's tracer and meter providers, so PydanticAI's spans go to provrun's providers. The spans hold no binary content. They hold prompts and replies only when every one of its destinations receives bodies.

  A model passed in, wrapped in an `InstrumentedModel`, is taken out of the wrapper first, because the wrapper's settings would win. An `InstrumentedModel` deeper inside, such as in a `FallbackModel`, fails the run. PydanticAI looks only at the outer model, so the inner one would send spans to its own provider.
- **The loggers.** provrun holds the loggers `LiteLLM`, `LiteLLM Router`, `LiteLLM Proxy`, `anthropic`, `openai` and their `_base_client` children at `WARNING`, because some log request bodies at `DEBUG`. It holds `botocore` there too, which provcat uses for S3, because it logs request signatures and headers. A root logger at `DEBUG` then cannot turn them on. The event names a logger that would have logged below `WARNING`.

When the last run that is going on ends, LiteLLM's lists get back what they held before the first run. The loggers get back their levels. A callback that was added during a run does not come back. MLflow's autolog and tracing stay off, because MLflow has no call that puts them back as they were.

**What `run_created` records,** under `telemetry`:

| Key | Meaning |
|---|---|
| `policy` | The SHA-256 of the policy the run ran under, after the run file narrowed it. |
| `policy_source` | Where the policy came from. |
| `variables` | The telemetry variables that were set, as above. A credential is recorded by its name only. |
| `overridden` | The user's value of each setting that the run changed. |
| `problems` | The problems found, as below. |
| `imported_before` | The libraries the program imported before the run. |
| `destinations` | Each place that the variables name, and whether the run sends to it, as below. |
| `redirects` | The proxy and Postgres variables that were set, by name only, as below. |

**Problems.** A problem is something that sends past the policy, or may, and that provrun cannot turn off. As a run starts, a problem fails a strict run before the run folder is made. In lenient mode, the run warns, and `run_created` lists it under `problems`.

A run looks for each of the following as it starts:

- an approved library is installed at a version that the policy does not name;
- someone else set a global provider before provrun did. provrun's spans still come from its own provider;
- `logfire.configure()` has run. Logfire's own exporters then send to Logfire, and to the `OTEL_EXPORTER_OTLP_*` endpoint, past the policy;
- an OpenTelemetry auto-configuration is loaded, by `opentelemetry-instrument` or a distro. It sends by the environment, past the policy;
- an MLflow patch is still on after MLflow's autolog was turned off;
- MLflow cannot be turned off, such as an MLflow with no tracing module.

**Checks during a run.** Before and after each model call, provrun turns off again what came back, as "The libraries" above says. It then looks for Logfire, an auto-configuration, and a global provider that was replaced. This check takes some tens of microseconds.

At the run's end, provrun scans the modules of LiteLLM, PydanticAI, the OpenAI and Anthropic SDKs and the HTTP libraries for patches. It runs even when the run has failed with an error, but not when the run is cancelled or interrupted. A patch is a function that was there at the run's start and was replaced by a wrapper, or an MLflow patch. The scan also runs as the run starts, to note each function. A function first seen during the run, as a module loads, is not a patch.

The scan reads each function without running any library code. It takes from 200 to 350 milliseconds, by how many of these libraries are loaded. So it adds up to about 0.7 seconds to a run. It holds up the other runs in the process while it runs.

A problem found during a run is written as a `telemetry_problem` event, with the problem under `problem`. A strict run then fails at its next check. The model call or the end of the run raises the error, and the run ends with `run_failed`. A lenient run warns and goes on. Each problem is reported once to each run.

The checks look at the whole process. A problem that one run causes, such as a `logfire.configure()`, is a problem for every open run. It fails each strict one.

`prove telemetry RUN` lists the `telemetry_blocked` and the `telemetry_problem` events.

**Where a run's spans go.** A run's spans are provrun's own, one for the run and one for each step. Spans also come from a library whose instrumentation the policy approves, such as PydanticAI's. The run keeps them all, and sends them only to the places that the policy approves.

- **The evidence.** Each step's event holds its span's IDs and data, so provrun's own spans are in the evidence already. An approved library's span is written as a `span_recorded` event. The event holds the span's name, library, IDs, times, status and its names and usage attributes. Its bodies, such as a prompt, and its events go into the event's payload.
- **A library that the policy does not approve.** A span from such a library, made with provrun's provider, goes nowhere. The run writes one `telemetry_blocked` event for that library, with the path `opentelemetry.span`.

As each run starts, provrun reads the variables that name a place:

| Kind | Variables | Where provrun sends |
|---|---|---|
| `otlp` | `OTEL_EXPORTER_OTLP_TRACES_ENDPOINT` or `OTEL_EXPORTER_OTLP_ENDPOINT`, with the protocol, headers, timeout and certificate variables | The traces address, by the OpenTelemetry rules that provevi's `otlp_traces_target` gives. Nothing if `OTEL_SDK_DISABLED` is true or `OTEL_TRACES_EXPORTER` is `none`. |
| `logfire` | `LOGFIRE_TOKEN`, and `LOGFIRE_BASE_URL` | The base URL, or else the region in the token, `us` or `eu`. The token is the only header provrun adds, besides the protocol's own. Nothing if the user set `LOGFIRE_SEND_TO_LOGFIRE` to `false`. |
| `mlflow` | `MLFLOW_TRACKING_URI`, or with it unset the sqlite store's own file | No span while the run goes on. A copy of the sealed run, [below](#copy-a-sealed-run-into-mlflow). |

A place gets spans only when the policy has a destination of its kind at that endpoint. provevi's `match_destination` decides this. A place with no match gets nothing, and the run writes `telemetry_blocked` with the path `destination`.

A matched place gets provrun's spans, and the spans of each library that the policy lets feed that destination. A span's library is known by its tracer's name, its scope. provevi's `span_libraries` gives the names the policy may know it by. They come only from an installed package that ships the scope's module, such as `pydantic-ai-slim` for `pydantic_ai`.

A scope that no installed package shows, such as `com.anthropic.sdk.python` or `pydantic_ai.evil`, has no name. The policy then approves none of its spans, and the run records a `telemetry_blocked` event. The copy in MLflow decides the same way.

Each span is cut down to the level the destination receives:

- `names` keeps the names attributes;
- `usage` adds the token counts;
- `bodies` keeps everything.

Below `bodies`, a span's events and its status message are dropped, because they can hold an exception's text. Its links and its scope keep only what the level allows.

A span name can hold a prompt or a person's name. So a name that is not one or two plain words becomes the library's name, as provevi's `plain_span_name` decides. A plain word holds no URL, no user and password, no setting such as `token=x` and no path from `/`. Below `bodies`, an attribute's text that passes as a name but is not plain is left out too, as provevi's `plain_name` decides. The evidence keeps such a span name in the payload.

provrun sends OTLP over HTTP itself, with protobuf. Its sender reads no variable of its own. So the OTLP headers never reach Logfire, and Logfire's token never reaches the collector. It does not follow a redirect from the place.

**Certificates and headers.** provrun's sender ignores `REQUESTS_CA_BUNDLE`. It trusts the certificate that `OTEL_EXPORTER_OTLP_CERTIFICATE` names. It sends the client certificate that `OTEL_EXPORTER_OTLP_CLIENT_CERTIFICATE` and `OTEL_EXPORTER_OTLP_CLIENT_KEY` name. A headers variable cannot set `Host`, `Content-Length` or another header that would change where or how the request goes.

**Protocols.** For gRPC, provrun uses OpenTelemetry's gRPC exporter, which `provrun[otlp-grpc]` installs. OpenTelemetry for Python has no `http/json` exporter, so a run with that protocol sends nothing, and warns.

**Proxies.** Some variables send a connection to another place than its URL names. They are `HTTP_PROXY`, `HTTPS_PROXY`, `ALL_PROXY`, `grpc_proxy` and their lower-case forms. provrun's senders ignore them, so the spans go straight to the place the policy approved.

`run_created` lists each one that was set under `redirects`, by name only, because a proxy URL can hold a password. The list also holds `PGHOST`, `PGHOSTADDR`, `PGSERVICE` and the other Postgres variables, which matter only for the MLflow destination. A firewall that allows only the proxy then blocks the spans, and the run warns that they were not sent.

`run_created` records each place under `destinations`. For example:

```json
"destinations": [
  {"kind": "otlp", "variable": "OTEL_EXPORTER_OTLP_ENDPOINT", "url": "http://127.0.0.1:4318",
   "destination": "collector", "receives": "names", "sending": true, "note": ""}
]
```

provrun sends the spans in batches while the run goes on. It sends the rest when the run ends, after the run's own span has ended, to all places at once.

The run's end waits up to about ten seconds in all, however many places there are. It waits in a thread, so the event loop goes on. The `OTEL_BSP_*` variables do not change the batches. A request that started before the run's end, and a gRPC call, keep their own timeout, from `OTEL_EXPORTER_OTLP_TIMEOUT`. A place that cannot be reached gives a warning with the number of spans lost. The run does not fail, and its evidence does not record the loss, because the evidence is closed by then.

### Copy a sealed run into MLflow

MLflow gets a copy of the run, not its spans as they end. The place is the policy's `mlflow` destination that matches `MLFLOW_TRACKING_URI`. With the variable unset, a destination with no endpoint names the sqlite evidence store's own file. With neither, a run copies nothing.

As the run starts, provrun matches the place, and records it under `destinations` in `run_created`. A place that the policy does not approve is a `telemetry_blocked` event, and gets nothing.

So is an approved place that a variable could move. For an http or https place, that is a proxy variable or one that changes which servers the copy trusts, such as `REQUESTS_CA_BUNDLE`. For a Postgres or MySQL place, it is one of that database's variables, such as `PGHOST`. provevi's `refuse_redirects` gives the list.

The event names the variable, and records only the scheme, host and port of its URL, so no password or local path. A file store has no file for MLflow's tables, so a run with the file store and no variable copies nothing, and warns.

When the run is sealed, provrun calls provevi's `copy_run` in a process of its own, with the user's variables. The copy changes variables and loaded modules while it works, and these do not reach another run of the program.

It reads the run back from the store and checks its chain. Then it writes one MLflow run and one trace, cut to the level the destination receives. [provevi's page](provevi.md#copy-a-run-into-mlflow) gives its rules. A failed run is copied too. A cancelled or interrupted run is not.

The summary of `provr run`, and `outcome.mlflow`, say where the copy went:

```json
"mlflow": {"destination": "own", "receives": "names", "copied": true,
           "tracking_uri": "sqlite:////tmp/provrun-tour/mlflow-evidence.db", "experiment_id": "1",
           "mlflow_run_id": "...", "trace_id": "tr-...", "spans": 7}
```

The run's end waits for the copy, for 120 seconds at most, `COPY_SECONDS` in `provrun.destinations`. Unless you set them, MLflow retries a server twice, and waits 20 seconds for each answer. A copy that fails, or takes too long, gives a warning, and the summary has `"copied": false` with the error. The run does not fail, because its evidence is sealed by then. `prove mlflow RUN` makes the copy again, once the cause is fixed. Step 13 of [the tour](provrun-tour.md) makes a copy.

## The stub broker

The stub broker grants a URI if all of these are true:

- The URI, without its selector, is an input of the agent or is in the `allow` list. An `allow` entry that ends with `/` allows every URI under it.
- A URI with a selector, such as `?at=branch:prod`, is in the `allow` list with that same selector. A selector can name another branch, and the branch is often the environment.
- The mode is `mounted_snapshot`.
- For a logical URI, of the form `scheme://org/domain/env/...`, the org and the environment are those of the run.
- For a physical URI, the run is in the `dev` environment. A URI is physical if its scheme names a store of the catalog. The store can be built in, such as `file` or `s3`, or added by the configuration, such as `lakefs-acme`. A physical URI names no environment, so the stub does not grant one in `staging` or `prod`.

## The bundle

If the run file sets `bundle_scheme`, provrun writes the bundle through provcat to `<scheme>://<org>/<domain>/<env>/<run ID>`, and commits it. The scheme must be a logical scheme in the provcat configuration. For example, this scheme puts bundles in a versioned bucket named `runs`:

```
schemes:
  run:
    parts: [org, domain, env]
    rest: path
    target: "s3://runs/{ref}/{org}/{domain}/{env}/{path}"
    ref: main
```

provrun prints the pinned URI of the commit. Another run can commit the same branch first and take in this bundle. Then provrun reads the newest commit, and accepts it if it holds this bundle. 

If the store for bundles is `file`, which has no commits, provrun writes the bundle and says that it is not pinned. A bucket for bundles must have versioning on. Otherwise the commit fails, and so does the bundle. If the bundle cannot be written or committed, a strict run fails, and a lenient run warns.

## Known limits

| Limit | What to do |
|---|---|
| The broker is a stub. It reads no profile and checks no identity. | Use it only for development. |
| Snapshots and staged inputs are read-only, so `rm -rf` cannot delete them. | Run `chmod -R u+w` on the folder first. |
| Snapshots are never deleted. | Delete old folders under `<cache>/.provcat/snapshots` when no run uses them. |
| LiteLLM can print a warning that a logging coroutine was never awaited. | Ignore it. It comes from LiteLLM's logging, after the run ends. |
| PydanticAI printed a banner about Logfire in a terminal if a program imported it before a run. | Set `PYDANTIC_AI_NO_BANNER=1`. A run sets it before the backend loads PydanticAI. |
| provrun does not guard the network. A run can reach any host the machine can. | Control egress with the firewall or the container that runs provrun. |
| A copy into MLflow that fails is a warning only, and the evidence does not record it. | Read the summary's `mlflow` part, and run `prove mlflow RUN` again once the cause is fixed. |
| A span that a place did not get is a warning only, because the run's evidence is closed when the last spans are sent. | Watch the warnings, or the place's own logs. |
| A library is known by the name of its tracer, if an installed package ships that module. Code that names its tracer after a module of an approved library, such as `litellm`, has its spans kept and sent as that library's. | Approve only libraries whose code you run. A container is the boundary for the rest. |
| A library's span made in a thread that lost the run's context starts a new trace. It goes to no run, so it is neither kept nor sent. | None yet. |
| The scan for patches looks only in the libraries named above, and finds a function replaced by a wrapper. A library that changes a function in place, or patches another library, is not found. A Logfire configured with `local=True` is not found either. | The checks catch the known paths. A container is the boundary for the rest. |
| The scan finds a patch only on a library that was loaded when the run started. `provr run` starts with no backend loaded. So a library that is loaded during the run, and patched by an import hook as it loads, is not found. Only MLflow's patches are found then, by their marks. | Load the backend's libraries before the run, in a program that calls provrun, or use a container. |
| After a run, MLflow's autolog and tracing stay off for the rest of the process. | Turn them on again after the run, if the program needs them. |
| Only the in-process backends exist. | Docker and Foundry backends are planned. See [provrun-plan.md](provrun-plan.md). |
