# Capture routes for Claude Code and Codex

Research for provevi, as of 2026-10-31. Provevi is the planned evidence store and trace store. It must
record every model call, tool call, prompt, subagent and session event of a coding agent. It records each
one as an OpenTelemetry span and as an evidence event in a hash chain.

**Versions checked:**

| Product | Version | Source |
|---|---|---|
| Claude Code CLI | 2.1.316 | `claude --version` on this machine |
| Claude Agent SDK, TypeScript | 0.3.316 (`@anthropic-ai/claude-agent-sdk`) | `npm view` |
| Claude Agent SDK, Python | 0.2.176 (`claude-agent-sdk`) | PyPI |
| Codex CLI | 0.166.0 (tag `rust-v0.166.0`, 2026-10-29) | `gh release list -R openai/codex` |
| Codex SDK, TypeScript | 0.166.0 (`@openai/codex-sdk`) | `npm view` |
| Codex SDK, Python | 0.163.0 (`openai-codex`) | PyPI |

**Terms used on this page:**

- **Hook.** A program or callback that the agent runs at a fixed point in its loop. The agent passes it a
  JSON payload. Some hooks can return a decision that changes what the agent does.
- **Block.** A hook blocks when its answer stops or changes an action before the action takes effect.
- **Observe.** A route observes when it only reports what happened.
- **Transcript.** The JSONL file in which the agent saves a session. Claude Code calls it a transcript.
  Codex calls it a rollout file.
- **Model call.** One HTTP request from the agent to the model API and its response.

A note on the documentation sites. The OpenAI URLs `developers.openai.com/codex/hooks` and
`developers.openai.com/codex/config-advanced` now return a 308 redirect to `learn.chatgpt.com/docs/...`.
The page cites the target URLs. The Codex GitHub `docs/` folder holds only stubs that point to the site.

## Claude Code

### 1. Hook events

The hooks reference lists the events, the payloads and the answers. The source is
https://code.claude.com/docs/en/hooks.

**Hook types.** A hook entry has a `type` of `command`, `http`, `mcp_tool`, `prompt` or `agent`. The
`agent` type is experimental. A `command` hook reads the payload on stdin. An `http` hook receives the
payload as the body of a POST. A `command` hook can set `async: true` to run in the background, and an
async hook cannot block.

**Common input fields.** Every event receives `session_id`, `prompt_id`, `transcript_path`, `cwd`,
`scratchpad_dir`, `permission_mode`, `effort` and `hook_event_name`. Inside a subagent, or with `--agent`,
the payload also has `agent_id` and `agent_type`.

**Universal answer fields.** Every event accepts `continue`, `stopReason`, `systemMessage` and
`terminalSequence`. The value `continue: false` stops Claude. A `command` hook blocks with exit code 2.
The `additionalContext` field goes to the model as a system reminder, with a cap of 10,000 characters.

