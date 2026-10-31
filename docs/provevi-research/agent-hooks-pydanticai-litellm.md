# Hooks for provevi in PydanticAI, LiteLLM and OpenTelemetry

Research date: 2026-10-31. The findings come from the official docs and from the source of the released
packages. The wheels were downloaded and unpacked under a scratch folder, and none was
installed or run.

> **A dated snapshot.** This note describes provrun and its pins on 2026-10-31, before later changes. PR #11 replaced the model and toolset wrappers with a capability, `evidence_capability`, which records streamed requests and every tool. It also keeps LiteLLM's spans off provrun's span, and makes `otel.py` claim no schema URL. Commit 227fed4 moved provrun to `litellm==1.83.0` and PydanticAI 2.64.0. The design page, [the design](../provevi-design.md), is kept current.

## Terms

| Term | Meaning here |
|---|---|
| provrun | The MahaCata runner, in `packages/provrun`. |
| provevi | The planned evidence and trace store. |
| evidence event | One line of `events.jsonl`, chained to the line before it by SHA-256. |
| span | One OpenTelemetry (OTel) record of an operation, with a start, an end and attributes. |
| GenAI conventions | The OTel semantic conventions for generative AI, the `gen_ai.*` names. |
| hook | A function that a library calls at a fixed point of its work. |
| blocking hook | A hook that can stop or change the call. An observing hook only sees it. |
| baggage | OTel key and value pairs that travel with the trace context, in process and over HTTP. |
| canonical record | The record that provevi trusts. Other records are copies or cross-checks. |

## Summary

- **PydanticAI 2.x has a full lifecycle hook system.** Capabilities wrap the run, each model request, each
  tool validation and each tool execution. The hooks can block, change or skip a call. They also see
  streamed requests, which the provrun `WrapperModel` does not see.
- **LiteLLM callbacks are best-effort.** The success callbacks of `acompletion` run in a bounded
  background queue that can drop work. A pre-call logging hook cannot block, because LiteLLM catches its
  errors. provrun's own wrapper around `acompletion` is the right canonical record.
- **The GenAI conventions moved.** They now live in the `open-telemetry/semantic-conventions-genai`
  repository. That repository has no release, no tag and no schema URL yet. Every `gen_ai.*` constant in
  `opentelemetry-semantic-conventions` 0.66b2 is marked deprecated with a pointer to the new repository.
- **The pinned LiteLLM 1.82.6 is clean of the March 2026 attack, but it is old.** It lacks the
  semantic-convention mode of the `otel` callback and the per-attempt failure hook. Its proxy has known
  CVEs. A second compromised release, 1.107.0, came on 2026-10-21, as "The supply chain incidents" below says, with its sources.
- **Recommendation:** make provrun's own wrappers the canonical evidence source for every substrate. Use
  each library's OTel spans as a second, parsed source, joined by trace ID and by the provrun run ID.

## Versions checked

| Package | Latest on PyPI | Released | Locked in MahaCata `uv.lock` |
|---|---|---|---|
| `pydantic-ai-slim` | 2.64.0 | 2026-10-31 | 2.63.0 |
| `litellm` | 1.106.6 | 2026-10-28 | 1.82.6 (pinned `<=1.82.6`) |
| `opentelemetry-api` | 1.45.2 | 2026-10-29 | 1.45.1 |
| `opentelemetry-semantic-conventions` | 0.66b2 | 2026-10-29 | not locked |
| `opentelemetry-util-genai` | 1.3b0 | 2026-10-23 | not locked |
| `opentelemetry-instrumentation-genai-openai` | 1.3b0 | 2026-10-23 | not locked |
| `opentelemetry-instrumentation-genai-anthropic` | 1.3b0 | 2026-10-23 | not locked |
| `opentelemetry-instrumentation-genai-openai-agents` | 1.3b0 | 2026-10-23 | not locked |
| `anthropic` | 2.3.0 | 2026-10-23 | 2.3.0 |
| `openai` | 3.42.0 | 2026-10-31 | 3.41.0 |
| `openai-agents` | 0.24.0 | 2026-10-15 | not locked |
| `logfire` | 5.4.0 | 2026-10-26 | `logfire-api` 5.4.0 |

The PyPI JSON API at `https://pypi.org/pypi/<name>/json` gave the versions and dates.

## What provrun captures today

provrun writes evidence from its own code, and the agent does not write it. The files are
`packages/provrun/src/provrun/evidence.py`, `otel.py`, `backends/pydantic_ai.py` and `backends/litellm.py`.

- **`EvidenceRecorder.step`** opens a span named after the event type, with `provrun.run_id`. It writes one
  event when the step ends, and it copies the trace ID and span ID into the event data.
- **`otel.py`** maps `model_called` to `chat`, `tool_called` to `execute_tool` and `sandbox_started` to
  `invoke_agent`. It pins `SEMCONV_VERSION = "1.37.0"`.
- **PydanticAI backend.** `EvidenceModel(WrapperModel)` overrides `request` only. `EvidenceToolset(WrapperToolset)`
  overrides `call_tool`. After the run, the whole message history becomes one `trace_imported` event.
