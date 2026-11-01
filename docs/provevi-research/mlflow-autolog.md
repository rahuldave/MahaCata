# MLflow autolog: what it records, and how to keep it out of a run

Research date: 2026-11-01. The versions are MLflow 3.17.2, pydantic-ai 2.64.0, litellm 1.83.0, openai 3.41.0, anthropic 2.3.0 and opentelemetry-sdk 1.45.1.

These notes follow [MLflow tracing](mlflow-tracing.md), which describes the trace data model, the SQLite schema and OTLP ingest. These notes do not repeat that material.

The facts come from two sources:

- **Source.** Citations such as `pydantic_ai/autolog_v2.py:85` are relative to the MLflow 3.17.2 source tree. Citations that start with `litellm/` point into the litellm 1.83.0 package.
- **Tests.** The scripts were run in a scratch folder, and they are not in the repository. Each script runs with `./run.sh SCRIPT`, which builds a throwaway environment with the versions above. The scripts call no model and use no key. PydanticAI uses `FunctionModel` or `TestModel`, LiteLLM uses `mock_response`, and the OpenAI and Anthropic clients talk to an httpx `MockTransport` in `stubs.py`.

Terms used on this page:

- **Flavor.** A flavor is an MLflow integration module for one library, for example `mlflow.openai`. Each flavor has an `autolog()` function.
- **Autolog.** Autolog is MLflow's switch that makes a library write traces with no change to the calling code.
- **Patch.** A patch replaces a method on a class, so that MLflow code runs around the original method. MLflow keeps the original method on the class under another name, so that it can put it back.
- **Gorilla.** Gorilla is the small patching module in `utils/gorilla.py`. It marks each patched attribute.
- **Tracer provider.** A tracer provider is the OpenTelemetry (OTel) object that makes spans and sends them to span processors. A process has one global tracer provider. MLflow keeps a second, private one by default.
- **Callback.** A LiteLLM callback is a logger object or a name in one of the callback lists in the `litellm` module. LiteLLM calls it after each model call.
- **Span type.** The span type is MLflow's kind of span, for example `LLM`, `CHAT_MODEL`, `TOOL` or `AGENT`. It is in the attribute `mlflow.spanType`.

## Summary

- **pydantic-ai 2.64.0 uses `autolog_v2`.** It patches `Agent`, the `Instrumentation` capability and `MCPToolset`. It also patches `Agent.__init__` so that each new agent gets `instrument=True`. With that setting, PydanticAI also writes its own OTel spans to the global tracer provider.
- **LiteLLM autolog is a callback.** It adds `"mlflow"` to `litellm.success_callback` and `failure_callback`. LiteLLM then copies the logger into `_async_success_callback` and `_async_failure_callback`.
- **Disable leaves a LiteLLM leak.** `mlflow.litellm.autolog(disable=True)` and `mlflow.autolog(disable=True)` do not clean the two async lists. After a disable, each `litellm.acompletion` call still wrote a span. provrun's LiteLLM backend uses `acompletion`.
- **The OpenAI and Anthropic flavors change the request.** They add a W3C `traceparent` header to every call to the provider. The stub servers received the header on every call.
- **The generic switch does not undo a flavor switch.** After `mlflow.openai.autolog()`, a call to `mlflow.autolog(disable=True)` left the OpenAI patches on, and spans were still written.
- **`mlflow.tracing.disable()` is the strongest single switch.** It stopped every span and every header. But `mlflow.set_tracking_uri()` with a new URI turns tracing on again.
- **No environment variable turns autolog on.** Code must call `autolog()`. Other code can do it for us. For example, `mlflow.genai.evaluate()` turns tracing on for every flavor that is not disabled by name.
- **Storage adds attributes.** On write, MLflow adds `mlflow.spanType`, inputs, outputs, token usage, tools, model, provider and cost from GenAI semantic convention attributes. It also fills the token, cost and session columns of the trace.

## 1. How autolog switches on

### The common machinery

Every flavor goes through the same steps:

1. **Decorator.** `@autologging_integration(name)` wraps each `autolog()` (`utils/autologging_utils/__init__.py:385`). It stores the call's arguments in the global dict `AUTOLOGGING_INTEGRATIONS[name]` (line 420).
2. **Revert first.** The wrapper calls `revert_patches(name)` on every call, before anything else (line 431).
3. **Stop on disable.** If the flavor is not `"mlflow"` and `disable=True`, the wrapper returns (lines 436 to 437). The body of the flavor's `autolog()` does not run.
4. **Patch.** The body calls `safe_patch(flavor, cls, method, patch_function)` (`utils/autologging_utils/safety.py:232`). The patched method checks at each call whether the flavor is disabled, or whether `disable_autologging()` is active (lines 386 to 392). If so, it calls the original method.
5. **Record.** `_wrap_patch` (line 806) applies a `gorilla.Patch`, and `_store_patch` (line 825) records it in `_AUTOLOGGING_PATCHES[flavor]`.

Gorilla marks each patch on the class. It sets `_gorilla_active_patch_<name>` to the patch and `_gorilla_original_<name>` to the original (`utils/gorilla.py:62`, `65`, `apply` at 263). `revert_patches` (`safety.py:710`) reverts each stored patch and runs any cleanup callbacks in `_AUTOLOGGING_CLEANUP_CALLBACKS` (line 25).

Two flavors, `litellm` and `openai`, put the decorator on an inner `_autolog()` and keep work outside it. The source gives the reason. The decorated body does not run on disable, so the callback removal must sit outside (`litellm/__init__.py:68` to 71, `openai/autolog.py:113` to 116).

Each `autolog()` call also sends a usage event to MLflow's telemetry service through `_record_event(AutologgingEvent, ...)`, for example at `pydantic_ai/__init__.py:256`. Section 4 covers that service.

### `mlflow.autolog()`

`mlflow.autolog()` is in `tracking/fluent.py:3616`. It patches nothing by itself.