| Event | When it fires | Event-specific input | What it can return | Blocks |
|---|---|---|---|---|
| `SessionStart` | Session starts or resumes. Matcher on `startup`, `resume`, `clear`, `compact`, `fork` | `source`, `model` (optional), `agent_type`, `session_title`. On resume or fork (v2.1.251+): `seconds_since_last_response`, `context_tokens`, `prompt_cache_likely_expired`, `estimated_cache_write_usd` | `additionalContext`, `initialUserMessage`, `watchPaths`, `sessionTitle`, `reloadSkills` | No |
| `Setup` | `--init-only`, or `--init` / `--maintenance` with `-p` | `trigger` (`init`, `maintenance`) | None | No |
| `InstructionsLoaded` | A `CLAUDE.md` or `.claude/rules/*.md` file loads | `file_path`, `memory_type`, `load_reason`, `globs`, `trigger_file_path`, `parent_file_path` | None | No |
| `UserPromptSubmit` | Prompt submitted, before Claude processes it | `prompt`, `session_title` | `decision: "block"`, `reason`, `additionalContext` | Yes |
| `UserPromptExpansion` | A typed command or MCP prompt expands into a prompt | `expansion_type`, `command_name`, `command_args`, `command_source`, `prompt` | `decision: "block"`, `additionalContext` | Yes |
| `PreToolUse` | After Claude makes tool parameters, before the tool runs | `tool_name`, `tool_input`, `tool_use_id`, `mcp_server` | `permissionDecision` (`allow`, `deny`, `ask`, `defer`), `permissionDecisionReason`, `updatedInput`, `additionalContext` | Yes |
| `PermissionRequest` | A tool call needs a permission decision | `tool_name`, `tool_input`, `permission_suggestions`, `mcp_server`. No `tool_use_id` | `decision.behavior` (`allow`, `deny`), `decision.updatedInput` | Decides, exit 2 ignored |
| `PermissionDenied` | Auto mode denies a tool call | `tool_name`, `tool_input`, `tool_use_id`, `reason` | `retry: true` | No |
| `PostToolUse` | After a tool succeeds | `tool_name`, `tool_input`, `tool_response`, `tool_use_id`, `duration_ms` | `decision: "block"`, `reason`, `updatedToolOutput`, `additionalContext` | Changes what Claude sees. Tool already ran |
| `PostToolUseFailure` | After a tool fails | `tool_name`, `tool_input`, `tool_use_id`, `error`, `is_interrupt`, `duration_ms` | `decision`, `additionalContext` | No |
| `PostToolBatch` | A parallel batch of tools resolves, before the next model call | `tool_calls[]` with `tool_name`, `tool_input`, `tool_use_id`, `tool_response` | `decision: "block"` stops the loop | Yes |
| `MessageDisplay` | Assistant text displays | `turn_id`, `message_id`, `index`, `final`, `delta` | `displayContent` changes the screen only | No |
| `Notification` | Claude Code sends a notification | `message`, `title`, `notification_type` | None | No |
| `SubagentStart` | A subagent starts | `agent_id`, `agent_type` | `additionalContext` | No |
| `SubagentStop` | A subagent finishes | `stop_hook_active`, `agent_id`, `agent_type`, `agent_transcript_path`, `last_assistant_message`, `background_tasks`, `session_crons` | `decision`, `additionalContext` | Yes |
| `TaskCreated` / `TaskCompleted` | Task tool creates or completes a task | `task_id`, `task_subject`, `task_description`, `teammate_name`, `agent_id` | exit 2 or `decision: "block"` | Yes |
| `TeammateIdle` | A team agent is about to idle | Not read in detail | exit 2 or `continue: false` | Yes |
| `Stop` | Claude finishes a response | `stop_hook_active`, `last_assistant_message` | `decision: "block"` and `reason` continue the turn | Yes |
| `StopFailure` | A turn ends on an API error | Not read in detail | Output ignored | No |
| `PreCompact` / `PostCompact` | Before and after compaction. Matcher `manual` or `auto` | `trigger` and more, not read in detail | `PreCompact` takes `decision` | `PreCompact` yes |
| `PreModelSwitch` / `PostModelSwitch` | Before and after a model change | `from_model`, `to_model` | `permissionDecision`, or `additionalContext` after | Pre yes |
| `ConfigChange`, `CwdChanged`, `DirectoryAdded`, `FileChanged` | Settings, folder or watched file changes | Not read in detail | `ConfigChange` takes `decision` | `ConfigChange` yes |
| `WorktreeCreate` / `WorktreeRemove` | Worktree made or removed | Not read in detail | Path on stdout, or `worktreePath` | Non-zero exit fails it |
| `Elicitation` / `ElicitationResult` | An MCP server asks the user for input | Not read in detail | `action`, `content` | Yes |
| `SessionEnd` | Session ends. Matcher `clear`, `resume`, `logout`, `prompt_input_exit`, `other` | `reason` | None. Default budget 1.5 seconds | No |

**Model calls in hooks.** No hook receives a model request or a model response. The nearest data are
`last_assistant_message` on `Stop` and `SubagentStop`, the text deltas on `MessageDisplay`, and
`PostToolBatch`, which fires just before the next model call. Hooks give tool calls, prompts and
lifecycle events. They give no token counts.

### 2. Built-in OpenTelemetry

Claude Code exports metrics and log events, and in beta it exports trace spans. The source is
https://code.claude.com/docs/en/monitoring-usage.

**Switches and exporters:**