- **LiteLLM backend.** One `model_called` step wraps one `litellm.acompletion` call. No LiteLLM callback is
  registered.

The gaps that the research found are in the Issues section at the end.

## 1. PydanticAI

Docs moved from `ai.pydantic.dev` to `pydantic.dev/docs/ai`. For example, `https://ai.pydantic.dev/logfire/`
answers 301 to `https://pydantic.dev/docs/ai/integrations/logfire/`. The source read is
`pydantic_ai_slim` 2.64.0.

### Turning on instrumentation

In 2.x the `Agent` constructor has no `instrument` argument. The constructor sets `self._instrument = None`.
Three ways turn spans on:

- **`Agent.instrument_all(settings)`** sets the default for all agents.
- **`agent.instrument = settings`** sets it for one agent.
- **`capabilities=[Instrumentation(settings=...)]`** adds the capability explicitly.

An `InstrumentedModel` passed as the model also works. The run unwraps it and adds the capability. See
`pydantic_ai/agent/__init__.py`, near the comment "Resolve instrumentation".

### InstrumentationSettings

The fields, from `pydantic_ai/models/instrumented.py`:

| Field | Default | Meaning |
|---|---|---|
| `tracer_provider`, `meter_provider` | global providers | Where spans and metrics go. |
| `include_content` | `True` | Prompts, completions, tool arguments and tool results on spans. |
| `include_binary_content` | `True` | Binary file data in the recorded content. |
| `include_model_request_parameters` | `True` | The `model_request_parameters` attribute with every tool definition. |
| `version` | `5` | The data format. 2, 3 and 4 are deprecated. 6 is opt-in. |
| `use_aggregated_usage_attribute_names` | `True` | Run spans use `gen_ai.aggregated_usage.*`, a name outside the conventions. |

`event_mode` does not exist in 2.64.0. A search of the source for `event_mode` finds nothing. Messages go
on span attributes only.

The format versions:

- **Version 2** puts `gen_ai.system_instructions`, `gen_ai.input.messages` and `gen_ai.output.messages` on
  model request spans, and `pydantic_ai.all_messages` on the run span.
- **Version 3** renames the spans to `invoke_agent {name}` and `execute_tool {name}`. It renames the tool
  attributes to `gen_ai.tool.call.arguments` and `gen_ai.tool.call.result`, and it adds thinking parts.
- **Version 4** uses the `uri` and `blob` part types for media.
- **Version 5**, the default, leaves the span status unset for a deferred tool call.
- **Version 6** sends tool results in a message with `role='tool'`.

The docs say that PydanticAI follows the GenAI conventions at version 1.37.0, and that they are still
experimental. Source: `https://pydantic.dev/docs/ai/integrations/logfire/`.

### Spans that PydanticAI emits

| Span name | Kind | Opened by | Key attributes |
|---|---|---|---|
| `invoke_agent {agent name}` | INTERNAL | `Instrumentation.wrap_run` | `gen_ai.agent.name`, `gen_ai.agent.call.id` (the run ID), `gen_ai.conversation.id`, `pydantic_ai.all_messages`, `gen_ai.aggregated_usage.*` |
| `chat {model}` | CLIENT | `open_model_request_span` | `gen_ai.operation.name=chat`, `gen_ai.request.model`, `gen_ai.provider.name`, `gen_ai.system`, `gen_ai.input.messages`, `gen_ai.output.messages`, `gen_ai.tool.definitions`, `gen_ai.usage.*`, `gen_ai.response.finish_reasons` |
| `execute_tool {tool name}` | INTERNAL | the tool execute wrapper | `gen_ai.tool.name`, `gen_ai.tool.call.id`, `gen_ai.tool.call.arguments`, `gen_ai.tool.call.result`, `pydantic_ai.tool.retry.*`, `pydantic_ai.tool.failure_stage` |
| `decide {model}` | INTERNAL | a decision model | the decision |

The run span sets the baggage keys `gen_ai.agent.name`, `gen_ai.agent.call.id` and `gen_ai.conversation.id`
for the spans under it. Model request spans copy them as attributes. The key `gen_ai.agent.call.id` is a
PydanticAI name. The current GenAI registry does not have it. Source: `pydantic_ai/_instrumentation.py`
and `pydantic_ai/capabilities/instrumentation.py`.

### Lifecycle hooks (capabilities)

A capability is a subclass of `AbstractCapability`, passed in `Agent(capabilities=[...])` or in
`agent.run(..., capabilities=[...])`. The `Hooks` capability registers plain functions with decorators,
for example `@hooks.on.before_tool_execute`. Docs: `https://pydantic.dev/docs/ai/capabilities/overview/`
and `https://pydantic.dev/docs/ai/core-concepts/hooks/`.

