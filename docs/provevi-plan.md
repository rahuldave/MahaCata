# The plan for provevi

This plan builds provevi, the evidence store, in six phases. [The design](provevi-design.md) says what provevi is and why. Each phase ends with tests that pass, docs that match the code, and a site that builds.

## Terms

- **Evidence event:** one line that provrun writes for each step of a run, chained to the event before it by a hash.
- **Store:** where provevi keeps the events, payloads, runs and experiments. A driver gives each kind of store.
- **Sink:** an object that provrun's recorder hands each event line to. A store gives provrun a sink.
- **Seal:** the end of a run's evidence. The store writes the manifest and records the last hash.
- **Copy in MLflow:** the experiments, runs and traces that provevi writes into MLflow's tables, for MLflow's viewer.
- **Autolog:** MLflow's feature that patches a library, such as PydanticAI, so that each call writes a trace into MLflow.
- **Callback list:** a list in the `litellm` module of loggers that LiteLLM calls after each model call.
- **Tracer provider:** the OpenTelemetry object that makes spans and hands each finished span to its span processors.
- **Span processor:** an object that a tracer provider hands each span to, for example to send it somewhere.
- **Audit hook:** a function that Python calls before events such as a network connection, set with `sys.addaudithook`.
- **Logfire:** a service and a library from the makers of PydanticAI that collect OpenTelemetry spans.
- **Evidence modes:** provrun has two, strict and lenient. In strict mode, a problem fails the run. In lenient mode, the problem gives a warning.

## How the packages depend on each other

provrun depends on provevi, and provevi depends on provcat. provevi never imports provrun.

| Package | Owns |
|---|---|
| provcat | Reading and writing catalog URIs, with hooks at each lifecycle spot. |
| provrun | Running an agent, granting and staging its inputs, and deciding what each event means. The recorder that builds the hash chain stays here. |
| provevi | The format of an event line, the stores, the checks, the MLflow copy, and the span receiver. |

## Phase 1: the package, and the `file` store