- **Library map.** It maps library names to flavors. `LIBRARY_TO_AUTOLOG_MODULE` (line 3759) lists the machine learning libraries. `GENAI_LIBRARY_TO_AUTOLOG_MODULE` (line 3777) lists `anthropic`, `openai`, `litellm`, `pydantic_ai` and 16 more.
- **Import hooks.** It registers a post-import hook for each library (line 3874). A hook runs the flavor's `autolog()` when the library is imported. It also runs at once for a library that is already imported.
- **Indirect imports.** In `generic_autolog.py`, `import pydantic_ai` turned on `pydantic_ai`. `pydantic_ai`'s import of `openai` turned on the `openai` flavor too.
- **Respect for a flavor call.** The hook skips a flavor whose config came from a direct flavor call (line 3844). So `mlflow.autolog(disable=True)` does not disable a flavor that someone turned on with `mlflow.openai.autolog()`.
- **Databricks.** In the Databricks runtime, a call with `disable=False` uses only the machine learning map. The GenAI flavors are left out (line 3804).

After `mlflow.autolog()`, `_post_import_hooks` held hooks for 32 libraries. The hooks stay after `mlflow.autolog(disable=True)`. They then call each flavor with `disable=True`.

### `mlflow.pydantic_ai.autolog()`

`autolog()` in `pydantic_ai/__init__.py:133` reads the installed version. For pydantic-ai 2.x at 2.5.0 or newer, it calls `setup_autologging()` from `autolog_v2.py` and returns (lines 145 to 163). For 2.x older than 2.5.0, it logs a warning and patches nothing. The 1.x code below line 165 does not run for 2.64.0.

`setup_autologging()` (`pydantic_ai/autolog_v2.py:422` to 476) applies these patches:

| Class | Method | Wrapper | Span |
|---|---|---|---|
| `Agent` | `__init__` | `patched_agent_init` (line 85) | none |
| `Agent` | `run` | `patched_agent_run` (line 93) | `Agent.run`, `AGENT` |
| `Agent` | `run_sync` | `patched_agent_run_sync` (line 105) | `Agent.run_sync`, `AGENT` |
| `Agent` | `run_stream` | `patched_agent_run_stream` (line 117), own wrapper | `Agent.run_stream`, `AGENT` |
| `Agent` | `run_stream_sync` | `patched_agent_run_stream_sync` (line 337), own wrapper | `Agent.run_stream_sync`, `AGENT` |
| `Instrumentation` | `wrap_model_request` | `patched_capability_model_request` (line 140) | `<ModelClass>.request`, `LLM` |
| `Instrumentation` | `on_tool_validate_error` | `patched_capability_tool_validate_error` (line 157) | `<tool>.validation`, `PARSER` |
| `Instrumentation` | `wrap_tool_execute` | `patched_capability_tool_execute` (line 184) | `<tool name>`, `TOOL` |
| `MCPToolset` | `list_tools` | `patched_mcp_list_tools` (line 210) | `MCPToolset.list_tools`, `TOOL` |
| `MCPToolset` | `direct_call_tool` | `patched_mcp_direct_call_tool` (line 221) | `MCPToolset.direct_call_tool`, `TOOL` |

`pa_autolog.py` printed the same set of gorilla markers after the call.

The `Agent.__init__` patch changes a global behaviour. `patched_agent_init` (line 85) sets `self.instrument = True` when the agent's `instrument` is `None`. The `Instrumentation` capability exists only for an instrumented agent. So the LLM and tool spans depend on that patch. Also, an instrumented agent writes PydanticAI's own OTel spans to the global tracer provider. `otel_interplay.py` showed both effects:

- **Our provider gets spans.** With a global SDK tracer provider in place, autolog made our exporter receive `invoke_agent agent` and `chat test`. These spans carry `gen_ai.input.messages`, `gen_ai.output.messages` and `pydantic_ai.all_messages`. Without autolog, the same agent wrote no PydanticAI span.
- **Old agents stay instrumented.** After `mlflow.pydantic_ai.autolog(disable=True)`, an agent that was built while autolog was on still wrote PydanticAI spans. A new agent did not.

An agent with `instrument = False` set after construction gets only the `Agent.run` span (`instrument_false.py`). In pydantic-ai 2.64.0, `instrument` is a property and not an `Agent()` argument.

### `mlflow.litellm.autolog()`

`mlflow.litellm.autolog()` (`litellm/__init__.py:13` to 61) does three things when it is on:

- **Callbacks.** It adds the string `"mlflow"` to `litellm.success_callback` and `litellm.failure_callback` (lines 45 to 47).
- **Executor patch.** It patches `submit` on the LiteLLM thread pool executor instance (lines 81 to 107). The patched `submit` runs a success handler at once, in the calling thread, in place of the pool.
- **The logger.** LiteLLM resolves `"mlflow"` to `litellm.integrations.mlflow.MlflowLogger`. The logger, and not MLflow, writes the span (`litellm/integrations/mlflow.py`).

On the first call, LiteLLM replaces the strings with `MlflowLogger` objects. It also adds the logger to `_async_success_callback` and `_async_failure_callback` (seen in `litellm_autolog.py`). On disable, MLflow removes MLflow loggers from `success_callback`, `failure_callback` and `callbacks` only (lines 52 to 57). The async lists keep the logger.

### `mlflow.openai.autolog()`

`mlflow.openai.autolog()` (`openai/autolog.py:33` to 106) calls the decorated `_autolog()` (line 117). That function patches `create` on these classes, each sync and async:

- **Chat completions.** `chat.completions.Completions` and `AsyncCompletions`, also `parse`.
- **Other APIs.** `completions.Completions`, `embeddings.Embeddings` and `images.Images`, where `generate` is the patched method.
- **Responses.** `responses.Responses` and `AsyncResponses`, also `parse`.
- **Beta parse.** `beta.chat.completions` `parse`, if the module exists. openai 3.41.0 has no such module.