| Hook family | Methods | Can block or change |
|---|---|---|
| Run | `before_run`, `after_run`, `wrap_run`, `on_run_error` | yes |
| Graph node | `before_node_run`, `after_node_run`, `wrap_node_run`, `on_node_run_error` | yes |
| Model request | `before_model_request` (once per step), `prepare_model_request` (once per attempt), `after_model_request`, `wrap_model_request`, `on_model_request_error` | yes, with `SkipModelRequest`, `ModelRetry` or `RetryModelRequest` |
| Tool validation | `before_tool_validate`, `after_tool_validate`, `wrap_tool_validate`, `on_tool_validate_error` | yes, with `SkipToolValidation`, `ModelRetry`, `ApprovalRequired` or `CallDeferred` |
| Tool execution | `before_tool_execute`, `after_tool_execute`, `wrap_tool_execute`, `on_tool_execute_error` | yes, with `SkipToolExecution`, `ModelRetry`, `ToolFailed`, `ApprovalRequired` or `CallDeferred` |
| Output | `before_output_validate`, `after_output_validate`, `wrap_output_validate`, `on_output_validate_error` | yes |
| Tools list | `prepare_tools`, `prepare_output_tools` | yes, it can hide a tool |
| Wrappers | `get_wrapper_model`, `get_wrapper_toolset` | yes |
| Events | `on_event`, `wrap_run_event_stream` | `on_event` observes. A listener with `dispatch='immediate'` can cancel a capability event. |

Facts that matter for provevi:

- **`wrap_model_request` covers streamed requests.** The docstring of `after_model_request` names streamed
  responses. A `WrapperModel` that overrides only `request` misses `request_stream`.
- **`before_tool_validate` sees a call before PydanticAI checks its arguments.** The hook therefore sees
  a tool call with bad arguments. `call_tool` on a toolset never sees such a call.
- **`before_tool_execute` is the place for a policy gate.** Raise `ApprovalRequired` there to defer the
  call before the tool runs. Raise `SkipToolExecution(result)` to give the model a result without
  running the tool.
- **`get_wrapper_toolset`** receives the combined, non-output toolset of the run. It includes tools that
  other capabilities add. Output tools are added separately and are not included.
- **`prepare_model_request`** runs once for each attempt, with the model that serves the attempt. A
  `FallbackModel` or a retry therefore gives one call for each attempt.

### WrapperModel and WrapperToolset

`WrapperModel` (`pydantic_ai/models/wrapper.py`) delegates every method of `Model`. Besides `request`, it
delegates `request_stream`, `count_tokens`, `compact_messages`, the batch methods and the methods for
suspended responses. A wrapper that overrides only `request` does not see the other model calls.

`WrapperToolset` (`pydantic_ai/toolsets/wrapper.py`) delegates `get_tools`, `call_tool`, `get_instructions`,
`for_run`, `for_run_step` and the context manager methods. `call_tool(name, tool_args, ctx, tool)` gets the
arguments after validation.

### Event stream

`agent.run_stream(...)`, `agent.iter(...)` and `event_stream_handler=` give `AgentStreamEvent` values. The
types are in `pydantic_ai/messages.py`:

- **Model stream:** `PartStartEvent`, `PartDeltaEvent`, `PartEndEvent`, `FinalResultEvent`.
- **Tool handling:** `FunctionToolCallEvent`, `FunctionToolResultEvent`, `OutputToolCallEvent`,
  `OutputToolResultEvent`, `DeferredToolRequestsEvent`, `DeferredToolResultsEvent`.
- **Run changes:** `ToolAvailabilityDeltaEvent`, `CapabilityLoadDeltaEvent`, `ModelSettingsDeltaEvent`,
  `EnqueuedMessagesEvent`.
- **Other:** `CustomEvent`, `CapabilityEvent`, and the realtime session events.

The stream is good for live display. It is a poor evidence source, because a consumer can stop reading it.

### Tool calls, results, retries and usage

- **Tool calls** are `ToolCallPart` values in a `ModelResponse`. Each has `tool_name`, `args` and
  `tool_call_id`. Calls that the provider runs, for example a hosted web search, are `NativeToolCallPart`
  and `NativeToolReturnPart`. No toolset sees them.
- **Tool results** are `ToolReturnPart` values in the next `ModelRequest`.
- **Retries** are `RetryPromptPart` and `RetryFeedbackPart`. Tool spans carry `pydantic_ai.tool.retry.cause`,
  `pydantic_ai.tool.retry.attempt` and `pydantic_ai.tool.retry.max_retries`.
- **Usage per request** is `ModelResponse.usage`, a `RequestUsage`. Its fields include `input_tokens`,
  `output_tokens`, `cache_read_tokens`, `cache_write_tokens`, `output_reasoning_tokens` and `cost`.
- **Usage per run** is `result.usage`, a `RunUsage`. It adds `requests` and `tool_calls`.

### Correlation with a provrun run

`agent.run(..., run_id=...)` takes a caller's run ID. The docstring says an empty string, or an ID that is
already in `message_history`, raises `UserError`. The run ID goes into the run span as
`gen_ai.agent.call.id`, into baggage, and onto each message as `run_id`. The `invoke_agent` span opens
under the current OTel context, so it nests under provrun's own span automatically.

## 2. LiteLLM

Source read: `litellm` 1.82.6, the pinned version. For comparison, `litellm` 1.106.6 was downloaded only to
read its source.

### The supply chain incidents