| Variable | Meaning |
|---|---|
| `CLAUDE_CODE_ENABLE_TELEMETRY=1` | Required for all signals |
| `OTEL_METRICS_EXPORTER` | `otlp`, `prometheus`, `console`, `none` |
| `OTEL_LOGS_EXPORTER` | `otlp`, `console`, `none` |
| `OTEL_TRACES_EXPORTER` | `otlp`, `console`, `none`. Beta |
| `CLAUDE_CODE_ENHANCED_TELEMETRY_BETA=1` | Required for spans |
| `ENABLE_BETA_TRACING_DETAILED=1` with `BETA_TRACING_ENDPOINT` | Adds `claude_code.hook` spans and content attributes. The interactive CLI also needs an allowlisted organization |
| `OTEL_EXPORTER_OTLP_ENDPOINT`, `_PROTOCOL`, `_HEADERS`, and per-signal forms | Standard OTLP settings |
| `OTEL_LOGS_EXPORT_INTERVAL`, `OTEL_TRACES_EXPORT_INTERVAL` | Default 5000 ms |
| `OTEL_METRIC_EXPORT_INTERVAL` | Default 60000 ms |
| `OTEL_RESOURCE_ATTRIBUTES` | Custom resource keys |
| `CLAUDE_CODE_PROPAGATE_TRACEPARENT=1` | Sends trace context through a custom `ANTHROPIC_BASE_URL` proxy |

**Content switches.** All content is off by default. When content is off, the value is `<REDACTED>`.

| Variable | What it adds |
|---|---|
| `OTEL_LOG_USER_PROMPTS=1` | Prompt text in `prompt`, `prompt_text` and the span attribute `user_prompt` |
| `OTEL_LOG_ASSISTANT_RESPONSES=1` | Response text in `assistant_response`. Follows `OTEL_LOG_USER_PROMPTS` when unset |
| `OTEL_LOG_TOOL_DETAILS=1` | `tool_input`, `tool_parameters`, Bash commands, file paths, MCP, skill and agent names, error text |
| `OTEL_LOG_TOOL_CONTENT=1` | Tool output as a `tool.output` span event. Needs tracing |
| `OTEL_LOG_RAW_API_BODIES=1` | `api_request_body` and `api_response_body` events, cut at the content limit |
| `OTEL_LOG_RAW_API_BODIES=file:<dir>` | Full bodies as `.request.json` and `.response.json` files, a `body_ref` on the event, and an `index.jsonl` that links each response to its request and its transcript message (v2.1.274+) |
| `CLAUDE_CODE_OTEL_CONTENT_MAX_LENGTH` | Limit for content attributes, default 61440 UTF-16 units |

The raw bodies hold the system prompt, every earlier turn and every tool result. Claude Code always removes
extended thinking from the raw bodies. Project and local settings cannot set `OTEL_LOG_RAW_API_BODIES`.

**Metrics.** The metrics are `claude_code.session.count`, `claude_code.lines_of_code.count`,
`claude_code.pull_request.count`, `claude_code.commit.count`, `claude_code.cost.usage`,
`claude_code.token.usage`, `claude_code.code_edit_tool.decision` and `claude_code.active_time.total`.

**Log events.** Each name has the prefix `claude_code.`:

| Event | Main attributes |
|---|---|
| `user_prompt` | `prompt_length`, `prompt` (gated), `command_name`, `message.uuid` |
| `assistant_response` | `response_length`, `response` (gated), `model`, `request_id`, `query_source` |
| `api_request` | `model`, `cost_usd`, `duration_ms`, `input_tokens`, `output_tokens`, `cache_read_tokens`, `cache_creation_tokens`, `request_id`, `query_source`, `effort` |
| `api_error`, `api_refusal` | `model`, `error` or `category`, `status_code`, `attempt`, `request_id` |
| `api_request_body`, `api_response_body` | `body` or `body_ref`, `body_length`, `body_truncated`, `request_body_id` |
| `tool_decision` | `tool_name`, `tool_use_id`, `decision`, `source` (`config`, `hook`, `user_*`), `classifier_verdict` |
| `tool_result` | `tool_name`, `tool_use_id`, `success`, `duration_ms`, `error`, `tool_input` (gated), result size |
| `subagent_completed` | `agent_type`, `total_tool_uses`, `duration_ms`, `model`, `is_async` |
| `hook_registered`, `hook_execution_start`, `hook_execution_complete` | Hook inventory and outcome counts, for example `num_blocking` |
| `compaction` | `trigger`, `duration_ms`, `pre_tokens`, `post_tokens` |
| `safety_event` (v2.1.307+) | `category`, `decision`, `decision_source`, `reason_code`, `hook_event` |
| Other events | `plugin_loaded`, `mcp_server_connection`, `managed_settings_resolved`, `system_prompt`, `retention_sweep`, `feedback_survey` |

Events carry `session.id`, `prompt.id`, `event.sequence`, `message.uuid`, `request_id`, `agent_id` and
`parent_agent_id`. The `prompt.id` value links all events from one user prompt. The `event.sequence` value
is a counter for each process.

