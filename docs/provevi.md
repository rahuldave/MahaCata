---
title: provevi reference
---

This page lists provevi's commands, stores, tables, policy keys, exit codes and errors. [What provevi does](provevi-features.md) shows the features with examples, [the tour](provevi-tour.md) runs them, and [the design](provevi-design.md) explains how the parts fit together.

provevi stores, reads and checks the evidence of provrun's runs. Its command is `prove`. Phases 1, 2 and 4 of [the plan](provevi-plan.md) are built. Phase 1 gave the package, the `file` store and the checks. Phase 2 gave the `sqlite` store, which answers questions across runs. Phase 4 gave the copy in MLflow, for MLflow's viewer.

Of Phase 3, provevi's part is built. It reads and checks the telemetry policy. provrun applies steps 2 to 10. They record the telemetry variables, own the providers, turn off each library's own telemetry, and find what cannot be turned off. Step 9 sends provrun's spans to approved destinations, using provevi's rule that matches a variable to a destination. For MLflow, provrun calls Phase 4's copy when a run is sealed.

Experiments as catalog objects and the span receiver come in later phases.

## Terms

- **Run folder:** the folder of one run, under the runs root. It holds `evidence/`, `output/` and, when the run has one, `bundle.json`.
- **Evidence:** the file `events.jsonl`, the folder `payloads/` and the file `manifest.json`, in `evidence/`.
- **Store:** an object that keeps the evidence of runs. `FileStore` writes the files in each run folder, as provrun always has. `SqliteStore` also keeps every run in one SQLite file.
- **Store URI:** the name of a store. `file` is the file store, and `sqlite:///evidence.db` is a SQLite file.
- **Experiment:** the runs of one agent in one org, domain and environment, such as `acme/demo/dev/revenue-question`.
- **Writer:** what a store gives for one run. provrun's recorder hands it each event line and each payload, and seals it at the end.
- **Committed bundle:** a copy of the run's evidence that provrun commits through provcat, in another store.
- **Telemetry policy:** the operator's file that says what may record or send data from a run, besides the evidence.
- **Destination:** a place outside the evidence that spans may go to, such as Logfire or an OpenTelemetry endpoint.
- **Copy in MLflow:** a sealed run, rebuilt from its evidence in MLflow's tables, for MLflow's viewer. The evidence stays the record.

## How the packages depend on each other

provrun depends on provevi, and provevi depends on provcat. provevi never imports provrun.

provrun still decides what each event means, and builds the hash chain. provevi holds the event format, the stores and every check.

## Check a run from a program

Each check is a library function. It returns the problems it finds, and prints nothing. A malformed run folder, such as a `bundle.json` that is not JSON, also gives a problem, not an exception.

```python
from provevi import find_run, verify_run

check = verify_run(find_run("run-20261031T182026Z-f9c3b25f"))
print(check.ok, check.problems)
```

`verify_run` checks the evidence first, then the outputs, then the committed bundle:

- `verify` checks the hash chain, the sequence, the payloads and the manifest of an evidence folder, or of a run that a store reads back.
- `verify_outputs` checks that `output/` holds exactly the recorded outputs, with their bytes.
- `verify_bundle` reads the events and the manifest from the committed bundle, through a provcat `Catalog`, and compares them with the folder.

Pass `store=` to check the run as a store holds it, too. With a `SqliteStore`, `verify_run` checks the rows of the run, and that the files in the run folder are the same as the rows.

Pass `catalog=` as a `Catalog`, or as a function that opens one. A function opens no catalog when the run has no bundle. Pass `bundle=` with the pinned URI of the bundle, when you do not trust the `bundle.json` in the run folder. `require_bundle=True` fails a run that has no bundle to compare with.

