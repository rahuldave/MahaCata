# provrun

provrun runs agents and model calls on data that provcat resolves, and records the evidence of each run. provrun is the caller of record. It makes the run ID, asks the broker for each input, makes read-only snapshots of the inputs through provcat and runs the agent. Then it writes the evidence and the outputs as one bundle. [provrun-plan.md](provrun-plan.md) has the design and the reasons for it. [provrun-tour.md](provrun-tour.md) is a guided tour that follows one file from a store into a run and its evidence.

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
| `run_file(rf, catalog=..., runs_root=..., mode=..., bundle_scheme=...)` | Runs it, as a coroutine. It also takes `broker`, `resolver`, `stager`, `backend` and `store`, which replace the defaults. Without `store`, it opens the run file's store. `bundle_scheme=False` writes no bundle through the catalog, even if the run file sets a scheme. |
| `run_file_sync(rf, ...)` | The same, for a program with no event loop running. |
| `outcome.to_dict()` | The summary that `provr run` prints. |
| `find_run(run, root, ids_only=False)` | The absolute folder of a run, from its ID or its path. A run ID under `root` comes before a folder in the working directory. With `ids_only=True`, only a run ID is taken, so a name from a user cannot reach another folder. A missing run raises `RunNotFound`. |
| `read_events(folder)` | The events in the evidence folder of a run, as `provr show` prints them. |
| `run_agent(agent, catalog=..., org=..., ...)` | Runs an agent spec with the run settings given one by one. |

A run checks its settings before it makes the run folder. Each of these raises `RunSettingError` and leaves no folder behind:

- a mode that is not `strict` or `lenient`;
- an org, domain or environment that is not one part of a URI;
- a bundle scheme that is not a plain scheme name, or that names a store;
- an unknown backend.

An error after that fails the run. The outcome holds the error, and the evidence records it.

Every error that provrun raises on purpose is a `ProvrunError`, so a program can catch them in one place. An error from the system, such as a runs root that cannot be written, comes through as it is. The checks behind `provr verify` are in provevi's library, and [provevi's page](provevi.md) shows how a program calls them.

Two settings in provrun affect the whole process:

- When the LiteLLM backend first calls LiteLLM, it sets `litellm.suppress_debug_info`, so LiteLLM prints nothing on stdout. This also applies to the program's own LiteLLM calls.
- Before each LiteLLM call, the backend sets the environment variable `USE_OTEL_LITELLM_REQUEST_SPAN` to `true`, so that LiteLLM's tracing does not write onto provrun's span. A value that is already set is kept, with a warning if it is not `true`.

## The run file

A run file has two sections:

- **`run`** names the org, the domain, the environment and the actor of the run. It can also hold `allow`, a list of more URIs that the stub broker allows, `bundle_scheme`, and `store`, the evidence store, such as `sqlite:///evidence.db`. [provevi's page](provevi.md) describes the stores. Any other key is refused. The org, the domain and the environment become parts of the run URI. So each must be one part, with no `/`, `?`, `#` or space. Each entry of `allow` must be a URI.
- **`agent`** is an agent spec.

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

The backend gives text output and no provider tools, so its runs have neither of the last two. Other code can pass the capability, `evidence_capability`, to its own agent. In a streamed request, the output tool's event comes before the `model_called` that asked for it.

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

provrun also opens an OpenTelemetry span for each step, with the names of the GenAI semantic conventions. Each event holds the trace ID and the span ID of its span, if a tracer provider is set.

The names are those of release 1.37.0 of the semantic conventions. The GenAI conventions have since moved to their own repository, `open-telemetry/semantic-conventions-genai`, which has no release and no schema URL yet. So provrun claims no schema URL. `provrun/otel.py` keeps every name in one place.

LiteLLM's OpenTelemetry loggers, such as `otel`, `logfire` and `arize`, make no span of their own when a span is active. They write onto the active span, which is provrun's `model_called` span, often after that span ended. So the LiteLLM backend sets `USE_OTEL_LITELLM_REQUEST_SPAN=true` before each call, and those loggers make their own span. The setting stays for the rest of the process. If you set that variable yourself to another value, provrun keeps it and warns.

The evidence has two modes. In the strict mode, a lost event stops the run. provrun still closes the evidence, writes the manifest and the bundle, and then raises the error. In the lenient mode, provrun warns and goes on. In both modes, the manifest marks the evidence as incomplete and names each lost event.

A run in the `dev` environment is lenient by default, and a run in any other environment is strict. The option `--mode` changes it.

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
| PydanticAI prints a banner about Logfire on each run in a terminal. | Set `PYDANTIC_AI_NO_BANNER=1`. |
| Only the in-process backends exist. | Docker and Foundry backends are planned. See [provrun-plan.md](provrun-plan.md). |
