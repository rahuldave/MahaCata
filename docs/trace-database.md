# Spec: the trace database

**Status:** draft. Nothing in this spec is built. The research behind it is in [MLflow autolog](provevi-research/mlflow-autolog.md) and [Library telemetry](provevi-research/library-telemetry.md).

## Terms

- **Evidence:** the events, payloads and manifest that provevi keeps for each run. The evidence is the record of truth.
- **Seq:** the number of an event in its run, from 0 upward.
- **Assessment:** a judgement on a run, a span or a call, such as a score or a reward, with its source and reason.
- **WAL mode:** SQLite's write-ahead log, in which readers and a writer can use the file at the same time.
- **GenAI names:** the OpenTelemetry semantic conventions for generative AI, the attribute names that start with `gen_ai.`.
- **Payload:** a large body, such as a model's request and reply, kept apart from its event and named by its SHA-256.
- **Span:** one timed step of a run, such as one model call or one tool call, in the OpenTelemetry sense.
- **Trace database:** a SQLite file, apart from the evidence, that holds the steps of runs as rows that SQL can join.
- **Build:** the process that reads the evidence and writes the trace database.

## Purpose

The trace database lets a person ask exact questions about what agents did, across many runs. Examples are "every call to tool X whose arguments held Y, and the step after it" and "the token use of each model call in experiment Z". It also gives the rows that a reinforcement learning job needs. Each row holds the state before a step, the action, and the result.

MLflow keeps each span as one JSON text, so a question about a message or a tool argument must parse every span. provevi's own tables keep each event as one line, with the bodies in payload files. Neither has a row for each message or each tool call. The trace database has one.

## What it is not

- **It is not part of a run.** provrun and provevi never write it. A run does not wait for it, and a run does not fail because of it.
- **It is not evidence.** It is a copy that the build makes from the evidence. A person can delete it and build it again, and the result is the same.
- **It holds no labels of its own.** A judgement on a step, such as a reward, goes into provevi's assessments. The build copies the assessments. A label kept only in the trace database would be lost at the next build.
- **It is not MLflow's viewer.** The copy in MLflow, Phase 4 of provevi's plan, stays a separate thing.

## The package and its command

A new package in the workspace, proposed name `provtrace`, with its own command. It depends on provevi, and provevi never imports it. The command reads a provevi store, and it opens that store read-only.

| Command | What it does |
|---|---|
| `build --store URI --db PATH` | Read each sealed run that the database does not hold yet, check it, and add its rows. |
| `build --store URI --db PATH --run ID` | Build one run again, and replace its rows. |
| `check --db PATH` | Compare each run in the database with the evidence it came from. |
| `export --db PATH --kind steps` | Write rows as JSON lines, for a training job. |
| `runs --db PATH` | List the runs in the database, with the build that added each one. |

## Rules for the build

1. **Read only sealed runs.** A run that is still open has no last hash yet, so the build skips it.
2. **Check each run before it adds the run.** The build runs provevi's `verify_run`, which also compares the run with its committed bundle. `verify` alone checks only that the run's files agree with each other, so it misses events and a manifest that were rewritten together. If a run does not verify, the build refuses it and names the problem. A flag, `--allow-unverified`, adds it anyway and marks it.
3. **Record the source of each run.** The database keeps the store URI, the run ID, the run's last hash and the build's version. `check` compares the last hash with the store's.
4. **Build each run in one transaction.** A run's rows are all there or none are.
5. **Use WAL mode only.** The database follows the same rule as provevi's store. The build refuses a file in another journal mode.
6. **Take bodies from the payload files.** The build reads each payload by its hash, and checks the hash before it parses the payload.
7. **Count each message once.** A model's request repeats the whole conversation. The build stores each message once, keyed by its hash, and links each call to the messages it sent.

## Tables

All tables start with `trace_`. The first version of the schema is version 1, and the build changes it only through numbered upgrade steps, as provevi does.

| Table | One row for each | Main columns |
|---|---|---|
| `trace_meta` | key | the schema version, the build's version |
| `trace_sources` | run | store URI, run ID, last hash, built at, verified or not |
| `trace_runs` | run | experiment, org, domain, env, actor, status, start, end, agent, model |
| `trace_spans` | step | run, seq, span ID, parent span ID, kind, name, start, end, duration, status, error |
| `trace_model_calls` | model call | span, model asked, model that answered, provider, input tokens, output tokens, finish reason, streamed |
| `trace_messages` | distinct message | hash, role, part kind, text, tool call ID |
| `trace_call_messages` | message in a call | model call, position, message hash, sent or received |
| `trace_tool_calls` | tool call | span, call ID, tool, arguments as JSON, result as JSON, error, deferred, run by the provider, output tool |
| `trace_access` | grant or refusal | run, seq, URI, mode, reason |
| `trace_assessments` | assessment | target run, span or call, name, value, rationale, source |