`openai_anthropic_autolog.py openai` printed 14 patched methods. The wrappers are `patched_call` and `async_patched_call` (line 257).

If the OpenAI Agents SDK is installed, `autolog()` also patches `AgentRunner.run` and `run_streamed` (lines 86 to 87). That code sits outside the decorated function, so it runs even on `disable=True`. With the default `disable_openai_agent_tracer=True`, it calls `clear_trace_processors()` (line 99). That call removes the Agents SDK's own trace processors. The workspace does not install the Agents SDK, so I did not test this path.

### `mlflow.anthropic.autolog()`

`mlflow.anthropic.autolog()` (`anthropic/__init__.py:16` to 65) patches `Messages.create` and `AsyncMessages.create`. If `claude_agent_sdk` is installed, it also patches `ClaudeSDKClient.__init__` (line 55). That patch replaces `query` and `receive_response` on each client object (`anthropic/autolog.py:21` to 72). The docstring says that streaming is not recorded, and `messages.stream` is not patched.

### The `traceparent` header

The OpenAI and Anthropic wrappers add a W3C `traceparent` header to the request (`openai/autolog.py:526`, `anthropic/autolog.py:145`). The header holds the MLflow trace ID and span ID (`tracing/distributed/__init__.py:71` to 82). The stub servers received `traceparent: 00-<trace id>-<span id>-03` on every call. So autolog changes what goes to the model provider, and it gives the provider an ID that joins calls into one trace. The Gemini, Groq and Mistral flavors do the same.

### `mlflow.otel.autolog()`

`mlflow.otel.autolog()` (`otel/__init__.py:79` to 145) is a different switch. It is not in `mlflow.autolog()`'s map. It sets a global SDK tracer provider if there is none, and it adds an OTLP exporter to it. Every OTel span in the process then goes to the MLflow server. A disable only turns the processor off. The processor stays on the provider.

## 2. What each flavor records

The scripts wrote to `sqlite:///<scratch>/<name>.db` and printed every key of `spans.content`. The script `helpers.py` holds the dump code.

### What every trace gets

Every stored span has the keys `trace_id`, `span_id`, `parent_span_id`, `name` and `status` in `content`. It also has `start_time_unix_nano`, `end_time_unix_nano`, `attributes`, `events` and `links`. There is no `kind` key.

Every span has these attributes:

- `mlflow.traceRequestId`, the MLflow trace ID in the form `tr-<32 hex>`.
- `mlflow.spanType`, a JSON string such as `"LLM"`.
- `mlflow.spanLogLevel`, which was `20` in every run.
- `mlflow.spanInputs` and `mlflow.spanOutputs`, as JSON.

Each attribute value is JSON encoded, so a string value keeps its quotes.

Each trace also gets rows outside `spans`:

- **Metadata.** The table `trace_request_metadata` holds `mlflow.traceInputs` and `mlflow.traceOutputs`, cut to a preview length. It also holds `mlflow.trace_schema.version` (`3`), `mlflow.trace.sizeBytes`, `mlflow.trace.sizeStats` and `mlflow.trace.infoFinalized`. Last come `mlflow.source.name`, `mlflow.source.type`, `mlflow.source.git.*` and `mlflow.user`, which is the login name.
- **Tags.** The table `trace_tags` holds `mlflow.trace.spansLocation` and `mlflow.artifactLocation`.
- **Columns.** The table `trace_info` holds `request_preview` and `response_preview`. It also holds the totals `input_tokens`, `output_tokens`, `total_tokens`, `cache_read_input_tokens` and `total_cost`.

### `pydantic_ai`

`pa_autolog.py` ran an agent named `adder` on a `FunctionModel`. The model called the tool `add` once and then answered. The span tree:

```
Agent.run                 AGENT  (root)
  FunctionModel.request   LLM    (first model call, returns the tool call)
  add                     TOOL   (tool call)
  FunctionModel.request   LLM    (second model call, returns the answer)
```

The tool span is a sibling of the model spans. It is not a child of the model call that asked for it.

**`Agent.run`** (`autolog_v2.py:59` and 93):

- **Format.** `mlflow.message.format` is `"pydantic_ai"`.
- **Agent fields.** Every public, serializable field of the agent becomes an attribute, through `extract_safe_attributes` (`pydantic_ai/utils.py:23`). The run gave `name`, `deps_type`, `end_strategy`, `instrument`, `model`, `output_type`, `root_capability` and `toolsets`. `model` is the model object's `__dict__`, which held a function `repr` in this run.
- **Inputs.** `mlflow.spanInputs` holds the bound call arguments without `self` and `deps`, for example `{"user_prompt": "What is 2+3?"}` (line 45).
- **Outputs.** `mlflow.spanOutputs` holds `dataclasses.asdict(result)` and adds `_new_messages_serialized`, a list of the new messages (`pydantic_ai/utils.py:52`). The result also holds the full private `_state`, with the message history and the tool schemas. The stored span was 10.7 KB for a two-step run.
- **Usage.** `mlflow.chat.tokenUsage` is the run total.

**`<ModelClass>.request`** (`autolog_v2.py:65` and 140):

- **Name.** The name is the class name of the model, for example `FunctionModel.request`, `OpenAIChatModel.request` or `TestModel.request`.
- **Model fields.** Every public field of the model becomes an attribute, for example `model_name`, `model_id`, `system`, `canonical_system`, `label` and `batch_max_requests`.
- **Model and provider.** `mlflow.llm.model` is `model.model_name`. `mlflow.llm.provider` is `model.system`, for example `"openai"` or `"function"`.
- **Inputs.** `mlflow.spanInputs` is `{"messages", "model_settings", "model_request_parameters"}` (`pydantic_ai/utils.py:41`). The messages are PydanticAI `ModelRequest` and `ModelResponse` objects as dicts, with `part_kind` on each part. `model_request_parameters` holds the tool definitions.
- **Outputs.** `mlflow.spanOutputs` is the `ModelResponse` as a dict.
- **Usage.** `mlflow.chat.tokenUsage` is described below.

