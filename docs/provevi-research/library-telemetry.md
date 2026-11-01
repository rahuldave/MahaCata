# Library telemetry in provrun runs

These notes list each path by which LiteLLM, PydanticAI, OpenTelemetry, Logfire, openai and anthropic can record or send data in a provrun run. For each path they say how it is switched on, how to suppress it and how to detect it. MLflow autolog is out of scope here. [MLflow autolog](mlflow-autolog.md) covers it.

The notes cover the versions in the MahaCata workspace on 2026-11-01:

| Package | Version |
|---|---|
| pydantic-ai | 2.64.0 |
| litellm | 1.83.0 |
| logfire | 5.4.0 |
| opentelemetry-sdk and opentelemetry-api | 1.45.1 |
| opentelemetry-instrumentation and opentelemetry-instrumentation-httpx | 0.66b1 |
| openai | 3.41.0 |
| anthropic | 2.3.0 |
| mlflow, for one coexistence test | 3.17.2 |

Every citation is a path relative to `site-packages` in the workspace's environment, with a line number. The MLflow citations are relative to the MLflow 3.17.2 source.

## Terms

- **Telemetry path**: any mechanism that copies data about a run out of provrun's control. The data can go to a remote endpoint, a terminal, a log file or a span processor that provrun did not add.
- **Callback**: a function or object that LiteLLM calls before or after each model call. LiteLLM passes it the messages and the response.
- **Tracer provider**: the OpenTelemetry object that makes tracers. Its span processors decide where each finished span goes.
- **Global tracer provider**: the one tracer provider that `opentelemetry.trace.get_tracer_provider()` returns. A process can set it only once.
- **Span processor**: an object on a tracer provider that receives each span. An exporter behind it sends the span somewhere.
- **Instrumentor**: an OpenTelemetry package that patches a library, for example httpx, so that the library makes spans.
- **Net guard**: the audit hook in the test scripts. It records and blocks every DNS lookup and connection to a host other than loopback.

## How the tests were run

The test scripts were run in a scratch folder, and they are not in the repository. The notes name each one. No test called a real model or sent data to a remote endpoint.

- **Net guard.** `netguard.py` installs `sys.addaudithook` for the `socket.getaddrinfo` and `socket.connect` events. It records each non-loopback attempt and raises an error, so the attempt fails.
- **Model calls.** The tests use PydanticAI `TestModel`, LiteLLM `mock_response` and a local HTTP server, `mockserver.py`. The server answers OpenAI chat and Anthropic messages requests on 127.0.0.1 and records the request headers.
- **Spans.** The tests use `InMemorySpanExporter` on a private `TracerProvider`.
- **Marker text.** The prompts contain `SECRET-PROMPT`, the answers contain `SECRET-ANSWER`, and the tool results contain `SECRET-TOOL-RESULT`. A test counts these markers in output, logs and span attributes.

## Main findings

- **LiteLLM fetches its cost map at import.** `import litellm` makes an HTTPS request to raw.githubusercontent.com, unless `LITELLM_LOCAL_MODEL_COST_MAP=True` is set. The net guard caught the lookup.
- **LiteLLM loads a .env file at import.** It searches upward from its own install folder, so it finds a `.env` file in the project that holds the virtual environment.
- **A per-call `callbacks=` argument to LiteLLM is permanent.** LiteLLM copies it into the global callback lists, and the callback then sees every later call.
- **The LiteLLM "otel" callback takes the global tracer provider.** If no SDK provider is set, it makes one, sets it as global and prints spans with prompts to stdout. If provrun's provider is set, it writes prompt bodies into provrun's provider.
- **No PydanticAI instrumentation is on by default.** It is on only through `Agent.instrument_all`, `agent.instrument`, an `Instrumentation` capability or an `InstrumentedModel`.
- **`include_content=False` does not remove everything.** It removes prompt, answer and tool argument text. It keeps tool names, tool descriptions and JSON schemas.
- **Instrumented httpx sends baggage to the model provider.** With PydanticAI instrumentation and an httpx instrumentor, each request carried `traceparent` and a `baggage` header with the agent name and conversation ID.
- **Logfire takes the global providers.** `logfire.configure()` sets the global tracer, meter and logger providers and the global propagator. It also exports to `OTEL_EXPORTER_OTLP_ENDPOINT` when `send_to_logfire=False`.
- **MLflow can add a processor to provrun's provider.** With `MLFLOW_USE_DEFAULT_TRACER_PROVIDER=false`, MLflow adds its own span processor to the global provider, even when provrun owns that provider.
- **`OTEL_SDK_DISABLED=true` also stops provrun's own spans.** A private SDK provider recorded 0 spans with it.
- **The anthropic SDK logs the request body at DEBUG.** The openai SDK 3.41.0 does not. LiteLLM logs the body at DEBUG.
- **The openai and anthropic SDKs send `X-Stainless-*` headers.** The headers give the OS, CPU, Python version and SDK version. Neither SDK sends data to any host other than the model API.

