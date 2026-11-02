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

Add `--online` to run Step 13 too, which calls a real model. The script starts moto and a small collector, runs the steps, stops both and deletes its scratch folder.

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
| `tours/provrun/field.py`, `event.py`, `spans.py`, `modes.py`, `drop_event.py` | Small helpers that print one field of a run's output or evidence, or change the evidence. |

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
local-s3://data/main/acme/demo/dev/reports?at=commit:97a34a15217876c6b5f14a2be09f07178921398374a7a0769f1c4001a68a5615
```

The `version` that `put` prints is the ETag of the object, a hash that S3 gives its content. The pin below shows the version ID instead, which a versioned bucket gives each write.

Pin the file, and fetch it.

```
$ uv run provc pin data://acme/demo/dev/reports/report.txt
local-s3://data/main/acme/demo/dev/reports/report.txt?at=version:331f10c3-64f3-43f3-966a-75fb58398960

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
  "run_id": "run-20261102T195556Z-88d8d7ec",
  "status": "completed",
  "folder": "/tmp/provrun-tour/runs/run-20261102T195556Z-88d8d7ec",
  "bundle": "run://acme/demo/dev/run-20261102T195556Z-88d8d7ec",
  "pinned": "local-s3://runs/main/acme/demo/dev/run-20261102T195556Z-88d8d7ec?at=commit:8f206c3924a18eaa16044cc1c55f55b9d3f595ace09b1d8cae242974cd4e7df9",
  "note": "",
  "error": null
}

$ R1=$(uv run python tours/provrun/field.py $TOUR_DIR/r1.json run_id)
```

**What happened:** provrun asked the broker for the input, made a snapshot, staged it, ran the agent, and committed the bundle to the bucket `runs`. `pinned` is the URI of that commit. The next steps look at each part.

## Step 3: Look at the cache

```
$ find $TOUR_DIR/cache -type f | sort
/tmp/provrun-tour/cache/.provcat/locks/94c319e2d0007faa0c7404ce262c520d461c24246cfb26e7a16df72ee7cfe5a7.lock
/tmp/provrun-tour/cache/.provcat/meta/3d5694fed14124677ab233153f65622540d70ee046e7bfcd0e5f52dc5fde2678.json
/tmp/provrun-tour/cache/.provcat/meta/f56a44fe7fcc5f807321f25b306828115c7bc7f388faeda61dc5dba8ed2c0694.json
/tmp/provrun-tour/cache/.provcat/snapshots/file/205cfa28c510eeba46bd4bc49acc2fb13f7645f66bab5ff5f0c3bda90d26f0b2/report.txt
/tmp/provrun-tour/cache/local-s3/data/main/acme/demo/dev/reports/report.txt
/tmp/provrun-tour/cache/local-s3@version-331f10c3-64f3-43f3-966a-75fb58398960/data/main/acme/demo/dev/reports/report.txt

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

The records under `.provcat/meta` hold the version, the size and the SHA-256 of each cached file. The files under `.provcat/locks` keep two processes from writing the same path at once.

## Step 4: Look at the run folder