- **March 2026.** `litellm` 1.82.7 and 1.82.8 were malicious. They were live on PyPI on 2026-03-24 from
  10:39 UTC for about 40 minutes. Version 1.82.8 added a `litellm_init.pth` file that stole credentials.
  LiteLLM says the likely cause was the Trivy scanner in its CI. Both versions are gone from PyPI. Version
  1.83.0, from a rebuilt pipeline, came on 2026-03-30. Sources:
  `https://docs.litellm.ai/blog/security-update-march-2026`,
  `https://www.bitsight.com/blog/litellm-versions-1-82-7-1-82-8-supply-chain-compromise`.
- **October 2026.** LiteLLM's own pipeline built, signed and published a malicious `litellm` 1.107.0 on
  2026-10-21. The pipeline used Trusted Publishing and cosign, and those checks did not stop it. LiteLLM
  says all earlier versions are safe. Sources: `https://docs.litellm.ai/blog/security-incident-oct-21-2026`,
  `https://docs.litellm.ai/blog/security-changes-after-oct-21-2026`. Reports from others:
  `https://bleepingcomputer.com/news/security/litellm-release-pipeline-hacked-to-distribute-info-stealer-malware`,
  `https://cloudsmith.com/blog/litellm-1-107-0-compromised-on-pypi.md`. The release is gone from PyPI, so the
  latest release that PyPI lists is 1.106.6.
- **Proxy CVEs that affect 1.82.6.** CVE-2026-35029, CVE-2026-35030 and GHSA-69x8-hrgq-fjj8 are fixed in
  1.83.0. CVE-2026-42208, an SQL injection, affects 1.81.16 to 1.83.6. CVE-2026-30623 is fixed in
  1.83.7-stable. CVE-2026-48710, a host header bypass, is fixed in 1.84.0. All of them are in the proxy
  server, not in the SDK call path that provrun uses. Source: `https://docs.litellm.ai/blog/tags/security`.
- **Provenance.** PyPI has no provenance attestation for `litellm-1.82.6-py3-none-any.whl`. It has one for
  1.83.0. The first wheel of 1.106.6 also has none. The check used
  `https://pypi.org/integrity/litellm/<version>/<file>/provenance`.
- **Packaging change.** Version 1.106.6 ships compiled `cp310-abi3` wheels for each platform, where 1.82.6
  was a pure Python wheel.

The upper bound `<=1.82.6` keeps out both bad releases. It also keeps out every fix since March.

### CustomLogger hooks in 1.82.6

`litellm/integrations/custom_logger.py` defines the class. Register an instance in `litellm.callbacks`.

| Hook | Runs in | Blocks | Notes |
|---|---|---|---|
| `log_pre_api_call(model, messages, kwargs)` | SDK and proxy | no | `Logging.pre_call` catches every exception and logs it. |
| `async_log_pre_api_call` | SDK and proxy | no | Same. |
| `async_pre_call_deployment_hook(kwargs, call_type)` | SDK `acompletion` and proxy | yes | Called without a `try`. It can change the kwargs, and an exception stops the call. |
| `async_post_call_success_deployment_hook` | SDK and proxy | can change the response | Per attempt. |
| `log_success_event`, `log_failure_event` | SDK and proxy | no | For an async call, they run in a thread pool after the call. |
| `async_log_success_event`, `async_log_failure_event` | SDK and proxy | no | Queued on `GLOBAL_LOGGING_WORKER`. |
| `async_log_stream_event` | SDK and proxy | no | Per stream. |
| `async_pre_request_hook` | Anthropic `/v1/messages` pass-through only | yes | Not on the `acompletion` path. |
| `async_logging_hook`, `logging_hook` | SDK and proxy | no | Masks what the loggers see. |
| `async_pre_call_hook`, `async_post_call_success_hook`, `async_moderation_hook`, `async_post_call_streaming_hook` | proxy only | yes | The docs say not to use them in library mode. |

`async_post_call_failure_deployment_hook` is in the current docs, but not in 1.82.6. The docs are at
`https://docs.litellm.ai/docs/observability/custom_callback`.

**The success path is best-effort.** `litellm/litellm_core_utils/logging_worker.py` describes the worker as
"best-effort with bounded queues to prevent backpressure". The queue holds 50,000 items, from
`LOGGING_WORKER_MAX_QUEUE_SIZE`. Each coroutine has 20 seconds, from
`LOGGING_WORKER_MAX_TIME_PER_COROUTINE`. A flush runs at exit through `atexit`. Also, a request with
`"no-log": True` skips logging, unless `litellm.global_disable_no_log_param` is `True`.

### StandardLoggingPayload

The success and failure hooks get `kwargs["standard_logging_object"]`. The table lists its fields in 1.82.6,
from `litellm/types/utils.py`.

| Group | Fields |
|---|---|
| Identity | `id`, `trace_id`, `call_type`, `stream`, `status`, `status_fields` |
| Model | `custom_llm_provider`, `model`, `model_id`, `model_group`, `api_base`, `model_map_information`, `model_parameters` |
| Usage and cost | `total_tokens`, `prompt_tokens`, `completion_tokens`, `response_cost`, `cost_breakdown`, `cache_hit`, `cache_key`, `saved_cache_cost` |
| Time | `startTime`, `endTime`, `completionStartTime`, `response_time` |
| Content | `messages`, `response`, `error_str`, `error_information` |
| Caller | `metadata`, `request_tags`, `end_user`, `requester_ip_address`, `user_agent`, `hidden_params` |
| Other | `guardrail_information`, `standard_built_in_tools_params` |