**Spans (beta).** The span tree for one prompt has this form:

```text
claude_code.interaction            one span for each user prompt
├── claude_code.llm_request        model, gen_ai.*, tokens, ttft_ms, stop_reason, request_id, attempt
├── claude_code.hook               needs detailed beta tracing
└── claude_code.tool               tool_name, tool_use_id, duration_ms, result_tokens
    ├── claude_code.tool.blocked_on_user
    ├── claude_code.tool.execution success, error
    └── subagent spans nest under the Agent tool span
```

Bash subprocesses inherit `TRACEPARENT`. In `-p` mode and in Agent SDK sessions, Claude Code reads an
inbound `TRACEPARENT` and `TRACESTATE` from the environment. Provevi can therefore set the parent trace.

### 3. Session transcripts

Claude Code writes one JSONL file for each session. The source is
https://code.claude.com/docs/en/claude-directory.

- **Main file.** `~/.claude/projects/<project>/<session>.jsonl`. The `<project>` part is the working
  folder with each character that is not a letter or digit changed to `-`.
- **Subagents.** `~/.claude/projects/<project>/<session>/subagents/` holds subagent transcripts in the same
  format.
- **Large outputs.** `~/.claude/projects/<project>/<session>/tool-results/` holds tool outputs that were
  too large for the main file.
- **Retention.** Claude Code deletes these files after `cleanupPeriodDays`, which is 30 by default. Set
  the value higher, or copy the files out, so that evidence stays.

The documentation says the transcript holds messages, tool calls and tool results. It also holds the
system prompt, tool definitions and the parameters of each model request. The files are plain text and
are not encrypted.

The documentation does not define the line schema, and it makes no promise that the schema is stable.
Treat the schema as internal. A local file from v2.1.316 shows these record types:

| Record `type` | Content seen |
|---|---|
| `user` | `message` (prompt or `tool_result` blocks), `toolUseResult`, `promptId`, `permissionMode` |
| `assistant` | `message` (the API message with `content`, `usage`, `stop_reason`), `requestId`, `requestedModel` |
| `system` | Hook results, compaction metadata, `stopReason` |
| `api-request`, `api-request-blob`, `api-request-shape` | Model request parameters and shared request parts, by hash |
| `attachment`, `file-history-snapshot`, `file-history-delta` | Attachments and file snapshots before edits |
| `cost-state` | `modelUsage`, `totalCostUSD`, durations |
| Others | `mode`, `permission-mode`, `custom-title`, `last-prompt`, `queue-operation` |

Message records carry `uuid`, `parentUuid`, `sessionId`, `timestamp`, `isSidechain` and `version`.
Thinking blocks may appear in the file. The OTel raw bodies always remove them.

### 4. Agent SDK capture

The SDK runs the same Claude Code engine, so the hooks and OTel settings above also apply. The sources are
https://code.claude.com/docs/en/agent-sdk/hooks, https://code.claude.com/docs/en/agent-sdk/permissions
and https://code.claude.com/docs/en/agent-sdk/typescript.

**Hook callbacks.** Pass `hooks` in the options. The value maps an event name to a list of matchers.

- **Signature.** A callback receives `(input, tool_use_id, context)`. In TypeScript, `context.signal` is
  an `AbortSignal`. In Python the argument is reserved.
- **Answers.** A callback returns the same JSON as a shell hook. It can also return `{async: true}`
  (`async_` in Python) to log without delay, and then it cannot block.
- **Order.** Hooks run first, before deny rules, ask rules, the permission mode, allow rules and
  `canUseTool`. A hook deny applies even in `bypassPermissions` mode.

**Python and TypeScript differ.** Python supports `PreToolUse`, `PostToolUse`, `PostToolUseFailure`,
`UserPromptSubmit`, `Stop`, `SubagentStart`, `SubagentStop`, `PreCompact`, `PermissionRequest` and
`Notification`. TypeScript supports every event in the `HookEvent` union, which has the same list as the
CLI. Python has no `SessionStart` or `SessionEnd` callbacks. Load them as shell hooks through
`setting_sources`, or use the first message in the stream.

**`canUseTool`.** The callback runs only when the permission flow reaches a prompt. Calls that an allow
rule or the permission mode already approved never reach it. Use `PreToolUse` to see every call. The
TypeScript signature is `(toolName, input, {signal, suggestions, blockedPath, decisionReason, toolUseID,
agentID, requestId})`, and it returns a `PermissionResult`.

**Message stream.** The stream from `query()` gives the following `SDKMessage` types:

| Type | What it gives |
|---|---|
| `system` / `init` | `session_id`, `claude_code_version`, `model`, `tools`, `mcp_servers`, `permissionMode`, `agents` |
| `assistant` | `message` as a full API `BetaMessage` with `content`, `usage`, `stop_reason`, plus `parent_tool_use_id` and `agent_id` |
| `user` | Prompts and tool results, `tool_use_result`, `parent_tool_use_id` |
| `stream_event` | Raw stream events, with `includePartialMessages: true` |
| `system` / `compact_boundary` | `trigger`, `pre_tokens` |
| `result` | `usage` (main loop only), `modelUsage` (all models and subagents), `total_cost_usd`, `num_turns`, `permission_denials` |
| Hook, task and tool progress messages | `SDKHookStartedMessage`, `SDKHookResponseMessage`, `SDKTaskStartedMessage`, `SDKTaskNotificationMessage`, `SDKToolProgressMessage`. Hook messages need `includeHookEvents` |

The `parent_tool_use_id` field ties a subagent message to the `Agent` tool call that started the
subagent. Each `assistant` message gives one model response with its usage. The stream does not give the
request body that went to the model.

### 5. Gaps for Claude Code

- **Model request bodies.** Hooks and the SDK stream do not give them. Use `OTEL_LOG_RAW_API_BODIES` with
  `file:<dir>`, or the `api-request` records in the transcript.
- **Model responses in hooks.** Hooks give only `last_assistant_message`. Use the SDK stream, the
  `assistant_response` event, or the transcript.
- **Tokens and cost in hooks.** Hooks give none. Use `api_request` events, `llm_request` spans, the SDK
  `result` message, or `usage` in the transcript.
- **Thinking.** OTel always removes extended thinking. Only the transcript or the SDK stream can hold it.
- **Content in OTel.** Prompts, tool input and tool output are redacted unless each switch is on.
- **Spans.** Spans are beta. Hook spans need a detailed beta mode that the interactive CLI gets only for
  allowlisted organizations.
- **Async and timeouts.** A hook that times out loses its output, and an async hook cannot block. A hook
  may also not fire when the SDK reaches `max_turns`.
- **Retention.** The transcript disappears after `cleanupPeriodDays`.

## OpenAI Codex

### 6. Hooks, notify and OpenTelemetry

**Hooks.** Codex now has a hooks system that copies the Claude Code model. Hooks are on by default. The
source is https://learn.chatgpt.com/docs/hooks.

- **Configuration.** Put hooks in `hooks.json` or in a `[hooks]` table of `config.toml`, in `~/.codex/`
  or `<repo>/.codex/`. Plugins can also ship hooks. Turn hooks off with `[features] hooks = false`.
- **Trust.** A person must review each hook that is not managed in `/hooks` before it runs. A changed hook
  does not run until a person trusts it again. Managed hooks come from `requirements.toml`, and
  `allow_managed_hooks_only = true` ignores all other sources.
- **Handlers.** Codex runs `command` and `mcp_tool` handlers. It parses `prompt` and `agent` handlers but
  skips them. A command hook with `async: true` runs in the background.
- **Common input.** Each payload has `session_id`, `transcript_path` (may be null), `cwd`,
  `hook_event_name` and `model`. Most events also have `permission_mode`.

| Event | When it fires | Event-specific input | What it can return |
|---|---|---|---|
| `SessionStart` | Start, resume, clear, compact, fork | `source` | `additionalContext`. `continue: false` |
| `UserPromptSubmit` | Before a prompt goes to the model | `turn_id`, `prompt` | `decision: "block"` with `reason`, `additionalContext` |
| `PreToolUse` | Before a supported tool call | `turn_id`, `tool_name`, `tool_use_id`, `tool_input` | `permissionDecision: "deny"`, `updatedInput` with `allow`, `additionalContext`, exit 2 |
| `PermissionRequest` | Before Codex asks for approval | `turn_id`, `tool_name`, `tool_input` | `decision.behavior` `allow` or `deny` |
| `PostToolUse` | After a supported tool gives output | `turn_id`, `tool_name`, `tool_use_id`, `tool_input`, `tool_response` | `decision: "block"` replaces the result, `additionalContext`, `continue: false` |
| `SubagentStart` | A subagent starts | `turn_id`, `agent_id`, `agent_type` | `additionalContext`, `systemMessage` |
| `SubagentStop` | A subagent stops | `turn_id`, `agent_id`, `agent_type`, `agent_transcript_path`, `stop_hook_active`, `last_assistant_message` | `decision: "block"` continues the subagent |
| `PreCompact` / `PostCompact` | Before and after compaction | `turn_id`, `trigger` | `continue: false` |
| `Stop` | End of a turn | `turn_id`, `stop_hook_active`, `last_assistant_message` | `decision: "block"` starts a new prompt from `reason` |
| `Interrupt` | The user interrupts a turn | `turn_id`, `permission_mode` | `systemMessage` only. Cannot block |
| `SessionEnd` | Archive, delete, normal close, or 30 minutes idle | `reason` (always `other`) | Advisory only |