[provrun's page](provrun.md) lists what the evidence check covers.

## Write a run through a store

`run_file` and `run_agent` in provrun take `store=`. Without it, `run_file` opens the store that the run file names in `run: store:`, and without that it uses `FileStore()`. `provr --store URI run` takes the place of the run file's store.

```python
from provcat import Catalog
from provevi import SqliteStore
from provrun import RunFile, run_file_sync

outcome = run_file_sync(RunFile.load("run.yaml"), catalog=Catalog.open(), store=SqliteStore("evidence.db"))
```

A store is a subclass of `EvidenceStore`. Its `open_run(run_id, folder)` returns a `RunWriter`, which appends lines, stores payloads by their SHA-256, and seals the run with its manifest.

`open_store(uri)` gives the store of a store URI. A store can also read a run back, with `read_run(run_id)`, which returns a `RunRecord`. The checks read a run through a `RunRecord`.

Every store must also keep `events.jsonl`, `payloads/` and `manifest.json` in `folder`. provrun commits that folder as the bundle. A store that keeps no `events.jsonl` and `manifest.json` fails the run when it closes, in both modes, because nothing could verify the run. A seal that fails also fails the run in both modes.

Before the seal, provrun treats any error from a writer as a lost event. The strict mode stops the run, and the lenient mode records the loss in the manifest.

## Keep the evidence in SQLite

`SqliteStore(path)` keeps the evidence of many runs in one SQLite file. Its store URI is `sqlite:///relative/path.db`, or `sqlite:////absolute/path.db`, as SQLAlchemy and MLflow write them.

- A relative path is fixed against the current folder when the store opens. So a run file's `store:` is relative to the folder you run `provr` in, not to the run file.
- `~` is your home folder.
- A URI with a query or a fragment, such as `?mode=ro`, is refused.

How the store writes:

- **Each event is a row as it happens.** The row is committed only once the line is also in the run's `events.jsonl`. If the file write or the commit fails, both are taken back, and the recorder counts the event as lost. If the process dies between the two, the file holds one more line than the rows, and the check against the store reports it.
- **The run folder holds the same files as the `file` store.** So the bundle that provrun commits is the same in both stores. The payload bytes live only in the run's `payloads/` folder, and the table records their hash, size and place.
- **Many runs can write one file at once,** from threads or from processes. A writer waits up to a minute for another one.
- **The file records its schema version.** Opening a file to write brings an older version up to date, one step at a time. A file of a newer provevi is refused.
- **`SqliteStore(path, create=False)` only reads.** It writes nothing, and refuses a missing file, a file that holds no provevi evidence, and a file of another schema version. `prove` opens the store this way.
- **provevi uses SQLite's WAL mode only.** A writer puts the file in WAL mode, which stays in the file. Every connection checks it, and a file in any other mode is refused. Even to read, SQLite writes a side file next to a WAL file, so the file's folder must be writable.
- **A copy may leave WAL mode.** A copy made with `VACUUM INTO` is in another mode, so `prove` refuses it. Open the copy once to write, as `SqliteStore(path)` or `provr run` does, and it is in WAL mode again. A copy in a folder you cannot write must first be copied to one you can.
- **The schema is checked when the store opens.** provevi's tables, indexes and views must be exactly what its upgrade steps make. A file whose schema was changed is refused, because the queries read the views.
  - An extra index on a provevi table is allowed, since it cannot change what a query returns.
  - A trigger is refused if it is on a provevi table, or if its text names one.
- **A run sealed without its ending event** has the status `incomplete`.
- **The table names start with `provevi_`,** so the same file can also hold MLflow's tables.

| Table or view | What it holds |
|---|---|
| `provevi_runs` | One row for each run: its experiment, actor, status, times, mode, folder, last hash, manifest, and where its bundle was committed. |
| `provevi_run_params` | The agent, backend, model, mode, tools and limits of a run. |
| `provevi_experiments` | One row for each experiment, made by its first run. It holds that run's backend, model, tools and limits, and the SHA-256 of its whole agent spec. |
| `provevi_events` | Each event: its line, and its fields as columns, with the trace and span IDs. |
| `provevi_event_refs` | Each URI that an event names. |
| `provevi_payloads` | The SHA-256, size and place of each payload. |
| `provevi_assessments` | Judgements on a run or an event. Nothing writes this table yet. |
| `provevi_grants`, `provevi_refusals`, `provevi_inputs`, `provevi_outputs` | Views over the events. |

The agent's `settings` may hold secrets, so the run's settings and the experiment leave them out. The `run_created` event still holds the whole spec, in the file as in `events.jsonl`.

A changed or deleted row fails the check:

- the chain covers each event's line;
- each row's columns and URIs must match its line;
- the run's manifest must match its hash;
- the run's org, domain, environment, actor, mode, experiment, status and times must be what its events say;
- its settings, its experiment's name and parts, and its payload sizes must match its events and files;
- where its bundle was committed must match the run's `bundle.json`, if the store recorded it;
- the schema must be provevi's own, with no trigger on its tables.

The check does not cover the run's folder, the time of its seal, or who made an experiment and when. Nothing in the events fixes those.

The store answers questions across runs:

| Method | Returns |
|---|---|
| `runs_reading(uri)` | The runs that staged `uri` as an input, by its URI or by a pinned version of it. |
| `runs_of_experiment(name)` | The runs of an experiment, by its name or its `experiment://` URI. |
| `experiments()` | The experiments in the file. |
| `run(run_id)` | The run's row, with its settings as `params`. |
| `access(run_id)` | The grants and refusals of a run, in order. |
| `inputs(run_id)` | The inputs that a run staged, with their pinned URIs. |

## Check a telemetry policy

During a run, provrun turns off each path by which a library records or sends data, such as MLflow's autolog and LiteLLM's callbacks. Phase 3 of [the plan](provevi-plan.md) gives the steps, and [Library telemetry](provevi-research/library-telemetry.md) gives the research. The operator approves exceptions in a telemetry policy. provevi reads and checks the policy, and provrun applies it.

A policy is a YAML or JSON file:

```yaml
version: 1
libraries:
  pydantic-ai:
    versions: ">=2.64,<2.65"
    telemetry: [logfire]
  litellm:
    versions: "==1.83.0"
destinations:
  logfire:
    kind: logfire
    receives: usage
    endpoint: https://logfire-us.pydantic.dev
hosts:
  - api.anthropic.com
  - .amazonaws.com
  - logfire-us.pydantic.dev
```

| Key | What it holds |
|---|---|
| `version` | `1`, the format of the file. |
| `libraries` | Each approved library, by its distribution name. `versions` is the versions that were tested, as a version specifier. `telemetry` is the destinations that its instrumentation may feed. |
| `destinations` | Each place outside the evidence that spans may go to. `kind` is `logfire`, `otlp` or `mlflow`. `receives` is `names`, `usage` or `bodies`. `endpoint` is an http or https URL, and its host must be in `hosts`. An `mlflow` endpoint may also be a database URL, such as `postgresql://db.example.com/mlflow` or `sqlite:////srv/mlflow.db`. An `mlflow` destination may leave the endpoint out, for the copy in the store's own file. An endpoint names no user and no password, because the policy holds no secrets. |
| `hosts` | The hosts that destinations may send to. provevi checks each endpoint's host against this list, and the list is a record for the firewall. Neither provevi nor provrun limits where a run connects, because that is the job of the firewall or the container. An entry with a leading dot, such as `.amazonaws.com`, names each host under the domain, but not the domain itself. A dotted entry needs two labels at least, so `.com` is refused. |

A library or a destination that the policy does not name gets nothing. A run with no policy file gets `TelemetryPolicy.deny_all()`, which approves nothing. The loader refuses an unknown key, a key named twice in one mapping, and a `version` that is not the integer `1`. It reports every problem in the file at once. A library installed as a local build, such as `2.64.0+patched`, passes only if `versions` names the local build.

**What a destination receives.** `names` is the names, models, providers and IDs of each span, its error type, its finish reasons, and the server's address and port. `usage` adds the token counts, the cost, and the temperature, `max_tokens` and `top_p` of the request. `bodies` adds every other attribute, such as the messages and the tool arguments.

An attribute that provevi does not know counts as a body, so a new attribute never leaves at a lower level.

provevi also checks each value. A `names` attribute must hold one word of at most 200 characters, with no space. A `usage` attribute must hold numbers. Any other value counts as a body, because it may hold text from a prompt. `attributes_for(attributes, level)` gives what a destination receives. `split_body(attributes)` takes the bodies out of a span's attributes, so that the evidence keeps them in a payload, not in the event.

A secret or a path can pass the test of a name, such as `https://user:password@host` or `token=x`. `plain_name(value)` is stricter. It refuses a URL, a user and password, a setting with `=` and a path from `/`. It also refuses a name such as `ns:model@tag`, which looks like `user:password@host`. A number and a yes or no are plain.

`plain_span_name(name)` takes one or two plain words, such as `chat gpt-4o`. The copy in MLflow uses both below `bodies`, and so do provrun's senders.

**Narrow the policy for one run.** `policy.narrow(section)` reads a run file's `telemetry` section. The section has the keys `libraries`, `destinations` and `hosts`, and each one replaces the policy's list with a smaller one. A destination can receive a lower level, and a library can feed fewer destinations. The section cannot change versions, kinds or endpoints.

A destination whose endpoint is on a host that the section cuts is dropped too, and no library feeds it. A destination that the section names itself, in `destinations` or in a library's `telemetry`, is refused instead, because the section contradicts itself. If the section would widen the policy in any way, `narrow` raises `ProveviError`.

**Match a variable to a destination.** A run finds its destinations in the user's variables, such as `OTEL_EXPORTER_OTLP_ENDPOINT` and `MLFLOW_TRACKING_URI`. `policy.match_destination(kind, url)` gives the destination of that kind whose endpoint is the URL, or `None`. provrun sends nothing to a URL with no match, and writes `telemetry_blocked` with the path `destination`.

Before the match, `normal_endpoint` puts both URLs in one form. It lowercases the scheme and the host, and adds the default port. It drops a trailing `/`, and a database driver such as `+psycopg`. It drops the user and the password of a database URL. A sqlite URL keeps only its absolute path, with its trailing `/` if it has one.

`normal_endpoint` refuses a URL that two URL parsers could read as two places. Such a URL matches no destination. It refuses:

- a backslash, a space, a control character or a `#`, because a database library reads no fragment, so `#@evil.example` names another host;
- a host that is not ASCII, because the two standards of international names read some hosts, such as `faß.de`, as two hosts;
- a user or a password in an `http` or `https` URL;
- a `%` in the host, or a list of hosts;
- a port of 0, or an empty port;
- a path with `%`, with `.` or `..` segments, or with `//`;
- a query, because a query such as `?host=` can name another server. A database URL may still carry a few settings that name no place. They are `sslmode`, `connect_timeout` and `application_name` for Postgres, and `charset` and `connect_timeout` for MySQL. The match ignores them, except `sslmode`. So a policy endpoint that ends in `?sslmode=require` matches only a URL that asks for TLS;
- an `mssql` URL with no port or no database, because ODBC can read its host as the name of a data source;
- a driver such as `+x` on an `http` or `https` scheme;
- a sqlite URL with a relative path.

Some variables send a matched URL somewhere else. `HTTPS_PROXY` sends it through a proxy, and `PGHOSTADDR` makes Postgres connect to another address. provrun's senders ignore the proxy variables, and a run records which were set. [provrun's page](provrun.md#control-of-library-telemetry) says how. The copy in MLflow refuses to run while one of them is set, as the next section says.

A policy endpoint holds no user and no password, because the policy holds no secrets. Its host must be in `hosts`.

`otlp_traces_target(environ)` gives the protocol and the address to which provrun's OpenTelemetry exporter sends traces. It follows the OpenTelemetry specification:

- `OTEL_EXPORTER_OTLP_TRACES_ENDPOINT` wins, as it is.
- Otherwise `OTEL_EXPORTER_OTLP_ENDPOINT` is used, with `/v1/traces` added over HTTP.
- The protocol comes from `OTEL_EXPORTER_OTLP_TRACES_PROTOCOL`, then `OTEL_EXPORTER_OTLP_PROTOCOL`, then `OTEL_TRACES_EXPORTER`. It is `http/protobuf` when none is set.
- A variable that is set but empty counts as not set.

It gives `None` when neither endpoint variable is set. provrun then makes no OTLP exporter. An OpenTelemetry library left to itself would send to `localhost:4318` instead.

An MLflow destination needs the tracking URI too. provrun sets it on the client itself, because MLflow's default, when `MLFLOW_TRACKING_URI` is unset, is not the evidence store's file.

This program matches three URLs against a policy with two destinations:

```python
from provevi import TelemetryPolicy, otlp_traces_target

policy = TelemetryPolicy.from_dict({
    "version": 1,
    "destinations": {
        "collector": {"kind": "otlp", "receives": "usage", "endpoint": "https://otel.example.com/v1/traces"},
        "tracking": {"kind": "mlflow", "receives": "bodies", "endpoint": "postgresql://db.example.com/mlflow"},
    },
    "hosts": ["otel.example.com", "db.example.com"],
})
protocol, url = otlp_traces_target({"OTEL_EXPORTER_OTLP_ENDPOINT": "https://OTEL.example.com:443/"})
print(protocol, url, "->", policy.match_destination("otlp", url).name)
print(policy.match_destination("mlflow", "postgresql+psycopg://u:pw@db.example.com:5432/mlflow").name)
print(policy.match_destination("otlp", "https://evil.example@otel.example.com/v1/traces"))
```

It prints:

```
http/protobuf https://OTEL.example.com:443/v1/traces -> collector
tracking
None
```

**Record what was blocked.** provrun writes a `telemetry_blocked` event for each attempt that the policy blocked. `blocked_data(path, library=..., detail=...)` gives its data, where `path` names what was blocked, such as `litellm.callbacks` or `network`. `policy.digest()` is the SHA-256 of the policy, for a run to record which policy it ran under.

`prove policy FILE` checks a policy file, and checks the installed version of each approved library. The file below is in the repository:

```
$ uv run prove policy packages/provevi/tests/data/telemetry-policy.yaml
packages/provevi/tests/data/telemetry-policy.yaml: sha256 927ccec8f74bf254aaae11282df1f64dfc73f6291e6dd50b3c83faa22e313b03
  library litellm ==1.83.0 feeds nothing
  library pydantic-ai >=2.64,<2.65 feeds logfire
  destination logfire (logfire) receives usage at https://logfire-us.pydantic.dev
  hosts: api.anthropic.com, .amazonaws.com, logfire-us.pydantic.dev
```

If an approved library is installed at a version that the policy does not name, `prove policy` says so and exits with 1. With `versions: ">=2.65"` for pydantic-ai, it printed:

```
prove: pydantic-ai 2.64.0 is installed, but the policy approves >=2.65
```

`prove telemetry RUN` prints the `telemetry_blocked` and `telemetry_problem` events of a run, one on each line, with the sequence number and the time. A blocked attempt shows `blocked`, the path, the library and the detail. A problem, such as a library that set up its own telemetry, shows `problem` and its text. If the run has neither, it prints `RUN: no blocked attempts and no problems`.

## Copy a run into MLflow

The evidence is the record of a run. MLflow holds a copy, so that its viewer can show the run, search runs and add judgements. `copy_run(store, run_id, policy)` makes the copy, and `prove mlflow RUN` calls it. The copy needs the extra `provevi[mlflow]`, which installs MLflow 3.17.

**Where the copy goes.** `MLFLOW_TRACKING_URI` names the place, and the policy must approve it:

- An `http` or `https` URL is an MLflow tracking server.
- A database URL, such as `postgresql://db.example.com/mlflow`, is MLflow's tables in that database.
- With the variable unset, the copy goes into the `sqlite` store's own file, beside provevi's tables. The policy approves this with an `mlflow` destination that has no endpoint.

The destination that matches gives the level. A place that no destination matches gets nothing, and `prove mlflow` exits with 1. `prove mlflow` reads the policy from `--policy FILE`, or else from the file that `PROVRUN_TELEMETRY_POLICY` names, as provrun does. With neither, the policy approves nothing.

**What the copy holds.** The copy makes an MLflow experiment named for the run's experiment, and one MLflow run, tagged `provrun.run_id`. MLflow picks its own run ID, so provevi finds the copy by the tag. The run's trace keeps provrun's trace and span IDs. Each step is a span from its start to its end, under the root span of the run.

| Level | What the copy holds |
|---|---|
| `names` | The experiment, the run with its tags, the settings that are names, but no name that holds a URL, a password, a setting such as `token=` or a path from `/`. A name such as `ns:model@tag` looks like `user:password@host`, so it stays out too, each input's SHA-256, and the spans with their names, models and IDs. A part of the experiment's name that is not a name, such as an agent called with free text, shows as `agent`. An input whose name is not a name is called `input`. |
| `usage` | Also the token counts on the spans, and the counts of tokens and calls as the run's metrics. |
| `bodies` | Also every other setting, each input's URIs, each step's data and URIs, the request and response of each model call, and the events that had no span, as events of the root span. |

The store's assessments of the run and of its spans go onto the trace. Below `bodies`, an assessment whose name is not a name stays out, and one whose author is not a name shows `provevi` as its author. Its value goes in at `names` if it is a name or a yes or no, and at `usage` if it is a number too. A rationale goes only with `bodies`.

**A library's spans.** provrun writes the span of an approved library, such as PydanticAI, as a `span_recorded` event. The copy makes it a span of the trace, with its own name, times and status. It does so only if the policy lets that library feed the MLflow destination. Its names and usage come from the event. With `bodies`, its other attributes and its events come from the payload.

**Which library made a span.** A span names its library only by its scope, a label that its code picks, such as `pydantic-ai`. `span_libraries(scope)` gives the names that the policy may know that library by. It never guesses, so a span it cannot place goes nowhere:

- the scope's own name counts only if a library of that name is installed, because any code can call its scope `litellm`. A name with a dot, such as `pydantic.ai`, is a module path, so it does not count this way;
- the library whose files hold the scope's module counts, such as `pydantic-ai-slim` for `pydantic_ai`. A made-up module, such as `pydantic_ai.evil`, is held by no library;
- when Python cannot read what is installed, no name counts.

`distribution_of(module)` gives one name for a report, such as the library that a blocked attempt came from. It guesses when it cannot tell, so it decides nothing that the policy approves.

**What the copy guards.** Before it writes, the copy checks the run's chain, as `prove verify` does, and refuses a run that does not verify. A run ID must be letters, digits, `_`, `.` and `-`, because the copy searches MLflow for it.

Some variables would send the copy somewhere the policy did not approve. The copy refuses to run while one is set. For a tracking server, these are `HTTP_PROXY`, `HTTPS_PROXY` and `ALL_PROXY`, in either case. For Postgres, they are `PGHOST`, `PGHOSTADDR`, `PGPORT`, `PGDATABASE`, `PGSERVICE`, `PGSERVICEFILE`, `PGSYSCONFDIR`, `PGSSLMODE` and `PGREQUIRESSL`. For MySQL, they are `MYSQL_HOST`, `MYSQL_TCP_PORT`, `MYSQL_UNIX_PORT` and `MYSQL_PWD`.

Variables that change which servers the copy trusts are refused too. They are `MLFLOW_TRACKING_INSECURE_TLS`, `REQUESTS_CA_BUNDLE`, `CURL_CA_BUNDLE` and `MLFLOW_TRACKING_SERVER_CERT_PATH`, and `MLFLOW_TRACKING_AUTH`, which loads a plugin.

`refuse_redirects(url, environ)` makes this check alone, and raises `ProveviError` naming the variables. provrun calls it as a run starts, so that the run records a copy that would be refused.

macOS and Windows can name a proxy in the system settings, with no variable set. The copy sets `NO_PROXY` to `*` while it runs, so MLflow's HTTP client uses no proxy.

It writes whole spans through MLflow's store, so MLflow adds nothing of its own, such as the user name or the git commit. MLflow's usage telemetry is off. MLflow prices a model call with LiteLLM if it can import it, and importing LiteLLM fetches a price table from the network. So the copy keeps LiteLLM out, and MLflow uses the price table that it ships.

A SQLite file is put in WAL mode before MLflow opens it, with or without a driver such as `sqlite+pysqlite`. The copy puts back the MLflow variables that it sets. MLflow's artifacts go in a folder next to the SQLite file, not in the current folder.

If a copy fails part way, it deletes its run and its trace, as a rebuild deletes the first copy. An experiment that it made stays, empty. A database that cannot be reached gives an error, with no password in it. A store makes one copy at a time. The copy holds a lock file next to the store's file, or in the runs root for the `file` store.

**Copy again to rebuild.** A second copy of the same run deletes the first, with its trace, and writes it again. So the copy can be deleted and rebuilt from the evidence at any time.

In a database, the first copy's rows are deleted for good, with its inputs, and each dataset that no run names any more. MLflow shares one dataset row among the runs of an experiment. So a dataset's digest is of its name and its source, and a copy never points at a row written at another level. A tracking server only marks the first copy's run as deleted, and keeps its params and tags until its owner runs `mlflow gc`.

This example runs the agent of [the provrun tour](provrun-tour.md) on a local file, with the model's answer mocked, into a `sqlite` store. The policy file `/tmp/provevi-mlflow/policy.yaml` approves the store's own file at `usage`:

```yaml
version: 1
destinations:
  viewer:
    kind: mlflow
    receives: usage
```

Without the policy, nothing is copied. With it, the run is copied, and a second copy replaces the first. A tracking URI that the policy does not name is refused:

```
$ export S=sqlite:////tmp/provevi-mlflow/evidence.db
$ uv run prove --store $S mlflow run-20261102T191147Z-d573ce99
prove: the telemetry policy approves no mlflow destination for the store's own file (MLFLOW_TRACKING_URI is unset)

$ uv run prove --store $S mlflow run-20261102T191147Z-d573ce99 --policy /tmp/provevi-mlflow/policy.yaml
run-20261102T191147Z-d573ce99: copied to MLflow at sqlite:////private/tmp/provevi-mlflow/evidence.db, destination viewer, level usage
  experiment 1, MLflow run 542b1c611ed14d38aff7784d9557ca9f, trace tr-369c7182fc0007833cd3d866168b5e95 with 7 spans

$ uv run prove --store $S mlflow run-20261102T191147Z-d573ce99 --policy /tmp/provevi-mlflow/policy.yaml
run-20261102T191147Z-d573ce99: copied to MLflow at sqlite:////private/tmp/provevi-mlflow/evidence.db, destination viewer, level usage
  experiment 1, MLflow run f2845cc102a24a7e8c7e673f645a1a8d, trace tr-369c7182fc0007833cd3d866168b5e95 with 7 spans
  deleted the earlier copy, MLflow run 542b1c611ed14d38aff7784d9557ca9f

$ MLFLOW_TRACKING_URI=https://mlflow.example.com uv run prove --store $S mlflow run-20261102T191147Z-d573ce99 --policy /tmp/provevi-mlflow/policy.yaml
prove: the telemetry policy approves no mlflow destination for MLFLOW_TRACKING_URI https://mlflow.example.com:443
```

On macOS, `/tmp` is a link to `/private/tmp`, so the store's path shows as `/private/tmp`.

`prove ui` opens MLflow's viewer on the store's own file. It listens on 127.0.0.1 only, at port 5000 unless `--port` gives another. It passes MLflow no `MLFLOW_` or `OTEL_` variable of the shell:

```
$ uv run prove --store $S ui --port 5088
MLflow's viewer on /private/tmp/provevi-mlflow/evidence.db: http://127.0.0.1:5088
```

provrun makes the copy itself when a run is sealed, if the policy approves an MLflow place, as [provrun's page](provrun.md#copy-a-sealed-run-into-mlflow) says. `prove mlflow` makes the copy again, or makes it for a run with no approved place at the time.

## The command `prove`

`prove` reads and checks. provrun writes the runs. The one command that writes, `prove mlflow`, writes only the copy in MLflow.

| Command | What it does |
|---|---|
| `prove runs` | Lists the run IDs in the store, oldest first. |
| `prove runs --reading URI` | Lists the runs that staged the URI as an input. |
| `prove runs --experiment NAME` | Lists the runs of an experiment. |
| `prove experiments` | Lists the experiments. |
| `prove access RUN` | Prints the grants and refusals of a run. |
| `prove show RUN` | Prints the events of a run. |
| `prove telemetry RUN` | Prints the attempts that the telemetry policy blocked in a run, and the telemetry problems that the run found. |
| `prove policy FILE` | Checks a telemetry policy file, and the installed version of each library it approves. |
| `prove verify RUN` | Checks the evidence, the outputs and the committed bundle. It takes `--config`, `--tenant`, `--bundle` and `--require-bundle`, as `provr verify` does. |
| `prove mlflow RUN` | Copies a sealed run into MLflow, at the place that `MLFLOW_TRACKING_URI` names and the policy approves. It takes `--policy FILE`. |
| `prove ui` | Opens MLflow's viewer on the `sqlite` store's own file, on 127.0.0.1. It takes `--port`. |

`--store URI` chooses the store, before the command, as in `prove --store sqlite:///evidence.db runs`. The default is the file store under the runs root. The file store has no index of runs, so `--reading`, `--experiment`, `experiments` and `access` need a `sqlite` store.

`RUN` is a run ID under the runs root, or the path of a run folder. With a `sqlite` store, a run ID that is not under the runs root is found at the folder that the store records. `show` reads the events from the store when the run is in it. A run ID that names a link under the runs root is refused, so a link cannot pass off one run as another. The runs root is the same as provrun's. It is `--runs`, or else `PROVRUN_RUNS`, or else `$XDG_DATA_HOME/provrun/runs`, or else `~/.local/share/provrun/runs`.

`provr verify` calls the same code, so the two commands give the same answer.

The repository holds a run that provrun wrote before provevi existed, in `packages/provevi/tests/data`. `prove` reads and verifies it:

```
$ uv run prove --runs packages/provevi/tests/data runs
run-20261031T182026Z-f9c3b25f

$ uv run prove --runs packages/provevi/tests/data verify run-20261031T182026Z-f9c3b25f
run-20261031T182026Z-f9c3b25f: the evidence and the outputs verify; no committed bundle to compare with

$ uv run prove --runs packages/provevi/tests/data verify run-20261031T182026Z-f9c3b25f --require-bundle
prove: no committed bundle to compare with, and a bundle is required
```

`prove show` prints one line for each event, with its sequence number, time, type, component, references and data. The two lines below are cut short at the right:

```
$ uv run prove --runs packages/provevi/tests/data show run-20261031T182026Z-f9c3b25f
   0 2026-10-31T18:20:26.242767Z run_created              RunContext               run://acme/demo/dev/run-20261031T182026Z-f9c3b25f {"agent": "q", ...
   ...
   6 2026-10-31T18:20:35.505766Z run_completed            RunContext               run://acme/demo/dev/run-20261031T182026Z-f9c3b25f {}
```

## Exit codes

| Code | Meaning |
|---|---|
| 0 | The command worked, and the run verifies. |
| 1 | The run does not verify, or there is no such run, or the catalog cannot be opened. Also a copy in MLflow that the policy does not approve, or that MLflow cannot take. Also a telemetry policy that cannot be read, or an approved library at a version that the policy does not name. Also a store URI that names no store, a store file that cannot be opened or was changed, and a question that the file store cannot answer. |
| 2 | `prove show` or `prove telemetry` cannot read the events, or met a line that is not an event. Also a command line that argparse rejects. |

## Errors

Every error that provevi raises on purpose is a `ProveviError`. `RunNotFound` is also a `LookupError`. provrun's own `EvidenceError` and `RunNotFound` are subclasses of provevi's, so code that catches provevi's errors also catches provrun's.