`trace_id` comes from `litellm_session_id`, else from `litellm_trace_id`, else from a new UUID. The
function is `StandardLoggingPayloadSetup._get_standard_logging_payload_trace_id`.

### The "otel" callback

Turn it on with `litellm.callbacks = ["otel"]`. The class is `litellm/integrations/opentelemetry.py`.

In 1.82.6:

- **Parent.** The span parent comes, in order, from `metadata["litellm_parent_otel_span"]`, from an inbound
  `traceparent` header, or from the active span.
- **Span shape.** With no parent, it makes a `litellm_request` span and a `raw_gen_ai_request` child. A
  `metadata["generation_name"]` value replaces the name. With a parent, it makes no span of its own. It
  sets attributes on the parent and sets the parent status to OK. `USE_OTEL_LITELLM_REQUEST_SPAN=true`
  forces its own span.
- **Attributes.** It mixes the OpenLLMetry names (`llm.request.type`, `llm.is_streaming` and so on) with
  `gen_ai.system`, `gen_ai.request.model`, `gen_ai.usage.*_tokens`, `gen_ai.input.messages`,
  `gen_ai.output.messages`, `gen_ai.system_instructions` and `gen_ai.framework=litellm`.
- **Content.** `litellm.turn_off_message_logging` or `message_logging=False` removes message content.
  `LITELLM_OTEL_INTEGRATION_ENABLE_EVENTS` turns on log events.

The current docs add three things that 1.82.6 lacks. The first is
`OTEL_SEMCONV_STABILITY_OPT_IN=gen_ai_latest_experimental`, which names the span `{operation} {model}`
with kind CLIENT. The second is `OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT`. The third is a new
integration behind `LITELLM_OTEL_V2=true`. A search of the 1.82.6 source finds none of these names.
Source: `https://docs.litellm.ai/docs/observability/opentelemetry_integration`.

### MLflow, Langfuse and the proxy

- **MLflow.** `litellm.callbacks = ["mlflow"]` loads `MlflowLogger`, a `CustomLogger`. It writes MLflow
  trace spans from the success and failure hooks, so it has the same best-effort delivery. Docs:
  `https://docs.litellm.ai/docs/observability/mlflow`.
- **Langfuse.** 1.82.6 has the Langfuse SDK logger (`langfuse.py`) and an OTel exporter
  (`langfuse_otel.py`). Docs: `https://docs.litellm.ai/docs/observability/langfuse_otel_integration`.
- **Proxy.** The proxy reads callbacks from `litellm_settings.callbacks`, `success_callback` and
  `failure_callback` in `config.yaml`. It reads a `traceparent` header into its spans. Each response has
  an `x-litellm-call-id` header. A client can send `"no-log": true`. Docs:
  `https://docs.litellm.ai/docs/proxy/logging`.

## 3. OpenTelemetry GenAI conventions

### Where they live and their status

The GenAI conventions moved to `https://github.com/open-telemetry/semantic-conventions-genai`. The page
`https://opentelemetry.io/docs/specs/semconv/gen-ai/` says they are "no longer maintained in this
repository".

- **Status.** Every GenAI document says "Status: Development".
- **Releases.** The new repository was created on 2026-05-05. On 2026-10-31 it has no release and no tag.
  Its changelog has only an "Unreleased" heading, and its README says "Schema URL: TODO".
- **Base version.** The documents link to core semantic conventions v1.44.0, released 2026-08-04.
- **Python package.** In `opentelemetry-semantic-conventions` 0.66b2, every `GEN_AI_*` constant is marked
  "Deprecated: Moved to the OpenTelemetry GenAI semantic conventions repository".

### Span names

| Operation | Span name | Kind | Document |
|---|---|---|---|
| inference | `{gen_ai.operation.name} {gen_ai.request.model}`, for example `chat gpt-5` | CLIENT | `docs/gen-ai/client-inference.md` |
| `create_agent` | `create_agent {gen_ai.agent.name}` | CLIENT | `docs/gen-ai/gen-ai-agent-spans.md` |
| `invoke_agent` | `invoke_agent {gen_ai.agent.name}`, or `invoke_agent` | CLIENT for a remote agent, INTERNAL in process | `docs/gen-ai/gen-ai-agent-spans.md` |
| `invoke_workflow` | `invoke_workflow {gen_ai.workflow.name}` | INTERNAL | `docs/gen-ai/gen-ai-agent-spans.md` |
| `plan` | `plan {gen_ai.agent.name}` | INTERNAL | `docs/gen-ai/gen-ai-agent-spans.md` |
| `execute_tool` | `execute_tool {gen_ai.tool.name}` | INTERNAL | `docs/gen-ai/gen-ai-spans.md` |
| `retrieval` | `retrieval {gen_ai.data_source.id}` | CLIENT | `docs/gen-ai/gen-ai-spans.md` |

The documents also define spans for embeddings, fetch response, memory, agent skills and command
execution. A span for an automatic retry covers the whole logical operation with all its retries.

