---
title: A runnable tour of provrun
---

This tour follows one file from a store, through the provcat cache, into a run of an agent, and into the evidence of that run. It needs no model, no key and no network, except in the last step, which is optional. The agent answers with the LiteLLM setting `mock_response`. The store is moto, a local server that speaks the S3 protocol, which the tests also use.

A script, `tours/provrun/run.sh`, runs every step and prints each command before its output. The output below is from that script. The run IDs, times, version IDs and hashes differ on each run. [What provrun does](provrun-features.md) shows the features first, and [provrun.md](provrun.md) is the reference. [The provcat tour](tour.md) shows provcat alone.

## Terms

| Term | Meaning |
|---|---|
| cache | The folder where provcat keeps the files that it fetched. |
| pinned URI | A URI with a fixed version, for example `?at=version:d3ae2233-...`. |
| snapshot | A read-only copy of one pinned version, in a folder named by the SHA-256 of its content. |
| run folder | The folder of one run. It holds `input/`, `output/`, `scratch/` and `evidence/`. |
| evidence | The events of a run, each holding the SHA-256 of the one before, and the payloads they name. |
| bundle | The evidence and the outputs of a run. If the run sets `bundle_scheme`, provrun commits the bundle to a store. Otherwise it stays in the run folder. |
| telemetry policy | The operator's file that says which libraries may record a run, and where a run's spans may go. |
| span | One timed step of a run, in the OpenTelemetry format that collectors read. |

## Before you start

