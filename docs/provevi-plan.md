# The plan for provevi

This plan builds provevi, the evidence store, in seven phases. [The design](provevi-design.md) says what provevi is and why. Each phase ends with tests that pass, docs that match the code, and a site that builds.

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
- **Audit hook:** a function that Python calls before events such as a network connection, set with `sys.addaudithook`. Phase 3 no longer uses one.
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

**Status: provevi's part is built, and so are steps 2 to 8 and step 10 in provrun.** [provevi's page](provevi.md) describes the policy, `prove policy` and `prove telemetry`. [provrun's page](provrun.md#control-of-library-telemetry) describes how a run finds the policy, records the variables and owns the providers. It also describes how a run turns off each library's own telemetry and checks during the run. 

Step 9 is not built. provevi's parts for it, the rule that matches a variable to a destination and the decision on `hosts`, are built. Its MLflow destination waits for Phase 4.

**A change of design, on 2026-11-02.** The telemetry variables are configuration, not a leak. `OTEL_EXPORTER_OTLP_ENDPOINT` and its headers name the org's collector. `MLFLOW_TRACKING_URI` names a tracking server, or a database such as Postgres. `LOGFIRE_TOKEN` names a Logfire project.

So a run keeps them, records them, and sends provrun's own spans where they point, if the policy approves. It no longer removes them. The libraries stay quiet because steps 4 to 7 turn their telemetry off, not because their settings are gone. Removing variables one family at a time would also mean chasing each new library's variables. Control of the network moves out of the process, as step 8 says.

1. **Write the telemetry policy.** The operator owns a policy file. It lists the approved libraries, with the versions that were tested and the telemetry that each may use. It lists the approved destinations, such as Logfire, an OpenTelemetry endpoint or the copy in MLflow. For each destination, it says what the destination may receive. The levels are names and times, also token counts, or also bodies. A run file's `telemetry` section can narrow the policy, but never widen it. provrun does not enforce the policy's `hosts` list after the change to step 8. The list stays as the check on each destination's endpoint, and as a record for the firewall.
2. **Set the quiet settings, and record the telemetry variables.** Before any import, set `LITELLM_LOCAL_MODEL_COST_MAP=True`, `LITELLM_LOCAL_ANTHROPIC_BETA_HEADERS=True`, `LITELLM_MODE=PRODUCTION`, `PYDANTIC_AI_NO_BANNER=1` and `MLFLOW_DISABLE_TELEMETRY=true`. These stop fetches and usage reports at import, and name no destination. Also set `MLFLOW_USE_DEFAULT_TRACER_PROVIDER=true`, which is MLflow's default. With `false`, MLflow adds its processor to the global tracer provider, and can replace that provider. Keep `LITELLM_LOG=ERROR`, because with no value LiteLLM's log handler takes every level. Keep the Logfire off switches `LOGFIRE_SEND_TO_LOGFIRE=false` and `LOGFIRE_CONSOLE=false`, which name no destination.

   Some of these change what the user set. `LITELLM_MODE=PRODUCTION` stops LiteLLM loading a `.env` file, and `LITELLM_LOCAL_MODEL_COST_MAP=True` ignores a custom price table. So `run_created` records the value each one had before the run.

   Leave every other variable as the user set it, credentials included. `MLFLOW_TRACE_SAMPLING_RATIO=0` is not needed, because step 4 turns MLflow's tracing off. `ANTHROPIC_LOG` and `OPENAI_LOG` stay too, because step 7 holds their loggers at `WARNING`.

   `run_created` records each variable of the `OTEL_*`, `LOGFIRE_*`, `MLFLOW_*` and `LITELLM_*` families that is set, under a key `variables` that replaces `removed`. It records the value only of a name that ends in `_ENDPOINT`, `_URI` or `_URL`, or of `OTEL_SERVICE_NAME` and `OTEL_EXPORTER_OTLP_PROTOCOL`. A URL is recorded only by its scheme, host and port. A token, a password or a local path can sit in the rest of a URL. Every other variable, such as a token, a header or a key, is recorded by name only. This replaced the first build of 2026-11-02, which removed each `OTEL_*` and `LOGFIRE_*` variable and set the OpenTelemetry exporters to `none`.
3. **Own the providers.** provrun sets the global tracer, meter and logger providers first. Its tracer provider refuses a span processor that it did not add, and records the attempt.
4. **Turn MLflow off, if it is loaded.** Do this step only when `mlflow` is in `sys.modules`, because the calls import MLflow. Call `mlflow.autolog(disable=True)` and each library's `autolog(disable=True)`, because the general call does not undo a library's call. Then call `mlflow.tracing.disable()`. If MLflow was imported before step 2, also call `set_telemetry_client()` so that it reads the new variables.
5. **Clear LiteLLM's callbacks.** Clear all nine callback lists. A `callbacks=` argument on one call stays in the global lists, so clear them after each call too. No logger is kept, even for a policy that approves LiteLLM. Each sends to its own service, past the policy's levels. `otel`, the one that writes to the global tracer provider, does not start without LiteLLM's proxy package. Approved spans of LiteLLM calls come from provrun's own spans, in step 9.
6. **Turn PydanticAI's instrumentation off.** Set `agent.instrument = False` on the agent that provrun builds, after it is built, because MLflow's autolog patches the constructor. The agent's value wins over `Agent.instrument_all`, so the class setting is left alone. provrun gives the agent no `Instrumentation` capability, and it unwraps a model passed in an `InstrumentedModel`. A model with one deeper inside, such as in a `FallbackModel`, fails the run. An approved instrumentation uses provrun's providers.
7. **Quiet the loggers.** Set the loggers of the libraries to `WARNING`, because some log request bodies at `DEBUG`.
8. **Find Logfire and an OpenTelemetry auto-configuration, and leave the network to the deployment.** With its variables kept, a `logfire.configure()` in the user's program sends spans to Logfire and to the `OTEL_EXPORTER_OTLP_*` endpoint, past the policy. So does an `opentelemetry-distro` configurator or an auto-instrumentation `sitecustomize`. provrun cannot turn these off once they run. So as each run starts, and in step 10's checks, it looks for a configured Logfire and a loaded configurator. Each one found is a problem. It fails a strict run, and a lenient run warns and records it.

   **The network.** provrun does not guard the network. A Python audit hook sees only Python's own sockets. Code in the same process can get around it, so it would not be a boundary.

   Control of where a run may connect belongs to the firewall or the container that runs it. The docs list the hosts a run needs. They are the model provider, the catalog's stores and the approved destinations.
9. **Keep provrun's spans as evidence, and send them where the policy approves.** A span processor on provrun's tracer provider takes provrun's spans, and those of an approved instrumentation. It moves each body, such as a prompt in an attribute, into a payload named by its hash. It writes the span into the run's evidence store. Then it sends each approved destination what the policy lets it receive. A destination's address and credential come from the variables the user set, and the policy must approve that destination. How a variable is matched to the policy's destinations is open, below. provrun sends OTLP itself, with the headers the user set. If the user set `OTEL_SDK_DISABLED=true` or `OTEL_TRACES_EXPORTER=none`, provrun sends nothing to the OTLP destination. The destinations are:
   - an OpenTelemetry collector or receiver, at the `OTEL_EXPORTER_OTLP_*` endpoint, with its headers;
   - an MLflow tracking server, or an MLflow database such as Postgres or SQLite, at `MLFLOW_TRACKING_URI`. This needs Phase 4, widened to a tracking server and to Postgres;
   - Logfire, with `LOGFIRE_TOKEN`.

   The span receiver of Phase 6 does the same for spans that come from another process. A run in process needs no receiver.
10. **Check during the run.** A check before and after each model call finds a callback, a configured Logfire, an auto-configuration or a replaced global provider. It takes some tens of microseconds. A full scan for patched methods runs at the start and the end of a run. It runs too when a run fails with an error. It takes from 200 to 350 milliseconds, by how many of these libraries are loaded. It looks in the modules of the libraries that make the model call, and skips the SDKs' `types` modules, which hold only data models. Each finding is written as a `telemetry_problem` event. In strict mode, it fails the run. In lenient mode, it gives a warning.

**Tests:** each path in the two research notes has a test that the path is turned off or approved as the policy says. The tests run offline. The `OTEL_*` and `LOGFIRE_*` endpoints and `MLFLOW_TRACKING_URI` point at local fake receivers and stores, and each test checks what arrives at each one. A test runs a program that calls `logfire.configure()`.

**Done when:** MLflow, Logfire and an OpenTelemetry exporter are installed, with their variables set. Then a real PydanticAI run and a real LiteLLM run write no telemetry of their own. Their evidence records each thing that was turned off, and the variables that were set. provrun's spans reach each approved destination, and no other. An MLflow tracking server and a Postgres store come with Phase 4, widened as above.

**Open:**

- **Decided on 2026-11-02: matching a destination to a variable.** `TelemetryPolicy.match_destination(kind, url)` matches by kind, then by endpoint, after `normal_endpoint` puts both in one form. An `mlflow` endpoint may be a database URL, such as `postgresql://` or `sqlite:///`. For traces, `otlp_traces_target(environ)` gives the protocol and the address, by the OpenTelemetry rules, and provrun makes the exporter of that protocol. `normal_endpoint` refuses a URL that two parsers could read as two places, and such a URL matches nothing. A URL with no match gets nothing, and provrun writes `telemetry_blocked` with the path `destination`. [provevi's page](provevi.md#check-a-telemetry-policy) gives the rule.
- **Open: variables that redirect a matched URL.** `HTTPS_PROXY`, `grpc_proxy`, `PGHOSTADDR` and `PGSERVICE` can send a matched URL to another place. provrun clears them for its exporters, or the firewall stops them; step 9 decides which.
- **Decided on 2026-11-02: the `hosts` list stays.** provevi checks each destination's endpoint against it, and it is a record for the firewall. It does not limit a run's connections.
- **Decided on 2026-11-02: `prove telemetry RUN` lists `telemetry_problem` events** as well as `telemetry_blocked` events.

- The SDKs of OpenAI and Anthropic send `X-Stainless-*` headers, with the operating system, the processor, and the Python and SDK versions, to the model provider. The research did not settle whether `default_headers` can remove them.
- Streamed calls, the OpenAI Agents SDK and the Claude Agent SDK are not tested yet.

## Phase 4: the copy in MLflow

Write each experiment, run and trace into MLflow, as an optional extra, `provevi[mlflow]`. The place is the `mlflow` destination of the telemetry policy that matches the user's `MLFLOW_TRACKING_URI`:

- **An MLflow tracking server**, at an http or https URL.
- **MLflow's tables in a database**, at a database URL. An enterprise uses Postgres, and a small org uses SQLite.
- **MLflow's tables in the store's own SQLite file**, for an `mlflow` destination with no endpoint.

provevi writes all three through MLflow's client, `MlflowClient(tracking_uri=...)`. MLflow sends the calls to a tracking server through its REST API, and writes a database through its SQLAlchemy store. So one code path serves each place.

1. Find or make the MLflow experiment by its unique name, such as `acme/demo/dev/revenue-agent`, and keep MLflow's number in the experiments table.
2. Make MLflow's run for provrun's run, and record MLflow's run ID beside provrun's run ID. A tracking server picks its own run IDs, so provevi cannot write a row with provrun's run ID through the REST API. Write the settings, tags, metrics and inputs through MLflow's API, with provrun's run ID as a tag. An input's digest is the first 32 characters of its SHA-256, and its source holds the whole URI.
3. Write an OpenTelemetry span exporter for MLflow. It writes each span of the run through MLflow's tracing client, linked to the run.
4. Copy assessments on a trace or a span into MLflow.
5. Set `MLFLOW_DISABLE_AGENT_HINT=1`, and flush before reading back. Fail loudly on a tracking URI that the installed MLflow cannot write, and on one that the policy does not approve.
6. Write `prove ui`, which opens MLflow's viewer on the store's own file.

**Tests:** a run shows in MLflow under its provrun run ID tag, with its settings, inputs, traces and assessments. Each of the three places has a test. The tests use a local MLflow tracking server, a Postgres database in a container, and the store's SQLite file. The copy can be deleted and rebuilt from provevi's tables. A test runs against the pinned MLflow minor release. provevi uses SQLite's WAL mode only, so a test checks that MLflow's writes leave the shared file in WAL mode.

**Done when:** a real PydanticAI run and a real LiteLLM run verify with `prove verify`. Both show in MLflow's viewer, from a tracking server and from the store's own file.

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

## Phase 7: the `postgres` store

An enterprise keeps its evidence in Postgres, not in SQLite. A small org keeps SQLite.

1. Write a `postgres` driver beside `file` and `sqlite`, with the same tables, the same queries and the same `EvidenceStore` interface. Its URI is `postgresql://host/database`, the scheme that Postgres and MLflow use. Its password comes from the environment or a password file, never from the URI in a run file.
2. Keep the guarantees of the `sqlite` store. Each run's events have one order, two runs at once lose no event, and a sealed run cannot change.
3. Keep the payloads as files or objects named by hash. The database holds only their hashes.
4. Let MLflow's tables of Phase 4 sit in the same database, under their own schema.

**Tests:** the tests of the `sqlite` store run against a Postgres in a container, and give the same answers. A run in the `postgres` store verifies, and its bundle equals the bundle of the same run in the `file` store.

**Done when:** provrun writes a run to Postgres, and `prove` lists, queries and verifies it.

## A separate tool: the trace database

[The trace database](trace-database.md) holds the steps of runs as rows that SQL can join, for analysis and for reinforcement learning. It is a separate package and command, built from sealed runs. It is not part of a run, and it is not a phase of this plan.

## Checks before each commit

1. Run the tests of all three packages.
2. Ask an adversarial reviewer, a subagent on the strongest model, to break the change. Fix each finding, with a test for each.
3. Update the docs in the same commit, and check that the site builds, as `CLAUDE.md` says.

## Out of scope for this plan

- Redaction rules for the evidence. The first build stores content as it is, but only in payloads named by hash, never in span attributes. The levels of Phase 3 step 1 are not redaction. They choose what a destination outside the evidence receives.
- A long-lived `prove serve` daemon. The receiver is written so that it can run as one later.
- Stores other than `file`, `sqlite` and `postgres`.