### Key attributes

- **Operation:** `gen_ai.operation.name`, `gen_ai.provider.name`, `gen_ai.request.model`,
  `gen_ai.response.model`, `gen_ai.response.id`, `gen_ai.response.finish_reasons`.
- **Agent:** `gen_ai.agent.id`, `gen_ai.agent.name`, `gen_ai.agent.version`, `gen_ai.conversation.id`,
  `gen_ai.workflow.name`.
- **Tool:** `gen_ai.tool.name`, `gen_ai.tool.call.id`, `gen_ai.tool.type`, `gen_ai.tool.call.arguments`,
  `gen_ai.tool.call.result`, `gen_ai.tool.definitions`.
- **Usage:** `gen_ai.usage.input_tokens`, `gen_ai.usage.output_tokens`, `gen_ai.usage.cache_read.input_tokens`,
  `gen_ai.usage.cache_write.input_tokens`, `gen_ai.usage.reasoning.output_tokens`.
- **Content:** `gen_ai.system_instructions`, `gen_ai.input.messages`, `gen_ai.output.messages`.
- **Content references:** `gen_ai.system_instructions_ref`, `gen_ai.input.messages_ref`,
  `gen_ai.output.messages_ref`, `gen_ai.tool.definitions_ref`.

`gen_ai.system` is gone from the current registry, and `gen_ai.provider.name` replaces it. The names
`gen_ai.usage.prompt_tokens` and `gen_ai.usage.completion_tokens` are older forms.

### How content is recorded

The rules are in `docs/gen-ai/gen-ai-spans.md`, section "Capturing instructions, inputs, and outputs".

- **Default.** Instrumentations do not record content.
- **On attributes.** Content goes in `gen_ai.system_instructions`, `gen_ai.input.messages` and
  `gen_ai.output.messages`. A span holds them as JSON strings. An event holds them in structured form.
  JSON schemas define the message shape.
- **On events.** Inference content goes in the event `gen_ai.client.inference.operation.details`. The
  older events with one message per event are gone.
- **The switch.** The documents name the variable `OTEL_INSTRUMENTATION_GENAI_CAPTURE_CONTENT`, with the
  values `NO_CONTENT`, `SPAN_ONLY`, `EVENT_ONLY` and `SPAN_AND_EVENT`. But `opentelemetry-util-genai` 1.3b0
  reads `OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT`.
- **External storage.** An instrumentation can call a content hook with the messages before they become
  JSON. The hook can store them elsewhere and put a `*_ref` attribute on the span. The documents leave the
  URI scheme open, and they allow content-derived IDs.

The external storage pattern matches provrun's payload store, which names each payload by its SHA-256.

The context propagation rule changed on 2026-10-30. Instrumentations may pass `gen_ai.conversation.id` to
nested instrumentations in the same process. They should not pass other attributes. Source: `docs/gen-ai/README.md`
and pull request 556 in the same repository.

### Instrumentation packages

The GenAI instrumentations moved from `opentelemetry-python-contrib` to
`https://github.com/open-telemetry/opentelemetry-python-genai`. The copies in contrib get security fixes only.

| Package | Version | Instruments |
|---|---|---|
| `opentelemetry-instrumentation-genai-openai` | 1.3b0 | `openai >= 1.26.0` |
| `opentelemetry-instrumentation-genai-anthropic` | 1.3b0 | `anthropic >= 0.16.0` |
| `opentelemetry-instrumentation-genai-openai-agents` | 1.3b0 | `openai-agents >= 0.3.3` |
| `opentelemetry-util-genai` | 1.3b0 | Shared handler, content modes and `CompletionHook` |
| `opentelemetry-instrumentation-openai-v2` | 2.4b0 | The old contrib name, May 2026 |

`opentelemetry-instrumentation-anthropic` and `opentelemetry-instrumentation-openai` 0.62.4 belong to
Traceloop OpenLLMetry, `https://github.com/traceloop/openllmetry`. They are not OTel projects.

`opentelemetry-util-genai` defines `CompletionHook.on_completion(inputs, outputs, system_instruction,
tool_definitions, span, log_record)`. It loads the hook from the entry point group
`opentelemetry_genai_completion_hook`, named by `OTEL_INSTRUMENTATION_GENAI_COMPLETION_HOOK`. The wrapper
`_SafeCompletionHook` logs and drops any exception, so the hook observes only.

## 4. Other substrates

### Anthropic Python SDK 2.3.0

- **Built-in OTel.** The client makes one CLIENT span for each API call, named for example
  `anthropic.messages.create`, with `gen_ai.operation.name=chat`. It needs only `opentelemetry-api`. Spans
  are on by default when a tracer provider is registered.
- **Options.** The client takes `open_telemetry={"tracer_provider", "propagation", "traces",
  "conversation_id"}`. `traces` holds `enabled`, `content_mode` (`metadata_only` or `content`) and
  `max_content_bytes` (default 750,000). The environment variables start with `ANTHROPIC_OPEN_TELEMETRY`.
- **Propagation.** The SDK sends the trace context of the call span on each attempt, unless `propagation`
  is `False`.