You need Python 3.13, [uv](https://docs.astral.sh/uv/), `curl` and `bash`.

1. Clone the repository, and go into its top folder. Run every command of the tour from there.

   ```
   git clone https://github.com/rahuldave/MahaCata
   cd MahaCata
   ```

2. Install the packages.

   ```
   uv sync
   ```

## Run the whole tour

Run the script. It takes about 45 seconds.

```
bash tours/provrun/run.sh
```

Add `--online` to run Steps 15 and 16 too, which call a real model. The script starts moto and a small collector, runs the steps, stops both and deletes its scratch folder.

## Or run it step by step

1. Start the tour in your shell.

   ```
   source tours/provrun/start.sh
   ```

   The script does these things:

   - It makes the scratch folder `/tmp/provrun-tour`, or the folder in `TOUR_DIR`. It refuses a folder that is already there, unless an earlier tour made it, and a second start in the same shell.
   - It points `PROVCAT_CONFIG`, `PROVCAT_CACHE` and `PROVRUN_RUNS` at that folder, so the tour does not touch your own cache or runs.
   - It sets fake AWS keys for moto. It unsets `AWS_PROFILE`, `AWS_SESSION_TOKEN`, the provcat hook variables, the proxy and Postgres variables, and every `OTEL_`, `LOGFIRE_`, `MLFLOW_` and `LITELLM_` variable. These variables would change what a run records.
   - It keeps the old values in a shell variable that it does not export, so that `stop.sh` can put them back. It never prints them.
   - It hides one LiteLLM warning, that a logging coroutine was never awaited, through `PYTHONWARNINGS`.
   - It starts moto on port 5055, or the port in `TOUR_S3_PORT`. It starts the collector on port 4318, or the port in `TOUR_OTLP_PORT`. It refuses a port that is in use.
   - It makes the buckets `data` and `runs`, with versioning on, and copies the tour's files into the folder.

2. Copy each command of the steps below into the same shell, in order. Copy only the text after `$ `.

3. Stop the tour when you finish, in the same shell.

   ```
   source tours/provrun/stop.sh
   ```

   It stops moto and the collector, deletes the scratch folder, and gives each variable that `start.sh` changed its old value.

## The tour's files

| File | What it is |
|---|---|
| `tours/provrun/provcat.yaml` | The provcat configuration. It has one store, `local-s3`, which is moto, and two logical schemes. `data://acme/demo/dev/reports/report.txt` names the org `acme`, the domain `demo` and the environment `dev`, in the bucket `data`. `run://` names the bucket `runs`, which holds the bundles. |
| `tours/provrun/run.yaml` | The run file. It asks one question about one input, and commits the bundle through the scheme `run`. |
| `tours/provrun/policy.yaml` | The telemetry policy of Step 10. |
| `tours/provrun/quiet.py` | The program of Step 11. |
| `tours/provrun/collector.py` | The collector of Step 12. |
| `tours/provrun/mlflow.yaml` | The telemetry policy of Step 13, with an MLflow destination. |
| `tours/provrun/agent.yaml` | The PydanticAI agent of Step 15, which needs a real model. |
| `tours/provrun/follow.py` | The program of Step 14 that follows a run's live feed. |
| `tours/provrun/streamed.py` | The streamed PydanticAI agent of Step 16, which needs a real model, or `PROVRUN_TOUR_MODEL=test`. |
| `tours/provrun/field.py`, `event.py`, `spans.py`, `event_types.py`, `mlflow_spans.py`, `modes.py`, `drop_event.py` | Small helpers that print one field of a run's output or evidence, or change the evidence. |

The run file holds this:

```yaml
run:
  org: acme
  domain: demo
  env: dev
  actor: you@example.com
  bundle_scheme: run
agent:
  name: revenue-question
  backend: litellm
  model: openai/gpt-4o-mini
  instructions: Answer from the document only.
  prompt: What was revenue in 2025?
  inputs:
    - data://acme/demo/dev/reports/report.txt
  settings:
    mock_response: Revenue in 2025 was 42 million.
```

## The path of one input

```text
data://acme/demo/dev/reports/report.txt            the logical URI in the run file
  | StubBroker: may this run read it?              access_requested, access_granted
  | provcat: resolve to the store                  context_resolved
  v
local-s3://data/main/.../report.txt                the physical URI
  | provcat: pin to version V, and fetch V into the cache
  | provcat: copy it into a read-only snapshot     snapshot_materialized
  v
cache/.provcat/snapshots/file/<sha256>/report.txt
  | InProcessStager: clone into the run folder     input_staged
  v
runs/<run ID>/input/report.txt                     the agent sees /input/report.txt
```

## Step 1: Put a file in the store, and fetch it

Write the report, put it in the bucket and commit the folder.

```
$ echo "Revenue in 2025 was 42 million." > $TOUR_DIR/report.txt

$ uv run provc put data://acme/demo/dev/reports/report.txt $TOUR_DIR/report.txt
[
  {
    "uri": "local-s3://data/main/acme/demo/dev/reports/report.txt",
    "kind": "file",
    "size": 32,
    "version": "de5877cfcc0dfd578914982c36e261dc"
  }
]

$ uv run provc commit data://acme/demo/dev/reports -m 'first report'
local-s3://data/main/acme/demo/dev/reports?at=commit:370305b1b05729828309544ccacf185983325762c9e9f54a063fb21a5e347ee2
```

The `version` that `put` prints is the ETag of the object, a hash that S3 gives its content. The pin below shows the version ID instead, which a versioned bucket gives each write.

Pin the file, and fetch it.

```
$ uv run provc pin data://acme/demo/dev/reports/report.txt
local-s3://data/main/acme/demo/dev/reports/report.txt?at=version:ff132f17-7295-4220-8667-de2e7316bae0

$ uv run provc fetch data://acme/demo/dev/reports/report.txt
/tmp/provrun-tour/cache/local-s3/data/main/acme/demo/dev/reports/report.txt
```

**What happened:**
- `pin` turned the logical URI into the physical URI with the version that S3 gave the file.
- `fetch` of the URI without a version downloaded the newest version to a path that names no version.
- When the file changes in the store, the next fetch rewrites that same path. So a program must never read that path while the file can change. provrun never mounts it. It uses snapshots, as in Step 2.

## Step 2: Run the agent

`provr run` reads the provcat configuration from `PROVCAT_CONFIG`, and writes the run folder under `PROVRUN_RUNS`. It prints a summary of the run. The tour keeps the summary in a file, and the run ID in `$R1`.

```
$ uv run provr run $TOUR_DIR/run.yaml | tee $TOUR_DIR/r1.json
{
  "run_id": "run-20261102T224004Z-d7651699",
  "status": "completed",
  "folder": "/tmp/provrun-tour/runs/run-20261102T224004Z-d7651699",
  "bundle": "run://acme/demo/dev/run-20261102T224004Z-d7651699",
  "pinned": "local-s3://runs/main/acme/demo/dev/run-20261102T224004Z-d7651699?at=commit:2c12c5e026ede88e9f12a86a480f06d1a772b97cd475329e0dce9fe801f62f5d",
  "note": "",
  "error": null
}

$ R1=$(uv run python tours/provrun/field.py $TOUR_DIR/r1.json run_id)
```

**What happened:** provrun asked the broker for the input, made a snapshot, staged it, ran the agent, and committed the bundle to the bucket `runs`. `pinned` is the URI of that commit. The next steps look at each part.

## Step 3: Look at the cache

```
$ find $TOUR_DIR/cache -type f -not -path "*/.provcat/locks/*" | sort
/tmp/provrun-tour/cache/.provcat/meta/a35d1a702b018acdce543e9820b4472ef8af7beea862699a6751183b37088305.json
/tmp/provrun-tour/cache/.provcat/meta/f56a44fe7fcc5f807321f25b306828115c7bc7f388faeda61dc5dba8ed2c0694.json
/tmp/provrun-tour/cache/.provcat/snapshots/file/205cfa28c510eeba46bd4bc49acc2fb13f7645f66bab5ff5f0c3bda90d26f0b2/report.txt
/tmp/provrun-tour/cache/local-s3/data/main/acme/demo/dev/reports/report.txt
/tmp/provrun-tour/cache/local-s3@version-ff132f17-7295-4220-8667-de2e7316bae0/data/main/acme/demo/dev/reports/report.txt

$ uv run python tours/provrun/modes.py $TOUR_DIR/cache/.provcat/snapshots/file/*/report.txt
-r--r--r-- /tmp/provrun-tour/cache/.provcat/snapshots/file/205cfa28c510eeba46bd4bc49acc2fb13f7645f66bab5ff5f0c3bda90d26f0b2/report.txt
```

The cache now holds the file three times:

| Path | Made by | Can it change? |
|---|---|---|
| `local-s3/.../report.txt` | `provc fetch` in Step 1, without a version | Yes. The next fetch of a new version rewrites it. |
| `local-s3@version-.../report.txt` | the run, which fetched the pinned URI | No fetch rewrites it, because the path names the version. But it is writable. |
| `.provcat/snapshots/file/205cfa28.../report.txt` | the run, which made a snapshot of the pinned copy | Not without notice. The file is read-only, and provcat checks its SHA-256 before it uses it again. A snapshot that someone changed is replaced. |

The folder name of the snapshot is the SHA-256 of the content. Two runs that read the same content under the same name share one snapshot. On macOS and on Linux disks that can clone, the snapshot is a copy-on-write clone, so it costs almost no space.

The records under `.provcat/meta` hold the version, the size, the modification time, the inode and the SHA-256 of each cached file. The listing leaves out `.provcat/locks`. It holds one lock file for each record, of the same name, so that two processes never write the same path at once.

## Step 4: Look at the run folder

```
$ find $TOUR_DIR/runs/$R1 -type f | sort
/tmp/provrun-tour/runs/run-20261102T224004Z-d7651699/bundle.json
/tmp/provrun-tour/runs/run-20261102T224004Z-d7651699/evidence/events.jsonl
/tmp/provrun-tour/runs/run-20261102T224004Z-d7651699/evidence/manifest.json
/tmp/provrun-tour/runs/run-20261102T224004Z-d7651699/evidence/payloads/169a2eac7d7f094a88744ea5c1500e3a7ccb706952cdd6d5fa67eef58ff6c601
/tmp/provrun-tour/runs/run-20261102T224004Z-d7651699/evidence/payloads/f4503948841cb1db460bbdec8f9b4178f81cc3254f1c17a27487d62495373fcd
/tmp/provrun-tour/runs/run-20261102T224004Z-d7651699/input/report.txt
/tmp/provrun-tour/runs/run-20261102T224004Z-d7651699/output/response.txt

$ uv run python tours/provrun/modes.py $TOUR_DIR/runs/$R1/input $TOUR_DIR/runs/$R1/input/report.txt
dr-xr-xr-x /tmp/provrun-tour/runs/run-20261102T224004Z-d7651699/input
-r--r--r-- /tmp/provrun-tour/runs/run-20261102T224004Z-d7651699/input/report.txt
```

The input and its folder are read-only, so a write to the file fails with `Permission denied`. The root user ignores read-only bits, so as root, for example in a container, the write succeeds. Then `input_verified` is what catches a change.

**What happened:**
- The stager cloned the snapshot into `input/` and made the folder and the file read-only. The agent knows the file as `/input/report.txt`. The LiteLLM backend has no file system, so it puts the text of the file into the prompt under that name. The PydanticAI backend mounts the folder at `/input`.
- The clone shares no storage that a write could reach, so a change in one run cannot reach the snapshot that other runs use.
- After the agent ran, the stager hashed `input/report.txt` again and wrote `input_verified`. A changed input fails the run.
- The agent wrote `output/response.txt`. `scratch/` is empty and is not part of the bundle.

## Step 5: Read the evidence

`provr show` prints each event of a run on one line. The line holds its number, time and type, the part of provrun that wrote it, its URI and its data. The `awk` keeps the number, the type and the part. `event.py` prints the data of one event.

```
$ uv run provr show $R1 | awk '{print $1, $3, $4}'
0 run_created RunContext
1 access_requested StubBroker
2 access_granted StubBroker
3 context_resolved ProvcatResolver
4 snapshot_materialized ProvcatResolver
5 input_staged InProcessStager
6 sandbox_started LiteLLMBackend
7 prompt_rendered LiteLLMBackend
8 model_called LiteLLMBackend
9 sandbox_completed LiteLLMBackend
10 input_verified InProcessStager
11 output_artifact_created Outputter
12 run_completed RunContext

$ uv run python tours/provrun/event.py $R1 snapshot_materialized
{
  "kind": "file",
  "parent_span_id": "dd5aca3e4559ab69",
  "pinned": "local-s3://data/main/acme/demo/dev/reports/report.txt?at=version:ff132f17-7295-4220-8667-de2e7316bae0",
  "sha256": "205cfa28c510eeba46bd4bc49acc2fb13f7645f66bab5ff5f0c3bda90d26f0b2",
  "span_id": "4b14991141c42f4d",
  "started_at": "2026-11-02T22:40:04.463245Z",
  "trace_id": "95d933aadf4041b35d3c2f51143a38b9"
}
```

**What happened:** each part of provrun wrote one or more events. Each event holds the SHA-256 of the line before it, so the events form a chain. Event 4 ties the logical URI to the exact version and to the SHA-256 of its content. The payloads are in `evidence/payloads`, each in a file named by its SHA-256. One holds the rendered prompt. The other holds the request and the response of the model call.

## Step 6: Change the file, and run again

Restate the report in the store, run the agent again, and print the version and the SHA-256 that the second run pinned.

```
$ echo "Revenue in 2025 was 45 million, after a restatement." > $TOUR_DIR/report.txt

$ uv run provc put data://acme/demo/dev/reports/report.txt $TOUR_DIR/report.txt
[
  {
    "uri": "local-s3://data/main/acme/demo/dev/reports/report.txt",
    "kind": "file",
    "size": 53,
    "version": "978a4eeb4c507d40a87a3346b012b44f"
  }
]

$ uv run provc commit data://acme/demo/dev/reports -m 'restated report'
local-s3://data/main/acme/demo/dev/reports?at=commit:6b8d5c87d14c4f9d793c59fc92d42c1c3f5adfd27d4ef022189d04731e637e33

$ uv run provr run $TOUR_DIR/run.yaml > $TOUR_DIR/r2.json

$ R2=$(uv run python tours/provrun/field.py $TOUR_DIR/r2.json run_id)

$ uv run python tours/provrun/event.py $R2 snapshot_materialized pinned sha256
local-s3://data/main/acme/demo/dev/reports/report.txt?at=version:60cd1115-53ce-454f-8da0-b8cf58e18441
106b090a711c049ade3f3601e56d787f023438b672af9d36aa6112a7f123dc75
```

Look at the snapshots.

```
$ for f in $TOUR_DIR/cache/.provcat/snapshots/file/*/report.txt; do echo "$f: $(cat $f)"; done
/tmp/provrun-tour/cache/.provcat/snapshots/file/106b090a711c049ade3f3601e56d787f023438b672af9d36aa6112a7f123dc75/report.txt: Revenue in 2025 was 45 million, after a restatement.
/tmp/provrun-tour/cache/.provcat/snapshots/file/205cfa28c510eeba46bd4bc49acc2fb13f7645f66bab5ff5f0c3bda90d26f0b2/report.txt: Revenue in 2025 was 42 million.
```

**What happened:**
- The second run pinned the new version and made a new snapshot with a new SHA-256.
- The snapshot of the first run did not change. The evidence of the first run still names its version and the SHA-256 `205cfa28...`, and that snapshot still holds those bytes.
- A third run on the same version makes no new snapshot. provcat checks the SHA-256 of the existing snapshot and reuses it.
- The agent gave the same answer twice, because `mock_response` fixes it. A real model would read the restated figure.

## Step 7: Run again on the first version

The store now holds the restated report. To repeat the first run exactly, give the agent the pinned URI from the evidence of the first run. `sed` writes a copy of the run file with that URI as its input.

```
$ PINNED=$(uv run python tours/provrun/event.py $R1 snapshot_materialized pinned)

$ echo "$PINNED"
local-s3://data/main/acme/demo/dev/reports/report.txt?at=version:ff132f17-7295-4220-8667-de2e7316bae0

$ sed "s|- data://acme/demo/dev/reports/report.txt|- \"$PINNED\"|" $TOUR_DIR/run.yaml > $TOUR_DIR/rerun.yaml

$ uv run provr run $TOUR_DIR/rerun.yaml > $TOUR_DIR/r3.json

$ R3=$(uv run python tours/provrun/field.py $TOUR_DIR/r3.json run_id)

$ uv run python tours/provrun/event.py $R3 access_granted reason
in the allow list; physical URI in dev

$ cat $TOUR_DIR/runs/$R3/input/report.txt
Revenue in 2025 was 42 million.
```

**What happened:**
- `local-s3` is a store in the configuration, so the broker treats the URI as physical. A physical URI names no environment, so the stub broker grants one only in `dev`.
- provcat fetched the first version, not the newest one, and found the snapshot from the first run. The agent saw the report as it was before the restatement.
- So the evidence of a run is enough to run it again on the same bytes, as long as the store keeps the old version.

## Step 8: Verify the runs, and break one

```
$ uv run provr verify $R1
run-20261102T224004Z-d7651699: the evidence and the outputs verify, and match the committed bundle

$ uv run provr verify $R2
run-20261102T224021Z-3bf0d5f0: the evidence and the outputs verify, and match the committed bundle
```

**What happened:** `provr verify` checked the chain of events, the payloads and the outputs. Then it read the events and the manifest from the commit that `bundle.json` names, and compared them with the run folder. To reach that commit, it needs the provcat configuration, from `PROVCAT_CONFIG` or `--config`.

Now change an output of the second run.

```
$ echo "Revenue was 99 million." > $TOUR_DIR/runs/$R2/output/response.txt

$ uv run provr verify $R2 || echo "exit status $?"
provr: output response.txt was changed
exit status 1
```

The command exits with status 1. The event `output_artifact_created` holds the SHA-256 of the output, and the new bytes do not match it.

Someone who can change the run folder can also write a new chain that agrees with itself. The committed bundle is a copy in another store, so the check against the commit catches that chain. But `bundle.json` is in the run folder too, and that person can delete it. Then `provr verify` finds no commit, and it says only `no committed bundle to compare with`.

So a checker who does not trust the folder passes the commit with `--bundle`. The commit comes from outside the folder, here from the summary that `provr run` printed. `--require-bundle` fails a run that has no commit to compare with.

```
$ BUNDLE=$(uv run python tours/provrun/field.py $TOUR_DIR/r1.json pinned)

$ uv run provr verify $R1 --bundle "$BUNDLE" --require-bundle
run-20261102T224004Z-d7651699: the evidence and the outputs verify, and match the committed bundle
```

## Step 9: Keep the evidence in SQLite

The runs so far wrote their evidence only as files in each run folder. A `sqlite` store also keeps every run in one SQLite file, so you can ask questions across runs. Run the same file twice with `--store`. The path after `sqlite:///` is absolute, because `$TOUR_DIR` starts with a slash.

```
$ S=sqlite:///$TOUR_DIR/evidence.db

$ uv run provr --store $S run $TOUR_DIR/run.yaml > $TOUR_DIR/r4.json

$ uv run provr --store $S run $TOUR_DIR/run.yaml > /dev/null

$ Q=$(uv run python tours/provrun/field.py $TOUR_DIR/r4.json run_id)
```

Ask which runs read the report, and which agents ran. Then ask what the first of these runs could read.

```
$ uv run prove --store $S runs --reading data://acme/demo/dev/reports/report.txt
run-20261102T224043Z-e6ed9133
run-20261102T224050Z-406f3eaf

$ uv run prove --store $S agents
acme/demo/dev/revenue-question  1 version, 2 runs

$ uv run prove --store $S access $Q
   2 granted  mounted_snapshot   data://acme/demo/dev/reports/report.txt  in the allow list; org and environment match
```

**What happened:**
- Each run wrote each event as a row of the file, as it happened. The run folder still holds the same files as before, so the committed bundle is the same.
- The agent class is the agent's name in its org, domain and environment. Both runs are one version of it, because they ran the same spec.
- `runs --reading` matched the URI of each staged input.

Now delete one row, and check the run.

```
$ uv run python tours/provrun/drop_event.py $TOUR_DIR/evidence.db $Q 3

$ uv run prove --store $S verify $Q || echo "exit status $?"
prove: in the store: line 3: seq is 4, not 3
prove: in the store: line 3: the chain is broken
prove: in the store: refs of row 3 have no event
prove: in the store: the manifest counts 13 events, and there are 12
exit status 1
```

The command exits with status 1. Without `--store`, `prove verify` checks only the run folder, which still verifies.

## Step 10: Run under a telemetry policy

The operator's telemetry policy says what may record or send data from a run, besides the evidence. This one approves LiteLLM at the tested version, sends spans nowhere, and lets a run reach only OpenAI.

Run the file under it, with four variables set. Two name a collector for spans and its token. One names an MLflow database with its password. The last is a LiteLLM setting that provrun changes for the run. Then print the `telemetry` part of the run's first event, `run_created`.

```
$ cat $TOUR_DIR/policy.yaml
# A telemetry policy that approves LiteLLM at the tested version, sends spans
# nowhere, and lets a run reach only OpenAI.
version: 1
libraries:
  litellm:
    versions: "==1.83.0"
destinations: {}
hosts:
  - api.openai.com

$ export PROVRUN_TELEMETRY_POLICY=$TOUR_DIR/policy.yaml

$ OTEL_EXPORTER_OTLP_ENDPOINT=http://collector.example:4318 \
  OTEL_EXPORTER_OTLP_HEADERS="Authorization=Bearer example-token" \
  MLFLOW_TRACKING_URI="postgresql://mlflow:example-password@db.example:5432/mlflow" \
  LITELLM_MODE=DEV \
  uv run provr run $TOUR_DIR/run.yaml > $TOUR_DIR/r5.json

$ R4=$(uv run python tours/provrun/field.py $TOUR_DIR/r5.json run_id)

$ uv run python tours/provrun/event.py $R4 run_created telemetry
{
  "destinations": [
    {
      "destination": null,
      "kind": "otlp",
      "note": "matches no otlp destination of the policy; nothing is sent",
      "receives": null,
      "sending": false,
      "url": "http://collector.example:4318",
      "variable": "OTEL_EXPORTER_OTLP_ENDPOINT"
    },
    {
      "destination": null,
      "kind": "mlflow",
      "note": "matches no mlflow destination of the policy; nothing is copied",
      "receives": null,
      "sending": false,
      "url": "postgresql://db.example:5432",
      "variable": "MLFLOW_TRACKING_URI"
    }
  ],
  "imported_before": [],
  "overridden": {
    "LITELLM_MODE": "DEV"
  },
  "policy": "408d9fe84aa3ae7943c374d9e3f5b275c22f23173f2735d72bd2082adfae1e16",
  "policy_source": "/tmp/provrun-tour/policy.yaml",
  "problems": [],
  "redirects": [],
  "variables": {
    "MLFLOW_TRACKING_URI": "postgresql://db.example:5432",
    "OTEL_EXPORTER_OTLP_ENDPOINT": "http://collector.example:4318",
    "OTEL_EXPORTER_OTLP_HEADERS": null
  }
}
```

**What happened:**
- `run_created` names the policy by its SHA-256, so the run says which policy it ran under.
- provrun left the variables as you set them. They are your configuration. `variables` records where each one points. The database URI keeps only its scheme, host and port, so the password is not recorded. The header, which holds a token, is recorded by its name only.
- provrun set `LITELLM_MODE=PRODUCTION` for the run, so that LiteLLM read no `.env` file, and put `DEV` back after it. `overridden` records your value.
- The policy approves no destination, so provrun sent the run's spans nowhere, and copied nothing into MLflow. `destinations` records the collector and the MLflow database that the variables name, and that nothing went to either. A `telemetry_blocked` event for each says so too:

```
$ uv run prove telemetry $PROVRUN_RUNS/$R4
   1 2026-11-02T22:41:00.519122Z blocked destination   OTEL_EXPORTER_OTLP_ENDPOINT: matches no otlp destination of the policy; nothing is sent
   2 2026-11-02T22:41:00.519258Z blocked destination   MLFLOW_TRACKING_URI: matches no mlflow destination of the policy; nothing is copied
```

A run file can narrow the policy in a `telemetry` section, but never widen it. Add a host that the policy does not allow:

```
$ cp $TOUR_DIR/run.yaml $TOUR_DIR/wide.yaml

$ printf 'telemetry:\n  hosts: [api.openai.com, paste.example]\n' >> $TOUR_DIR/wide.yaml

$ uv run provr run $TOUR_DIR/wide.yaml || echo "exit status $?"
provr: the run file: host 'paste.example' is not allowed by the policy
exit status 1

$ unset PROVRUN_TELEMETRY_POLICY
```

The command exits with status 1, before it makes a run folder.

## Step 11: See what a run turns off

A program can switch on a library's own telemetry before it starts a run. `tours/provrun/quiet.py` adds a LiteLLM callback, and sets the root logger to `DEBUG`, writing to a file:

```python
logging.basicConfig(level=logging.DEBUG, filename=f"{tour}/debug.log")
litellm.success_callback.append(lambda *args, **kwargs: None)

outcome = run_file_sync(RunFile.load(f"{tour}/run.yaml"), catalog=Catalog.open())
print(outcome.run_id, outcome.status)
print("callbacks after the run:", len(litellm.success_callback))
```

Run it, and list what the run turned off with `prove telemetry`.

```
$ uv run python tours/provrun/quiet.py > $TOUR_DIR/quiet.out 2> $TOUR_DIR/quiet.err

$ cat $TOUR_DIR/quiet.out
run-20261102T224119Z-b9b5dfdb completed
callbacks after the run: 1
the LiteLLM logger logs at: DEBUG

$ R5=$(head -1 $TOUR_DIR/quiet.out | cut -d" " -f1)

$ uv run prove telemetry $PROVRUN_RUNS/$R5
   1 2026-11-02T22:41:19.310981Z blocked litellm.callbacks   removed __main__.<lambda> from litellm.success_callback
   2 2026-11-02T22:41:19.311116Z blocked logging litellm  raised the logger 'LiteLLM' from DEBUG to WARNING
   3 2026-11-02T22:41:19.311216Z blocked logging litellm  raised the logger 'LiteLLM Router' from DEBUG to WARNING
   4 2026-11-02T22:41:19.311297Z blocked logging litellm  raised the logger 'LiteLLM Proxy' from DEBUG to WARNING
   5 2026-11-02T22:41:19.311371Z blocked logging anthropic  raised the logger 'anthropic' from DEBUG to WARNING
   6 2026-11-02T22:41:19.311443Z blocked logging openai  raised the logger 'openai' from DEBUG to WARNING
   7 2026-11-02T22:41:19.311510Z blocked logging botocore  raised the logger 'botocore' from DEBUG to WARNING
```

Each line gives the sequence number of a `telemetry_blocked` event, its time, the word `blocked`, what was blocked, the library, and the detail. The callback has no library, because it is the program's own.

**What happened:**
- As the run started, provrun emptied LiteLLM's callback lists, so the callback saw no model call. The loggers that can log request bodies stayed at `WARNING`. So the log file holds no prompt, and none of the request signatures that botocore, the S3 library, logs at `DEBUG`.
- When the run ended, provrun put the callback and the logger levels back. So the program has its callback again, and the LiteLLM logger logs at `DEBUG` again.
- After the run, LiteLLM printed a few `DEBUG` lines as it shut down, which the tour keeps in `quiet.err`. Its logger follows the program's root logger again, and the program loaded LiteLLM before the run set `LITELLM_LOG`.

The run is one trace. Print the type, the span ID and the parent span ID of each event with a span. Then print their trace IDs:

```
$ uv run python tours/provrun/spans.py $R5
run_created a909b9f8cff023d2 None
context_resolved 294f8a32872ca1ed a909b9f8cff023d2
snapshot_materialized ae3e68aca3437e5b a909b9f8cff023d2
input_staged f989377ef613b5f1 a909b9f8cff023d2
model_called df89e1eac8128dac a909b9f8cff023d2
input_verified fbdbc1359fb6dc06 a909b9f8cff023d2
output_artifact_created c5c298915218c019 a909b9f8cff023d2
run_completed a909b9f8cff023d2 None
trace IDs: 31e521753eb5398df983d1c489f76830
```

`run_created` and `run_completed` hold the span ID of the run's root span. Each step's parent is that span, and every event with a span holds the same trace ID.

## Step 12: Send the spans to a collector

A policy can approve a place for the run's spans. `start.sh` wrote this one, with the collector's port. It approves the collector on this machine, which receives names only.

```
$ cat $TOUR_DIR/send.yaml
version: 1
libraries:
  litellm:
    versions: "==1.83.0"
destinations:
  collector:
    kind: otlp
    receives: names
    endpoint: http://127.0.0.1:4318/v1/traces
hosts:
  - api.openai.com
  - 127.0.0.1
```

The collector, `tours/provrun/collector.py`, prints the name and the attribute names of each span it gets, to `collector.log`. Run the file under the new policy, with a proxy set that does not exist. `NO_PROXY=localhost` lets the run still reach moto.

```
$ PROVRUN_TELEMETRY_POLICY=$TOUR_DIR/send.yaml \
  OTEL_EXPORTER_OTLP_ENDPOINT=http://127.0.0.1:$TOUR_OTLP_PORT \
  HTTPS_PROXY=http://127.0.0.1:9 HTTP_PROXY=http://127.0.0.1:9 NO_PROXY=localhost \
  uv run provr run $TOUR_DIR/run.yaml > $TOUR_DIR/r6.json

$ R6=$(uv run python tours/provrun/field.py $TOUR_DIR/r6.json run_id)

$ uv run python tours/provrun/event.py $R6 run_created telemetry.destinations telemetry.redirects
[
  {
    "destination": "collector",
    "kind": "otlp",
    "note": "",
    "receives": "names",
    "sending": true,
    "url": "http://127.0.0.1:4318",
    "variable": "OTEL_EXPORTER_OTLP_ENDPOINT"
  }
]
[
  "HTTPS_PROXY",
  "HTTP_PROXY",
  "NO_PROXY"
]
```

The collector's log shows the run's spans:

```
$ cat $TOUR_DIR/collector.log
/v1/traces context_resolved ['provrun.component', 'provrun.run_id', 'provrun.step']
/v1/traces snapshot_materialized ['provrun.component', 'provrun.run_id', 'provrun.step']
/v1/traces input_staged ['provrun.component', 'provrun.run_id', 'provrun.step']
/v1/traces model_called ['gen_ai.operation.name', 'gen_ai.request.model', 'provrun.component', 'provrun.run_id', 'provrun.step']
/v1/traces input_verified ['provrun.component', 'provrun.run_id', 'provrun.step']
/v1/traces output_artifact_created ['provrun.component', 'provrun.run_id', 'provrun.step']
/v1/traces run ['gen_ai.agent.name', 'gen_ai.operation.name', 'provrun.component', 'provrun.run_id', 'provrun.step']
```

**What happened:**
- The variable named a place that the policy approves, so provrun sent the run's spans to the collector. There is one for each step, and the run's own span comes last.
- The collector receives names only, so `model_called` came without its token counts. Its `gen_ai.usage.*` attributes stayed in the evidence.
- provrun's sender ignored the proxy, which does not exist, and went straight to the collector. `redirects` records which proxy variables were set, by name only.

## Step 13: Copy a run into MLflow

MLflow is a place to look at runs, with a web viewer. A policy can approve an MLflow destination. This one has no endpoint, so it names the SQLite evidence store's own file, and MLflow there receives names only:

```
$ cat $TOUR_DIR/mlflow.yaml
# The policy of Step 13: it approves LiteLLM, and one MLflow destination with
# no endpoint, which is the SQLite evidence store's own file. MLflow there
# gets names only: no token counts, prompts or answers.
version: 1
libraries:
  litellm:
    versions: "==1.83.0"
destinations:
  own:
    kind: mlflow
    receives: names
hosts:
  - api.openai.com
```

Run the file under it, with the evidence in a new SQLite store. Then print the `mlflow` part of the summary that `provr run` printed:

```
$ M=sqlite:///$TOUR_DIR/mlflow-evidence.db

$ PROVRUN_TELEMETRY_POLICY=$TOUR_DIR/mlflow.yaml uv run provr --store $M run $TOUR_DIR/run.yaml > $TOUR_DIR/r7.json

$ uv run python tours/provrun/field.py $TOUR_DIR/r7.json mlflow
{
  "destination": "own",
  "receives": "names",
  "copied": true,
  "tracking_uri": "sqlite:////tmp/provrun-tour/mlflow-evidence.db",
  "experiment_id": "1",
  "mlflow_run_id": "8b98d520a6fc4602a123be383e8b9a8d",
  "trace_id": "tr-264afdee126d9782ae0a97a967b374cc",
  "spans": 7
}
```

Print the spans of the copy, each with the names of its attributes:

```
$ uv run python tours/provrun/mlflow_spans.py $TOUR_DIR/r7.json
run gen_ai.agent.name gen_ai.operation.name mlflow.spanType mlflow.traceRequestId provrun.component provrun.run_id provrun.step
context_resolved mlflow.traceRequestId provrun.component provrun.run_id provrun.step
snapshot_materialized mlflow.traceRequestId provrun.component provrun.run_id provrun.step
input_staged mlflow.traceRequestId provrun.component provrun.run_id provrun.step
model_called gen_ai.operation.name gen_ai.request.model mlflow.llm.model mlflow.spanType mlflow.traceRequestId provrun.component provrun.run_id provrun.step
input_verified mlflow.traceRequestId provrun.component provrun.run_id provrun.step
output_artifact_created mlflow.traceRequestId provrun.component provrun.run_id provrun.step
```

**What happened:**
- MLflow got nothing while the run went on. When the run was sealed, provrun called provevi's `copy_run` in a process of its own. It read the run back from the store and checked its chain. Then it wrote one MLflow run and one trace into the store's file.
- The trace has a span for each step that has one, and the run's own span first.
- The destination receives names only, so `model_called` has its model's name but not its token counts. The prompt and the answer stay in the evidence.
- `uv run prove --store $M ui` would open MLflow's viewer on the file, as [provevi's page](provevi.md#copy-a-run-into-mlflow) says.

Now set `MLFLOW_TRACKING_URI` to another file, which the policy does not approve:

```
$ PROVRUN_TELEMETRY_POLICY=$TOUR_DIR/mlflow.yaml MLFLOW_TRACKING_URI=sqlite:///$TOUR_DIR/elsewhere.db \
  uv run provr --store $M run $TOUR_DIR/run.yaml > $TOUR_DIR/r8.json

$ R8=$(uv run python tours/provrun/field.py $TOUR_DIR/r8.json run_id)

$ uv run python tours/provrun/event.py $R8 telemetry_blocked detail
MLFLOW_TRACKING_URI: matches no mlflow destination of the policy; nothing is copied

$ test -e $TOUR_DIR/elsewhere.db || echo "no copy"
no copy
```

The run gives no copy to the place that the policy does not approve. The event names the variable, and records no path of the file.

## Step 14: Ask for the reply in pieces

An agent spec with `stream: true` asks the model for its reply in pieces, and the run hands each piece on as it comes. The evidence still records each whole response once, as one `model_called`. Write a copy of the run file that streams, and look at its end:

```
$ { cat $TOUR_DIR/run.yaml; echo "  stream: true"; } > $TOUR_DIR/stream.yaml

$ tail -3 $TOUR_DIR/stream.yaml
  settings:
    mock_response: Revenue in 2025 was 42 million.
  stream: true
```

Run it, and read its model call:

```
$ uv run provr run $TOUR_DIR/stream.yaml > $TOUR_DIR/r11.json

$ R11=$(uv run python tours/provrun/field.py $TOUR_DIR/r11.json run_id)

$ uv run python tours/provrun/event.py $R11 model_called streamed input_tokens output_tokens finish_reason
true
48
10
stop

$ cat $PROVRUN_RUNS/$R11/output/response.txt; echo
Revenue in 2025 was 42 million.
```

**What happened:**
- LiteLLM's `mock_response` sent the reply in pieces, as a model does, so this step needs no key.
- `model_called` says `streamed`, with the tokens in and out from the stream's last chunk, and why the model stopped. It also holds `first_token_ms`, the milliseconds to the first piece, which differs on each run, so the step does not print it.
- `response.txt` holds the whole reply, as for a run that does not stream.

A program can watch a run as it goes, through the run's live feed. The script `follow.py` starts the same run with `start_run`, and prints each item of the feed as it comes:

```
$ uv run python tours/provrun/follow.py $TOUR_DIR/stream.yaml
1 event run_created
2 event access_requested
3 event access_granted
4 event context_resolved
5 event snapshot_materialized
6 event input_staged
7 event sandbox_started
8 event prompt_rendered
9 event model_called
10 event sandbox_completed
11 event input_verified
12 event output_artifact_created
13 event run_completed
14 done completed
```

Each event of the run is one item, in order, and the last item says how the run ended. The feed shows the `names` level, so it holds no text and no event data. [provrun's page](provrun.md#watch-a-run-as-it-goes) says how a server sends the feed on.

## Step 15: Call a real model (needs ANTHROPIC_API_KEY)

This step runs only with `--online`. It needs `ANTHROPIC_API_KEY` in the environment, and it costs a few cents. It runs a real model twice, once with LiteLLM and once as a PydanticAI agent with tools. Both runs go under a policy with two places, and with three variables set.

Write a copy of the run file with Claude Haiku and no `settings`. Then look at the policy, which `start.sh` wrote with the collector's port:

```
$ sed -e "s|openai/gpt-4o-mini|anthropic/claude-haiku-5-5|" -e "/settings:/,\$d" $TOUR_DIR/run.yaml > $TOUR_DIR/real.yaml

$ cat $TOUR_DIR/online.yaml
version: 1
libraries:
  litellm:
    versions: "==1.83.0"
destinations:
  collector:
    kind: otlp
    receives: usage
    endpoint: http://127.0.0.1:4318/v1/traces
  own:
    kind: mlflow
    receives: usage
hosts:
  - api.anthropic.com
  - 127.0.0.1
```

It approves the collector and a copy in MLflow, both with token counts. Set the variables: the policy, the collector, and a Logfire token that the policy does not approve. Keep the evidence in a new SQLite store, and note how many lines the collector has logged so far:

```
$ export PROVRUN_TELEMETRY_POLICY=$TOUR_DIR/online.yaml OTEL_EXPORTER_OTLP_ENDPOINT=http://127.0.0.1:$TOUR_OTLP_PORT LOGFIRE_TOKEN=pylf_v1_us_example-token

$ O=sqlite:///$TOUR_DIR/online.db

$ N=$(wc -l < $TOUR_DIR/collector.log)
```

Run the file, and read the answer and the model call:

```
$ uv run provr --store $O run $TOUR_DIR/real.yaml > $TOUR_DIR/r9.json

$ R9=$(uv run python tours/provrun/field.py $TOUR_DIR/r9.json run_id)

$ cat $PROVRUN_RUNS/$R9/output/response.txt; echo
Revenue in 2025 was 45 million, after a restatement.

$ uv run python tours/provrun/event.py $R9 model_called model input_tokens output_tokens finish_reason
anthropic/claude-haiku-5-5
62
23
stop
```

The model read the restated report. `model_called` records the model, the real token counts and why the model stopped.

Look at what was blocked, what the collector got, and the copy in MLflow. Then verify the run:

```
$ uv run prove telemetry $PROVRUN_RUNS/$R9
   1 2026-11-02T22:42:06.909446Z blocked destination   LOGFIRE_TOKEN: matches no logfire destination of the policy; nothing is sent

$ tail -n +$((N + 1)) $TOUR_DIR/collector.log
/v1/traces context_resolved ['provrun.component', 'provrun.run_id', 'provrun.step']
/v1/traces snapshot_materialized ['provrun.component', 'provrun.run_id', 'provrun.step']
/v1/traces input_staged ['provrun.component', 'provrun.run_id', 'provrun.step']
/v1/traces model_called ['gen_ai.operation.name', 'gen_ai.request.model', 'gen_ai.usage.input_tokens', 'gen_ai.usage.output_tokens', 'provrun.component', 'provrun.run_id', 'provrun.step']
/v1/traces input_verified ['provrun.component', 'provrun.run_id', 'provrun.step']
/v1/traces output_artifact_created ['provrun.component', 'provrun.run_id', 'provrun.step']
/v1/traces run ['gen_ai.agent.name', 'gen_ai.operation.name', 'provrun.component', 'provrun.run_id', 'provrun.step']

$ uv run python tours/provrun/field.py $TOUR_DIR/r9.json mlflow
{
  "destination": "own",
  "receives": "usage",
  "copied": true,
  "tracking_uri": "sqlite:////tmp/provrun-tour/online.db",
  "experiment_id": "1",
  "mlflow_run_id": "4342bf59dca34bbebe3bcccc28be6461",
  "trace_id": "tr-dac16b0c726306a44cbe1f8f1c12a41f",
  "spans": 7
}

$ uv run provr --store $O verify $R9
run-20261102T224206Z-45187a05: the evidence and the outputs verify, and match the committed bundle
```

**What happened:**
- Logfire got nothing, because the policy approves no Logfire place. The event says so.
- The collector got a span for each step, with the token counts, because it receives `usage`.
- MLflow got a copy of the sealed run, in the store's own file.
- The run verifies, with a real model's answer in its evidence.

Now run an agent with tools. `tours/provrun/agent.yaml` asks the same question of a PydanticAI agent. It must find the fact with `grep_file`, note the line with `add_note`, and write its answer to a file:

```
$ cat $TOUR_DIR/agent.yaml
# The agent of Step 15: a real PydanticAI agent with tools, on the tour's report.
run:
  org: acme
  domain: demo
  env: dev
  actor: you@example.com
  bundle_scheme: run
agent:
  name: revenue-agent
  backend: pydantic-ai
  model: anthropic:claude-haiku-5-5
  instructions: >-
    Answer from the inputs only. Use grep_file to find the facts. Record the
    line you used with add_note. Write the answer to /output/answer.txt.
  prompt: What was revenue in 2025?
  inputs:
    - data://acme/demo/dev/reports/report.txt
  tools: [grep_file, add_note]
  settings:
    max_tokens: 1000
  limits:
    request_limit: 10

$ N=$(wc -l < $TOUR_DIR/collector.log)

$ uv run provr --store $O run $TOUR_DIR/agent.yaml > $TOUR_DIR/r10.json

$ R10=$(uv run python tours/provrun/field.py $TOUR_DIR/r10.json run_id)

$ cat $PROVRUN_RUNS/$R10/output/answer.txt
Revenue in 2025 was 45 million (after a restatement). Source: /input/report.txt, line 1.
```

List the events of the run, with the tool of each tool call:

```
$ uv run python tours/provrun/event_types.py $R10
0 run_created 
1 telemetry_blocked 
2 access_requested 
3 access_granted 
4 context_resolved 
5 snapshot_materialized 
6 input_staged 
7 sandbox_started 
8 prompt_rendered 
9 telemetry_blocked 
10 model_called 
11 tool_called grep_file
12 model_called 
13 note_added 
14 tool_called add_note
15 tool_called write_file
16 model_called 
17 trace_imported 
18 sandbox_completed 
19 input_verified 
20 output_artifact_created 
21 output_artifact_created 
22 run_completed 
```

The agent called the model three times. It searched the report, noted the line, and wrote the answer with the filesystem tool `write_file`. `trace_imported` holds PydanticAI's whole message history. Look at what was blocked, what the collector got, and the copy, and verify the run:

```
$ uv run prove telemetry $PROVRUN_RUNS/$R10
   1 2026-11-02T22:42:25.064639Z blocked destination   LOGFIRE_TOKEN: matches no logfire destination of the policy; nothing is sent
   9 2026-11-02T22:42:31.803022Z blocked opentelemetry.span com-anthropic-sdk-python  a span from com.anthropic.sdk.python, which the policy does not approve

$ tail -n +$((N + 1)) $TOUR_DIR/collector.log
/v1/traces context_resolved ['provrun.component', 'provrun.run_id', 'provrun.step']
/v1/traces snapshot_materialized ['provrun.component', 'provrun.run_id', 'provrun.step']
/v1/traces input_staged ['provrun.component', 'provrun.run_id', 'provrun.step']
/v1/traces model_called ['gen_ai.operation.name', 'gen_ai.request.model', 'gen_ai.usage.input_tokens', 'gen_ai.usage.output_tokens', 'provrun.component', 'provrun.run_id', 'provrun.step']
/v1/traces tool_called ['gen_ai.operation.name', 'gen_ai.tool.name', 'provrun.component', 'provrun.run_id', 'provrun.step']
/v1/traces model_called ['gen_ai.operation.name', 'gen_ai.request.model', 'gen_ai.usage.input_tokens', 'gen_ai.usage.output_tokens', 'provrun.component', 'provrun.run_id', 'provrun.step']
/v1/traces tool_called ['gen_ai.operation.name', 'gen_ai.tool.name', 'provrun.component', 'provrun.run_id', 'provrun.step']
/v1/traces tool_called ['gen_ai.operation.name', 'gen_ai.tool.name', 'provrun.component', 'provrun.run_id', 'provrun.step']
/v1/traces model_called ['gen_ai.operation.name', 'gen_ai.request.model', 'gen_ai.usage.input_tokens', 'gen_ai.usage.output_tokens', 'provrun.component', 'provrun.run_id', 'provrun.step']
/v1/traces input_verified ['provrun.component', 'provrun.run_id', 'provrun.step']
/v1/traces output_artifact_created ['provrun.component', 'provrun.run_id', 'provrun.step']
/v1/traces output_artifact_created ['provrun.component', 'provrun.run_id', 'provrun.step']
/v1/traces run ['gen_ai.agent.name', 'gen_ai.operation.name', 'provrun.component', 'provrun.run_id', 'provrun.step']

$ uv run python tours/provrun/field.py $TOUR_DIR/r10.json mlflow
{
  "destination": "own",
  "receives": "usage",
  "copied": true,
  "tracking_uri": "sqlite:////tmp/provrun-tour/online.db",
  "experiment_id": "2",
  "mlflow_run_id": "c527b3eca90a440386f2c868f691f03e",
  "trace_id": "tr-ba603b0371ea52c5a49ba060ebd2ed06",
  "spans": 13
}

$ uv run provr --store $O verify $R10
run-20261102T224225Z-8e0c9702: the evidence and the outputs verify, and match the committed bundle
```

**What happened:**
- Logfire got nothing again.
- The Anthropic SDK made a span of its own, under the name `com.anthropic.sdk.python`. The policy does not approve it, so provrun kept it out of the evidence, and sent it nowhere.
- The collector got the run's spans, each tool call among them.
- The run verifies, and its copy is in MLflow too, with its 13 spans.

Unset the variables:

```
$ unset PROVRUN_TELEMETRY_POLICY OTEL_EXPORTER_OTLP_ENDPOINT LOGFIRE_TOKEN
```

The examples `claude-run.yaml` and `claude-agent.yaml` in `packages/provrun/examples` run the same two on a local file, with no store and no policy.

## Step 16: Stream a real model in your own agent (needs ANTHROPIC_API_KEY)

This step also runs only with `--online`, and costs less than a cent. A run file asks for a streamed reply with `stream: true`, as Step 14 showed. Your own code can stream too, and still get provrun's evidence, by giving its PydanticAI agent provrun's capability, `evidence_capability`. The script `streamed.py` does this, with one tool. It prints the reply as it arrives, then the events that the capability wrote.

The agent, and how it is run. `PROVRUN_TOUR_MODEL` can name another model, such as PydanticAI's `test`, which needs no key; the tests run the script that way:

```
$ sed -n "/^model = /,/^    print()/p" tours/provrun/streamed.py
model = os.environ.get("PROVRUN_TOUR_MODEL", "anthropic:claude-haiku-5-5")
agent = Agent(model, tools=[revenue], capabilities=[evidence_capability(ctx)],
              instructions="Use the revenue tool. Answer in one short sentence.")


async def main() -> None:
    async with agent.run_stream("What was revenue in 2025?", model_settings={"max_tokens": 200}) as run:
        async for text in run.stream_text(delta=True):
            print(text, end="", flush=True)
    print()
```

Run it:

```
$ uv run python tours/provrun/streamed.py $TOUR_DIR/streamed
Revenue in 2025 was 42 million.
model_called claude-haiku-5-5 streamed 404 48 tool_call
tool_called revenue
model_called claude-haiku-5-5 streamed 460 17 stop
```

**What happened:**
- The agent asked for a streamed reply, and the script printed the text as it arrived.
- Each of the two model requests wrote one `model_called`, marked `streamed`. The first asked for the tool, and the second gave the answer.
- Each `model_called` holds the tokens in and out, and why the model stopped. The capability reads them from the whole response, after the stream ends.
- The tool call wrote `tool_called`.

This script writes only events. It has no store, no bundle and no telemetry policy, which `provr run` gives a run. Its evidence has no `run_created` first and no `run_completed` last, so `provr verify` does not pass it. The script refuses a folder that is not empty, as a second run there would add to the first.

## Where each step lives in the code

| Step | File |
|---|---|
| Decide access | `packages/provrun/src/provrun/access.py` |
| Pin and make a snapshot | `packages/provrun/src/provrun/resolve.py`, which calls `Catalog.snapshot` in `packages/provcat/src/provcat/catalog.py` |
| Hash, clone and freeze | `packages/provcat/src/provcat/cache.py` |
| Stage and verify inputs | `packages/provrun/src/provrun/stagers.py` |
| Run the model | `packages/provrun/src/provrun/backends/litellm.py` |
| Record each request of a PydanticAI agent, streamed or not | `evidence_capability` in `packages/provrun/src/provrun/backends/pydantic_ai.py` |
| Write the events | `packages/provrun/src/provrun/evidence.py`, through a store in `packages/provevi/src/provevi/store.py` |
| Check the evidence, the outputs and the bundle | `packages/provevi/src/provevi/checks.py` |
| Commit the bundle | `packages/provrun/src/provrun/context.py` |
| Keep the evidence in SQLite, and answer queries | `packages/provevi/src/provevi/sqlite.py` |
| The library entry and its errors | `packages/provrun/src/provrun/runner.py`, `spec.py` and `errors.py` |
| Apply the telemetry policy | `packages/provrun/src/provrun/telemetry.py`, with the policy in `packages/provevi/src/provevi/telemetry.py` |
| Send the spans to approved places | `packages/provrun/src/provrun/destinations.py` |
| Copy a sealed run into MLflow | `Routes.copy` in `packages/provrun/src/provrun/destinations.py`, which calls `packages/provevi/src/provevi/mlflow_copy.py` |
| The commands | `packages/provrun/src/provrun/cli.py`; `provr verify` calls `packages/provevi/src/provevi/cli.py` |
| The tour | `tours/provrun/run.sh`, `start.sh` and `stop.sh`, checked by `packages/provrun/tests/test_tour.py` |
