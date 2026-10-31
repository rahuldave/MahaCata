# Plan for provrun, the runtime beside provcat

provrun is a Python library that runs agents and model calls on data that provcat resolves. It is the caller of record for every run. It makes the run ID, asks the access broker for a grant and asks provcat for the files. Then it stages the files, runs the agent and records the evidence. The first version runs agents in the same process as the caller. Docker and Azure Foundry come later, so the classes must already have room for them.

**Status:** phases 0 to 4 are built. Commit `5d8246b` made the workspace, and commit `2524d03` added snapshots and provrun. [provrun.md](provrun.md) is the guide for users. This plan records the design, so some steps below describe work that is now finished.

The design comes from three sources. The first source is the private planning documents of the larger system that provrun serves. The sections of its implementation spec on access, evidence and execution matter most. The second source is the agent execution spec of an earlier, private runtime. The third source is the current Microsoft documentation of the Foundry Agent Service.

## Terms

| Term | Meaning |
|---|---|
| run | One execution of one agent or one model call, with one run ID. |
| caller of record | The process that owns a run. It writes the facts about the run, and the agent does not. |
| grant | The answer of the access broker to one request for one URI. It names the mode, the pinned URI and the expiry. |
| broker | The access broker. It decides each grant. In this plan it is a stub. |
| snapshot | A read-only copy of one pinned version of a file or a folder, which no later fetch changes. |
| stager | The part that makes the granted files visible to the agent, for example as a mount in a sandbox. |
| agent spec | The portable description of an agent: instructions, model route, tool names, inputs and outputs. |
| backend | The part that runs an agent spec in one place, for example in this process or in Foundry. |
| evidence event | One typed, append-only record of one thing that happened in a run. |
| evidence bundle | All the evidence events and payloads of one run, committed as one catalog object. |
| sink | A place that the evidence recorder writes events to, for example a local JSONL file. |
| importer | Code that turns a trace from another system into evidence events. |
| JSONL | A text file with one JSON object on each line. |

## Decisions already made

- **The earlier runtime does not change.** It will be retired. provrun copies its generic parts and changes them. Nothing in provrun imports it.
- **provrun depends on provcat, and provcat never depends on provrun.**
- **The broker is a stub.** Profiles come later.
- **The first backends run in this process.** They are a single LiteLLM call and a PydanticAI agent with a filesystem sandbox.
- **Evidence comes first.** Every other part writes evidence from its first version.
- **One call writes two kinds of record.** Each step writes a canonical evidence event and an OpenTelemetry span.
- **The backend interface is asynchronous.** Foundry runs are remote and need start, poll and cancel. An in-process backend completes at once.
- **Evidence failure has two modes.** In the strict mode, a run fails when the recorder cannot write an event. In the lenient mode, the recorder logs a warning and the run goes on. Strict is the default outside development.

## Layout of the repository

MahaCata becomes a uv workspace. A uv workspace is one repository with several Python packages, one lock file and editable installs between the packages.

```
MahaCata/
  pyproject.toml            # workspace root, no package of its own
  uv.lock
  docs/                     # shared docs, including this plan
  packages/
    provcat/
      pyproject.toml        # today's pyproject.toml, with the provc command
      src/provcat/
      tests/
    provrun/
      pyproject.toml        # the provr command
      src/provrun/
      tests/
```

The move of provcat changes paths only. The provcat package, its `provc` command and its tests keep their names and their behavior.

## Classes

The classes below are the whole design. The phases build some of them now and leave the others as named interfaces.