- **Tool runner.** `messages.tool_runner` makes an `anthropic.messages.tool_runner` span with
  `invoke_agent`, and one `execute_tool` span for each tool.
- **Middleware.** `Anthropic(middleware=[...])` or `client.with_middleware(...)` takes `Middleware.handle`
  and `handle_async`. The chain runs once for each HTTP attempt, inside the retry loop. A middleware can
  return without calling `call_next`, or raise, so it can block.

Source: `anthropic/_tracing.py`, `anthropic/_middleware.py`, `anthropic/lib/_tracing/`, and the README in
the wheel metadata.

### OpenAI Python SDK 3.42.0

- **No built-in OTel.** A search for `opentelemetry` and `middleware` in the package finds nothing.
- **HTTP client.** The SDK now uses `httpx2` from Pydantic, `https://github.com/pydantic/httpx2`. A custom
  `http_client` with a transport or event hooks is the only hook in the SDK.
- **Instrumentation.** `opentelemetry-instrumentation-genai-openai` makes the spans. It observes only.

### OpenAI Agents SDK 0.24.0

- **Tracing processors.** `add_trace_processor(p)` adds a processor, and `set_trace_processors([...])`
  replaces the default exporter to OpenAI. A `TracingProcessor` has `on_trace_start`, `on_trace_end`,
  `on_span_start`, `on_span_end`, `shutdown` and `force_flush`. The provider catches each exception from a
  processor, so processors observe only.
- **Span data.** The types are `AgentSpanData`, `TaskSpanData`, `TurnSpanData`, `FunctionSpanData`,
  `GenerationSpanData`, `ResponseSpanData`, `HandoffSpanData`, `GuardrailSpanData`, `MCPListToolsSpanData`
  and the voice types.
- **Sensitive data.** `RunConfig.trace_include_sensitive_data` defaults to `True`, from
  `OPENAI_AGENTS_TRACE_INCLUDE_SENSITIVE_DATA`. `RunConfig` also takes `trace_id`, `group_id` and
  `trace_metadata`.
- **Blocking.** Tool input guardrails return `ToolGuardrailFunctionOutput.reject_content(...)` or
  `raise_exception`, so they can block. `RunHooks` and `AgentHooks` (`on_llm_start`, `on_tool_start` and
  others) are awaited in the run.
- **OTel.** The SDK traces are not OTel spans. `opentelemetry-instrumentation-genai-openai-agents` turns
  them into OTel spans through a processor.

Docs: `https://openai.github.io/openai-agents-python/tracing/`. Source: `agents/tracing/`,
`agents/tool_guardrails.py`, `agents/lifecycle.py`, `agents/run_config.py`.

## 5. Where to hook each substrate

| Substrate | Hook that nothing escapes | Blocks | Correlation with a provrun run ID |
|---|---|---|---|
| PydanticAI 2.x | A provrun capability with `wrap_run`, `wrap_model_request`, `wrap_tool_validate` and `wrap_tool_execute` | yes | `agent.run(run_id=...)`, then the span nests under the provrun span |
| LiteLLM SDK | provrun's own wrapper around `acompletion`, plus `async_pre_call_deployment_hook` per attempt | the wrapper and the deployment hook, yes | `litellm_session_id` or `litellm_trace_id`, `metadata`, and the active span |
| LiteLLM proxy | `async_pre_call_hook` for policy, the `otel` callback for spans | yes, proxy hooks | `traceparent` header, `metadata`, and `x-litellm-call-id` back |
| Anthropic SDK | Client middleware for each attempt, plus the built-in call span | middleware, yes | Active span and `open_telemetry.conversation_id` |
| OpenAI SDK | A custom `httpx2` transport | yes, at HTTP level | Active span, through the instrumentation package |
| OpenAI Agents SDK | `RunHooks` and tool guardrails for policy, a `TracingProcessor` for the record | guardrails and hooks, yes | `RunConfig(trace_id=..., group_id=run_id, trace_metadata={...})` |
| GenAI instrumentations | `CompletionHook` from `opentelemetry-util-genai` | no | The active span |

**Baggage.** Set `provrun.run_id` in OTel baggage at the start of a run. Add `BaggageSpanProcessor` from
`opentelemetry-processor-baggage` 0.66b2 to copy it onto every span, including spans from libraries.
Baggage also crosses HTTP when the W3C baggage propagator is on. The GenAI conventions only allow
`gen_ai.conversation.id` as a shared GenAI attribute, so keep the run ID in the `provrun.*` namespace.

## Recommendation for provevi ingest

1. **Keep two sources for each run, with clear trust.** provrun's own wrappers write the canonical
   evidence events, with confidence `observed`. Library spans are a second source, kept as an OTel file and
   imported as events with confidence `parsed`.
2. **PydanticAI.** Replace `EvidenceModel` and `EvidenceToolset` with one provrun capability:
   - `wrap_model_request` writes `model_called` for each step, streamed or not.
   - `prepare_model_request` notes each attempt.
   - `before_tool_validate` writes the raw call, valid or not.
   - `wrap_tool_execute` writes `tool_called` with arguments and result.
   - `before_tool_execute` is the policy gate. It raises `ApprovalRequired` or `SkipToolExecution`.
   - `get_wrapper_toolset` covers tools from other capabilities.

   Pass `run_id=<provrun run id>`. Turn on `Instrumentation(settings=InstrumentationSettings(version=5))`
   with a provevi tracer provider. Record `NativeToolCallPart` from responses as provider-run tool events.