**`<tool name>`** (`autolog_v2.py:184`):

- **Inputs.** `mlflow.spanInputs` is the tool arguments, for example `{"a": 2, "b": 3}`.
- **Outputs.** `mlflow.spanOutputs` is the return value, for example `5`.
- **Other.** The span has no other attributes. It has no tool call ID.

The token usage comes from `parse_usage` (`pydantic_ai/utils.py:75`). It maps `input_tokens` to `input_tokens` and `output_tokens` to `output_tokens`, and it computes `total_tokens`. It adds `cache_read_input_tokens` and `cache_creation_input_tokens` only when they are not zero. The function calls `usage()` as a method, and pydantic-ai 2.64.0 warns that `usage` is a property.

### `openai`

`openai_anthropic_autolog.py openai` made a chat call with a tool, an async chat call with the tool result, and a Responses call. Each call became one root span, so each call is its own trace:

| Span name | Type | Call |
|---|---|---|
| `Completions` | `CHAT_MODEL` | `chat.completions.create` |
| `AsyncCompletions` | `CHAT_MODEL` | async `chat.completions.create` |
| `Responses` | `CHAT_MODEL` | `responses.create` |

The span name is the class name of the resource (`openai/autolog.py:303`). The span type comes from `_get_span_type` (line 172). The legacy completions API gives `LLM`, embeddings give `EMBEDDING` and images give `TOOL`.

The attributes:

- **Inputs.** `mlflow.spanInputs` is the full keyword arguments of the call, for example `model`, `messages`, `tools` and `temperature`. The messages are in the OpenAI chat format.
- **Plain keys.** Every argument except `messages` and `input` is also a plain attribute, for example `model`, `tools`, `temperature` and `instructions`.
- **Format.** `mlflow.message.format` is `"openai"` for chat and LLM spans.
- **Outputs.** `mlflow.spanOutputs` is the `ChatCompletion` or `Response` object as a dict.
- **Tools.** `mlflow.chat.tools` is a list in the form `{"type": "function", "function": {"name", "description", "parameters"}}` (`openai/utils/chat_schema.py:26`).
- **Model.** `mlflow.llm.model` comes from the response. If the response has no model, it comes from the request.
- **Usage.** `mlflow.chat.tokenUsage` maps `prompt_tokens` and `completion_tokens` to `input_tokens` and `output_tokens`. It maps `prompt_tokens_details.cached_tokens` to `cache_read_input_tokens`.
- **Cost.** `mlflow.llm.cost` is added on storage, as section 5 says.

The flavor does not set `mlflow.llm.provider`. For a stream, the flavor adds one span event per chunk, and it builds the output from the chunks (`openai/autolog.py:332`).

### `anthropic`

`openai_anthropic_autolog.py anthropic` made a sync call that returned a `tool_use` block, and an async call with the `tool_result`. The spans were `Messages.create` and `AsyncMessages.create`, both `CHAT_MODEL` and both roots. The name is `<class>.<method>` (`anthropic/autolog.py:120`).

The attributes:

- **Inputs and outputs.** `mlflow.spanInputs` is the full keyword arguments, including `system`, `messages`, `tools` and `max_tokens`. `mlflow.spanOutputs` is the `Message` as a dict. There are no plain attribute copies of the arguments.
- **Format.** `mlflow.message.format` is `"anthropic"`.
- **Tools.** `mlflow.chat.tools` holds the tools, converted to the OpenAI form above (`anthropic/chat.py`).
- **Model and provider.** `mlflow.llm.model` comes from the request `model`. `mlflow.llm.provider` is always `"anthropic"` (line 140).
- **Usage.** `mlflow.chat.tokenUsage` has `input_tokens`, `output_tokens`, `total_tokens`, `cache_read_input_tokens` and `cache_creation_input_tokens` (line 182).

The flavor adds the cache tokens into `input_tokens` and `total_tokens` (lines 204 to 206). The stub returned `input_tokens=15`, `cache_read_input_tokens=4` and `cache_creation_input_tokens=1`. The span recorded `input_tokens=20`. A rebuild from Anthropic's raw usage must do the same sum to match.

### `litellm`

`litellm_autolog.py` made an `acompletion` call with tools and a `completion` call. Each became one root span, named `litellm-<call_type>`, for example `litellm-acompletion` (`litellm/integrations/mlflow.py:247`). The type is `LLM`.

The attributes (`litellm/integrations/mlflow.py:167` to 225):

- **Inputs.** `mlflow.spanInputs` is `{"messages", "tools"}` in the OpenAI format. Optional `functions`, `stream`, `tool_choice`, `user` and `prediction` are added when they are set.
- **Outputs.** `mlflow.spanOutputs` is the LiteLLM `ModelResponse` as a dict.
- **Call fields.** The plain attributes `litellm_call_id`, `call_type`, `model`, `api_base` and `cache_hit` come from the call.
- **Logging payload.** `raw_llm_response`, `response_cost`, `saved_cache_cost` and `request_tags` come from LiteLLM's standard logging payload. `request_tags` came from `metadata={"tags": [...]}`.
- **Usage.** `mlflow.chat.tokenUsage` holds `input_tokens`, `output_tokens` and `total_tokens` only. It has no cache keys.
- **Start time.** `mlflow.spanStartTimeNs` holds the start time, because the logger starts the span after the call with an explicit start time.

The logger tries to set chat messages and tools through `mlflow.tracing.utils.set_span_chat_messages`. That function does not exist in 3.17.2, so the logger skips the step in silence (`litellm/integrations/mlflow.py:54` to 59). So LiteLLM spans have no `mlflow.chat.tools` and no `mlflow.message.format`. The storage step added `mlflow.llm.model` from the output, and `mlflow.llm.cost` where LiteLLM knew the price. The span has no `mlflow.llm.provider`.