Tool hooks cover shell (`Bash`), unified exec, `apply_patch` (also matched as `Edit` or `Write`), MCP
tools and other local function tools. They do not cover hosted tools such as `WebSearch`. The page calls
tool hooks "a useful guardrail, not a complete enforcement boundary". The page gives no version number.

**`notify`.** Set `notify = ["python3", "/path/to/notify.py"]` in the user config. Codex runs the program
with one JSON argument. The only event is `agent-turn-complete`. The fields are `type`, `thread-id`,
`turn-id`, `cwd`, `input-messages` and `last-assistant-message`. The source is
https://learn.chatgpt.com/docs/config-file/config-advanced.

**OpenTelemetry.** Set `[otel]` in `~/.codex/config.toml`. A project config cannot set it. The sources are
https://learn.chatgpt.com/docs/config-file/config-advanced,
https://learn.chatgpt.com/docs/config-file/config-reference and the crate README at
https://github.com/openai/codex/blob/rust-v0.166.0/codex-rs/otel/README.md.

| Key | Meaning | Default |
|---|---|---|
| `otel.exporter` | Log exporter: `none`, `otlp-http` (with `endpoint`, `headers`, `protocol` `binary` or `json`, `tls`), `otlp-grpc` | `none` |
| `otel.trace_exporter` | Trace exporter, same choices | Not stated |
| `otel.metrics_exporter` | `none`, `statsig`, `otlp-http`, `otlp-grpc` | `statsig` |
| `otel.environment` | Environment tag | `dev` |
| `otel.log_user_prompt` | Export prompt text. Otherwise `[REDACTED]` | `false` |
| `otel.log_agent_responses` | Emit `codex.agent_response` with final answer text, up to 65,536 bytes, for main agents and subagents | `false` |
| `otel.log_guardian_assessments` | Emit `codex.guardian_assessment` | `false` |
| `otel.span_attributes`, `otel.tracestate` | Extra span attributes and W3C tracestate members | Empty |

The `otel.log_agent_responses`, `otel.log_guardian_assessments`, `otel.span_attributes` and
`otel.tracestate` keys come from the crate README. The configuration reference page did not show them.

Codex log events include `codex.conversation_starts`, `codex.api_request`, `codex.sse_event`,
`codex.websocket_request`, `codex.websocket_event`, `codex.user_prompt`, `codex.tool_decision`,
`codex.tool_result`, `codex.skill_invocation`, `codex.sandbox_outcome`, `codex.turn_ttft` and
`codex.turn_cost`. The `codex.tool_result` event has a duration, a success flag and an output snippet.
Token counts arrive on the `response.completed` SSE event. The metrics include `codex.tool.call` and
`codex.tool.call.duration_ms`.

Codex emits spans through the Rust `tracing` crate when `trace_exporter` is set. The documentation does not
name the spans. The source of `session_telemetry.rs` at v0.166.0 records `gen_ai.usage.input_tokens`,
`gen_ai.usage.output_tokens`, `gen_ai.usage.cache_read.input_tokens` and
`codex.usage.reasoning_output_tokens` on the span that handles a model response. Codex logs no raw
request bodies or response bodies.

### 7. Rollout files and `codex exec --json`

**Rollout files.** Codex writes each session to
`~/.codex/sessions/YYYY/MM/DD/rollout-<timestamp>-<thread-id>.jsonl`. Archived sessions move to
`~/.codex/archived_sessions/`. The constants are in `codex-rs/rollout/src/lib.rs`. The documentation does
not describe the rollout format. The format below comes from the source at v0.166.0
(`codex-rs/history/src/rollout_payload.rs`, `codex-rs/rollout/src/policy.rs`) and a local file.

Each line has `timestamp`, `ordinal`, `type` and `payload`. The `type` values are these:

| `type` | Content |
|---|---|
| `session_meta` | `id`, `session_id`, `cli_version`, `cwd`, `git`, `model_provider`, `base_instructions`, `source`, `originator` |
| `turn_context` | Model, approval and sandbox settings for each turn |
| `response_item` | Responses API items: `message`, `reasoning`, `function_call`, `function_call_output`, `custom_tool_call`, `custom_tool_call_output`, `agent_message` |
| `event_msg` | Runtime events: `task_started`, `task_complete`, `item_completed`, `token_count` |
| `token_usage_record` | `thread_id`, `turn_id`, `response_id` and `usage` for each model response |
| `inter_agent_communication`, `inter_agent_communication_metadata` | Messages between agents |
| `compacted`, `world_state`, `retained_context`, `security_risk_score`, `dynamic_tools_updated`, `realtime_item` | Compaction and other state |

Codex cuts command output and MCP results at 64 KB in the rollout file. The `reasoning` items hold
`encrypted_content` and a `summary`, so the reasoning text is not readable.

**`codex exec --json`.** The command prints one JSON event on each line. The types are in
`codex-rs/exec/src/exec_events.rs` at v0.166.0.

- **Thread and turn events.** `thread.started` (`thread_id`), `turn.started`, `turn.completed` (`usage`
  with `input_tokens`, `cached_input_tokens`, `cache_write_input_tokens`, `output_tokens`,
  `reasoning_output_tokens`), `turn.failed` and `error`.
- **Item events.** `item.started`, `item.updated` and `item.completed`. Each carries an `item` with `id`
  and `type`.
- **Item types.** The item types are in the table below.

| Item `type` | Fields |
|---|---|
| `agent_message`, `reasoning` | `text` |
| `command_execution` | `command`, `aggregated_output`, `exit_code`, `status` |
| `file_change` | `changes`, `status` |
| `mcp_tool_call` | `server`, `tool`, `arguments`, `result`, `error` |
| `collab_tool_call` | Subagent spawn and messages: `sender_thread_id`, `receiver_thread_ids`, `prompt` |
| `web_search`, `todo_list`, `error` | Query and results, todo items, message |

