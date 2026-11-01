# The design of provevi

provevi is the evidence store of MahaCata, beside provcat and provrun. Its command is `prove`. This page says what provevi is, the driver interface it offers, the tables it keeps, and how each agent and library is wired to it. [The plan](provevi-plan.md) says how it is built. Phases 1 and 2 are built, and [provevi's page](provevi.md) describes them.

The design draws on four research notes, written on 2026-10-31:

- [How the earlier runtime stored traces](provevi-research/earlier-runtime-traces.md), in MLflow's SQLite store.
- [MLflow tracing](provevi-research/mlflow-tracing.md), on MLflow 3.17.2's trace model, its storage, and its OpenTelemetry support.
- [Claude Code and Codex](provevi-research/agent-hooks-claude-codex.md), on their hooks, OpenTelemetry and session files.
- [PydanticAI and LiteLLM](provevi-research/agent-hooks-pydanticai-litellm.md), on the hooks and spans of PydanticAI, LiteLLM, the Anthropic and OpenAI SDKs, and the OpenAI Agents SDK.

## Terms

- **Evidence event:** one line that provrun writes for each step of a run. It holds the run ID, its position, its type and the pinned URIs it is about. It also holds small facts, a confidence, and the SHA-256 of a large payload and of the event before it. provrun writes these today, in `provrun/evidence.py`.
- **Hash chain:** each event holds the hash of the event before it, so a changed or removed event shows.
- **Span:** one timed piece of work in OpenTelemetry, with a name, a start, an end, attributes and a parent span. A trace is a tree of spans with one trace ID.
- **OTLP:** the OpenTelemetry protocol, which sends spans to a receiver over HTTP or gRPC.
- **GenAI conventions:** OpenTelemetry's names for model work, such as the span names `chat`, `execute_tool` and `invoke_agent`, and attributes such as `gen_ai.usage.input_tokens`.
- **Driver:** one back end behind provevi's interface, chosen by the scheme of a URI, as provcat chooses a store.
- **Hook:** a point where an agent or a library calls our code before or after a step. A hook that **blocks** can stop the step. A hook that **observes** can only report it.

## What provevi is

provevi takes in two kinds of record and stores them through a driver:

1. **Evidence events and their payloads**, which provrun writes. Events are the record of truth. They are hash-chained, and the bundle is committed through provcat.
2. **Spans**, from provrun itself, from libraries such as PydanticAI, and from agent programs such as Claude Code and Codex. Spans are an index and a timeline over the evidence. They arrive as OTLP, or in process from an OpenTelemetry tracer provider.

Each evidence event already holds the trace ID and span ID of its step (`otel.ids` in provrun). Each span should hold the run ID, as the attribute `provrun.run_id`. With both links, a reader can go from an event to its span and back.

The trust of a record is kept in the event's `confidence` field:

- **observed:** written by provrun's own code or hook at the moment of the step.
- **parsed:** read from something another program wrote, such as a library span, a Claude Code transcript or a Codex rollout file.
- **verified:** checked against a second source.

## The driver interface

A sketch, in the style of provcat's stores:

```python
class EvidenceStore(ABC):
    def open_run(self, run: RunRecord) -> Sink: ...                     # the sink provrun's recorder writes to
    def put_payload(self, data: bytes) -> str: ...                        # returns the SHA-256
    def get_payload(self, digest: str) -> bytes: ...
    def add_spans(self, spans: Iterable[ReadableSpan | OtlpSpan]) -> None: ...
    def seal(self, run_id: str, manifest: dict) -> None: ...
    def events(self, run_id: str) -> Iterator[EvidenceEvent]: ...
    def trace(self, trace_id: str) -> list[Span]: ...
    def assess(self, target: str, name: str, value, rationale: str, source: str) -> None: ...
```

Each driver also gives an OpenTelemetry `SpanExporter`, so that a tracer provider can send spans to it in process. A small OTLP receiver in front of any driver lets agent programs send spans over HTTP.

Candidate drivers, by URI scheme:

| Scheme | What it stores | State |
|---|---|---|
| `file` | JSONL events and `payloads/<sha256>` in a folder. provrun commits the folder through provcat when the run sets a bundle scheme. | Built in Phase 1. |
| `sqlite` | provevi's own tables (see "The run record"). With the optional extra `provevi[mlflow]`, also a copy of each experiment, run and trace in MLflow's tables in the same file. | Its tables are built in Phase 2. The copy in MLflow comes in Phase 3. |
| `mlflow+http` | The MLflow copy, through a running `mlflow server`. MLflow's Claude Code and Codex plugins, its TypeScript SDK and the small `mlflow-tracing` package need one. | Not in the plan yet. |
| `otlp` | Sends spans on to any OpenTelemetry collector. Stores nothing. | Not in the plan yet. |

## MLflow

**Writing straight to SQLite is not going away.** The research corrects this point. MLflow 3.17.2 (2026-10-20) writes traces into a `sqlite:///` file with no server, through the full `mlflow` package. SQLite has been the default store since 3.7.0. What MLflow retired is the file store, `./mlruns`. Since 3.13.0 it fails unless `MLFLOW_ALLOW_FILE_STORE=true` is set.

These clients cannot write to SQLite, and need a server:

- the small `mlflow-tracing` package, which drops every span with no warning when given a `sqlite:` URI;
- the TypeScript SDK;
- MLflow's Claude Code and Codex plugins.

Reasons for MLflow first:

- An MLflow span is an OpenTelemetry span with extra `mlflow.*` attributes.
- `Span.from_otel_proto` and the store's `log_spans` write OTel spans into SQLite with no server. The research ran this.
- MLflow maps GenAI convention attributes, and the span names of Claude Code and Codex, to its own.
- It gives a viewer, search, sessions, cost totals and assessments (MLflow's judgements on a trace or span) for nothing.

Rules for the MLflow driver:

1. **Never read or write MLflow's tables directly, with one exception.** Use its client API or OTLP. The exception is the one `runs` row that gives a run provrun's ID, in the section "IDs" below. There were ten trace-related migrations in about 18 months.
2. **Pin one minor version**, and test the driver against each new minor release.
3. **Load `mlflow` only inside the driver**, as an optional extra. With its dependencies it takes about 509 MB.
4. **Flush before reading back**, with `mlflow.flush_trace_async_logging()`. Trace writes are asynchronous.
5. **Fail loudly on a URI the client cannot write.** This guards against the silent drop in `mlflow-tracing`.
6. **Keep provevi's IDs in attributes.** MLflow rewrites trace IDs as `tr-<hex>`.
7. **Read spans back through `get_trace` in tests**, not through columns. In 3.17.2 the `spans.type` column stays empty for spans that arrive with GenAI attributes.
8. **Silence MLflow's note to coding agents.** On import, MLflow 3.17.2 logs a note that tells coding agents to load a skill it ships. The driver sets `MLFLOW_DISABLE_AGENT_HINT=1`, as the note says, so the note stays out of run logs.

The research note on MLflow recommends provevi's own tables first, so that provevi does not depend on MLflow's rate of change. The section "The run record" does both. provevi's own tables are the record, and MLflow holds a copy for its viewer and its traces.

## Lessons from the earlier runtime

The earlier runtime turned on `mlflow.litellm.autolog()` and `mlflow.pydantic_ai.autolog()`, and let MLflow write a SQLite file. Its weak points become rules for provevi:

- **Make each tool call and model turn its own span.** There, a PydanticAI run gave only two spans, and tool calls were messages inside the output.
- **Pass trace IDs explicitly.** It linked a run to "the last trace in the process", and 81 runs had more than one trace.
- **Keep large content out of spans.** 598 MB of its 604 MB database was span content. Store payloads once, by hash. Put the hash in the GenAI `*_ref` attributes, which the conventions define for content kept elsewhere.
- **Keep token counts in one place**, `gen_ai.usage.*` on the model span. It kept them in four places.
- **Do not swallow errors.** Evidence errors stop the run in strict mode, as provrun does now.
- **Check that nothing changed.** It had no check. provrun's hash chain is that check.

## Wiring each agent and library

provrun's own capture, a PydanticAI capability or a wrapper around a call, stays the record of truth for each library, with confidence `observed`. Library spans and agent files are a second source, with confidence `parsed`. Set `provrun.run_id` in OpenTelemetry baggage at the start of a run, and copy it onto every span with `BaggageSpanProcessor`.

| Substrate | Gate (blocks) | Record of model calls | Spans | Gaps to record |
|---|---|---|---|---|
| PydanticAI 2.64 | provrun's capability, `evidence_capability`. A gate would use `before_tool_execute`. | `wrap_model_request`, streamed or not | Built-in `invoke_agent`, `chat`, `execute_tool` | None known. |
| LiteLLM 1.83.0 | provrun's wrapper around `acompletion` | The same wrapper | `otel` callback, with `USE_OTEL_LITELLM_REQUEST_SPAN=true` | Its success callbacks can be dropped. |
| Anthropic SDK 2.3 | Client middleware | A provrun step around the call | Built in | None known. |
| OpenAI SDK | A custom HTTP transport | A provrun step around the call | The OTel instrumentation package | None known. |
| OpenAI Agents SDK | Tool guardrails and `RunHooks` | A provevi `TracingProcessor` | From the processor | Set it so no trace goes to OpenAI. |
| Claude Code | A synchronous `PreToolUse` hook | `OTEL_LOG_RAW_API_BODIES=file:<dir>`, one file per request and response | Built-in spans (beta), joined with `TRACEPARENT` | Thinking text is removed from the bodies. |
| Codex | Hooks set by an admin in `requirements.toml` | The rollout file | Built-in OTel traces | No request bodies; reasoning is encrypted; tool output is cut at 64 KB; hosted tools have no hooks. |

For Claude Code and Codex, each hook event becomes one evidence event. When the session ends, provevi hashes the new transcript or rollout lines into the chain. It keeps them as raw bytes, because their formats are not documented. Claude Code deletes transcripts after 30 days unless `cleanupPeriodDays` is raised.

## Problems the research found in provrun, now fixed

The research found five problems in provrun, and all five are fixed:

- **Streamed PydanticAI model calls wrote no evidence,** because the old model wrapper overrode only `request`. PR #11 replaced the wrappers with a capability, `evidence_capability`, which records each request, streamed or not.
- **Some PydanticAI tools wrote no evidence.** They were tools added by other capabilities, tools the provider runs, and output tools. The capability now records them. In a streamed run, an output tool's `tool_called` comes before the `model_called` that asked for it.
- **LiteLLM's `otel` callback could write onto provrun's span.** provrun now sets `USE_OTEL_LITELLM_REQUEST_SPAN=true` before each call.
- **`otel.py` claimed a schema URL for conventions 1.37.0,** although the GenAI conventions moved to their own repository with no release yet. It now claims none.
- **The LiteLLM pin held back every fix since March 2026.** Commit 227fed4 moved provrun to `litellm==1.83.0` and PydanticAI 2.64.0.

## Where provevi sits: inside a provrun run

Every write to provevi happens inside the flow of a provrun program. Nothing else writes evidence.

The path of a record is this:

1. **A catalog operation** inside a run, such as a fetch or an open, passes through provcat's lifecycle spots. A recorder hook of provrun's will see each one. Step 6 of [the access notes](access-notes.md) plans this hook, and it is not built yet.
2. **A model call or a tool call** reaches provrun through the hook of its library or agent. That hook is a PydanticAI capability, provrun's wrapper around LiteLLM, or a Claude Code or Codex hook that the run sets up.
3. **provrun's recorder** turns each one into an evidence event, and opens its span.
4. **The recorder hands the event, its payload and its span to provevi**, which stores them through its driver.

For an agent program such as Claude Code, provrun starts the program. It also sets the program's hooks, its OTLP address and `TRACEPARENT`, so that everything the program sends lands in that run.

## Decisions

The decisions, agreed on 2026-10-31, are these:

1. **The `file` driver replaces provrun's `JsonlSink`.** All events go through provevi. provrun owns what an event means, and provevi owns how it is stored.
2. **Verification moves into provevi.** `provr verify` stays as a thin call into the provevi library.
3. **The OTLP receiver is provevi's code, and a run owns its lifetime.** provrun starts a provevi receiver at the start of a run, on `localhost` at a free port, and stops it when the run is sealed.

4. **provevi's own tables are the record of runs and experiments,** and MLflow holds a copy. The section "The run record" gives the tables.
5. **MLflow uses provrun's run IDs.** provevi writes MLflow's `runs` row with provrun's ID, and finds an MLflow experiment by its unique name.
6. **The command is `prove`.**
7. **Claude Code's full model requests and responses are kept when a run asks for them.** They are off by default, and are stored once each by hash, as payloads.
8. **The first build stores content as it is, with no redaction,** but only in payloads named by hash, never in span attributes. Redaction rules come later, behind provrun's redaction hook.
9. **An experiment is a catalog object,** with an `experiment://` URI, as a sealed run is committed under its bundle scheme. So a run names the exact version of its experiment.
10. **The first build covers PydanticAI and LiteLLM.** Claude Code and Codex come second, with the span receiver, their hooks and their session files.

For decision 3:

- A span belongs to a run because it reached that run's receiver, so no run ID written by the agent program needs to be trusted.
- No span can arrive after the hash chain is sealed.
- No service has to run before a run starts.
- The receiver is written so that it can later run as a long-lived daemon, `prove serve`, with the same code. That waits until runs move to containers or other machines, or until several runs at once contend for one SQLite file. Even then, provrun opens and seals each run with the daemon, and the daemon refuses spans for a run that is not open.

## The run record

MLflow holds traces well, but most of a run is not a trace. MLflow has no natural place for the creation of a run, its grants and refusals, or its snapshots. Nor does it have one for the staged and checked inputs, the outputs or the sealed bundle. So provevi has its own tables, and they are the record. MLflow gets a copy of each experiment and run, which its viewer shows and its traces link to.

### provevi's tables

| Table | What it holds |
|---|---|
| `provevi_experiments` | ID, logical URI, organisation, domain, environment, name, description, the agent spec and its hash, created by, created at, and MLflow's experiment number. |
| `provevi_runs` | provrun's run ID, its experiment, actor, status, start and end, evidence mode, parent run, the hash of the last event, the manifest hash, sealed at and the pinned bundle URI. |
| `provevi_run_params` | The settings of a run, such as the backend, model, tools and limits. Never secrets. |
| `provevi_events` | Every evidence event, with its hash-chain fields and its trace and span IDs. |
| `provevi_event_refs` | One row per URI in an event, so that a search by URI is fast. |
| `provevi_payloads` | The SHA-256, size and location of each payload. |
| `provevi_assessments` | Judgements on an experiment, a run, an event or a span. |

Grants, refusals, inputs and outputs are views over `provevi_events`, so the record has one copy. An experiment can also be a catalog object, with an `experiment://` URI, as a sealed run is committed under its bundle scheme, such as `run://`.

### The copy in MLflow

| MLflow | What provevi writes there |
|---|---|
| `experiments` | One per provevi experiment. Its name is the experiment's logical name, for example `acme/demo/dev/revenue-agent`. |
| `runs` | One per run, with provrun's own run ID. |
| `params`, `tags`, `metrics` | The settings, the organisation, domain and environment, and the token counts, costs and call counts. |
| `datasets`, `inputs` | Each staged input. The source is the pinned URI with its full SHA-256, and the digest is the first 32 characters of the SHA-256. |
| traces and spans | The spans of the run. Each trace names the run in `mlflow.sourceRun`. |
| `assessments` | Judgements on a trace or a span. |

The copy can be rebuilt from provevi's tables, the bundle and the stored spans. Nothing in provevi's tables refers to MLflow's tables with a foreign key, so an MLflow upgrade cannot break them.

### IDs

MLflow's API cannot create a run with a given ID. Its store makes the ID itself (`run_id = uuid.uuid4().hex` in `create_run`), and `start_run(run_id=...)` only resumes a run that exists. Experiment numbers come from the database's counter.

So provevi does two things:

- **It writes the one `runs` row itself, with provrun's run ID,** using MLflow's own row model, `SqlRun`, as `create_run` does. Then `start_run(run_id=...)` resumes that run, and everything after goes through the public API. MLflow accepts run IDs that match `^[a-zA-Z0-9][\w\-]{0,255}$`, and provrun's IDs, such as `run-20261031T164032Z-451450d6`, match. They are 29 characters long, so they also fit the 32-character column on stricter databases.
- **It finds an experiment by its unique name,** with `get_experiment_by_name`, and keeps MLflow's number in `provevi_experiments`.

The `runs` row is the one place where provevi writes MLflow's tables without its API. The driver test must check it against each MLflow minor release.

### What a test showed

A test with MLflow 3.17.2 checked these:

- provevi's tables, made first in the same SQLite file, did not stop MLflow from making its own tables. A new client opened the file afterwards.
- A run row with provrun's ID, resumed with `start_run(run_id=...)`, took settings, metrics, an input, a trace and a judgement.
- `get_run`, `search_runs` by tag, `search_traces` by run and `get_trace` all returned provrun's ID. The trace's `mlflow.sourceRun` was provrun's ID.
- MLflow refused a 64-character SHA-256 as a dataset digest, with "'digest' exceeds the maximum length of 36 characters". A 32-character digest worked, with the full SHA-256 in the source URI.
- `mlflow server` on the file served the run and its input, and its web page loaded.

## Libraries first, with a command line on top

provcat, provrun and provevi are each a library first. Each command line program is a small orchestrator on top of its library, and holds no logic that the library lacks. So another program, such as a workflow orchestrator, can use the three packages directly.

For provevi this means:

- **The receiver is a context manager,** for example `with provevi.receiver(store, run) as endpoint:`. An orchestrator that already runs a collector can skip it and pass its own span exporter.
- **The store is an object the caller passes in,** never a global. Two stores can be open in one process.
- **Every command is a function first.** `runs`, `show`, `trace`, `verify` and `export` are library functions that return data, and the command prints them.

On 2026-10-31, provrun's checks of outputs and bundles were in its command line, so an orchestrator could not call them. Phase 1 moved them into the provevi library, as `verify_outputs`, `bundle_pin` and `verify_bundle`.
provcat's command line is already thin. It opens a `Catalog` and calls its methods. A program can also pass its own hooks to each `Catalog`, with `hooks=`, and provrun's library entry, `run_file`, does what `provr run` does.

## The command line

Writing belongs to provrun, so the provevi command, `prove`, only reads, checks and moves what is stored:

| Command | What it does |
|---|---|
| `runs` | Lists the runs in a store. |
| `show RUN` | Prints the events of a run, with their spans. |
| `trace TRACE_ID` | Prints a trace as a tree. |
| `verify RUN` | Checks the hash chain, the manifest, the outputs and the committed bundle. |
| `export RUN --to URI` | Copies a run to another driver, for example from `file` to `sqlite`. |
| `ui` | Opens a viewer, for example `mlflow ui` on the MLflow driver. |

The command does not take in an agent's files after the fact. That import runs at the end of a run, inside provrun.

## Open questions

None now.