The async logger runs on LiteLLM's logging worker. In a script that ends the event loop at once, the worker did not run. Then the span was lost, with the warning "coroutine 'Logging.async_success_handler' was never awaited". The scripts wait 1.5 seconds after each `acompletion`.

### PydanticAI on an OpenAI model, with `mlflow.autolog()`

`generic_autolog.py` turned on `mlflow.autolog()` and ran a PydanticAI agent on `OpenAIChatModel` with the stub client. Two flavors wrote spans into one tree:

```
Agent.run                    AGENT       format pydantic_ai
  OpenAIChatModel.request    LLM         format pydantic_ai, provider openai
    AsyncCompletions         CHAT_MODEL  format openai, no provider
  add                        TOOL
  OpenAIChatModel.request    LLM
    AsyncCompletions         CHAT_MODEL
```

Each model call is recorded twice, once in PydanticAI's message format and once in OpenAI's format. Both spans have `mlflow.chat.tokenUsage`. The trace total was `input_tokens=24` and `output_tokens=8`, which is two calls of 12 and 4. So the roll up counted each call once, and it did not add the root's own total either.

## 3. Paths that switch autolog on without our code

I found these paths. Each one needs some code to run in the process, but that code need not be ours.

| Path | Where | What it turns on |
|---|---|---|
| A direct call to `mlflow.<flavor>.autolog()` or `mlflow.autolog()` by any imported package | anywhere | the flavor, or hooks for 32 libraries |
| Post-import hooks left by an earlier `mlflow.autolog()` | `tracking/fluent.py:3874` | a flavor at the moment its library is imported, also by another library |
| `mlflow.genai.evaluate()` | `genai/evaluation/base.py:354`, `models/evaluation/utils/trace.py:28` to 86 | tracing for every supported flavor whose config is not `disable=True`, during the evaluation |
| Databricks runtime start | outside MLflow, documented at `tracking/fluent.py:3801` to 3806 | the machine learning flavors only, not the GenAI flavors |
| `mlflow.otel.autolog()` | `otel/__init__.py` | an OTLP exporter on the global tracer provider |
| LiteLLM proxy configuration, `litellm_settings: success_callback: ["mlflow"]` | LiteLLM proxy | the LiteLLM logger, with no MLflow flavor call and no MLflow config |
| `litellm.success_callback`, `litellm.callbacks` or the per-call `success_callback=` argument | any code | the LiteLLM logger, which writes to MLflow's current tracking URI |
| `mlflow autolog claude` | MLflow CLI | Claude Code hooks, outside our process |

These checks found no other path:

- **Environment.** No environment variable turns autolog on. The only `AUTOLOG` variable in `environment_variables.py` is `MLFLOW_AUTOLOGGING_TESTING` (line 602), which changes error handling.
- **Import.** `import mlflow` turns nothing on. The audit after the import showed no config, no hook and no patch.
- **Entry points and startup files.** The `mlflow` 3.17.2 distribution has no `opentelemetry_instrumentor` entry point and no `.pth` file. Its entry points are `console_scripts`, `mlflow.app`, `mlflow.app.client` and `mlflow.deployments`.
- **The workspace.** Only `litellm/integrations/mlflow.py` refers to MLflow in the workspace's installed packages. I found no LiteLLM environment variable that adds callbacks in the SDK. The proxy reads them from its configuration file.

Two related paths are outside MLflow. `Agent.instrument_all()` in pydantic-ai, and `logfire.instrument_pydantic_ai()`, turn on PydanticAI's own OTel spans. The `opentelemetry-instrument` wrapper loads `opentelemetry_instrumentor` entry points. The workspace has one, for httpx.

## 4. Suppression

### Test results

`suppress.py SCENARIO` runs one scenario in a fresh process. The workload is a PydanticAI agent on the OpenAI stub with one tool call, one `litellm.acompletion` with `mock_response`, and one Anthropic call. A full run with everything on gives 8 spans and 3 `traceparent` headers.

| Scenario | Patches after the switch | Spans written after the switch | Headers |
|---|---|---|---|
| `baseline`: generic and four flavor switches on | 26 methods, and the LiteLLM executor | 8 | 3 of 3 |
| `flavor_disable`: each flavor `autolog(disable=True)` | none | 1, `litellm-acompletion` | 0 of 3 |
| `global_disable_after_global`: `mlflow.autolog()`, then `mlflow.autolog(disable=True)` | none | 1, `litellm-acompletion` | 0 of 3 |
| `global_disable_after_flavor`: `mlflow.openai.autolog()`, then `mlflow.autolog(disable=True)` | 14 OpenAI methods | 2, `AsyncCompletions` | 2 of 3 |
| `tracing_disable`: everything on, then `mlflow.tracing.disable()` | all, still applied | 0 | 0 of 3 |
| `tracing_disable_then_set_uri`: as above, then `mlflow.set_tracking_uri(other)` | all | 8 | 3 of 3 |
| `disable_autologging_ctx`: everything on, workload inside `disable_autologging()` | all | 1, `litellm-acompletion` | 0 of 3 |
| `sampling_zero`: everything on, with `MLFLOW_TRACE_SAMPLING_RATIO=0` | all | 0 | 3 of 3 |
| `recipe`: everything on, then `guard.suppress_all()` | none | 0 | 0 of 3 |

### What each switch does