## LiteLLM

### The callback lists

LiteLLM keeps its callbacks in module globals. All of them are empty after import, as the test `t1_litellm_import.py` printed:

| Global | Citation | Purpose |
|---|---|---|
| `litellm.callbacks` | `litellm/__init__.py:157` | Callbacks for every event; strings name a built-in integration |
| `litellm.input_callback` | `litellm/__init__.py:96` | Called before the call |
| `litellm.success_callback` | `litellm/__init__.py:97` | Called after a success |
| `litellm.failure_callback` | `litellm/__init__.py:98` | Called after a failure |
| `litellm.service_callback` | `litellm/__init__.py:99` | Service health events, for example Prometheus |
| `litellm.audit_log_callbacks` | `litellm/__init__.py:100` | Proxy audit logs |
| `litellm._async_input_callback` | `litellm/__init__.py:177` | Async form of input callbacks |
| `litellm._async_success_callback` | `litellm/__init__.py:182` | Async form of success callbacks |
| `litellm._async_failure_callback` | `litellm/__init__.py:187` | Async form of failure callbacks |
| `litellm.callback_settings` | `litellm/__init__.py:162` | Settings for named callbacks |
| `litellm.cold_storage_custom_logger` | `litellm/__init__.py:152` | A named logger for cold storage |

The list `_custom_logger_compatible_callbacks_literal` at `litellm/__init__.py:102` names 48 built-in integrations. Among them are "otel", "logfire", "mlflow", "langfuse", "langfuse_otel", "langsmith", "datadog", "arize", "braintrust", "posthog", "s3_v2" and "gcs_bucket". Each string turns into a logger object in `_init_custom_logger_compatible_class` at `litellm/litellm_core_utils/litellm_logging.py:3654`.

### Ways the callbacks get switched on

- **Assignment or append.** Any code in the process can assign or append to the globals above.
- **The per-call `callbacks=` argument.** `function_setup` at `litellm/utils.py:786` pops it and appends each entry to `input_callback`, `success_callback`, `failure_callback` and the two async lists. The test `t2_litellm_calls.py` passed a callback on one call. The callback then saw the next call, which had no `callbacks=` argument.
- **The per-call `success_callback=` and `failure_callback=` arguments.** `litellm/utils.py:899` to `litellm/utils.py:923` use them for that call only.
- **The per-call `logger_fn=` argument.** `litellm/utils.py:943` stores it in a module global, `user_logger_fn`.
- **Named integrations from environment variables.** No environment variable adds a callback by itself. But a named integration reads its settings from the environment when it starts. For example, "logfire" needs `LOGFIRE_TOKEN` (`litellm/litellm_core_utils/litellm_logging.py:3994`), and "otel" reads `OTEL_EXPORTER`, `OTEL_ENDPOINT`, `OTEL_HEADERS` and the `OTEL_EXPORTER_OTLP_*` variables (`litellm/integrations/opentelemetry.py:112` to `litellm/integrations/opentelemetry.py:117`).
- **The proxy configuration file.** A LiteLLM proxy `config.yaml` sets `litellm_settings.callbacks`. provrun does not run the proxy, so this path applies only if provrun calls the proxy code.
- **Other libraries.** `mlflow.litellm.autolog()` adds an MLflow callback. The other agent covers it.

### The "otel" integration and the global tracer provider

The "otel" integration decides its provider in `_get_or_create_provider` at `litellm/integrations/opentelemetry.py:248`. If the global provider is an SDK `TracerProvider`, it uses that provider and adds no exporter. Otherwise it makes a new provider with its own exporter and sets it as global (`litellm/integrations/opentelemetry.py:296` to `litellm/integrations/opentelemetry.py:319`). The exporter defaults to the console exporter.

The test `t12_litellm_otel.py` showed both cases:

- **No provider set.** LiteLLM set its own provider as global. The console exporter wrote two spans to stdout, and both held the prompt. provrun prints its JSON on stdout, so this output breaks that JSON.
- **provrun's provider set first.** The spans `litellm_request` and `raw_gen_ai_request` went to provrun's provider, with the prompt in their attributes.

provrun already sets `USE_OTEL_LITELLM_REQUEST_SPAN=true`, so these loggers make their own span and do not write onto provrun's step span (`packages/provrun/src/provrun/backends/litellm.py:94`).

### Network access by LiteLLM itself