**Status: done.** [provevi's page](provevi.md) describes what it built. Reading a run back through the store, the part of step 3 that Phase 1 left, came in Phase 2.

Move what stores and checks evidence out of provrun, into a new package at `packages/provevi`.

1. Make the package, with its command `prove`. Add it to the workspace and to the site's reference pages.
2. Move the event format into provevi, which is `EvidenceEvent`, the canonical JSON line and `sha256_bytes`. provrun's recorder imports them.
3. Write the store interface, `EvidenceStore`. It opens a run, gives a sink, stores a payload by hash, seals the run, and reads the events and payloads back.
4. Write the `file` store from provrun's `JsonlSink` and `PayloadStore`. It writes the same files that provrun writes today, so old runs still read and verify.
5. Move the checks `read_events`, `verify` and `verify_against` into provevi. The checks of outputs and of the committed bundle also move, from `provrun/cli.py`. They become library functions that return problems, and print nothing.
6. Give provrun's `run_agent` and `run_file` a `store=` argument. The default is the `file` store under the runs root, so nothing changes for a caller that passes none.
7. Make `provr verify` a thin call into provevi. Write `prove runs`, `prove show` and `prove verify`.

**Tests:** every evidence test of provrun passes, moved or unchanged. A run written before the move verifies with `prove verify`. Each library function works with no command line.

**Done when:** provrun has no storage or check code of its own, and `provr verify` and `prove verify` give the same answer.

## Phase 2: the `sqlite` store

**Status: done.** [provevi's page](provevi.md) describes the store, and Step 9 of [the provrun tour](provrun-tour.md) uses it.

Write provevi's own tables, as [the design](provevi-design.md#the-run-record) gives them. They hold experiments, runs, run settings, events, the URIs in events, payloads and assessments. Grants, refusals, inputs and outputs are views over the events.

1. Write the tables, with a schema version in the file and a migration step.
2. Write the `sqlite` store. Its sink writes each event as it happens. It writes the run row at the start and the last hash and manifest hash at the seal.
3. Keep the payloads in a folder, each named by its hash, as the `file` store does. Record each one in the payloads table.
4. Write the bundle files at the seal, so that provrun commits the same bundle through provcat as before.
5. Let a store read a run back, with its events, payloads and manifest, and run the checks through it. Phase 1 left this part of its step 3 for this phase.
6. Add queries to the library and to `prove`. They find the runs that read a URI, the runs of an experiment, and the grants and refusals of a run.
7. Choose the store from a URI, such as `sqlite:///path/evidence.db`, given to `run_file`, to `provr run --store`, or in the run file.

**Tests:** a run in the `sqlite` store verifies, and its bundle equals the bundle of the same run in the `file` store. A changed or removed row fails `verify`. Two runs at once write one file without losing an event.

**Done when:** a query across runs, such as "which runs read this URI", works from the library and from `prove`.

## Phase 3: control of what libraries log

In a controlled environment, the evidence must be the only record of a run, unless the operator approves another. The libraries that provrun loads can record or send data by themselves. [MLflow autolog](provevi-research/mlflow-autolog.md) and [Library telemetry](provevi-research/library-telemetry.md) list each path, and each was tested. Examples:

- `import litellm` fetches a price table from GitHub, and loads a `.env` file from a folder above its install folder.
- MLflow's LiteLLM autolog stays in LiteLLM's async callback lists after `autolog(disable=True)`.
- MLflow's PydanticAI autolog sets `instrument=True` on each new agent, so PydanticAI sends its own spans, prompts included.
- `logfire.configure()` takes the global tracer, meter and logger providers, and exports to `OTEL_EXPORTER_OTLP_ENDPOINT` even with `send_to_logfire=False`.

The code goes into provrun, in a new module, because it acts on the process that runs the agent. provevi checks the policy and records what the module blocked.

1. **Write the telemetry policy.** The operator owns a policy file. It lists the approved libraries, with the versions that were tested and the telemetry that each may use. It lists the approved destinations, such as Logfire, an OpenTelemetry endpoint or the copy in MLflow. For each destination, it says what the destination may receive. The levels are names and times, also token counts, or also bodies. It lists the network hosts that a run may reach. A run file's `telemetry` section can narrow the policy, but never widen it.
2. **Set the environment before any import.** Set `LITELLM_LOCAL_MODEL_COST_MAP=True`, `LITELLM_LOCAL_ANTHROPIC_BETA_HEADERS=True`, `LITELLM_MODE=PRODUCTION`, `PYDANTIC_AI_NO_BANNER=1`, `MLFLOW_USE_DEFAULT_TRACER_PROVIDER=true`, `MLFLOW_DISABLE_TELEMETRY=true` and `MLFLOW_TRACE_SAMPLING_RATIO=0`. Remove each `OTEL_*`, `LOGFIRE_*` and debug variable that the policy does not approve. Do not use `OTEL_SDK_DISABLED`, because it also stops provrun's own spans.
3. **Own the providers.** provrun sets the global tracer, meter and logger providers first. Its tracer provider refuses a span processor that it did not add, and records the attempt.
4. **Turn MLflow off, if it is loaded.** Do this step only when `mlflow` is in `sys.modules`, because the calls import MLflow. Call `mlflow.autolog(disable=True)` and each library's `autolog(disable=True)`, because the general call does not undo a library's call. Then call `mlflow.tracing.disable()`. If MLflow was imported before step 2, also call `set_telemetry_client()` so that it reads the new variables.
5. **Clear LiteLLM's callbacks.** Clear all seven callback lists, and keep only the loggers that the policy approves. A `callbacks=` argument on one call stays in the global lists, so compare the lists with a snapshot after each call.
6. **Turn PydanticAI's instrumentation off.** Call `Agent.instrument_all(False)`, and set `agent.instrument = False` on each agent. These calls do not stop an `Instrumentation` capability that an agent was given, or a model wrapped in `InstrumentedModel`. So remove each such capability, and unwrap each such model. An approved instrumentation uses provrun's providers.
7. **Quiet the loggers.** Set the loggers of the libraries to `WARNING`, because some log request bodies at `DEBUG`.
8. **Guard the network.** A Python audit hook allows connections only to the hosts in the policy, such as the model provider and the catalog's backends.
9. **Keep approved spans as evidence.** A span processor on provrun's tracer provider takes the spans of approved instrumentation. It moves each body, such as a prompt in an attribute, into a payload named by its hash. Then it writes the span into the run's evidence. Then it sends each destination what the policy lets it receive. The span receiver of Phase 6 does the same for spans that come from another process.
10. **Check during the run.** A check of about 5 microseconds after each model call finds a callback, a patch or a tracer that came back. A full scan for patched methods, about 650 milliseconds, runs at the start and the end of a run. In strict mode, a finding fails the run. In lenient mode, it writes an evidence event and a warning.

**Tests:** each path in the two research notes has a test that the path is blocked or approved as the policy says. The tests run offline, with an audit hook that refuses every host except loopback.

**Done when:** a real PydanticAI run and a real LiteLLM run reach only the approved hosts. MLflow, Logfire and an OpenTelemetry exporter are installed for the test, with their variables set. Their evidence records each attempt that the policy blocked.

**Open:**

- The SDKs of OpenAI and Anthropic send `X-Stainless-*` headers, with the operating system, the processor, and the Python and SDK versions, to the model provider. The research did not settle whether `default_headers` can remove them.
- Streamed calls, the OpenAI Agents SDK and the Claude Agent SDK are not tested yet.

## Phase 4: the copy in MLflow

Write each experiment, run and trace into MLflow's tables in the same SQLite file, as an optional extra, `provevi[mlflow]`.

1. Find or make the MLflow experiment by its unique name, such as `acme/demo/dev/revenue-agent`, and keep MLflow's number in the experiments table.
2. Write MLflow's run row with provrun's run ID, using MLflow's own row model, and resume it with `start_run(run_id=...)`. Write the settings, tags, metrics and inputs through MLflow's API. An input's digest is the first 32 characters of its SHA-256, and its source holds the whole URI.
3. Write an OpenTelemetry span exporter that writes each span of the run into MLflow, linked to the run.
4. Copy assessments on a trace or a span into MLflow.
5. Set `MLFLOW_DISABLE_AGENT_HINT=1`, flush before reading back, and fail loudly on a tracking URI that the installed MLflow cannot write.
6. Write `prove ui`, which opens MLflow's viewer on the store.

**Tests:** a run shows in MLflow under provrun's run ID, with its settings, inputs, traces and assessments. The copy can be deleted and rebuilt from provevi's tables. A test runs against the pinned MLflow minor release. provevi uses SQLite's WAL mode only, so a test checks that MLflow's writes leave the shared file in WAL mode.

**Done when:** a real PydanticAI run and a real LiteLLM run show in MLflow's viewer, and verify with `prove verify`.

## Phase 5: experiments in the catalog

1. Store an experiment as a catalog object, at `experiment://<org>/<domain>/<env>/<name>`, holding its agent spec and its description.
2. Let a run file name its experiment by URI. provrun pins it, and the run records the pinned version.
3. Write `prove experiments` and `prove experiment show`.

**Tests:** two runs of one experiment version name the same pin. A run after the experiment changes names the new pin.

**Done when:** an experiment has versions, and each run names the version it ran.

## Phase 6: Claude Code and Codex

1. Write the span receiver as a context manager. It listens on `localhost`, at a free port, for the length of a run.
2. Let provrun start Claude Code or Codex in a run. It sets their hooks, their OTLP address and `TRACEPARENT`, so that everything they send lands in that run.
3. Turn each hook event into an evidence event. At the end of the session, hash the new transcript or rollout lines into the chain as raw bytes.
4. Keep Claude Code's full model requests and responses when the run asks for them, as payloads.
5. Record what each agent cannot give, such as Codex's encrypted reasoning, as a known gap in the evidence.

**Done when:** a Claude Code run and a Codex run each give a sealed, verified run with their spans in the store.

## A separate tool: the trace database

[The trace database](trace-database.md) holds the steps of runs as rows that SQL can join, for analysis and for reinforcement learning. It is a separate package and command, built from sealed runs. It is not part of a run, and it is not a phase of this plan.

## Checks before each commit

1. Run the tests of all three packages.
2. Ask an adversarial reviewer, a subagent on the strongest model, to break the change. Fix each finding, with a test for each.
3. Update the docs in the same commit, and check that the site builds, as `CLAUDE.md` says.

## Out of scope for this plan

- Redaction rules for the evidence. The first build stores content as it is, but only in payloads named by hash, never in span attributes. The levels of Phase 3 step 1 are not redaction. They choose what a destination outside the evidence receives.
- A long-lived `prove serve` daemon. The receiver is written so that it can run as one later.
- Stores other than `file` and `sqlite`, such as a shared database server.