| Class | Role | Built now |
|---|---|---|
| `RunContext` | Holds the run ID, the environment, the recorder, the resolver and the broker. Every other part takes it. | Yes |
| `EvidenceRecorder` | Appends evidence events, stores large payloads by hash and opens OpenTelemetry spans. | Yes |
| `Sink` | Writes events. `JsonlSink` writes a local file. `EvidenceServerSink` comes later. | `JsonlSink` |
| `Importer` | Turns another system's trace into evidence events, with a confidence level. | PydanticAI messages only |
| `Resolver` | Pins a URI and makes a snapshot. `ProvcatResolver` calls provcat. | Yes |
| `Broker` and `Grant` | Decide access for one URI and one mode. `StubBroker` checks an allow list. | Stub |
| `Stager` | Makes granted files visible to the agent. | `InProcessStager` |
| `RunSpec` | Lists the staged inputs, the input, output and scratch folders, and the environment of one run. The limits are in `AgentSpec`. | Yes |
| `AgentSpec` | The portable agent description. | Yes |
| `Backend` | Runs an agent spec. | LiteLLM and PydanticAI |
| `Tool` and `ToolRegistry` | Hold tools by name, each with a function and an execution mode. PydanticAI makes the JSON schema from the function. | In-process mode |
| `Outputter` | Collects the outputs and hashes them. `RunContext` commits them with the evidence. | Yes |

Each step of a run writes an evidence event. The steps of one run follow this order:

1. `RunContext` writes `run_created`.
2. `Broker` writes `access_requested` and then `access_granted` or `access_denied` for each input URI.
3. `Resolver` writes `context_resolved` and `snapshot_materialized`. Steps 2 and 3 repeat for each input.
4. `Stager` writes `input_staged` for each input, with its hash.
5. `Backend` writes `sandbox_started`, `prompt_rendered`, each `model_called` and each `tool_called`, and then `sandbox_completed`.
6. `Stager` hashes each input again and writes `input_verified`.
7. `Outputter` writes `output_artifact_created` for each output.
8. `RunContext` writes `run_completed` or `run_failed`, and then commits the evidence bundle.

A step that raises an error writes `step_failed`. The PydanticAI backend also writes `trace_imported`, just before `sandbox_completed`. The agent can write `note_added`.

### Room for later backends

The later backends need no change to the classes above. They need new subclasses only:

- **Docker:** a `BindMountStager` and a `DockerBackend`. The container serves the Foundry hosted-agent protocol, `POST /responses` on port 8088, so the same image runs in Foundry.
- **Foundry prompt agents:** a `SessionUploadStager` or a `BlobRefStager`, and a `FoundryPromptBackend`. Function tools run in provrun through the `function_call` and `function_call_output` round trip.
- **Foundry hosted agents:** a `FoundryHostedBackend` and an importer for Application Insights spans.
- **Remote tools:** a remote MCP mode on `Tool`, with an importer for the calls that provrun cannot see.

Foundry has no read-only mounts, and its session files are writable. So the stager records the hash of each staged input, and integrity checks use those hashes.

## Evidence events

An evidence event has these fields:

| Field | Meaning |
|---|---|
| `run_id` | The run that the event belongs to. |
| `seq` | The position of the event in the run, from 0. |
| `time` | The UTC time at which the recorder wrote the event. |
| `type` | One of the event types in the section "Classes" above. |
| `component` | The class that wrote the event, for example `StubBroker`. |
| `actor` | The user or the service identity of the run. |
| `refs` | The pinned logical URIs that the event is about. |
| `data` | Small facts, for example a model name, a token count or a decision. |
| `payload` | The SHA-256 of a large payload, for example a prompt or a response, stored apart from the event. |
| `confidence` | `observed`, `parsed`, `inferred` or `verified`. Events that provrun writes itself are `observed`. |
| `prev` | The SHA-256 of the previous event of the run. The chain shows when someone changes or removes an event. |

Events refer to logical URIs and not to paths on a disk, so that a later job can ask for the same snapshot.

At the end of a run, the recorder writes a manifest with the hash of the last event and the list of payloads. Then `RunContext` commits the evidence and output folders of the run through provcat to `run://<org>/<domain>/<env>/<run_id>`. The commit uses the provcat write path that exists, which is `put` and then `commit`. The bundle then has a pinned version like any other catalog object.

The recorder also opens an OpenTelemetry span for each step. The spans use the GenAI semantic conventions, for example `invoke_agent` and `execute_tool`. Those conventions are still at Development status, so provrun pins one version. When no exporter is set, the spans go nowhere and cost little.