- **The cost map fetch at import.** `litellm/__init__.py:457` calls `get_model_cost_map`. That function does an `httpx.get` of the URL in `LITELLM_MODEL_COST_MAP_URL`, which defaults to raw.githubusercontent.com (`litellm/litellm_core_utils/get_model_cost_map.py:153`). The test `t1_litellm_import.py` caught the lookup of raw.githubusercontent.com port 443. With `LITELLM_LOCAL_MODEL_COST_MAP=True`, the import made no lookup and the source was "local" (`litellm/litellm_core_utils/get_model_cost_map.py:258`).
- **The Anthropic beta headers fetch.** `litellm/anthropic_beta_headers_manager.py:122` fetches a configuration file the first time LiteLLM needs it. `LITELLM_LOCAL_ANTHROPIC_BETA_HEADERS=True` stops the fetch (`litellm/anthropic_beta_headers_manager.py:142`). A mock Anthropic call in `t2_litellm_calls.py` made no fetch, so I did not see when the fetch happens.
- **The blog posts fetch.** `litellm/litellm_core_utils/get_blog_posts.py:66` fetches an RSS feed. Only the proxy calls it.
- **Hugging Face tokenizers.** The token counter can download a tokenizer for some models. `litellm.disable_hf_tokenizer_download = True` stops it (`litellm/__init__.py:1315`, `litellm/utils.py:2174`). The tiktoken files come with the package (`litellm/litellm_core_utils/default_encoding.py:18`).
- **The `telemetry` flag.** `litellm.telemetry = True` at `litellm/__init__.py:214` is not read anywhere in the package. LiteLLM 1.83.0 sends no usage pings.

### The .env file

`litellm/__init__.py:18` calls `dotenv.load_dotenv()` when `LITELLM_MODE` is "DEV", which is the default. If the main program is a script file, python-dotenv searches upward from the folder of the caller, which is the litellm package (`dotenv/main.py:366` to `dotenv/main.py:377`). If the main program is interactive or `python -c`, it searches from the current folder.

The test used a virtual environment in `dotproj/` with a `.env` file beside `.venv`. A script in another folder imported litellm, and the variable from that `.env` file appeared in `os.environ`. With `LITELLM_MODE=PRODUCTION`, it did not appear. The variables do not override variables that are set already.

### Message redaction

`litellm.turn_off_message_logging = True` replaced the messages with "redacted-by-litellm" in both `kwargs["messages"]` and the standard logging object that a callback receives (`t3_litellm_debuglog.py`). It is off by default (`litellm/__init__.py:194`). The response text was still present in the standard logging object in the test output, so I did not confirm that the response is redacted.

### How to clear and guard the callbacks

`litellm.logging_callback_manager._reset_all_callbacks()` at `litellm/litellm_core_utils/logging_callback_manager.py:359` clears six lists. It does not clear `_async_input_callback`, `service_callback` or `audit_log_callbacks`, so provrun must clear those itself. After the reset, the callback from the earlier test saw no more calls.

To guard the lists, provrun can record every list before the call and compare after the call. Any entry that is not on the approved list is a failure.

## PydanticAI

### Ways instrumentation gets switched on

PydanticAI makes no spans unless one of these paths is on. A default agent made 0 spans, even with a global SDK provider set (`t5_pydanticai.py`).

- **`Agent.instrument_all(settings_or_bool)`.** It sets the class variable `Agent._instrument_default` for every agent (`pydantic_ai/agent/__init__.py:1330`). The default is `False` (`pydantic_ai/agent/__init__.py:585`).
- **`agent.instrument = settings_or_bool`.** This property sets one agent. The 2.64.0 constructor has no `instrument=` parameter.
- **The `Instrumentation` capability.** `Agent(capabilities=[Instrumentation(settings)])` turns on spans for that agent (`pydantic_ai/capabilities/instrumentation.py:113`).
- **An `InstrumentedModel`.** A model wrapped in `InstrumentedModel` turns on spans, and its settings win over the agent settings (`pydantic_ai/agent/__init__.py:1929` to `pydantic_ai/agent/__init__.py:1942`).
- **`logfire.instrument_pydantic_ai()`.** It calls `Agent.instrument_all` with no argument, sets `agent.instrument` for an agent, or returns an `InstrumentedModel` for a model (`logfire/_internal/integrations/pydantic_ai.py:59` to `logfire/_internal/integrations/pydantic_ai.py:63`).

`Agent.instrument_all(False)` and `agent.instrument = False` do not stop an explicit `Instrumentation` capability. The test made 4 spans in both cases.

### Which tracer provider it uses

`InstrumentationSettings()` takes `tracer_provider` and `meter_provider` arguments (`pydantic_ai/models/instrumented.py:102`). Without them, it reads the global providers when the settings object is made (`pydantic_ai/models/instrumented.py:167`). Before any global provider is set, the global provider is a proxy, and the tracer follows the provider that is set later.

`logfire.instrument_pydantic_ai()` passes Logfire's own providers explicitly (`logfire/_internal/integrations/pydantic_ai.py:30`). So its spans go to Logfire's provider, even when provrun owns the global provider.

