# The plan for provevi

This plan builds provevi, the evidence store, in five phases. [The design](provevi-design.md) says what provevi is and why. Each phase ends with tests that pass, docs that match the code, and a site that builds.

## Terms

- **Evidence event:** one line that provrun writes for each step of a run, chained to the event before it by a hash.
- **Store:** where provevi keeps the events, payloads, runs and experiments. A driver gives each kind of store.
- **Sink:** an object that provrun's recorder hands each event line to. A store gives provrun a sink.
- **Seal:** the end of a run's evidence. The store writes the manifest and records the last hash.
- **Copy in MLflow:** the experiments, runs and traces that provevi writes into MLflow's tables, for MLflow's viewer.

## How the packages depend on each other

provrun depends on provevi, and provevi depends on provcat. provevi never imports provrun.

| Package | Owns |
|---|---|
| provcat | Reading and writing catalog URIs, with hooks at each lifecycle spot. |
| provrun | Running an agent, granting and staging its inputs, and deciding what each event means. The recorder that builds the hash chain stays here. |
| provevi | The format of an event line, the stores, the checks, the MLflow copy, and the span receiver. |

## Phase 1: the package, and the `file` store

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

Write provevi's own tables, as [the design](provevi-design.md#the-run-record) gives them. They hold experiments, runs, run settings, events, the URIs in events, payloads and assessments. Grants, refusals, inputs and outputs are views over the events.

1. Write the tables, with a schema version in the file and a migration step.
2. Write the `sqlite` store. Its sink writes each event as it happens. It writes the run row at the start and the last hash and manifest hash at the seal.
3. Keep the payloads in a folder, each named by its hash, as the `file` store does. Record each one in the payloads table.
4. Write the bundle files at the seal, so that provrun commits the same bundle through provcat as before.
5. Add queries to the library and to `prove`. They find the runs that read a URI, the runs of an experiment, and the grants and refusals of a run.
6. Choose the store from a URI, such as `sqlite:///path/evidence.db`, given to `run_file`, to `provr run --store`, or in the run file.

**Tests:** a run in the `sqlite` store verifies, and its bundle equals the bundle of the same run in the `file` store. A changed or removed row fails `verify`. Two runs at once write one file without losing an event.

**Done when:** a query across runs, such as "which runs read this URI", works from the library and from `prove`.

## Phase 3: the copy in MLflow

Write each experiment, run and trace into MLflow's tables in the same SQLite file, as an optional extra, `provevi[mlflow]`.

1. Find or make the MLflow experiment by its unique name, such as `acme/demo/dev/revenue-agent`, and keep MLflow's number in the experiments table.
2. Write MLflow's run row with provrun's run ID, using MLflow's own row model, and resume it with `start_run(run_id=...)`. Write the settings, tags, metrics and inputs through MLflow's API. An input's digest is the first 32 characters of its SHA-256, and its source holds the whole URI.
3. Write an OpenTelemetry span exporter that writes each span of the run into MLflow, linked to the run.
4. Copy assessments on a trace or a span into MLflow.
5. Set `MLFLOW_DISABLE_AGENT_HINT=1`, flush before reading back, and fail loudly on a tracking URI that the installed MLflow cannot write.
6. Write `prove ui`, which opens MLflow's viewer on the store.

**Tests:** a run shows in MLflow under provrun's run ID, with its settings, inputs, traces and assessments. The copy can be deleted and rebuilt from provevi's tables. A test runs against the pinned MLflow minor release.

**Done when:** a real PydanticAI run and a real LiteLLM run show in MLflow's viewer, and verify with `prove verify`.

## Phase 4: experiments in the catalog

1. Store an experiment as a catalog object, at `experiment://<org>/<domain>/<env>/<name>`, holding its agent spec and its description.
2. Let a run file name its experiment by URI. provrun pins it, and the run records the pinned version.
3. Write `prove experiments` and `prove experiment show`.

**Tests:** two runs of one experiment version name the same pin. A run after the experiment changes names the new pin.

**Done when:** an experiment has versions, and each run names the version it ran.

## Phase 5: Claude Code and Codex

1. Write the span receiver as a context manager. It listens on `localhost`, at a free port, for the length of a run.
2. Let provrun start Claude Code or Codex in a run. It sets their hooks, their OTLP address and `TRACEPARENT`, so that everything they send lands in that run.
3. Turn each hook event into an evidence event. At the end of the session, hash the new transcript or rollout lines into the chain as raw bytes.
4. Keep Claude Code's full model requests and responses when the run asks for them, as payloads.
5. Record what each agent cannot give, such as Codex's encrypted reasoning, as a known gap in the evidence.

**Done when:** a Claude Code run and a Codex run each give a sealed, verified run with their spans in the store.

## Checks before each commit

1. Run the tests of all three packages.
2. Ask an adversarial reviewer, a subagent on the strongest model, to break the change. Fix each finding, with a test for each.
3. Update the docs in the same commit, and check that the site builds, as `CLAUDE.md` says.

## Out of scope for this plan

- Redaction rules. The first build stores content as it is, but only in payloads named by hash, never in span attributes.
- A long-lived `prove serve` daemon. The receiver is written so that it can run as one later.
- Stores other than `file` and `sqlite`, such as a shared database server.
