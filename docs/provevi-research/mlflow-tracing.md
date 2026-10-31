# MLflow tracing as a backend for provevi

Research date: 2026-10-31. The current MLflow release is **3.17.2**, from 2026-10-20 ([CHANGELOG](https://github.com/mlflow/mlflow/blob/master/CHANGELOG.md), [PyPI](https://pypi.org/project/mlflow/)).

The facts below come from three sources:

- **Installed source.** MLflow 3.17.2 and `mlflow-tracing` 3.17.2, installed in two scratch environments. Paths such as `mlflow/entities/span.py` are relative to `site-packages`.
- **Experiments.** Two small scripts wrote traces into a SQLite file, with no server.
- **Web sources.** The web sources are the changelog, the docs at mlflow.org and the GitHub repository. Each claim links its source.

Terms used on this page:

- **Span.** A span is one timed operation, with a name, a start time, an end time, a parent and attributes. OpenTelemetry (OTel) defines it.
- **Trace.** A trace is a tree of spans that share one trace ID.
- **Tracking store.** The tracking store is the MLflow class that persists experiments, runs and traces. `SqlAlchemyStore` writes to a SQL database, and `FileStore` writes to a folder.
- **OTLP.** OTLP is the OpenTelemetry protocol for sending spans over HTTP or gRPC.

## Summary

- **Direct SQLite is not going away.** In 3.17.2, `sqlite:///mlflow.db` is the default tracking URI, and Python writes to it in process with no server. The **file store** (`./mlruns`, `file://`) is what MLflow retired.
- **The file store raises an error since 3.13.0.** It needs `MLFLOW_ALLOW_FILE_STORE=true`, and it is in maintenance mode.
- **Each span is one JSON row.** The SQL store keeps each span as a JSON document in `spans.content`, with some columns pulled out for search.
- **MLflow accepts OTLP spans.** The server has `POST /v1/traces` since 3.6.0, and it translates GenAI semantic convention attributes to MLflow attributes.
- **There is one trap.** The lightweight `mlflow-tracing` package and the TypeScript plugins for Claude Code and Codex cannot write to SQLite. They need an HTTP server. The Python package drops spans in silence when you give it a `sqlite:` URI.

## 1. The trace data model

### Trace, TraceInfo and TraceData

A `Trace` holds a `TraceInfo` and a `TraceData`, and `TraceData` holds the list of spans. The schema version in 3.17.2 is 3 (`mlflow.trace_schema.version: "3"`). Below is the `TraceInfo` of a real trace from `t.py`, made shorter:

```json
{"trace_id": "tr-fee99023d73b75f24da6a4258976baf9",
 "trace_location": {"type": "MLFLOW_EXPERIMENT", "mlflow_experiment": {"experiment_id": "1"}},
 "request_time": "2026-10-31T16:13:01.175Z", "state": "OK", "execution_duration_ms": 33,
 "request_preview": "{\"q\": \"hi\"}", "response_preview": "{\"a\": \"yo\"}",
 "trace_metadata": {"mlflow.sourceRun": "c660a66d...", "mlflow.trace.tokenUsage": "{\"input_tokens\": 3, ...}",
                    "mlflow.traceInputs": "...", "mlflow.traceOutputs": "...", "mlflow.user": "<user>",
                    "mlflow.source.name": "<script>", "mlflow.trace.sizeBytes": "2002"},
 "tags": {"mlflow.traceName": "agent", "mlflow.trace.spansLocation": "TRACKING_STORE"},
 "assessments": [ ... ]}
```

- **Trace ID.** The MLflow trace ID is `tr-` followed by the 32 hex digits of the OTel trace ID.
- **Metadata and tags.** Metadata is immutable string pairs that MLflow and the user write when the trace starts. Tags are mutable string pairs.
- **Trace location.** A trace belongs to one experiment through `trace_location`. In Databricks, the location can also be a Unity Catalog table, set with `MLFLOW_TRACE_LOCATION` (changelog 3.14.0).

### Spans

An MLflow `Span` wraps an OTel span. `Span.to_dict()` gives the OTLP JSON shape:

```json
{"trace_id": "/umQI9c7dfJNpqQliXa6+Q==", "span_id": "skRdJJD77cc=", "parent_span_id": "0cnaHvdxwZg=",
 "name": "llm", "start_time_unix_nano": 1793463181208994000, "end_time_unix_nano": 1793463181209235000,
 "events": [], "status": {"code": "STATUS_CODE_OK", "message": ""}, "links": [],
 "attributes": {"mlflow.traceRequestId": "\"tr-fee99023...\"", "mlflow.spanType": "\"CHAT_MODEL\"",
                "mlflow.spanInputs": "{\"messages\": [...]}", "mlflow.spanOutputs": "\"yo\"",
                "mlflow.chat.tokenUsage": "{\"input_tokens\": 3, \"output_tokens\": 1, \"total_tokens\": 4}",
                "mlflow.spanLogLevel": "20"}}
```

- **Attribute values are JSON strings.** MLflow encodes each attribute value as JSON, so a string value carries its own quotes.
- **Reserved attributes.** MLflow reserves `mlflow.spanType`, `mlflow.spanInputs`, `mlflow.spanOutputs`, `mlflow.chat.tokenUsage`, `mlflow.llm.model`, `mlflow.llm.provider`, `mlflow.llm.cost` and `mlflow.spanLogLevel`. Log levels came in 3.13.0.
- **Span events and links.** Spans keep OTel events, for example exceptions. Span links came in 3.13.0 (`LiveSpan.add_link()`).

The `SpanType` constants in `mlflow/entities/span.py` (3.17.2) are `LLM`, `CHAIN`, `AGENT`, `TOOL`, `CHAT_MODEL`, `RETRIEVER`, `PARSER`, `EMBEDDING`, `RERANKER`, `MEMORY`, `UNKNOWN`, `WORKFLOW`, `TASK`, `GUARDRAIL` and `EVALUATOR`. The type is a free string, so a custom type such as `EVIDENCE` is stored as given.

### Assessments

An assessment is a judgement attached to a trace, or to one span of a trace. MLflow has two kinds:

- **Feedback.** `mlflow.log_feedback(trace_id=..., name=..., value=..., rationale=..., source=...)` records a judgement, for example a judge score or a thumbs up.
- **Expectation.** `mlflow.log_expectation(...)` records a ground truth value.

Each assessment has a source with a type (`HUMAN`, `LLM_JUDGE` or `CODE`) and an ID. It also has `run_id`, `span_id`, `rationale`, `metadata`, `overrides` and `valid`. A new assessment can override an old one, and the old one then has `valid=false`. See the [assessments docs](https://mlflow.org/docs/latest/genai/assessments/).

### Links to runs and experiments

- **Experiment.** Each trace has exactly one experiment, which is `trace_info.experiment_id` in SQL.
- **Run.** A trace made inside `mlflow.start_run()` gets the metadata key `mlflow.sourceRun`. `MlflowClient.link_traces_to_run` links traces to a run after the fact. These links go in the `entity_associations` table. OTLP ingest takes an optional `x-mlflow-run-id` header since 3.14.0.
- **Session.** `trace_info.session_id` groups the traces of one conversation. The UI has a session view since 3.6.0.

### Mapping to OpenTelemetry

The mapping to OTel is direct. An MLflow span is an OTel span with MLflow attributes, and MLflow uses an OTel `TracerProvider` inside. `Span.from_otel_proto()` and `Span.to_otel_proto()` convert between the two forms. The [attribute mapping page](https://mlflow.org/docs/latest/genai/tracing/opentelemetry/attribute-mapping/) lists the mappings for each attribute schema.

## 2. Storage backends, and what changed

### Current backends (3.17.2)

| Tracking URI | Store class | Traces |
|---|---|---|
| `sqlite:///path.db`, `postgresql://`, `mysql://`, `mssql://` | `SqlAlchemyStore` (or `WorkspaceAwareSqlAlchemyStore` with `MLFLOW_ENABLE_WORKSPACES`) | Full support, in process, no server |
| `http(s)://host` | `RestStore` | Full support, through a tracking server |
| `databricks`, `databricks://profile` | `DatabricksRestStore` | Full support |
| `file:///path`, `./mlruns` | `FileStore` | Raises an error by default since 3.13.0. Has no `log_spans`, so OTLP ingest fails. |

A SQL store can also move old span data out to object storage, for example S3. The [trace archival](https://mlflow.org/docs/latest/genai/tracing/observe-with-traces/archive-traces) feature came in 3.13.0. The tag `mlflow.trace.spansLocation` says where the spans of a trace live (`TRACKING_STORE` or an archive).

### What is deprecated, and when

Direct SQLite with no server is **not** deprecated. A search of the 3.17.2 source and the changelog found no deprecation of it. Since 3.7.0 it is the **default**. Only the file store was retired, in these steps:

1. **3.6.0 (2025-11-07).** "[Model Registry] Add deprecation warnings for filesystem backends (#18524, @harupy)".
2. **3.7.0 (2025-12-05).** Breaking change: "**SQLite as Default Backend**: MLflow now uses SQLite as the default backend instead of file-based storage, unless existing mlruns data is detected." (#18497). The code is `_get_default_tracking_uri()` in `mlflow/tracking/_tracking_service/utils.py`. It returns `./mlruns` only if `./mlruns/<digits>/meta.yaml` exists, and returns `sqlite:///mlflow.db` in all other cases.
3. **3.10.0 (2026-02-20).** "Add mlflow `migrate-filestore` command (#20615)". The [migration guide](https://mlflow.org/docs/latest/self-hosting/migrate-from-file-store) says "The filesystem backend is in maintenance mode and will not receive further updates." The tool writes only to SQLite, with `mlflow migrate-filestore --source /path/to/mlruns --target sqlite:///path/to/mlflow.db`.
4. **3.13.0 (2026-05-29).** Breaking change: "Pointing the tracking or model registry store at a local file-system path now raises an error by default; set `MLFLOW_ALLOW_FILE_STORE=true` to keep using a file-based store. (#22773, @harupy)".

The exact error in 3.17.2, from `FileStore.__init__` in `mlflow/store/tracking/file_store.py`, as a run with a `file:///...` tracking URI showed it, is in the block below.

```text
The filesystem tracking backend (e.g., './mlruns') is in maintenance mode and will not receive further updates. Please migrate to a database backend (e.g., 'sqlite:///mlflow.db') to access the latest MLflow features. The `mlflow migrate-filestore` tool migrates your existing data losslessly. See https://mlflow.org/docs/latest/self-hosting/migrate-from-file-store for migration guidance. If the filesystem backend is required for your workflow, set `MLFLOW_ALLOW_FILE_STORE=true` to opt out of this exception.
```

No removal date is published. The [backend store page](https://mlflow.org/docs/latest/self-hosting/architecture/backend-store/) calls the file store "still supported for backward compatibility".

**Why SQLite can seem to need a server.** A client that is not the full `mlflow` Python package may be the cause. Section 6 shows that `mlflow-tracing`, the TypeScript SDK, the Claude Code plugin and the Codex plugin all reject or drop `sqlite:` URIs. For those clients, "SQLite" means "start `mlflow server --backend-store-uri sqlite:///mlflow.db`".

## 3. The SQLite schema for traces (3.17.2)

The schema comes from `sqlite3 <file> .schema` on a new database that MLflow 3.17.2 made. The Alembic head is `dc11669786a5`, and there are 69 migration files. The trace tables are below.

```sql
CREATE TABLE trace_info (
  request_id VARCHAR(50) PRIMARY KEY,            -- "tr-<32 hex>"
  experiment_id INTEGER NOT NULL REFERENCES experiments ON DELETE CASCADE,
  timestamp_ms BIGINT NOT NULL, execution_time_ms BIGINT,
  status VARCHAR(50) NOT NULL,                    -- OK / ERROR / IN_PROGRESS
  client_request_id VARCHAR(50),
  request_preview VARCHAR(1000), response_preview VARCHAR(1000),
  db_payload_generation INTEGER DEFAULT 0 NOT NULL,
  trace_name VARCHAR(4096), session_id VARCHAR(250),
  input_tokens BIGINT, output_tokens BIGINT, total_tokens BIGINT,
  cache_read_input_tokens BIGINT, cache_creation_input_tokens BIGINT,
  cache_creation_input_tokens_above_1hr BIGINT,
  input_cost FLOAT, output_cost FLOAT, total_cost FLOAT);
CREATE TABLE trace_tags (key VARCHAR(250), value VARCHAR(8000), request_id VARCHAR(50),
  PRIMARY KEY (key, request_id));                 -- FK trace_info ON DELETE CASCADE
CREATE TABLE trace_request_metadata (key VARCHAR(250), value VARCHAR(8000), request_id VARCHAR(50),
  PRIMARY KEY (key, request_id));                 -- FK trace_info ON DELETE CASCADE
CREATE TABLE spans (
  trace_id VARCHAR(50) NOT NULL,                  -- the "tr-..." ID, FK trace_info
  experiment_id INTEGER NOT NULL,
  span_id VARCHAR(50) NOT NULL, parent_span_id VARCHAR(50),
  name TEXT, type VARCHAR(500), status VARCHAR(50) NOT NULL,
  start_time_unix_nano BIGINT NOT NULL, end_time_unix_nano BIGINT,
  duration_ns BIGINT GENERATED ALWAYS AS (end_time_unix_nano - start_time_unix_nano) STORED,
  content TEXT NOT NULL,                          -- the whole span as OTLP-style JSON
  input_cost FLOAT, output_cost FLOAT, total_cost FLOAT,
  model_name VARCHAR(500), model_provider VARCHAR(500),
  PRIMARY KEY (trace_id, span_id));
CREATE TABLE span_metrics (trace_id, span_id, key VARCHAR(250), value FLOAT,
  PRIMARY KEY (trace_id, span_id, key));
CREATE TABLE trace_metrics (request_id, key VARCHAR(250), value FLOAT, PRIMARY KEY (request_id, key));
CREATE TABLE assessments (
  assessment_id VARCHAR(50) PRIMARY KEY, trace_id VARCHAR(50) NOT NULL,
  name VARCHAR(250) NOT NULL, assessment_type VARCHAR(20) NOT NULL,  -- feedback / expectation
  value TEXT NOT NULL, error TEXT, rationale TEXT, assessment_metadata TEXT,
  created_timestamp BIGINT NOT NULL, last_updated_timestamp BIGINT NOT NULL,
  source_type VARCHAR(50) NOT NULL, source_id VARCHAR(250),
  run_id VARCHAR(32), span_id VARCHAR(50), overrides VARCHAR(50), valid BOOLEAN NOT NULL,
  experiment_id INTEGER, trace_timestamp_ms BIGINT, aggregate_value FLOAT,
  is_numeric_value BOOLEAN DEFAULT 0 NOT NULL);
```

- **Span storage.** Each span is one row. The full span, with all attributes, events and links, is a JSON blob in `spans.content`, in the `Span.to_dict()` shape. MLflow copies some fields into columns for search and indexes, for example name, type, status, times, model and cost.
- **Translation before storage.** `SqlAlchemyStore.log_spans()` calls `translate_span_when_storing()`, and that function adds `mlflow.*` attributes for foreign schemas (section 4).
- **Analytics tables.** The rollup tables hold totals and do not hold raw data. They are `sql_span_cost_daily_rollups`, `sql_trace_metric_daily_rollups`, `sql_assessment_daily_rollups` and `sql_trace_rollup_rebuild_queue`.
- **Related tables.** `entity_associations` holds trace to run links. The same database also holds runs, the model registry, datasets, scorers, the gateway, MCP servers, workspaces and jobs, about 65 tables in all.

The trace migrations are in `mlflow/store/db_migrations/versions/`. Each one runs through Alembic when MLflow opens the database (`Creating initial MLflow database tables... Updating database tables`). For an existing database, run `mlflow db upgrade <uri>`.

| Migration | Created | Change |
|---|---|---|
| `867495a8f9d4_add_trace_tables` | 2024-04-27 | Adds `trace_info`, `trace_tags` and `trace_request_metadata` |
| `770bee3ae1dd_add_assessments_table` | 2025-06-23 | Adds `assessments` |
| `cbc13b556ace_add_v3_trace_schema_columns` | 2025 | Adds the v3 trace columns |
| `a1b2c3d4e5f6_add_spans_table` | 2025-08-03 | Adds `spans`. Before this migration, spans lived as a JSON artifact file. |
| `5b0e9adcef9c`, `6f8d9c3b2a1e` | | Add cascade deletes |
| `c8d9e0f1a2b3_add_span_metrics_and_dimension_attributes`, `b7c8d9e0f1a2_add_trace_metrics_table` | | Add the metric tables |
| `75868b020152_add_sql_trace_analytics_schema`, `c1d2e3f4a5b6`, `1971f2d9a75b`, `da6fb0208061` | 2026 | Add analytics, indexes and archival |

The schema changes often. Ten trace migrations came in about 18 months. Also, the changelog for 3.13.0 lists a full rework of the permission tables.

## 4. OpenTelemetry

### Ingest of OTLP spans

- **Endpoint.** The tracking server takes `POST /v1/traces` (`OTLP_TRACES_PATH` in `mlflow/tracing/utils/otlp.py`, router in `mlflow/server/otel_api.py`). The header `x-mlflow-experiment-id` is required. The header `x-mlflow-run-id` is optional (3.14.0).
- **Version.** The first ingest code came before 3.6.0. The 3.6.0 release (2025-11-07) announced "**Full OpenTelemetry Support in OSS Server**: ... ingest OpenTelemetry traces into MLflow" (#18540, #18532, #18357).
- **Encodings.** OTLP/HTTP protobuf works since 3.6.0, and 3.7.0 fixed it to return protobuf (#18929). Gzip and deflate came in 3.7.0 (#19024). JSON OTLP came in 3.12.0 (#22408). There is no gRPC endpoint.
- **Backend.** The server calls `store.log_spans_async()`. `FileStore` does not implement it, so ingest needs a SQL backend.
- **Allow list.** The server keeps `service.name` only for an allow list (`claude-code`, `codex_cli_rs`, `codex_vscode`, `gemini-cli` and `qwen-code`). It keeps resource attributes since 3.14.0.
- **Docs.** See the [ingest docs](https://mlflow.org/docs/latest/genai/tracing/opentelemetry/ingest/) and the [OpenTelemetry overview](https://mlflow.org/docs/latest/genai/tracing/opentelemetry/).

### Attribute translation on ingest

When MLflow stores a span, it maps these foreign attribute schemas to `mlflow.spanType`, inputs, outputs, token usage, model and provider. The translators are in `mlflow/tracing/otel/translation/`:

- **Standards.** OTel GenAI semantic conventions (`genai_semconv.py`, which reads `gen_ai.operation.name` and `gen_ai.usage.*`), OpenInference, Traceloop and OpenLLMetry.
- **Agent CLIs.** Claude Code, Gemini CLI, Google ADK and Spring AI.
- **Other tools.** Langfuse, Laminar, Vercel AI, VoltAgent and LiveKit.

A test checked the GenAI translation with no server. It builds an OTel protobuf span with `gen_ai.operation.name=chat`, `gen_ai.request.model`, `gen_ai.provider.name` and `gen_ai.usage.input_tokens`/`output_tokens`. It calls `Span.from_otel_proto()` and then `SqlAlchemyStore.log_spans(experiment_id, [span])` on `t.db`. The results:

- **Trace info.** MLflow made the `trace_info` row, with `input_tokens=10` and `output_tokens=5`.
- **Span content.** `content` gained `mlflow.spanType="CHAT_MODEL"`, `mlflow.chat.tokenUsage`, `mlflow.llm.model` and `mlflow.llm.provider`.
- **Columns.** `model_name` and `model_provider` were filled.
- **Quirk.** The `spans.type` column stayed `NULL` for the translated span, although `content` says `CHAT_MODEL`. A search by span type can miss spans that came in with foreign schemas.

### Export to an OTel collector

The settings below are in `mlflow/environment_variables.py`.

- **`OTEL_EXPORTER_OTLP_TRACES_ENDPOINT`** (with `OTEL_EXPORTER_OTLP_TRACES_PROTOCOL` and headers). If it is set, MLflow exports to the OTLP endpoint **in place of** the tracking store.
- **`MLFLOW_ENABLE_OTLP_EXPORTER`** (default `True`). It turns off the OTLP export above even when the OTel variable is set. It came in 3.5.0 (#17505).
- **`MLFLOW_TRACE_ENABLE_OTLP_DUAL_EXPORT`** (default `False`). It writes to both the tracking store and the collector.
- **`MLFLOW_ENABLE_OTEL_GENAI_SEMCONV`** (default `False`). It rewrites `mlflow.*` attributes to GenAI semantic conventions before OTLP export, since 3.11.0 (#21494). See the [GenAI semconv docs](https://mlflow.org/docs/latest/genai/tracing/opentelemetry/genai-semconv/).
- **`MLFLOW_USE_DEFAULT_TRACER_PROVIDER`** (default `True`, which gives an isolated provider). Set it to `False` to mix MLflow and plain OTel SDK spans in one trace. `MLFLOW_TRACE_PROPAGATE_TO_OTEL_CONTEXT` lets plain OTel libraries nest under MLflow spans.

The [export docs](https://mlflow.org/docs/latest/genai/tracing/opentelemetry/export/) describe these settings.

## 5. Integrations

| Integration | How it works | Notes |
|---|---|---|
| `mlflow.litellm.autolog()` | Adds MLflow callbacks to `litellm.success_callback` and `failure_callback` (`mlflow/litellm/__init__.py`) | Works with a direct SQLite URI |
| `mlflow.pydantic_ai.autolog()` | Patches Pydantic AI (`autolog.py` and `autolog_v2.py`) | Pydantic AI 2.x needs 2.5.0 or newer |
| `mlflow.anthropic.autolog()` | Patches the Anthropic SDK, and also traces the Python Claude Agent SDK | Has a GenAI semconv converter |
| `mlflow.openai.autolog()` | Patches the OpenAI SDK and the OpenAI Agents SDK (`_agent_tracer.py`) | Has a GenAI semconv converter |

The repo also has autolog modules for LangChain, LlamaIndex, DSPy, CrewAI, AutoGen, AG2, Agno, smolagents, Strands, Semantic Kernel, Haystack, Bedrock, Gemini, Groq, Mistral and MCP.

### Claude Code

**Current path (3.13.0 and later).** Claude Code tracing is a Claude Code plugin, `mlflow-tracing@mlflow-plugins`. The plugin is the npm package `@mlflow/claude-code`, and its source is in [`libs/typescript/integrations/claude-code`](https://github.com/mlflow/mlflow/tree/master/libs/typescript/integrations/claude-code). Install it with these commands:

```
claude plugin marketplace add mlflow/mlflow --sparse .claude-plugin
claude plugin install mlflow-tracing@mlflow-plugins
/mlflow-tracing:setup        # or: mlflow autolog claude
```

- **Hook.** The plugin registers one **Stop hook**, in `hooks/hooks.json`. The hook runs `node ${CLAUDE_PLUGIN_ROOT}/bundle/stop.cjs` with a 120-second timeout.
- **Transcript.** The hook reads the session transcript JSONL (`transcript.ts`, `processTranscript`). It makes an `AGENT` root span for each turn, with `LLM` and `TOOL` children and nested `AGENT` spans for subagents. Token and cache usage go on each LLM span.
- **Write-ahead log.** A write-ahead log, a local file of pending writes, keeps tracing from slowing the agent or losing traces (changelog 3.14.0).
- **Tracking URI.** `isValidTrackingUri()` in `src/config.ts` accepts only `http:`, `https:` and `databricks` URIs. **The plugin cannot write to SQLite.** It needs `mlflow server`.
- **History.** Claude Code tracing came in 3.4 and 3.5 (#17521, #18022), as a Python hook that `mlflow autolog claude` wrote into `.claude/settings.json`. In 3.12.0, the tracing moved to TypeScript (#22338). The 3.13.0 notes say "`mlflow autolog claude` no longer installs the old Python autolog hook; Claude Code tracing is now provided by the official Claude plugin, which must be installed separately. (#23339)". The Python `mlflow/claude_code/tracing.py` still has the transcript parser.
- **OTel path.** Claude Code also emits OTLP traces by itself, which is beta in Claude Code, to `/v1/traces`. MLflow maps `claude_code.interaction`, `claude_code.llm_request` and `claude_code.tool` to `AGENT`, `LLM` and `TOOL` (`translation/claude_code.py`). The content is `<REDACTED>` if the user does not set `OTEL_LOG_USER_PROMPTS`, `OTEL_LOG_TOOL_DETAILS` or `OTEL_LOG_TOOL_CONTENT`.
- **Agent SDK.** For the TypeScript Claude Agent SDK, use `createTracedQuery(query)`. For the Python Claude Agent SDK, use `mlflow.anthropic.autolog()`.
- **Docs.** See the [Claude Code docs](https://mlflow.org/docs/latest/genai/tracing/integrations/listing/claude_code).

### Codex

Codex tracing came in 3.12.0, as "Add TypeScript Codex CLI tracing via `notify` hook (#22410)". Its parts are these:

- **Notify hook.** The npm package `@mlflow/codex` adds `notify = ["mlflow-codex", "notify-hook"]` to `config.toml`. Codex calls the hook after each turn. The hook reads the rollout JSONL transcript and honors `CODEX_HOME` since 3.16.0. Each turn gives an `AGENT` root span with `LLM` and `TOOL` children.
- **Native OTel.** The `[otel]` table in `~/.codex/config.toml` can point at `http://host:5000/v1/traces` with `x-mlflow-experiment-id`. The docs warn that these spans show "internal execution details" and are not a clean conversation flow.
- **Server.** The docs say "The MLflow server must be running to receive traces."
- **Docs.** See the [Codex docs](https://mlflow.org/docs/latest/genai/tracing/integrations/listing/codex).

The AI Gateway (3.13.0) can also trace Claude Code, Codex and Gemini CLI. It works as a proxy for model calls.

## 6. Writing traces with no server

All three APIs below work in 3.17.2 with `mlflow.set_tracking_uri("sqlite:////abs/path.db")` and the **full** `mlflow` package. A test ran each of them.

- **`mlflow.start_span(name, span_type=...)`** is a context manager. **`@mlflow.trace`** is a decorator. Both make spans under the active trace, or start a new trace.
- **`MlflowClient().start_trace(name, span_type, inputs, attributes, tags, experiment_id, start_time_ns, run_id, links)`**, with `start_span`, `end_span` and `end_trace`, takes explicit IDs and times. Use it to replay events that happened earlier.
- **`mlflow.log_trace(name, request, response, intermediate_outputs, attributes, tags, start_time_ms, execution_time_ms)`** writes a trace with a single root span.
- **`SqlAlchemyStore(uri, artifact_root).log_spans(experiment_id, spans)`** is a lower-level store method, not a public API. It takes `mlflow.entities.Span` objects, which can come from `Span.from_otel_proto()`. MLflow makes the `trace_info` row for you.
- **`mlflow.log_feedback`** and **`mlflow.log_expectation`** write assessments.

Be careful with these behaviors:

- **Asynchronous writes.** Trace writes go through a background queue. If you read a trace in the same process, call `mlflow.flush_trace_async_logging()` first. Without the flush, my `log_feedback` call failed with "Trace ... not found".
- **Silent drops in the lightweight SDK.** `mlflow-tracing` 3.17.2 (47 MB) has no `sqlite` store. With a `sqlite:` URI, `start_span` writes no file and gives no warning. The failure shows only at DEBUG level (`KeyError: 'sqlite'` in the store registry). Tracing fails open by design. The TypeScript `@mlflow/core` SDK also needs HTTP or Databricks.
- **Install size.** The full `mlflow` package with its dependencies takes 509 MB in the venv. It pulls in pandas, scikit-learn, scipy, matplotlib, pyarrow, Flask, alembic and docker.

## 7. Recommendation

MLflow is a reasonable **first** backend for provevi, if provevi treats MLflow as one driver behind its own interface. Provevi must not take MLflow's schema as its own.

Reasons to use MLflow:

- **OTel shape.** The span model is OTel. An MLflow span is an OTel span with `mlflow.*` attributes, so OTel spans fit with no change.
- **Paths for OTel spans.** OTel spans can enter through `/v1/traces` with a server. They can also enter in process through `Span.from_otel_proto()` and `log_spans()` into SQLite with no server. The GenAI semconv translation is built in.
- **Evidence events.** Provevi evidence events can map to spans with a custom `span_type`, attributes and span events. Judgements about evidence map to assessments, which have a source, a rationale, an override chain and a `valid` flag.
- **Viewer.** The UI, search, sessions, cost rollups and the Claude Code and Codex collectors come with it.

Risks, and how to handle each one:

1. **The schema changes often and is internal.** Ten trace migrations came in 18 months, and analytics tables are new. Do not read or write the SQLite tables from provevi. Use the MLflow client APIs, or OTLP. Pin a minor version in the driver.
2. **The supported paths keep moving.** MLflow retired the file store over 3.6.0 to 3.13.0. It replaced the Claude Code hook in 3.12.0 and 3.13.0. Keep the driver small, and test it against each MLflow minor release.
3. **The clients disagree about SQLite.** Only full Python `mlflow` writes SQLite in process. The plugins for Claude Code and Codex, the TypeScript SDK and `mlflow-tracing` need a server, and `mlflow-tracing` drops spans in silence. For agent CLIs, plan to run `mlflow server --backend-store-uri sqlite:///...`. If a URI is wrong, make the provevi driver fail loudly.
4. **The dependency is heavy.** Full `mlflow` takes about 509 MB. Load it only inside the driver, as an optional extra.
5. **IDs and identity.** MLflow rewrites the trace ID to `tr-<hex>`. Every trace needs an experiment. Attribute values are JSON strings. Keep provevi's own IDs in attributes or metadata, so a round trip keeps them.
6. **Losses on ingest.** `service.name` is kept only for allow-listed clients. The `spans.type` column can stay `NULL` for translated spans. Spans can move to an archive store. A driver test must read spans back through `get_trace` and must not trust the columns.
7. **Asynchronous writes.** Writes are asynchronous by default. The driver must flush before it reads its own writes, and on shutdown.

**Alternative.** A small provevi SQLite driver of its own could store the OTLP JSON shape, one span per row, as MLflow does. An MLflow driver can then be the second backend, for the UI and the agent CLI collectors. With that order, provevi does not depend on MLflow's rate of change.

## Sources

- MLflow 3.17.2 source, installed from PyPI in a scratch environment. The files used are `mlflow/tracking/_tracking_service/utils.py`, `mlflow/store/tracking/file_store.py`, `mlflow/store/tracking/sqlalchemy_store.py`, `mlflow/entities/span.py`, `mlflow/server/otel_api.py`, `mlflow/tracing/otel/translation/*.py`, `mlflow/environment_variables.py`, `mlflow/claude_code/*.py` and `mlflow/store/db_migrations/versions/*.py`.
- MLflow CHANGELOG, https://github.com/mlflow/mlflow/blob/master/CHANGELOG.md, and the raw file at https://raw.githubusercontent.com/mlflow/mlflow/master/CHANGELOG.md
- Migrate from the file store, https://mlflow.org/docs/latest/self-hosting/migrate-from-file-store
- Backend stores, https://mlflow.org/docs/latest/self-hosting/architecture/backend-store/
- OpenTelemetry overview, https://mlflow.org/docs/latest/genai/tracing/opentelemetry/ and the subpages `ingest/`, `export/`, `genai-semconv/` and `attribute-mapping/`
- Claude Code tracing, https://mlflow.org/docs/latest/genai/tracing/integrations/listing/claude_code
- Claude Code plugin source, https://github.com/mlflow/mlflow/tree/master/libs/typescript/integrations/claude-code and https://github.com/mlflow/mlflow/blob/master/.claude-plugin/marketplace.json
- Codex tracing, https://mlflow.org/docs/latest/genai/tracing/integrations/listing/codex
- Lightweight SDK, https://mlflow.org/docs/latest/genai/tracing/lightweight-sdk/
- Trace archival, https://mlflow.org/docs/latest/genai/tracing/observe-with-traces/archive-traces