PydanticAI also records a metric, `gen_ai.client.token.usage`, through the meter provider.

### Spans and attributes

The test agent used `TestModel` and one tool. It made 4 spans, all with the instrumentation scope "pydantic-ai":

| Span | Attributes with content |
|---|---|
| `invoke_agent agent` | `pydantic_ai.all_messages`, `final_result`, `gen_ai.system_instructions` |
| `chat test` (one per model request) | `gen_ai.input.messages`, `gen_ai.output.messages`, `gen_ai.system_instructions`, `gen_ai.tool.definitions`, `model_request_parameters` |
| `execute_tool lookup` | `gen_ai.tool.call.arguments`, `gen_ai.tool.call.result` |

With `include_content=True`, which is the default, the spans held the prompt, the instructions and the tool result. With `include_content=False`, they held none of these markers. But the message attributes stayed, with roles and part types only. `gen_ai.tool.definitions` and `model_request_parameters` still held the tool description and the JSON schema (`t5b.py`).

`include_binary_content` defaults to `True`, so images and files go into the spans as data unless provrun turns it off.

### Baggage

The `Instrumentation` capability puts `gen_ai.agent.name`, `gen_ai.agent.call.id` and the conversation ID into OpenTelemetry baggage (`pydantic_ai/capabilities/instrumentation.py:311`). Baggage is context that a propagator copies into outgoing request headers.

The test `t9_sdks.py` ran an agent with an `OpenAIChatModel` against the local server, under `opentelemetry-instrument`. The request carried a `traceparent` header and a `baggage` header. The baggage header was `gen_ai.agent.name=SECRET-AGENT-NAME,gen_ai.agent.call.id=...,gen_ai.conversation.id=...`. Without the httpx instrumentor, the request had neither header.

### How to force it off or point it at a provider provrun owns

- **Off.** Call `Agent.instrument_all(False)` and set `agent.instrument = False`. Then remove any `Instrumentation` capability, and unwrap any `InstrumentedModel`.
- **Detect at run time.** provrun's evidence capability receives the run context in `for_run`. `ctx.tracer` is a `NoOpTracer` when the run has no instrumentation (`pydantic_ai/agent/__init__.py:5277` to `pydantic_ai/agent/__init__.py:5281`). The test `t13_detect_pa.py` saw `NoOpTracer` for a plain agent. It saw `ProxyTracer` for an agent with `Instrumentation()` and after `instrument_all(True)`.
- **Approved.** Add `Instrumentation(InstrumentationSettings(tracer_provider=provrun_tp, meter_provider=provrun_mp, include_content=..., include_binary_content=False))`. The test sent all 4 spans to the private provider and none to the global provider.

## OpenTelemetry

### The global tracer provider

`set_tracer_provider` works once. A second call logs the warning "Overriding of current TracerProvider is not allowed" and changes nothing (`opentelemetry/trace/__init__.py:544` to `opentelemetry/trace/__init__.py:552`). The test `t6_logfire.py` showed the warning when Logfire tried to set the provider after provrun.

`get_tracer_provider` loads a provider from `OTEL_PYTHON_TRACER_PROVIDER` if no provider is set and the variable is set (`opentelemetry/trace/__init__.py:564` to `opentelemetry/trace/__init__.py:573`). With `OTEL_PYTHON_TRACER_PROVIDER=sdk_tracer_provider`, the global provider was an SDK `TracerProvider` before any code set it (`t8_otel.py`).

Anyone with a reference to a provider can add a span processor to it. MLflow did so to provrun's provider (see the section on MLflow).

### Environment variables that switch on export or instrumentation

