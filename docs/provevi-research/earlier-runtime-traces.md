# How the earlier runtime stored traces

The earlier runtime is the evaluation tool that came before provrun. It ran prompts and agents against documents, judged the answers, and recorded each model call as an MLflow trace. This note says how it stored the traces, what one trace held, and what provevi learns from it. It was written on 2026-10-31, from the runtime's source and its trace databases, opened read-only.

## Terms

- **Run:** an MLflow run, one row in MLflow's `runs` table. The runtime opened one run for each combination of question, document, setup and model, and one for each judge call.
- **Trace:** an MLflow trace, one row in `trace_info`. A trace holds one or more spans.
- **Span:** one timed operation, one row in `spans`. The `content` column holds the whole span as JSON in the OpenTelemetry shape.
- **Autolog:** an MLflow integration that patches a client library, so that each call to the library writes a trace with no code in the caller.
- **Assessment:** a judge's result attached to a trace, one row in `assessments`.

## How it wrote traces

- **MLflow 3.9, with a local SQLite file.** The runtime set the tracking URI to `sqlite:///<folder>/mlflow.db`. MLflow's own SQL store wrote every row. The runtime wrote no trace rows itself.
- **Autolog was the only trace writer.** The runtime turned on `mlflow.litellm.autolog()` and `mlflow.pydantic_ai.autolog()`. It had no `mlflow.start_span`, no `@mlflow.trace` and no span of its own.
- **One module held all MLflow code,** and every call did nothing when MLflow was off. The rest of the code had no MLflow import.
- **A run found its trace through a global.** After a model call, the runtime called `mlflow.get_last_active_trace_id()`, which returns the last trace in the process. It stored that ID, and the MLflow run ID, in its own database.
- **Judges wrote onto the trace.** Each judge wrote each criterion and a verdict as a feedback assessment on the trace of the judged run, with `mlflow.log_feedback`.
- **Readers needed no server.** The web page and the judges read traces through `MlflowClient` on the SQLite file, with `get_trace` and `search_traces`.
- **One repair script read MLflow's tables with SQL.** It found missing trace IDs through the key `mlflow.sourceRun` in `trace_request_metadata`.

## What the database held

The largest database held 358 traces and 392 spans, in 604 MB. Of that, 598 MB was span content, because whole documents went into the prompts.

Only these kinds of span were present:

| Span name | Type | Place in the trace |
|---|---|---|
| `litellm-completion` | `LLM` | The only span of a plain model call. |
| `Agent.run_sync` | `AGENT` | The root of a PydanticAI run. |
| `Agent.run` | `AGENT` | The only child of `Agent.run_sync`. |

- **Tool calls and model turns were not spans.** In an agent trace, they were messages inside the output of `Agent.run`, as pairs of tool-call and tool-return parts.
- **Values were JSON inside JSON.** Each attribute, such as `mlflow.spanInputs`, is a JSON string inside the span's JSON, so a reader decodes it twice. The LiteLLM span stored the response twice.
- **Token counts were in four places.** They were the span attribute `mlflow.chat.tokenUsage`, the trace metadata `mlflow.trace.tokenUsage`, the `trace_metrics` table, and the `usage` object in the output.
- **Errors were span events.** A failed call had status ERROR and one event named `exception`.
- **Runs and traces did not match one to one.** 81 runs had more than one trace, because a failed call was retried once. The stored ID pointed at the last one.
- **Assessments pointed at whole traces.** Their run and span columns were empty.

## What worked well

- **One local file and no server.** Runs wrote traces to a SQLite file, and the web page, the judges and the tests read the same file.
- **No tracing code in the code that called models.** Autolog recorded every LiteLLM and PydanticAI call.
- **Judges wrote onto the trace,** so MLflow's viewer showed the trace and the verdicts together.
- **Spans in a standard shape.** The span JSON has OpenTelemetry fields, so a reader can parse it without MLflow.

## What was fragile

- **The schema belonged to MLflow.** The repair script read MLflow's tables with SQL, and an MLflow migration could rename what it read. Opening an old database also runs MLflow's migrations, so a read-only reader can fail or change the file.
- **Trace IDs came from a global,** so a retry or a thread could link a run to the wrong trace.
- **Spans held whole documents,** so the database grew without limit, and showing one answer meant loading megabytes.
- **Readers assumed one span.** The judges and the web page read the first span and expected chat messages. An agent trace's first span has a prompt and no messages, so judges got no conversation from an agent run.
- **Agent steps were not spans,** so no query could find every call of one tool without parsing message lists.
- **Attribute names came from libraries,** such as LiteLLM's `response_cost`, and could change. The agent span named the model's class, not the model.
- **Errors were caught and ignored** when writing assessments, deleting runs and reading traces. A lost assessment left no record.
- **Deleted runs left their traces behind.**
- **WAL mode did not stay on.** The runtime set it once, but the largest file was back in the default mode.
- **The run-to-trace link was kept twice,** in the runtime's database and in MLflow's, so the two could disagree.
- **Nothing showed whether a row had changed,** so the traces were not evidence in the sense of provrun's hash chain.

The runtime's own notes also expected MLflow's SQLite store to go away. [The MLflow note](mlflow-tracing.md) found that it is not going away. Only the older file store is retired.

## Notes for provevi

- **Keep one local file.** Read without a server, and keep test fixtures as copies of a store.
- **Own the schema.** Keep the record in tables that provevi defines. MLflow holds a copy.
- **Store large payloads once,** in files named by their hash. Keep only the hash in an event or a span.
- **Make tool calls and model turns real spans,** with the GenAI operation names `chat`, `execute_tool` and `invoke_agent`.
- **Pass trace IDs explicitly.** Return them from the call that makes the span, and never read a last-trace global.
- **Keep token counts and cost in one place,** `gen_ai.usage.*` on the model span, and compute totals from the spans.
- **Let an assessment point at a span or a trace,** with MLflow's fields of name, value, rationale, source type and source ID.
- **Keep provrun's hash chain as the record of truth.** Treat spans as an index over the events, and link each event to its span.