```
$ find $TOUR_DIR/runs/$R1 -type f | sort
/tmp/provrun-tour/runs/run-20261102T195556Z-88d8d7ec/bundle.json
/tmp/provrun-tour/runs/run-20261102T195556Z-88d8d7ec/evidence/events.jsonl
/tmp/provrun-tour/runs/run-20261102T195556Z-88d8d7ec/evidence/manifest.json
/tmp/provrun-tour/runs/run-20261102T195556Z-88d8d7ec/evidence/payloads/169a2eac7d7f094a88744ea5c1500e3a7ccb706952cdd6d5fa67eef58ff6c601
/tmp/provrun-tour/runs/run-20261102T195556Z-88d8d7ec/evidence/payloads/f4503948841cb1db460bbdec8f9b4178f81cc3254f1c17a27487d62495373fcd
/tmp/provrun-tour/runs/run-20261102T195556Z-88d8d7ec/input/report.txt
/tmp/provrun-tour/runs/run-20261102T195556Z-88d8d7ec/output/response.txt

$ uv run python tours/provrun/modes.py $TOUR_DIR/runs/$R1/input $TOUR_DIR/runs/$R1/input/report.txt
dr-xr-xr-x /tmp/provrun-tour/runs/run-20261102T195556Z-88d8d7ec/input
-r--r--r-- /tmp/provrun-tour/runs/run-20261102T195556Z-88d8d7ec/input/report.txt
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
  "parent_span_id": "c9fca02628b8d4aa",
  "pinned": "local-s3://data/main/acme/demo/dev/reports/report.txt?at=version:331f10c3-64f3-43f3-966a-75fb58398960",
  "sha256": "205cfa28c510eeba46bd4bc49acc2fb13f7645f66bab5ff5f0c3bda90d26f0b2",
  "span_id": "ce0da8a7d4e73d5c",
  "started_at": "2026-11-02T19:55:56.301512Z",
  "trace_id": "13a2ee8f025050d4cb2cefd70147b8cd"
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
local-s3://data/main/acme/demo/dev/reports?at=commit:a46dda60a0b8359ec27e84ee6924cbf2b2b139b0d97364450a546df08804325e

$ uv run provr run $TOUR_DIR/run.yaml > $TOUR_DIR/r2.json

$ R2=$(uv run python tours/provrun/field.py $TOUR_DIR/r2.json run_id)

$ uv run python tours/provrun/event.py $R2 snapshot_materialized pinned sha256
local-s3://data/main/acme/demo/dev/reports/report.txt?at=version:7974237a-bb0d-49cc-b585-71afe646f008
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
local-s3://data/main/acme/demo/dev/reports/report.txt?at=version:331f10c3-64f3-43f3-966a-75fb58398960

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
run-20261102T195556Z-88d8d7ec: the evidence and the outputs verify, and match the committed bundle

$ uv run provr verify $R2
run-20261102T195605Z-d482c7ba: the evidence and the outputs verify, and match the committed bundle
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
run-20261102T195556Z-88d8d7ec: the evidence and the outputs verify, and match the committed bundle
```

## Step 9: Keep the evidence in SQLite

The runs so far wrote their evidence only as files in each run folder. A `sqlite` store also keeps every run in one SQLite file, so you can ask questions across runs. Run the same file twice with `--store`. The path after `sqlite:///` is absolute, because `$TOUR_DIR` starts with a slash.

```
$ S=sqlite:///$TOUR_DIR/evidence.db

$ uv run provr --store $S run $TOUR_DIR/run.yaml > $TOUR_DIR/r4.json

$ uv run provr --store $S run $TOUR_DIR/run.yaml > /dev/null

$ Q=$(uv run python tours/provrun/field.py $TOUR_DIR/r4.json run_id)
```

Ask which runs read the report, and which experiments there are. Then ask what the first of these runs could read.

```
$ uv run prove --store $S runs --reading data://acme/demo/dev/reports/report.txt
run-20261102T195619Z-143ab2e0
run-20261102T195624Z-4f8fba83

$ uv run prove --store $S experiments
acme/demo/dev/revenue-question  created by you@example.com at 2026-11-02T19:56:19.325242Z

$ uv run prove --store $S access $Q
   2 granted  mounted_snapshot   data://acme/demo/dev/reports/report.txt  in the allow list; org and environment match
```

**What happened:**
- Each run wrote each event as a row of the file, as it happened. The run folder still holds the same files as before, so the committed bundle is the same.
- The experiment is the agent's name in its org, domain and environment. Its first run made it.
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
- The policy approves no destination, so provrun sent the run's spans nowhere. `destinations` records the collector that the variable names, and that nothing went to it. A `telemetry_blocked` event says so too:

```
$ uv run prove telemetry $PROVRUN_RUNS/$R4
   1 2026-11-02T19:56:33.087145Z blocked destination   OTEL_EXPORTER_OTLP_ENDPOINT: matches no otlp destination of the policy; nothing is sent
```