| Variable | Read by | Effect |
|---|---|---|
| `OTEL_SDK_DISABLED=true` | Every SDK `TracerProvider`, `MeterProvider` and `LoggerProvider` (`opentelemetry/sdk/trace/__init__.py:1278`) | All SDK providers make no-op spans, provrun's private provider too |
| `OTEL_PYTHON_TRACER_PROVIDER` | `opentelemetry/trace/__init__.py:569` | Loads a global provider from an entry point |
| `OTEL_TRACES_EXPORTER`, `OTEL_METRICS_EXPORTER`, `OTEL_LOGS_EXPORTER` | The SDK configurator (`opentelemetry/sdk/_configuration/__init__.py:81`), Logfire | Choose exporters; the configurator runs only through a distro |
| `OTEL_EXPORTER_OTLP_ENDPOINT` and `OTEL_EXPORTER_OTLP_TRACES_ENDPOINT` | Logfire, MLflow, LiteLLM "otel", the configurator | Add an OTLP exporter |
| `OTEL_EXPORTER_OTLP_HEADERS` | The OTLP exporter, LiteLLM "otel" | Headers for the exporter, often a credential |
| `OTEL_CONFIG_FILE` | The SDK configurator (`opentelemetry/sdk/_configuration/__init__.py:660`) | A declarative configuration file |
| `OTEL_PYTHON_DISTRO`, `OTEL_PYTHON_CONFIGURATOR` | `opentelemetry/instrumentation/auto_instrumentation/_load.py:49`, `:129` | Choose the distro and configurator for auto-instrumentation |
| `OTEL_PYTHON_DISABLED_INSTRUMENTATIONS` | `opentelemetry/instrumentation/auto_instrumentation/_load.py:71` | Turns off named instrumentors |
| `OTEL_TRACES_SAMPLER`, `OTEL_TRACES_SAMPLER_ARG` | `opentelemetry/sdk/trace/sampling.py:466` | Change the sampler of any provider made without a sampler |
| `OTEL_RESOURCE_ATTRIBUTES`, `OTEL_SERVICE_NAME` | `opentelemetry/sdk/resources/__init__.py:333` | Add attributes to every span of a provider made with `Resource.create` |
| `OTEL_ATTRIBUTE_VALUE_LENGTH_LIMIT`, `OTEL_SPAN_ATTRIBUTE_COUNT_LIMIT` | `opentelemetry/sdk/trace/__init__.py:40` | Cut attribute values and counts |
| `PYTHONPATH` with the auto-instrumentation folder | Python startup | Runs `sitecustomize.py` from `opentelemetry/instrumentation/auto_instrumentation/` |

### opentelemetry-instrument and sitecustomize

The workspace has the `opentelemetry-instrument` command, because logfire-sdk[httpx] pulls in `opentelemetry-instrumentation-httpx`. The command puts the auto-instrumentation folder first on `PYTHONPATH`, so Python runs its `sitecustomize.py`. That file calls `initialize()`, which loads every installed instrumentor (`opentelemetry/instrumentation/auto_instrumentation/__init__.py:83` to `opentelemetry/instrumentation/auto_instrumentation/__init__.py:105`).

The workspace has no distro, so no configurator runs. Under the command, `httpx` was instrumented, but the global provider stayed a proxy and `OTEL_TRACES_EXPORTER=console` printed nothing (`t8_otel.py`). If someone installs `opentelemetry-distro`, the command also sets a global provider with an OTLP exporter by default. `OTEL_PYTHON_DISABLED_INSTRUMENTATIONS=httpx,httpx2` left httpx not instrumented.

### How provrun can own the provider

- **Set its provider first.** Call `trace.set_tracer_provider(provrun_tp)` before any library import that can set one. Then check `trace.get_tracer_provider() is provrun_tp`. If the check fails, another component set the provider first.
- **Make the provider fixed.** Pass an explicit sampler, an explicit `Resource(...)` and explicit `SpanLimits`, so that the `OTEL_*` variables do not change provrun's spans.
- **Refuse other processors.** A subclass of `TracerProvider` can refuse `add_span_processor` after provrun has added its own. The test `t14_guarded_provider.py` refused MLflow's `MlflowV3SpanProcessor` and recorded the attempt.
- **Use a private provider.** provrun can keep its provider private and pass it explicitly to PydanticAI. But `opentelemetry/trace.get_tracer("provrun")` in `packages/provrun/src/provrun/otel.py` reads the global provider, so that module must take the private provider too. A private provider does not stop other libraries from setting the global one.
- **Own the meter and logger providers too.** Logfire sets all three globals, and PydanticAI records a metric.
- **Remove `OTEL_SDK_DISABLED` for provrun's provider.** The variable stops provrun's own spans, so provrun should not use it as the off switch. If it is set, provrun should warn.

## Logfire

### What logfire.configure does