- **`mlflow.<flavor>.autolog(disable=True)`.** It reverts the flavor's gorilla patches at once, and it records `disable=True`. Later hooks and `mlflow.genai.evaluate()` respect that record. It does not reset `instrument=True` on agents that exist already. For LiteLLM, it leaves `MlflowLogger` in the async lists.
- **`mlflow.autolog(disable=True)`.** It disables flavors that `mlflow.autolog()` turned on. It does not touch a flavor that a direct flavor call turned on (`tracking/fluent.py:3844`). It leaves the import hooks in place, set to disable.
- **`mlflow.tracing.disable()`.** It replaces MLflow's tracer provider with a `NoOpTracerProvider` (`tracing/provider.py:938` to 976). The patches stay, but each patched call makes a no-op span, so no span is stored and no header is sent. The LiteLLM logger also writes nothing, because it uses the same provider.
- **`disable_autologging()`.** The context manager (`utils/autologging_utils/__init__.py:544`) sets a module flag that `safe_patch` and the PydanticAI v2 wrappers read. The LiteLLM logger does not read it.
- **`MLFLOW_TRACE_SAMPLING_RATIO=0`.** The sampler drops every root span (`tracing/provider.py:744`). The flavors still make a span context and still send the header.
- **No environment variable disables tracing.** The tracing variables in `environment_variables.py` set the destination, sampling, export and buffers. None of them is an off switch.

### Ways to turn MLflow on again

- **A new tracking URI.** `mlflow.set_tracking_uri()` with a new URI calls `tracing.provider.reset()` (`tracking/_tracking_service/utils.py:108` to 121). The next span builds a real provider again.
- **Other calls.** `mlflow.tracing.enable()` and `mlflow.tracing.reset()` turn tracing on again (`tracing/provider.py:979`, 1083).
- **A flavor call.** Any `mlflow.<flavor>.autolog()` patches again. `mlflow.genai.evaluate()` turns on every flavor that is not disabled by name.
- **LiteLLM lists.** Any code can add a logger to a LiteLLM callback list.

### The shared tracer provider mode

The default is `MLFLOW_USE_DEFAULT_TRACER_PROVIDER=true`. MLflow then keeps a private tracer provider and does not touch the global one. `shared_provider.py` set a global provider first, as provrun's `otel.py` expects, and then set the variable to `false`. It found three effects:

1. **MLflow joins our provider.** MLflow added `MlflowV3SpanProcessor` to our provider (`tracing/provider.py:667` to 683). A span from the `provrun` tracer was then stored in the MLflow database, and MLflow spans went to our exporter.
2. **A new URI removes our provider.** `mlflow.set_tracking_uri(other)` set the global provider back to a `ProxyTracerProvider` (`tracing/provider.py:197` to 205). Our provider was gone.
3. **Disable removes it too.** `mlflow.tracing.disable()` set the global provider to a `NoOpTracerProvider`. That stops every OTel span in the process, and provrun's spans with them.

So provrun must keep the variable at `true`.

### MLflow's usage telemetry

MLflow sends usage events, such as each `autolog()` call, to `config.mlflow-telemetry.io` and the ingestion URL that it names (`telemetry/constant.py:9`, `telemetry/client.py:274`). `MLFLOW_DISABLE_TELEMETRY=true` or `DO_NOT_TRACK=true` turns it off (`telemetry/utils.py:144` to 161). MLflow makes the decision once, at import (`mlflow/__init__.py:456`). `telemetry_switch.py` showed that a variable set after the import has no effect until a call to `mlflow.telemetry.client.set_telemetry_client()`.

### The recipe

`guard.py` holds a sketch of the recipe as `suppress_all()`. It passed the `recipe` scenario. The steps:

1. **Set the environment.** Set `MLFLOW_USE_DEFAULT_TRACER_PROVIDER=true`, `MLFLOW_DISABLE_TELEMETRY=true` and `MLFLOW_TRACE_SAMPLING_RATIO=0`, before MLflow is imported if possible. If MLflow is imported already, call `set_telemetry_client()`.
2. **Disable by name.** Call `mlflow.autolog(disable=True)`, and then `mlflow.<flavor>.autolog(disable=True, silent=True)` for each flavor. The named calls revert patches that a flavor call made. They also record `disable=True`, so that `mlflow.genai.evaluate()` and import hooks leave the flavor off.
3. **Turn off MLflow's provider.** Call `mlflow.tracing.disable()`. It stops spans and headers even if a patch comes back.
4. **Clean LiteLLM.** Remove every logger that configuration does not approve from all seven lists. The lists are `input_callback`, `success_callback`, `failure_callback`, `service_callback`, `callbacks`, `_async_success_callback` and `_async_failure_callback`.
5. **Pin PydanticAI.** Set `agent.instrument = False` after `Agent(...)`, unless PydanticAI's own OTel spans are approved. The patched `__init__` sets `True` only when the value is `None`.

Approved telemetry fits into step 4 and step 5. A LiteLLM logger whose name is in the approved list stays. An approved PydanticAI instrumentation gets an explicit `InstrumentationSettings` with provrun's tracer provider.

### Detection

`guard.check()` returns a list of problems. It reads these values:

- `AUTOLOGGING_INTEGRATIONS`, for any flavor with `disable` false.
- `_AUTOLOGGING_PATCHES`, for any live patch.
- `mlflow.tracing.provider.is_tracing_enabled()`.
- The seven LiteLLM lists, for loggers that are not approved.
- `MLFLOW_USE_DEFAULT_TRACER_PROVIDER`.

The check takes about 5 microseconds. It reads `sys.modules` and imports nothing. `guard_reenable.py` showed that it found the re-enable by `set_tracking_uri()`. The `recipe` scenario showed that it found a later `mlflow.openai.autolog()` and `mlflow.litellm.autolog()`.

`audit.py` holds a wider check for reports. It scans every loaded module for gorilla markers, and it also finds patches on objects, such as LiteLLM's executor. It took 650 milliseconds over 8,800 modules, so it suits the start and the end of a run.

Run `guard.check()` before each model call and each tool call. Run the full audit at the start and the end of a run. If a check finds a problem, record it as evidence and apply the recipe again. A library that patches through `wrapt` does not leave gorilla markers. To find it, compare the method objects with a list taken at start, or look for `__wrapped__`.