A part kind is one of text, tool call, tool return, thinking or file. A view, `trace_steps`, joins each model call with the messages it sent, the reply and the tool calls that followed. It gives the state, action and result rows for a training job.

## Names of attributes

The trace database uses MLflow's names where MLflow has one, and keeps the OpenTelemetry GenAI name next to it. A query written for MLflow's spans then moves over with few changes. The names come from MLflow 3.17.2. The span kinds are in `entities/span.py`, class `SpanType`, and the attribute keys are in `tracing/constant.py`.

| Value | Column or key in the trace database | GenAI name |
|---|---|---|
| Span kind | `kind`: `AGENT`, `CHAT_MODEL`, `LLM`, `TOOL`, `PARSER`, `UNKNOWN` | `gen_ai.operation.name` |
| Model | `model`, the model asked, and `answered_by` | `gen_ai.request.model`, `gen_ai.response.model` |
| Provider | `provider` | `gen_ai.provider.name` |
| Tokens | `input_tokens`, `output_tokens`, `total_tokens`, `cache_read_input_tokens`, `cache_creation_input_tokens` | `gen_ai.usage.*` |
| Message format | `message_format`: `pydantic_ai`, `openai` or `anthropic` | none |
| Tools offered | `tools`, in OpenAI's function form | `gen_ai.tool.definitions` |
| Session | `session_id` | `gen_ai.conversation.id` |

Each span also keeps an `attributes` column, in MLflow's expanded form, with `mlflow.spanType`, `mlflow.spanInputs`, `mlflow.spanOutputs` and `mlflow.chat.tokenUsage`. The build maps each event to a span as MLflow's autolog does:

- **A run** becomes one `AGENT` span at the root, with the prompt and the final output.
- **A `model_called` event** becomes one `LLM` span, as MLflow's PydanticAI and LiteLLM autolog make. For the Anthropic format, the cache tokens are counted in `input_tokens`, as MLflow counts them.
- **A `tool_called` event** becomes one `TOOL` span under the agent span, not under the model span.

## What the evidence must hold first

The build can only copy what the evidence holds. Each step writes one event when it ends. Today the evidence lacks these values:

- **Span IDs.** provrun asks OpenTelemetry for the trace ID, span ID and parent span ID of each step, and of each tool call that the provider runs. provrun sets no tracer provider yet, so the IDs are there only when the program that runs provrun sets one. Phase 3 of [provevi's plan](provevi-plan.md) makes provrun own a provider, and then the IDs are always there.
- **The provider of each model call.** No event holds it.
- **The details of a LiteLLM reply.** The payload keeps the reply as plain text. It has no model that answered, no tool calls and no cache tokens.
- **The full tool definitions and a session ID.** `sandbox_started` holds the tool names only, and no event holds a session ID.

provrun writes all of these, so each change needs the agent that owns provrun.

The start of each step is there now. Each step's event holds `started_at`, so the build can give a duration. Runs written before this field have none. A tool that the provider ran has no `started_at` either, because provrun did not see it run. So the build must choose a start for its span, for example zero length at the end of its `model_called`.

## Tests

- A build of the tour's runs gives the same rows twice in a row.
- A deleted database built again equals the first one.
- The build refuses a run whose events and manifest were rewritten together after the seal, because they no longer match the bundle.
- `check` reports a run whose source changed after the build.
- A query for the calls of one tool, with arguments and results, returns the right rows for a PydanticAI run and a LiteLLM run.
- A message sent in five calls is stored once.
- The build refuses a database not in WAL mode.

## Open questions

- **The name.** `provtrace` is a proposal.
- **Cost.** Should the build compute the cost of each model call from a price table, as MLflow does? The price table changes, so the cost would change between builds.
- **Traces from outside.** Claude Code and Codex send their own spans. Once provevi's span receiver takes them in, the build can copy them too, but their shape differs.
- **Redaction.** The bodies are as the evidence holds them. If a body must not reach the analysts, the redaction belongs in provrun, before the evidence is written.