- **Global providers.** For the default instance, `configure()` sets the global tracer, meter and logger providers once (`logfire/_internal/config.py:1517` to `logfire/_internal/config.py:1521`). `configure(local=True)` does not set them. The test `t6_logfire.py` showed both cases.
- **Global propagator.** Both `local=True` and `local=False` set the global text map propagator to a wrapper, `WarnOnExtractTraceContextPropagator` (`logfire/_internal/config.py:1546` to `logfire/_internal/config.py:1555`).
- **Executors.** Both forms call `instrument_executors()`, which patches `ThreadPoolExecutor` and `ProcessPoolExecutor` to carry context (`logfire/_internal/config.py:1544`).
- **send_to_logfire.** The default comes from `LOGFIRE_SEND_TO_LOGFIRE`, then from `pyproject.toml` `[tool.logfire]`. Otherwise it is `True`, or `False` under pytest (`logfire/_internal/config_params.py:54`). With the default and no token, `configure()` raised `LogfireConfigError` and asked the user to run `logfire auth`. With `if-token-present`, it sent nothing.
- **Token.** `LOGFIRE_TOKEN` gives the write token (`logfire/_internal/config_params.py:62`). A credentials file in `.logfire/` also gives one. The exporter sends spans to `{base_url}/v1/traces` with the token in the `Authorization` header (`logfire/_internal/config.py:1333` to `logfire/_internal/config.py:1337`). When a token is present, a thread also checks it against the Logfire API.
- **OTLP from the environment.** If `OTEL_EXPORTER_OTLP_ENDPOINT` or `OTEL_EXPORTER_OTLP_TRACES_ENDPOINT` is set, Logfire adds an OTLP exporter, whatever `send_to_logfire` says (`logfire/_internal/config.py:1427` to `logfire/_internal/config.py:1436`). With `send_to_logfire=False` and the variable set to an example host, the net guard caught repeated lookups of that host.
- **Console.** The console exporter is on by default (`logfire/_internal/config_params.py:78`). `console=False` or `LOGFIRE_CONSOLE=false` turns it off.
- **Additional processors.** `additional_span_processors=[...]` adds processors to Logfire's provider. The test used it to capture Logfire's spans in memory.

### Other Logfire paths

- **The Pydantic plugin.** logfire-sdk registers a Pydantic plugin through an entry point. Pydantic loads it for every model. It records validations only if `LOGFIRE_PYDANTIC_PLUGIN_RECORD` or `[tool.logfire]` sets the record mode, and the default is "off" (`logfire/_internal/config_params.py:101`). `PYDANTIC_DISABLE_PLUGINS=logfire-plugin` stops Pydantic from loading it.
- **The pytest plugin.** logfire-sdk registers a pytest plugin, `logfire._internal.integrations.pytest`. It runs only in test runs.
- **`logfire.instrument_httpx()` and other instrument calls.** They patch libraries in the same way as the OpenTelemetry instrumentors, and httpx then sends baggage headers.

### Coexistence with MLflow tracing

`MLFLOW_USE_DEFAULT_TRACER_PROVIDER` defaults to `True`. Its name says the opposite of what it does. With `True`, MLflow uses an isolated provider of its own (`mlflow/environment_variables.py:1055` to `mlflow/environment_variables.py:1061`). With `False`, MLflow uses the global provider and adds its processor to it.

The test `t11_mlflow_provider.py` set provrun's provider first and then made an MLflow span with an OpenTelemetry child span:

| Setting | Processors on provrun's provider after the span | Spans that provrun's provider saw |
|---|---|---|
| Default | provrun's only | The child span, as a root span |
| `MLFLOW_USE_DEFAULT_TRACER_PROVIDER=false` | provrun's and `MlflowV3SpanProcessor` | The MLflow span and its child |
| `MLFLOW_TRACE_PROPAGATE_TO_OTEL_CONTEXT=true` | provrun's only | The child span, with the MLflow span as its parent |

So with `false`, MLflow exports every span of provrun's provider, which includes PydanticAI and LiteLLM spans. Logfire and MLflow both want the global provider. Logfire with `local=True` and MLflow in its default isolated mode do not compete for it.

### An approved export to Logfire under provrun's provider

There are two forms. I did not test either against the real Logfire service.

1. **No Logfire SDK at all.** provrun adds `BatchSpanProcessor(OTLPSpanExporter(endpoint=f"{base_url}/v1/traces", headers={"Authorization": token}))` to its own provider. LiteLLM's "logfire" integration and Logfire itself export in the same way. provrun keeps the provider and the processor list, and Logfire is one approved destination.
2. **A local Logfire instance.** provrun calls `logfire.configure(local=True, send_to_logfire=True, token=..., console=False)` and passes that instance's provider to `InstrumentationSettings`. This form leaves the global tracer provider alone. But it still changes the global propagator and patches the executors, and it still reads `OTEL_EXPORTER_OTLP_*`.

Form 1 gives provrun the most control. Form 2 adds Logfire's scrubbing of secrets and its token check.

## The openai and anthropic SDKs

Neither SDK sends data to a host other than the model API. The net guard saw no other lookups in `t9_sdks.py`.

Each request carries these headers, as the local server recorded:

- **openai 3.41.0.** `User-Agent: AsyncOpenAI/Python 3.41.0`, `X-Stainless-Lang`, `X-Stainless-Package-Version`, `X-Stainless-OS`, `X-Stainless-Arch`, `X-Stainless-Runtime`, `X-Stainless-Runtime-Version`, `X-Stainless-Async`, `X-Stainless-Retry-Count` and `X-Stainless-Read-Timeout` (`openai/_base_client.py:2305` to `openai/_base_client.py:2312`).
- **anthropic 2.3.0.** The same `X-Stainless-*` headers, plus `X-Stainless-Timeout` and `anthropic-version` (`anthropic/_base_client.py:2658` to `anthropic/_base_client.py:2665`).
- **Through PydanticAI.** The `User-Agent` became `pydantic-ai/2.64.0`.
- **Through LiteLLM.** LiteLLM used the openai SDK and added `X-Stainless-Raw-Response`.

