# provrun

provrun runs agents and model calls on data that provcat resolves, and records the evidence of each run. provrun is the caller of record. It makes the run ID, asks the broker for each input, makes read-only snapshots of the inputs through provcat and runs the agent. Then it writes the evidence and the outputs as one bundle. [provrun-plan.md](provrun-plan.md) has the design and the reasons for it.

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

The examples use the LiteLLM setting `mock_response`, so they call no model and need no key.

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

To call a real model, remove `mock_response` from the run file, and set the key of the provider in the environment, for example `OPENAI_API_KEY`.

## The run file

A run file has two sections:

- **`run`** names the org, the domain, the environment and the actor of the run. It can also hold `allow`, a list of more URIs that the stub broker allows, and `bundle_scheme`.
- **`agent`** is an agent spec.

| Key of the agent spec | Meaning |
|---|---|
| `name` | The name of the agent. |
| `backend` | `litellm` for one model call, or `pydantic-ai` for an agent with tools. |
| `model` | The model. LiteLLM takes `provider/model`, for example `openai/gpt-4o-mini`. PydanticAI takes `provider:model`, for example `openai:gpt-4o-mini`. |
| `instructions` | The system instructions. |
| `prompt` | The task. |
| `inputs` | URIs, or objects with `uri` and `name`. Each input appears at `/input/<name>`. |
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

If a step raises an error, provrun writes `step_failed` with the error, and the run fails. The evidence of a failed run is still complete, and it verifies.

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

Each event holds the SHA-256 of the line before it. `provr verify` checks these things:

- the chain, and the sequence number of each event
- that the first event is `run_created`, and that the last event is `run_completed` or `run_failed`
- that the manifest counts every event and ends at the last one
- that every event and the manifest belong to the run of the folder
- the payloads, by their SHA-256
- that the output folder holds exactly the recorded outputs, with the recorded SHA-256

If someone changes, removes, adds or reorders an event, or changes a payload or an output, the check fails.

The local check proves that the folder is consistent. But someone who can write the folder can write a consistent false record. If the run has a committed bundle, `provr verify RUN --config CONFIG` also reads the events and the manifest from the commit, and compares them with the folder. The file `bundle.json` in the run folder names the commit.

The runs root is `~/.local/share/provrun/runs`. The environment variable `PROVRUN_RUNS` or the option `--runs` changes it.

provrun also opens an OpenTelemetry span for each step, with the names of the GenAI semantic conventions. Each event holds the trace ID and the span ID of its span, if a tracer provider is set.

The evidence has two modes. In the strict mode, a lost event stops the run. provrun still closes the evidence, writes the manifest and the bundle, and then raises the error. In the lenient mode, provrun warns and goes on. In both modes, the manifest marks the evidence as incomplete and names each lost event.

A run in the `dev` environment is lenient by default, and a run in any other environment is strict. The option `--mode` changes it.

## The stub broker

The stub broker grants a URI if all of these are true:

- The URI, without its selector, is an input of the agent or is in the `allow` list. An `allow` entry that ends with `/` allows every URI under it.
- A URI with a selector, such as `?at=branch:prod`, is in the `allow` list with that same selector. A selector can name another branch, and the branch is often the environment.
- The mode is `mounted_snapshot`.
- For a logical URI, of the form `scheme://org/domain/env/...`, the org and the environment are those of the run.
- For a physical URI, such as `file://` or `lakefs://`, the run is in the `dev` environment. A physical URI names no environment, so the stub does not grant one in `staging` or `prod`.

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

provrun prints the pinned URI of the commit. Another run can commit the same branch first and take in this bundle. Then provrun reads the newest commit, and accepts it if it holds this bundle. If the store has no commits, for example `file`, provrun writes the bundle and says that it is not pinned. If the bundle cannot be written, a strict run fails, and a lenient run warns.

## Known limits

| Limit | What to do |
|---|---|
| The broker is a stub. It reads no profile and checks no identity. | Use it only for development. |
| The PydanticAI wrappers do not see a streamed model request. | provrun does not stream. Do not add an event stream handler. |
| Snapshots and staged inputs are read-only, so `rm -rf` cannot delete them. | Run `chmod -R u+w` on the folder first. |
| Snapshots are never deleted. | Delete old folders under `<cache>/.provcat/snapshots` when no run uses them. |
| LiteLLM can print a warning that a logging coroutine was never awaited. | Ignore it. It comes from LiteLLM's logging, after the run ends. |
| Only the in-process backends exist. | Docker and Foundry backends are planned. See [provrun-plan.md](provrun-plan.md). |