Model calls and tool calls inside PydanticAI must reach the recorder without help from the agent. provrun wraps the PydanticAI model in a subclass of its `WrapperModel` that writes `model_called`. It also wraps each toolset in a subclass of `WrapperToolset` that writes `tool_called`. The agent can add notes and citations through a tool, and those events have the component `agent`.

## Phases

### Phase 0: make the workspace

1. Wait until no other session has uncommitted changes in provcat. Another session is changing the tenants code now.
2. Move `src/provcat`, `tests` and `pyproject.toml` into `packages/provcat` with `git mv`, so that the history follows the files.
3. Write the workspace root `pyproject.toml`, and run `uv lock`.
4. Run the provcat unit and integration tests. They must pass with no change to their code.
5. Fix the paths in `README.md` and in `docs/`.

**Done when:** `uv run provc --help` works from the root, and the provcat tests pass.

### Phase 1: run context and evidence

1. Write `RunContext`, with the run ID, the environment, the actor and the evidence mode. A context variable holds the current run, so that code inside a tool can find it.
2. Write `EvidenceEvent`, `EvidenceRecorder`, `JsonlSink` and the payload store. The payload store writes each payload under its SHA-256.
3. Write the hash chain and the manifest.
4. Write `recorder.step(type, ...)`, which opens a span and writes the event. When the step fails, it writes `step_failed` with the name of the step and the error.
5. Write the commit of the bundle through provcat.
6. Write the strict and lenient modes.

**Tests:**
- A run with three steps writes a chain that verifies.
- A changed or removed event fails the verification.
- A sink that fails stops the run in strict mode and does not stop it in lenient mode.
- The bundle commits to a versioned bucket and reads back at the same version. The tests use a moto S3 bucket. A `file` store has no commits, so there the bundle stays unpinned.

### Phase 2: resolver, broker and snapshots

1. Write `Grant` and `StubBroker`. The stub grants a URI only if the URI is in the allow list of the run. The environment segment of the URI must also match the environment of the run. The stub grants only the mode `mounted_snapshot`. A physical URI, such as `file://`, has no environment segment, so the stub grants one only in `dev`.
2. Write `ProvcatResolver`. It calls `pin` and then makes a snapshot of the pinned version.
3. Add snapshots to provcat as `Catalog.snapshot(uri)`, which returns a `Snapshot` with the pinned URI, the path and the SHA-256. A snapshot lives at `.provcat/snapshots/<kind>/<sha256>/<name>` in the cache, where the kind is file or folder. The SHA-256 is of the content. For a folder, it is the SHA-256 of a list of its files and their hashes. So two versions with the same kind, the same name and the same content share one snapshot. A snapshot is read-only. provcat reuses it only if its hash still matches, and replaces it otherwise. It is made with a copy-on-write clone where the disk has one, and with a copy elsewhere.

When this plan was written, the provcat cache kept one path for each store and key, whatever the version. It now keeps fixed versions in their own folders, but the path without a version still moves. A later fetch of a moved branch rewrites that path in place. A mount of that path is not safe, so mounts use snapshots only.

**Tests:**
- The stub denies a URI that is not in the allow list.
- The stub denies a URI from another environment.
- A snapshot does not change after the branch moves and the cache fetches the new version.
- Every grant and every snapshot writes its evidence event.

### Phase 3: specs, stager and backends