The headers tell the provider the OS, the CPU type, the Python version and the SDK version. `default_headers` on the client can override them. I did not test that.

Both SDKs use `httpx2`, a fork of httpx. The instrumentor package has a separate `httpx2` entry point for it.

## Python logging

| Library | Logger names | Body at DEBUG | Switched on by |
|---|---|---|---|
| LiteLLM | `LiteLLM`, `LiteLLM Router`, `LiteLLM Proxy` | Yes, prompt and answer | Root logger at DEBUG, `litellm._turn_on_debug()` |
| anthropic | `anthropic`, `anthropic._base_client` | Yes, the request options with the messages | `ANTHROPIC_LOG=debug`, root logger at DEBUG |
| openai | `openai`, `openai._base_client` | No, only the method and status | `OPENAI_LOG=debug` |
| httpx2 and httpcore2 | `httpx2`, `httpcore2.connection`, `httpcore2.http11` | No, URL and status only | Root logger at DEBUG |
| PydanticAI | none for bodies | No | Not applicable |

Notes on each row:

- **LiteLLM.** `litellm/_logging.py:282` to `litellm/_logging.py:289` add a stderr handler at the level in `LITELLM_LOG`, which defaults to DEBUG. But the loggers themselves stay at NOTSET, so they follow the root logger. `LITELLM_LOG=DEBUG` alone printed nothing in the test. With the root logger at DEBUG, stderr held the prompt 6 times and the answer 2 times. `litellm._turn_on_debug()` sets the three loggers to DEBUG (`litellm/_logging.py:423`). `litellm.set_verbose = True` printed nothing in the test. LiteLLM also sets the `httpx` logger to WARNING at import (`litellm/_logging.py:293`). `JSON_LOGS` switches the format.
- **anthropic.** `anthropic/_base_client.py:601` logs "Request options" with the request JSON at DEBUG. The test found the prompt once on stderr.
- **openai.** openai 3.41.0 logs no body (`openai/_base_client.py:606`). `OPENAI_LOG` sets the level of the `openai` logger (`openai/_utils/_logs.py:38` to `openai/_utils/_logs.py:47`).

`LiteLLM` and `anthropic` have their own handlers, so they write to stderr even when provrun's handlers do not take their records.

## Suppression recipe

These steps are a proposal for provrun. Only the parts named in the test notes were tested.

1. **Before any library import,** set `LITELLM_LOCAL_MODEL_COST_MAP=True`, `LITELLM_LOCAL_ANTHROPIC_BETA_HEADERS=True`, `LITELLM_MODE=PRODUCTION` and `PYDANTIC_AI_NO_BANNER=1`. Do not set `OTEL_SDK_DISABLED`.
2. **Before any library import,** remove from the environment every `OTEL_EXPORTER_OTLP_*`, `OTEL_TRACES_EXPORTER`, `OTEL_PYTHON_TRACER_PROVIDER`, `OTEL_CONFIG_FILE`, `LOGFIRE_*`, `LITELLM_LOG` and `ANTHROPIC_LOG` variable that is not approved. Fail if the auto-instrumentation `sitecustomize` module is loaded.
3. **Set provrun's providers.** Make a guarded tracer provider, a meter provider and a logger provider with explicit settings. Set all three as global, and set a propagator that injects nothing.
4. **Configure PydanticAI.** Call `Agent.instrument_all(False)`. If the configuration approves instrumentation, add `Instrumentation` with provrun's providers, `include_binary_content=False` and the approved `include_content`.
5. **Configure LiteLLM.** Clear every callback list in the LiteLLM table, then add the approved callbacks. Set `turn_off_message_logging` from the configuration and `disable_hf_tokenizer_download = True`.
6. **Configure logging.** Set each logger in the logging table to WARNING or higher. Root DEBUG then cannot turn them on.
7. **Install a net guard.** Add an audit hook that lets provrun connect only to the model host and the approved exporter hosts. A blocked connection fails the run and goes into the evidence.

## Detection during a run

Before and after each model call and at the end of the run, provrun can check these things:

- **LiteLLM callbacks.** Compare every callback global in the LiteLLM table with the snapshot. Check `turn_off_message_logging` and the level of the `LiteLLM` logger.
- **PydanticAI.** Check `Agent._instrument_default`, and check `ctx.tracer` in the evidence capability. Any tracer that is not a `NoOpTracer` means instrumentation is on, and it must be the approved one.
- **The global providers.** Check that `trace.get_tracer_provider()`, `metrics.get_meter_provider()` and `_logs.get_logger_provider()` are provrun's objects. Check the propagator too.
- **Processors.** Read the refusals that the guarded provider recorded.
- **Instrumentors.** Check `HTTPXClientInstrumentor().is_instrumented_by_opentelemetry` and the same for `httpx2`.
- **Logfire.** Check `logfire.DEFAULT_LOGFIRE_INSTANCE.config._initialized`.
- **Network.** Read the events that the net guard recorded.