The TypeScript Codex SDK spawns the CLI and reads the same JSONL events. Its `runStreamed()` call returns
them (https://github.com/openai/codex/blob/rust-v0.166.0/sdk/typescript/README.md). The Python SDK uses
types that are generated from the app server protocol. Its event stream was not checked in detail.

### 8. Gaps for Codex

- **No raw model requests.** No route gives the full request body. The rollout file gives the instructions,
  the items and the usage, and Provevi can rebuild the request from them.
- **Encrypted reasoning.** Reasoning text is encrypted in the rollout file. Only summaries are readable.
- **Hosted tools.** Hooks do not cover `WebSearch` and other hosted tools. The rollout file and
  `exec --json` (`web_search` items) do show them.
- **Undocumented spans.** The span names are not in the documentation, so they can change in any release.
- **Prompt and agent hooks.** Codex skips `prompt` and `agent` handlers.
- **Hook trust.** A hook that is not managed does not run until a person trusts it. A headless run needs
  managed hooks or `--dangerously-bypass-hook-trust`.
- **Truncation.** The rollout file cuts tool output at 64 KB. The `codex.tool_result` event has only a
  snippet.
- **`notify`.** It fires only at the end of a turn.

## Summary table

"Blocks" means the route can stop or change an action. "Observes" means it only reports.

| Agent | Route | Model calls | Tool calls | Prompts | Outputs | Tokens | Subagents | Blocks or observes |
|---|---|---|---|---|---|---|---|---|
| Claude Code | Hooks | No. Only `last_assistant_message` | Yes, input and result | Yes | Tool output yes. Final text only | No | Start and stop, `agent_id`, transcript path | Blocks (`PreToolUse`, `UserPromptSubmit`, `Stop` and others) |
| Claude Code | OTel logs and metrics | `api_request` for each call. Full bodies with `OTEL_LOG_RAW_API_BODIES` | `tool_decision`, `tool_result` | With `OTEL_LOG_USER_PROMPTS` | With content switches | Yes, with cost | `agent_id`, `parent_agent_id`, `subagent_completed` | Observes |
| Claude Code | OTel spans (beta) | `llm_request` span for each call | `tool` spans, output with `OTEL_LOG_TOOL_CONTENT` | On `interaction` span, gated | Gated | Yes | Nested under the Agent tool span | Observes |
| Claude Code | Transcript | Request parameters and every response | Yes, full | Yes | Yes, with thinking | Yes, `usage` on each message | Separate files in `subagents/` | Observes, after the fact |
| Claude Agent SDK | Callbacks and stream | Each response with `usage`. No request body | Yes, from hooks and stream | Yes | Yes, with `stream_event` deltas | Yes, `modelUsage` and cost | `parent_tool_use_id`, `agent_id` | Blocks (hooks, `canUseTool`) |
| Codex | Hooks | No. Only `last_assistant_message` | Yes, except hosted tools | Yes | Tool output yes | No | Start and stop, transcript path | Blocks (`PreToolUse`, `UserPromptSubmit`, `Stop` and others) |
| Codex | `notify` | No | No | `input-messages` | Last message | No | No | Observes, end of turn only |
| Codex | OTel | `codex.api_request`, `codex.sse_event`. No bodies | `tool_decision`, `tool_result` with snippet | With `log_user_prompt` | With `log_agent_responses` | Yes, on SSE events and spans | `agent.type`, parent conversation and turn IDs | Observes |
| Codex | Rollout file | Each response item and `token_usage_record` | Yes, output cut at 64 KB | Yes | Yes. Reasoning encrypted | Yes | `inter_agent_communication`, separate threads | Observes, after the fact |
| Codex | `exec --json` and SDK | Items for each step. No request body | `command_execution`, `file_change`, `mcp_tool_call` | Given by the caller | `agent_message` | For each turn only | `collab_tool_call` | Observes |

## Recommendation for provevi

### Claude Code

1. **Use a hook as the gate.** Register `PreToolUse`, `PostToolUse`, `PostToolUseFailure`,
   `UserPromptSubmit`, `SubagentStart`, `SubagentStop`, `Stop`, `PreCompact`, `SessionStart` and
   `SessionEnd`. Use an `http` hook to a local provevi daemon, or a `command` hook. Keep `PreToolUse`
   synchronous, so that provevi writes the evidence event before the tool runs. Make the others async.
2. **Chain on the hook events.** Use `session_id`, `prompt_id`, `tool_use_id` and `agent_id` as keys. Each
   hook event becomes one evidence event in the hash chain.
3. **Take spans from OTel.** Turn on `CLAUDE_CODE_ENABLE_TELEMETRY`, `CLAUDE_CODE_ENHANCED_TELEMETRY_BETA`,
   the OTLP log and trace exporters, and the content switches. Point the exporter at a provevi OTLP
   receiver. Set `TRACEPARENT` so that the agent spans join the provevi trace.
4. **Keep the model calls as files.** Set `OTEL_LOG_RAW_API_BODIES=file:<provevi dir>`. Hash each request
   file and response file into the chain. The `index.jsonl` file links each response to its transcript
   message.
5. **Use the transcript to fill gaps.** At `Stop`, `SubagentStop` and `SessionEnd`, read `transcript_path`
   and `agent_transcript_path`. Hash the new lines into the chain. Raise `cleanupPeriodDays`, because the
   transcript otherwise goes after 30 days. Do not depend on its line schema. Store the lines as raw
   bytes.
6. **In the Agent SDK, prefer TypeScript.** Use the TypeScript SDK, which has every hook event. Record
   every `SDKMessage` from the stream with `includePartialMessages` and `includeHookEvents`. In Python, add
   `SessionStart` and `SessionEnd` as shell hooks.

### Codex

1. **Use managed hooks as the gate.** Put `PreToolUse`, `PostToolUse`, `UserPromptSubmit`,
   `SubagentStart`, `SubagentStop`, `Stop`, `PreCompact` and `SessionEnd` in `requirements.toml`, so that
   they run without a trust prompt. Use `command` handlers. Keep `PreToolUse` synchronous.
2. **Turn on OTel for spans and model events.** Set `otel.exporter` and `otel.trace_exporter` to the
   provevi OTLP receiver. Set `log_user_prompt` and `log_agent_responses` to `true`. Treat span names as
   unstable and key on attributes such as `conversation.id` and `turn.id`.
3. **Use the rollout file as the main record of model calls.** No other Codex route gives each model
   response with its usage. Tail the rollout file during the session, or read it at `Stop`. Hash each line
   into the chain. Record `response_id` from `token_usage_record` as the model call identity.
4. **Use `codex exec --json` or the SDK for automated runs.** Record each event. Join it to the rollout
   file by `thread_id`.
5. **Record what is missing.** Store a field in the evidence that says the request body and the reasoning
   text are not available for Codex. A reader of the evidence then knows the record has no gap by error.