1. Write `AgentSpec` and `RunSpec` as dataclasses that convert to JSON and back.
2. Write `InProcessStager`. It copies each snapshot into the input folder of the run, with a copy-on-write clone where the disk has one. The PydanticAI backend mounts that folder read-only at `/input`, and the output folder writable at `/output`. A clone shares no inode with the snapshot, so a change in one run cannot reach the snapshot that other runs use.
3. Write the `Backend` interface with `start`, `poll`, `stream`, `cancel` and `result`, all asynchronous.
4. Write `LiteLLMBackend` for one text call or one multimodal call.
5. Write `PydanticAIBackend`, with the filesystem toolset and `grep_file`.
6. Write `ToolRegistry`, which loads tools from Python entry points and from a tools folder. Each module in a tools folder names its tools in a list called `TOOLS`.
7. Write `Outputter`. It hashes each file in `/output`. `RunContext` commits the folder through provcat to `run://.../<run_id>/output`.
8. Write the PydanticAI importer. It puts the whole message history into one `trace_imported` event with confidence `parsed`. The wrappers have already recorded each model call and tool call.

**Tests:**
- Use the PydanticAI `TestModel` and `FunctionModel`, and a LiteLLM mock, so that the tests use no network and no keys.
- An agent that reads `/input/<name>` and writes `/output/answer.txt` gives a bundle with every event from the list in section "Classes".
- An agent cannot write to `/input`.

### Phase 4: the provr command

1. Write `provr run <spec.yaml>`, which runs one agent spec and prints the run ID.
2. Write `provr show <run_id>`, which prints the events of a run.
3. Write `provr verify <run_id>`, which checks the hash chain and the hashes of the payloads and the outputs.
4. Write `docs/provrun.md`, with one example that reads a `file` URI and one that reads a lakeFS URI.

**Done when:** the two examples run from a clean checkout, and `provr verify` passes on both bundles.

## What is copied from the earlier runtime

| Earlier runtime source | provrun destination | Change |
|---|---|---|
| `interfaces.py`: `ContentRetriever`, `Executor`, `Outputter`, `OutputResult` | `Stager`, `Backend`, `Outputter`, `RunResult` | Remove `experiment_path`, `file_key` and `question_key`. Add `RunContext`. Make the methods asynchronous. |
| `ContainerSpec` in the agent execution spec | `RunSpec` | Inputs are grants and snapshots, not host paths. |
| `executors.py`: `_LiteLLMBase`, `StandardExecutor`, `MultimodalExecutor`, `build_content_blocks` | `LiteLLMBackend` | Use `acompletion`. Write evidence for each call. Keep the version limit `litellm<=1.82.6` from the earlier runtime, which avoids a compromised release. |
| `executors.py`: `PydanticAIExecutor` | `PydanticAIBackend` | Mounts come from `RunSpec`, not from `incoming/`. Remove the answers folder and the `{file_key}` substitution. |
| `agent_tools.py`: `grep_file` and its deps class | `provrun.tools.grep_file` and `RunDeps` | `RunDeps` holds the sandbox and the `RunContext`. |
| `registry.py` | `load_backend` in `provrun.backends`, and `ToolRegistry` in `provrun.tools` | Keep the entry points. Rename the groups to `provrun.backends`, `provrun.stagers` and `provrun.tools`. |
| Workspace protocol in the agent execution spec, section 2.8 | `RunSpec` folder layout | Keep `input/`, `output/` and `scratch/`. |

These parts of the earlier runtime are not copied:

- the judge executors
- the code that finds a file in `incoming/` from a file key
- the question dependencies
- MLflow, Elo and the scores

## Checks before each commit

1. Run the unit tests of both packages.
2. Ask an adversarial reviewer to break the change. The reviewer is a subagent on the strongest model.
3. Fix each finding, and add a test for each finding.

## Out of scope for this plan

- Docker, Foundry and Azure Container Apps backends.
- Importers for Foundry, Application Insights, Claude Code and Codex traces.
- Profiles, a real broker and identity.
- An evidence server, Blob storage with immutability policies and redaction policies. The recorder has a redaction hook that does nothing yet.
- Durable runs that live through a restart of the process.

## Open questions

- **The org, domain and environment of `run://` URIs.** Today the `run` section of the run file gives them to `RunContext`. A profile will give them later.

## Questions that the build settled

- **The place of snapshots.** Snapshots live in provcat, because they are a cache feature.
- **The format of a tool folder.** Each module lists its tools in `TOOLS`. provrun does not load every public function, as the earlier runtime does.