## Summary table

| Path | On by default | How it is switched on | How to suppress | How to detect |
|---|---|---|---|---|
| LiteLLM cost map fetch | Yes, at import | Always, unless the variable is set | `LITELLM_LOCAL_MODEL_COST_MAP=True` | Net guard; `get_model_cost_map_source_info()` |
| LiteLLM beta headers fetch | Yes, on first use | Always, unless the variable is set | `LITELLM_LOCAL_ANTHROPIC_BETA_HEADERS=True` | Net guard |
| LiteLLM tokenizer download | Yes, for some models | Token counting | `litellm.disable_hf_tokenizer_download = True` | Net guard |
| LiteLLM .env load | Yes, at import | `LITELLM_MODE=DEV`, the default | `LITELLM_MODE=PRODUCTION` | Compare `os.environ` before and after import |
| LiteLLM callback lists | No | Assignment, append, per-call `callbacks=` | Clear every callback list | Snapshot and compare |
| LiteLLM "otel" integration | No | `"otel"` in a callback list | Clear the lists; guarded provider | Global provider check; callback snapshot |
| LiteLLM "logfire" and other named integrations | No | Name in a callback list, settings from the environment | Clear the lists; net guard | Callback snapshot; net guard |
| LiteLLM debug logs with bodies | No | Root logger at DEBUG; `_turn_on_debug()` | Set its loggers to WARNING | Logger level check |
| PydanticAI instrumentation | No | `instrument_all`, `agent.instrument`, `Instrumentation`, `InstrumentedModel`, `logfire.instrument_pydantic_ai` | `instrument_all(False)`; remove the capability | `ctx.tracer` is not a `NoOpTracer` |
| PydanticAI baggage in requests | No | Instrumentation and an httpx instrumentor | Propagator that injects nothing; no instrumentor | Instrumentor check; local server headers |
| OTel auto-instrumentation | No | `opentelemetry-instrument`; `PYTHONPATH` | Refuse to run; `OTEL_PYTHON_DISABLED_INSTRUMENTATIONS` | `sitecustomize` module path; instrumentor check |
| OTel global provider from the environment | No | `OTEL_PYTHON_TRACER_PROVIDER` | Remove the variable; set provrun's provider first | Global provider check |
| OTel SDK switch-off | No | `OTEL_SDK_DISABLED=true` | Remove the variable | Read the variable; provrun's span count is 0 |
| Logfire global providers | No | `logfire.configure()` | Do not call it, or use `local=True` | Global provider check |
| Logfire export to Logfire | Yes, once configured with a token | `LOGFIRE_TOKEN`, a credentials file | `send_to_logfire=False`; remove the token | Net guard; Logfire config check |
| Logfire OTLP export | No | `OTEL_EXPORTER_OTLP_ENDPOINT` and a `configure()` call | Remove the variable | Net guard; processor list of Logfire's provider |
| Logfire console output | Yes, once configured | `configure()` | `console=False` | stdout check |
| Logfire Pydantic plugin | Loaded, records nothing | `LOGFIRE_PYDANTIC_PLUGIN_RECORD` | `PYDANTIC_DISABLE_PLUGINS=logfire-plugin` | Read the variable |
| MLflow processor on provrun's provider | No | `MLFLOW_USE_DEFAULT_TRACER_PROVIDER=false` | Guarded provider; keep the default | Guarded provider refusals |
| openai and anthropic `X-Stainless-*` headers | Yes | Always | `default_headers` on the client, untested | Local server |
| anthropic request body log | No | `ANTHROPIC_LOG=debug`; root logger at DEBUG | Set its logger to WARNING | Logger level check |
| openai debug log | No | `OPENAI_LOG=debug` | Set its logger to WARNING | Logger level check |

## Open questions

- **The beta headers fetch.** I did not find which LiteLLM call path first loads the Anthropic beta headers configuration. A mock call did not load it.
- **Response redaction in LiteLLM.** With `turn_off_message_logging`, the test showed redacted messages. I did not check whether the response text in the standard logging object was redacted too.
- **Overriding the `X-Stainless-*` headers.** I did not test whether `default_headers` can remove the headers, or only change their values.
- **MLflow with the guarded provider.** When the guarded provider refused MLflow's processor, MLflow's span also did not reach provrun's exporter. I did not find the cause.
- **Exports to the real Logfire service.** Neither form of the approved Logfire path was tested against the real service, because the task forbids it.