- provrun sends nothing to MLflow yet. That waits for Phase 4 of [provevi's plan](provevi-plan.md).

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
run-20261102T195647Z-e039d455 completed
callbacks after the run: 1
the LiteLLM logger logs at: DEBUG

$ R5=$(head -1 $TOUR_DIR/quiet.out | cut -d" " -f1)

$ uv run prove telemetry $PROVRUN_RUNS/$R5
   1 2026-11-02T19:56:47.740780Z blocked litellm.callbacks   removed __main__.<lambda> from litellm.success_callback
   2 2026-11-02T19:56:47.740872Z blocked logging litellm  raised the logger 'LiteLLM' from DEBUG to WARNING
   3 2026-11-02T19:56:47.740940Z blocked logging litellm  raised the logger 'LiteLLM Router' from DEBUG to WARNING
   4 2026-11-02T19:56:47.740991Z blocked logging litellm  raised the logger 'LiteLLM Proxy' from DEBUG to WARNING
   5 2026-11-02T19:56:47.741041Z blocked logging anthropic  raised the logger 'anthropic' from DEBUG to WARNING
   6 2026-11-02T19:56:47.741089Z blocked logging openai  raised the logger 'openai' from DEBUG to WARNING
   7 2026-11-02T19:56:47.741133Z blocked logging botocore  raised the logger 'botocore' from DEBUG to WARNING
```

Each line gives the sequence number of a `telemetry_blocked` event, its time, the word `blocked`, what was blocked, the library, and the detail. The callback has no library, because it is the program's own.

**What happened:**
- As the run started, provrun emptied LiteLLM's callback lists, so the callback saw no model call. The loggers that can log request bodies stayed at `WARNING`. So the log file holds no prompt, and none of the request signatures that botocore, the S3 library, logs at `DEBUG`.
- When the run ended, provrun put the callback and the logger levels back. So the program has its callback again, and the LiteLLM logger logs at `DEBUG` again.
- After the run, LiteLLM printed a few `DEBUG` lines as it shut down, which the tour keeps in `quiet.err`. Its logger follows the program's root logger again, and the program loaded LiteLLM before the run set `LITELLM_LOG`.

The run is one trace. Print the type, the span ID and the parent span ID of each event with a span. Then print their trace IDs:

```
$ uv run python tours/provrun/spans.py $R5
run_created b7f61521ecc50c64 None
context_resolved 3e5f95e9dabbbf4a b7f61521ecc50c64
snapshot_materialized cf528e13564aad23 b7f61521ecc50c64
input_staged 7092a4bd3a0866e1 b7f61521ecc50c64
model_called 7cf767d323943ceb b7f61521ecc50c64
input_verified 1c3aaa34f1381a21 b7f61521ecc50c64
output_artifact_created 569f28ce62873d66 b7f61521ecc50c64
run_completed b7f61521ecc50c64 None
trace IDs: e9ab5d23aaebcf22dcc30930a0726ec2
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

## Step 13: Call a real model (needs ANTHROPIC_API_KEY)

This step runs only with `--online`. It needs `ANTHROPIC_API_KEY` in the environment, and it costs a few cents. It writes a copy of the run file with a real model and no `settings`, runs it, and prints the answer.

```
$ sed -e "s|openai/gpt-4o-mini|anthropic/claude-haiku-5-5|" -e "/settings:/,\$d" $TOUR_DIR/run.yaml > $TOUR_DIR/real.yaml

$ uv run provr run $TOUR_DIR/real.yaml > $TOUR_DIR/r7.json

$ cat $PROVRUN_RUNS/$(uv run python tours/provrun/field.py $TOUR_DIR/r7.json run_id)/output/response.txt
```

The model reads the restated report, so it should answer 45 million. The event `model_called` records the real token counts. The examples `claude-run.yaml` and `claude-agent.yaml` in `packages/provrun/examples` do the same with a local file, and the second runs an agent with tools.

## Where each step lives in the code

| Step | File |
|---|---|
| Decide access | `packages/provrun/src/provrun/access.py` |
| Pin and make a snapshot | `packages/provrun/src/provrun/resolve.py`, which calls `Catalog.snapshot` in `packages/provcat/src/provcat/catalog.py` |
| Hash, clone and freeze | `packages/provcat/src/provcat/cache.py` |
| Stage and verify inputs | `packages/provrun/src/provrun/stagers.py` |
| Run the model | `packages/provrun/src/provrun/backends/litellm.py` |
| Write the events | `packages/provrun/src/provrun/evidence.py`, through a store in `packages/provevi/src/provevi/store.py` |
| Check the evidence, the outputs and the bundle | `packages/provevi/src/provevi/checks.py` |
| Commit the bundle | `packages/provrun/src/provrun/context.py` |
| Keep the evidence in SQLite, and answer queries | `packages/provevi/src/provevi/sqlite.py` |
| The library entry and its errors | `packages/provrun/src/provrun/runner.py`, `spec.py` and `errors.py` |
| Apply the telemetry policy | `packages/provrun/src/provrun/telemetry.py`, with the policy in `packages/provevi/src/provevi/telemetry.py` |
| Send the spans to approved places | `packages/provrun/src/provrun/destinations.py` |
| The commands | `packages/provrun/src/provrun/cli.py`; `provr verify` calls `packages/provevi/src/provevi/cli.py` |
| The tour | `tours/provrun/run.sh`, `start.sh` and `stop.sh`, checked by `packages/provrun/tests/test_tour.py` |