3. **LiteLLM.** Keep the `acompletion` wrapper as the canonical record. Add a `CustomLogger` for two jobs.
   First, `async_pre_call_deployment_hook` writes one event for each attempt and applies policy. Second, a
   success hook adds `StandardLoggingPayload` as a `parsed` cross-check, with the cost and the cache fields.
   Pass `litellm_session_id=<run id>` and `metadata={"provrun_run_id": ...}`. Set
   `litellm.global_disable_no_log_param = True`. If the `otel` callback is on, set
   `USE_OTEL_LITELLM_REQUEST_SPAN=true`.
4. **LiteLLM proxy.** Do not run the 1.82.6 proxy, because of the CVEs above. If a proxy is needed, choose
   a reviewed version above 1.84.0, never 1.107.0, and pin it by hash. Ingest the proxy's OTel spans by
   `traceparent`.
5. **Anthropic and OpenAI SDKs.** Wrap the call in a provrun step, as for LiteLLM. Use Anthropic middleware
   or an `httpx2` transport for each attempt. Import the SDK spans as `parsed`.
6. **OpenAI Agents SDK.** Register a provevi `TracingProcessor` with `set_trace_processors`, so no trace
   goes to OpenAI by default. Use tool guardrails for policy. Set `trace_id` and `group_id` from the run.
7. **Content.** Keep content out of span attributes. Store it in the payload store and put the SHA-256 in
   the GenAI `*_ref` attributes. The GenAI conventions describe that pattern.
8. **Conventions version.** Keep the `gen_ai.*` names in one module, as `otel.py` does now. Change
   `gen_ai.system` to `gen_ai.provider.name`. Do not claim a schema URL until the new repository releases
   one.

## Issues

- **Streamed PydanticAI requests escape the evidence.** `EvidenceModel` overrides `request` only. The fix
  is a `wrap_model_request` capability.
- **Some PydanticAI tools escape the evidence.** `EvidenceToolset` wraps only the toolsets that provrun
  passes. Tools from a capability, provider-run native tools and output tools are not wrapped. The fix is
  `get_wrapper_toolset`, the tool hooks and `NativeToolCallPart` events.
- **A LiteLLM "otel" callback would write onto provrun's span.** In 1.82.6 it makes no span when a parent
  exists. It sets attributes and status OK on the parent, often after the parent ended. The fix is
  `USE_OTEL_LITELLM_REQUEST_SPAN=true`.
- **LiteLLM success callbacks can drop events.** The logging worker is best-effort. The fix is to keep
  provrun's wrapper as the canonical record.
- **The LiteLLM pin blocks every fix since March 2026.** The proxy CVEs above apply to 1.82.6. The SDK path
  that provrun uses is not named in them.
- **provrun pins semantic conventions 1.37.0.** The GenAI conventions now have no release. `otel.py` sets
  a schema URL for 1.37.0, which does not describe the current names.
- **Two names exist for the content switch.** The documents say `OTEL_INSTRUMENTATION_GENAI_CAPTURE_CONTENT`.
  `opentelemetry-util-genai` 1.3b0 reads `OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT`. Set both.

## Sources

- PydanticAI docs: `https://pydantic.dev/docs/ai/integrations/logfire/`,
  `https://pydantic.dev/docs/ai/capabilities/overview/`, `https://pydantic.dev/docs/ai/core-concepts/hooks/`.
- PydanticAI source: `https://github.com/pydantic/pydantic-ai`, wheel `pydantic_ai_slim-2.64.0`.
- LiteLLM docs: `https://docs.litellm.ai/docs/observability/custom_callback`,
  `https://docs.litellm.ai/docs/observability/opentelemetry_integration`,
  `https://docs.litellm.ai/docs/proxy/logging`, `https://docs.litellm.ai/docs/observability/mlflow`,
  `https://docs.litellm.ai/blog/tags/security`.
- LiteLLM source: `https://github.com/BerriAI/litellm`, wheels `litellm-1.82.6` and `litellm-1.106.6`.
- LiteLLM incident, from third parties, at `https://www.bitsight.com/blog/litellm-versions-1-82-7-1-82-8-supply-chain-compromise`,
  `https://www.netspi.com/blog/executive-blog/ai-ml-pentesting/litellm-supply-chain-compromise/`.
- OTel: `https://opentelemetry.io/docs/specs/semconv/gen-ai/`,
  `https://github.com/open-telemetry/semantic-conventions-genai`,
  `https://github.com/open-telemetry/opentelemetry-python-genai`,
  `https://github.com/open-telemetry/opentelemetry-python-contrib/tree/main/instrumentation-genai`.
- Anthropic SDK: wheel `anthropic-2.3.0`. OpenAI SDK: wheel `openai-3.42.0`.
- OpenAI Agents SDK: `https://openai.github.io/openai-agents-python/tracing/`, wheel `openai_agents-0.24.0`.