## 5. What MLflow adds when it stores a span

### The translation step

`SqlAlchemyStore._log_spans_once` (`store/tracking/sqlalchemy_store.py:5702`) calls `translate_span_when_storing(span)` for each span (line 5729). The in-process SQLite path uses the same code, so autolog spans pass through it too. The function is in `tracing/otel/translation/__init__.py:60` to 149. It adds an attribute only when the attribute is missing:

| Attribute | Source |
|---|---|
| `mlflow.spanType` | the span kind attribute of the first translator that knows it. It replaces only a missing type or `UNKNOWN` |
| `mlflow.spanInputs` | the translators' input keys, then the span events |
| `mlflow.spanOutputs` | the translators' output keys, then the span events |
| `mlflow.chat.tokenUsage` | the translators' token keys. `total_tokens` is the sum when it is missing |
| `mlflow.message.format` | a translator that knows a format |
| `mlflow.chat.tools` | the translators' tool definition keys |
| `mlflow.llm.model` | the translators' model keys, then `model` in the inputs, then `model` in the outputs |
| `mlflow.llm.provider` | the translators' provider key |
| `mlflow.llm.cost` | `litellm.cost_per_token` on the model, the provider and the usage (`tracing/utils/__init__.py:364`) |

MLflow tries the translators in the order of the list `_TRANSLATORS` (line 37). OpenInference comes first, then Gemini CLI, Claude Code and GenAI. Spring AI, Traceloop, Google ADK, Vercel AI, VoltAgent, LiveKit, Laminar and Langfuse follow.

The GenAI translator (`tracing/otel/translation/genai_semconv.py:26` to 59) reads these keys:

| MLflow attribute | GenAI semantic convention key |
|---|---|
| `mlflow.spanType` | `gen_ai.operation.name`: `chat` to `CHAT_MODEL`, `text_completion`, `generate_content` and `response` to `LLM`, `embeddings` to `EMBEDDING`, `execute_tool` to `TOOL`, `invoke_agent` and `create_agent` to `AGENT` |
| `input_tokens` | `gen_ai.usage.input_tokens` |
| `output_tokens` | `gen_ai.usage.output_tokens` |
| `cache_read_input_tokens` | `gen_ai.usage.cache_read.input_tokens` |
| `cache_creation_input_tokens` | `gen_ai.usage.cache_write.input_tokens`, or the older `gen_ai.usage.cache_creation.input_tokens` |
| `mlflow.spanInputs` | `gen_ai.input.messages`, then `gen_ai.tool.call.arguments`, then the events `gen_ai.system.message`, `gen_ai.user.message` and `gen_ai.assistant.message` |
| `mlflow.spanOutputs` | `gen_ai.output.messages`, then `gen_ai.tool.call.result`, then the event `gen_ai.choice` |
| `mlflow.llm.model` | `gen_ai.response.model`, then `gen_ai.request.model` |
| `mlflow.llm.provider` | `gen_ai.provider.name` |
| `mlflow.chat.tools` | `gen_ai.tool.definitions` |

### A test with PydanticAI's own spans

`translate.py` ran an instrumented PydanticAI agent with no autolog. It sent the spans through OTLP protobuf, `Span.from_otel_proto` and `SqlAlchemyStore.log_spans`, which is the path of the server's `/v1/traces`. MLflow added these attributes:

- **`chat gpt-4o-mini`.** It got `mlflow.spanType` `CHAT_MODEL`, `mlflow.spanInputs` and `mlflow.spanOutputs` in the GenAI message format with `role` and `parts`, `mlflow.chat.tokenUsage` with cache reads, `mlflow.chat.tools`, `mlflow.llm.model`, `mlflow.llm.provider` and `mlflow.llm.cost`.
- **`execute_tool add`.** It got `TOOL`, with inputs `{"a":2,"b":3}` and output `"5"`.
- **`invoke_agent adder`.** It got only `AGENT`. The run total in `gen_ai.aggregated_usage.*` is not read, so the root span has no usage.

No span got `mlflow.message.format`, because the GenAI translator names no format. The trace row got `input_tokens=31`, `output_tokens=8`, `cache_read_input_tokens=2`, a `total_cost` and a `session_id` from `gen_ai.conversation.id`. It got no `trace_name` and no previews. As `mlflow-tracing.md` found, the `spans.type` column stayed `NULL`, but `model_name` and `model_provider` were filled.

### Roll ups on the trace

- **Processor.** When the root span ends, MLflow's span processor writes `mlflow.traceInputs` and `mlflow.traceOutputs` from the root span. It also writes `mlflow.trace.tokenUsage`, the sum of usage over the spans (`tracing/processor/base_mlflow.py:342` to 375).
- **Client cost.** It writes `mlflow.trace.cost` only for backends that need the cost from the client, such as Databricks (`tracing/utils/__init__.py:943`).
- **Store.** For SQL, the store computes the totals, the cost columns, the session ID from `session.id` or `gen_ai.conversation.id`, and the user from `user.id` (`sqlalchemy_store.py:5715` to 5790). In the SQLite runs, the totals were in the `trace_info` columns.

### Export in GenAI format

With `MLFLOW_ENABLE_OTEL_GENAI_SEMCONV=true`, OTLP export runs `translate_span_to_genai` (`tracing/export/genai_semconv/translator.py:46` to 137). It does the reverse mapping:

- **Span type.** `mlflow.spanType` becomes `gen_ai.operation.name`, with `CHAT_MODEL` to `chat`, `LLM` to `generate_content`, `EMBEDDING` to `embeddings`, `TOOL` to `execute_tool` and `AGENT` to `invoke_agent`.
- **Model and usage.** `mlflow.llm.model` becomes `gen_ai.request.model`, and `mlflow.llm.provider` becomes `gen_ai.provider.name`. The usage becomes `gen_ai.usage.input_tokens` and `output_tokens`.
- **Tools and agents.** A `TOOL` span gets `gen_ai.tool.name`, `gen_ai.tool.call.arguments` and `gen_ai.tool.call.result`. An `AGENT` span gets `gen_ai.agent.name`.
- **Messages.** A converter for each `mlflow.message.format` writes `gen_ai.input.messages`, `gen_ai.output.messages` and `gen_ai.system_instructions`. It also writes `gen_ai.request.temperature`, `max_tokens`, `top_p`, `stop_sequences`, `gen_ai.response.id` and `gen_ai.response.model` (`tracing/export/genai_semconv/converter.py`). The formats are `openai`, `anthropic`, `gemini` and `bedrock`. Any other format, `pydantic_ai` included, uses the OpenAI chat converter.
- **Names and cleanup.** The span name becomes `<operation> <tool, agent or model>`, for example `chat gpt-4o-mini`. Other `mlflow.*` attributes are dropped, except `mlflow.traceTag.*`.

### Names for a provevi trace database

A provevi trace database can use these names, so that MLflow and OTel tools read the same columns. The keys are in `tracing/constant.py:56` to 157:

| Meaning | MLflow name | GenAI name |
|---|---|---|
| Span type | `mlflow.spanType`: `AGENT`, `LLM`, `CHAT_MODEL`, `TOOL`, `EMBEDDING`, `PARSER`, `UNKNOWN` | `gen_ai.operation.name` |
| Inputs, outputs | `mlflow.spanInputs`, `mlflow.spanOutputs` | `gen_ai.input.messages`, `gen_ai.output.messages`, `gen_ai.tool.call.arguments`, `gen_ai.tool.call.result` |
| Message format | `mlflow.message.format`: `pydantic_ai`, `openai`, `anthropic` | none |
| Token usage | `mlflow.chat.tokenUsage` with `input_tokens`, `output_tokens`, `total_tokens`, `cache_read_input_tokens`, `cache_creation_input_tokens`, `cache_creation_input_tokens_above_1hr` | `gen_ai.usage.*` |
| Model, provider | `mlflow.llm.model`, `mlflow.llm.provider` | `gen_ai.request.model`, `gen_ai.response.model`, `gen_ai.provider.name` |
| Cost | `mlflow.llm.cost` with `input_cost`, `output_cost`, `total_cost` in USD | none |
| Tools offered | `mlflow.chat.tools`, OpenAI function form | `gen_ai.tool.definitions` |
| Session, user | trace metadata `mlflow.trace.session`, `mlflow.trace.user`. Span attributes `session.id`, `user.id` | `gen_ai.conversation.id` |
| Trace totals | `mlflow.trace.tokenUsage`, `mlflow.trace.cost`, `mlflow.traceInputs`, `mlflow.traceOutputs` | none |

The provrun module `otel.py` already writes `gen_ai.operation.name`, `gen_ai.request.model`, `gen_ai.tool.name`, `gen_ai.usage.input_tokens`, `gen_ai.usage.output_tokens` and `gen_ai.agent.name`. With those keys alone, MLflow's storage step gives the span type, the model and the usage. For inputs, outputs and a provider, provrun must also write `gen_ai.input.messages`, `gen_ai.output.messages` and `gen_ai.provider.name`, or the `mlflow.*` keys.

To match autolog from provrun's own events:

- **One model call.** A `model_called` event maps to one `LLM` or `CHAT_MODEL` span with the message format of its backend. For the Anthropic format, put the cache tokens into `input_tokens`.
- **One tool call.** A `tool_called` event maps to one `TOOL` span under the agent span, named by the tool, with the arguments and the result. The tool span is not under the model span.
- **The run.** The run maps to one `AGENT` root span with the prompt and the final output.

## Scripts

The scripts were run in a scratch folder, and they are not in the repository. Each one ran with `./run.sh`.

| Script | What it shows |
|---|---|
| `run.sh` | Builds the environment, and sets `MLFLOW_DISABLE_AGENT_HINT`, `MLFLOW_DISABLE_TELEMETRY` and `DO_NOT_TRACK` |
| `helpers.py` | Fresh SQLite store, span dump, patch list |
| `stubs.py` | Stub OpenAI and Anthropic servers that record request headers |
| `pa_autolog.py` | PydanticAI patches, globals and span tree |
| `litellm_autolog.py` | LiteLLM callback lists, span tree and the async leak after a disable |
| `openai_anthropic_autolog.py openai` or `anthropic` | OpenAI and Anthropic patches, span trees and `traceparent` headers |
| `generic_autolog.py` | `mlflow.autolog()` hooks, and PydanticAI on OpenAI with two flavors |
| `suppress.py SCENARIO` | The suppression table |
| `otel_interplay.py`, `shared_provider.py` | Autolog with a global tracer provider, in both provider modes |
| `instrument_false.py` | `agent.instrument = False` under autolog |
| `telemetry_switch.py` | When the telemetry choice is made |
| `translate.py` | The storage translation of PydanticAI's own OTel spans |
| `audit.py`, `guard.py`, `guard_reenable.py` | The full audit, the recipe and the fast check |

## Open questions

- **The OpenAI Agents SDK path.** The SDK is not in the workspace, so I did not test the `AgentRunner` patches or the clearing of its trace processors. From the source, these run even on `disable=True`.
- **Claude Agent SDK.** I did not test the `ClaudeSDKClient` patch.
- **Streams.** I did not run streams for any flavor. The source says that OpenAI records chunk events, that Anthropic records no stream, and that LiteLLM adds `streaming_chunk` events.
- **Databricks.** I did not test in a Databricks runtime. The claim about it comes from the comment and the code at `tracking/fluent.py:3801` to 3806.
- **Other callers.** I cannot list every third-party package that may call `mlflow.autolog()`. The workspace has none today. The guard checks at run time for that reason.
