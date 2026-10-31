# A guided tour of provrun

This tour follows one file from a store, through the provcat cache, into a run of an agent, and into the evidence of that run. Each step gives commands to run and the output that they print. After each command, the tour says what happened inside. [provrun.md](provrun.md) is the reference for provrun, and [tour.md](tour.md) is the tour of provcat alone.

The tour needs no model and no key. The agent answers with the LiteLLM setting `mock_response`. The store is moto, a local S3 server that the tests also use. The last step calls a real model, and it is optional.

## Terms

| Term | Meaning |
|---|---|
| cache | The folder where provcat keeps the files that it fetched. |
| pinned URI | A URI with a fixed version, for example `?at=version:235061e9-...`. |
| snapshot | A read-only copy of one pinned version, in a folder named by the SHA-256 of its content. |
| run folder | The folder of one run. It holds `input/`, `output/`, `scratch/` and `evidence/`. |
| bundle | The evidence and the outputs of a run. If the run sets `bundle_scheme`, provrun commits the bundle to a store. Otherwise it stays in the run folder. |

## Before you start

You need Python 3.13 and [uv](https://docs.astral.sh/uv/). Run every command from the top folder of the repository.

1. Install the packages.

   ```
   uv sync
   ```

2. Start moto in a second terminal.

   ```
   uv run moto_server -p 5055
   ```

3. Point everything at a scratch folder, so the tour does not touch your real cache or your real runs. Give moto any keys, because it does not check them.

   ```
   mkdir -p /tmp/provrun-tour
   export PROVCAT_CACHE=/tmp/provrun-tour/cache
   export PROVRUN_RUNS=/tmp/provrun-tour/runs
   export PROVCAT_CONFIG=/tmp/provrun-tour/provcat.yaml
   export AWS_ACCESS_KEY_ID=test AWS_SECRET_ACCESS_KEY=test AWS_DEFAULT_REGION=us-east-1
   ```

4. Make two versioned buckets. The bucket `data` holds the data, and the bucket `runs` holds the bundles.

   ```
   uv run python -c "
   import boto3
   s = boto3.client('s3', endpoint_url='http://localhost:5055')
   for b in ('data', 'runs'):
       s.create_bucket(Bucket=b)
       s.put_bucket_versioning(Bucket=b, VersioningConfiguration={'Status': 'Enabled'})"
   ```

5. Write the provcat configuration to `/tmp/provrun-tour/provcat.yaml`. It has one store and two logical schemes. A URI `data://acme/demo/dev/reports/report.txt` names the org `acme`, the domain `demo` and the environment `dev`.

   ```yaml
   stores:
     local-s3:
       driver: s3
       endpoint_url: http://localhost:5055
   schemes:
     data:
       parts: [org, domain, env]
       rest: path
       target: "local-s3://data/{ref}/{org}/{domain}/{env}/{path}"
       ref: main
     run:
       parts: [org, domain, env]
       rest: path
       target: "local-s3://runs/{ref}/{org}/{domain}/{env}/{path}"
       ref: main
   ```

6. Write the run file to `/tmp/provrun-tour/run.yaml`. It asks one question about one input, and commits the bundle through the scheme `run`.

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

```
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
$ echo "Revenue in 2025 was 42 million." > /tmp/provrun-tour/report.txt
$ uv run provc put data://acme/demo/dev/reports/report.txt /tmp/provrun-tour/report.txt
[
  {
    "uri": "local-s3://data/main/acme/demo/dev/reports/report.txt",
    "kind": "file",
    "size": 32,
    "version": "de5877cfcc0dfd578914982c36e261dc"
  }
]
$ uv run provc commit data://acme/demo/dev/reports -m "first report"
local-s3://data/main/acme/demo/dev/reports?at=commit:5b1233df...
```

The `version` that `put` prints is the ETag of the object, a hash that S3 gives its content. The pin below shows the version ID instead, which a versioned bucket gives each write.

Pin the file, and fetch it.

```
$ uv run provc pin data://acme/demo/dev/reports/report.txt
local-s3://data/main/acme/demo/dev/reports/report.txt?at=version:235061e9-dc18-437b-a887-db80a7e82ef2

$ uv run provc fetch data://acme/demo/dev/reports/report.txt
/tmp/provrun-tour/cache/local-s3/data/main/acme/demo/dev/reports/report.txt
```

**What happened:**
- `pin` turned the logical URI into the physical URI with the version that S3 gave the file.
- `fetch` of the URI without a version downloaded the newest version to a path that names no version.
- When the file changes in the store, the next fetch rewrites that same path. So a program must never read that path while the file can change. provrun never mounts it. It uses snapshots, as in Step 2.

## Step 2: Run the agent

```
$ uv run provr run /tmp/provrun-tour/run.yaml --config $PROVCAT_CONFIG
{
  "run_id": "run-20261030T204319Z-e7abedc6",
  "status": "completed",
  "folder": "/tmp/provrun-tour/runs/run-20261030T204319Z-e7abedc6",
  "bundle": "run://acme/demo/dev/run-20261030T204319Z-e7abedc6",
  "pinned": "local-s3://runs/main/acme/demo/dev/run-20261030T204319Z-e7abedc6?at=commit:62e12bdf...",
  "note": "",
  "error": null
}
```

LiteLLM may also print `RuntimeWarning: coroutine 'Logging.async_success_handler' was never awaited`. The warning comes from its logging after the run, and you can ignore it.

Keep the run ID in a variable.

```
$ R1=run-20261030T204319Z-e7abedc6
```

Your run ID differs. Each later command uses `$R1`.

**What happened:** provrun asked the broker for the input, made a snapshot, staged it, ran the agent, and committed the bundle to the bucket `runs`. The next steps look at each part.

## Step 3: Look at the cache

```
$ find /tmp/provrun-tour/cache -type f | sort
/tmp/provrun-tour/cache/.provcat/locks/94c319e2....lock
/tmp/provrun-tour/cache/.provcat/meta/7bd49ca9....json
/tmp/provrun-tour/cache/.provcat/meta/f56a44fe....json
/tmp/provrun-tour/cache/.provcat/snapshots/file/205cfa28c510eeba.../report.txt
/tmp/provrun-tour/cache/local-s3/data/main/acme/demo/dev/reports/report.txt
/tmp/provrun-tour/cache/local-s3@version-235061e9-.../data/main/acme/demo/dev/reports/report.txt

$ ls -l /tmp/provrun-tour/cache/.provcat/snapshots/file/*/
total 8
-r--r--r--  1 you  wheel  32 Oct 30 16:43 report.txt
```

The cache now holds the file three times:

| Path | Made by | Can it change? |
|---|---|---|
| `local-s3/.../report.txt` | `provc fetch` in Step 1, without a version | Yes. The next fetch of a new version rewrites it. |
| `local-s3@version-235061e9-.../report.txt` | the run, which fetched the pinned URI | No fetch rewrites it, because the path names the version. But it is writable. |
| `.provcat/snapshots/file/205cfa28.../report.txt` | the run, which made a snapshot of the pinned copy | Not without notice. The file is read-only, and provcat checks its SHA-256 before it uses it again. A snapshot that someone changed is replaced. |

The folder name of the snapshot is the SHA-256 of the content. Two runs that read the same content under the same name share one snapshot. On macOS and on Linux disks that can clone, the snapshot is a copy-on-write clone, so it costs almost no space.

The records under `.provcat/meta` hold the version, the size and the SHA-256 of each cached file. The files under `.provcat/locks` keep two processes from writing the same path at once.

## Step 4: Look at the run folder

```
$ find /tmp/provrun-tour/runs/$R1 -type f | sort
/tmp/provrun-tour/runs/run-.../bundle.json
/tmp/provrun-tour/runs/run-.../evidence/events.jsonl
/tmp/provrun-tour/runs/run-.../evidence/manifest.json
/tmp/provrun-tour/runs/run-.../evidence/payloads/169a2eac...
/tmp/provrun-tour/runs/run-.../evidence/payloads/f4503948...
/tmp/provrun-tour/runs/run-.../input/report.txt
/tmp/provrun-tour/runs/run-.../output/response.txt

$ ls -ld /tmp/provrun-tour/runs/$R1/input
dr-xr-xr-x  3 you  wheel  96 Oct 30 16:43 /tmp/provrun-tour/runs/run-.../input

$ echo x >> /tmp/provrun-tour/runs/$R1/input/report.txt
zsh: permission denied: /tmp/provrun-tour/runs/run-.../input/report.txt
```

bash words the error as `Permission denied`. The root user ignores read-only bits, so as root, for example in a container, the write succeeds. Then `input_verified` is what catches a change.

**What happened:**
- The stager cloned the snapshot into `input/` and made the folder and the file read-only. The agent knows the file as `/input/report.txt`. The LiteLLM backend has no file system, so it puts the text of the file into the prompt under that name. The PydanticAI backend mounts the folder at `/input`.
- The clone shares no storage that a write could reach, so a change in one run cannot reach the snapshot that other runs use.
- After the agent ran, the stager hashed `input/report.txt` again and wrote `input_verified`. A changed input fails the run.
- The agent wrote `output/response.txt`. `scratch/` is empty and is not part of the bundle.

## Step 5: Read the evidence

```
$ uv run provr show $R1
   0 ... run_created              RunContext       run://acme/demo/dev/run-... {"agent": "revenue-question", ...}
   1 ... access_requested         StubBroker       data://acme/demo/dev/reports/report.txt {"mode": "mounted_snapshot", ...}
   2 ... access_granted           StubBroker       data://acme/demo/dev/reports/report.txt {"reason": "in the allow list; org and environment match", ...}
   3 ... context_resolved         ProvcatResolver  data://... {"physical": "local-s3://data/main/acme/demo/dev/reports/report.txt", ...}
   4 ... snapshot_materialized    ProvcatResolver  data://... {"kind": "file", "pinned": "local-s3://...report.txt?at=version:235061e9-...", "sha256": "205cfa28..."}
   5 ... input_staged             InProcessStager  data://... {"agent_path": "/input/report.txt", "sha256": "205cfa28...", ...}
   6 ... sandbox_started          LiteLLMBackend   run://... {"inputs": ["/input/report.txt"], ...}
   7 ... prompt_rendered          LiteLLMBackend   data://... {"messages": 2}
   8 ... model_called             LiteLLMBackend   {"finish_reason": "stop", "input_tokens": 10, "output_tokens": 20, ...}
   9 ... sandbox_completed        LiteLLMBackend   run://... {"status": "completed", ...}
  10 ... input_verified           InProcessStager  data://... {"sha256": "205cfa28...", "unchanged": true}
  11 ... output_artifact_created  Outputter        run://.../output/response.txt {"sha256": "297c05b2...", "size": 31}
  12 ... run_completed            RunContext       run://acme/demo/dev/run-... {}
```

The output above is shortened.

**What happened:** each part of provrun wrote one or more events. Each event holds the SHA-256 of the line before it, so the events form a chain. Event 4 ties the logical URI to the exact version and to the SHA-256 of its content. The payloads are in `evidence/payloads`, each in a file named by its SHA-256. One holds the rendered prompt. The other holds the request and the response of the model call.

## Step 6: Change the file, and run again

Restate the report in the store.

```
$ echo "Revenue in 2025 was 45 million, after a restatement." > /tmp/provrun-tour/report.txt
$ uv run provc put data://acme/demo/dev/reports/report.txt /tmp/provrun-tour/report.txt
$ uv run provc commit data://acme/demo/dev/reports -m "restated report"
local-s3://data/main/acme/demo/dev/reports?at=commit:eb847483...
```

Run the agent again, and keep the new run ID in `R2`.

```
$ uv run provr run /tmp/provrun-tour/run.yaml --config $PROVCAT_CONFIG
$ R2=run-20261030T204334Z-5ec7a594
$ uv run provr show $R2 | grep snapshot_materialized
   4 ... snapshot_materialized    ProvcatResolver  ... {"kind": "file", "pinned": "local-s3://...report.txt?at=version:baeae1e9-...", "sha256": "106b090a..."}
```

Look at the snapshots.

```
$ for f in /tmp/provrun-tour/cache/.provcat/snapshots/file/*/report.txt; do echo "$f: $(cat $f)"; done
/tmp/provrun-tour/cache/.provcat/snapshots/file/106b090a.../report.txt: Revenue in 2025 was 45 million, after a restatement.
/tmp/provrun-tour/cache/.provcat/snapshots/file/205cfa28.../report.txt: Revenue in 2025 was 42 million.
```

**What happened:**
- The second run pinned the new version, `baeae1e9`, and made a new snapshot with a new SHA-256.
- The snapshot of the first run did not change. The evidence of the first run still names version `235061e9` and SHA-256 `205cfa28`, and that snapshot still holds those bytes.
- A third run on the same version makes no new snapshot. provcat checks the SHA-256 of the existing snapshot and reuses it.
- The agent gave the same answer twice, because `mock_response` fixes it. A real model would read the restated figure.

## Step 7: Run again on the first version

The store now holds the restated report. To repeat the first run exactly, give the agent the pinned URI from the evidence of the first run.

1. Copy the run file to `/tmp/provrun-tour/rerun.yaml`.
2. In the copy, replace the input with the pinned URI from event 4 of the first run, in Step 5.

   ```yaml
     inputs:
       - "local-s3://data/main/acme/demo/dev/reports/report.txt?at=version:235061e9-dc18-437b-a887-db80a7e82ef2"
   ```

3. Run it, and look at the input that the agent saw.

   ```
   $ uv run provr run /tmp/provrun-tour/rerun.yaml --config $PROVCAT_CONFIG
   $ R3=<the new run ID>
   $ uv run provr show $R3 | grep access_granted
      2 ... access_granted           StubBroker  local-s3://...report.txt?at=version:235061e9-... {"reason": "in the allow list; physical URI in dev", ...}
   $ cat /tmp/provrun-tour/runs/$R3/input/report.txt
   Revenue in 2025 was 42 million.
   ```

**What happened:**
- `local-s3` is a store in the configuration, so the broker treats the URI as physical. A physical URI names no environment, so the stub broker grants one only in `dev`.
- provcat fetched version `235061e9`, not the newest version, and found the snapshot `205cfa28` from the first run. The agent saw the report as it was before the restatement.
- So the evidence of a run is enough to run it again on the same bytes, as long as the store keeps the old version.

## Step 8: Verify the runs, and break one

```
$ uv run provr verify $R1 --config $PROVCAT_CONFIG
run-20261030T204319Z-e7abedc6: the evidence and the outputs verify, and match the committed bundle

$ uv run provr verify $R2 --config $PROVCAT_CONFIG
run-20261030T204334Z-5ec7a594: the evidence and the outputs verify, and match the committed bundle
```

**What happened:** `provr verify` checked the chain of events, the payloads and the outputs. Then it read the events and the manifest from the commit that `bundle.json` names, and compared them with the run folder. To reach that commit, it needs the provcat configuration, from `--config` or from `PROVCAT_CONFIG`.

Now change an output of the second run.

```
$ echo "Revenue was 99 million." > /tmp/provrun-tour/runs/$R2/output/response.txt
$ uv run provr verify $R2 --config $PROVCAT_CONFIG
provr: output response.txt was changed
```

The command exits with status 1. The event `output_artifact_created` holds the SHA-256 of the output, and the new bytes do not match it.

Someone who can change the run folder can also write a new chain that agrees with itself. The committed bundle is a copy in another store, so the check against the commit catches that chain. But `bundle.json` is in the run folder too, and that person can delete it. Then `provr verify` finds no commit, and it says only `no committed bundle to compare with`.

So a checker who does not trust the folder passes the commit with `--bundle`. The commit comes from outside the folder, for example from the output of `provr run`. `--require-bundle` fails a run that has no commit to compare with.

```
$ uv run provr verify $R1 --bundle "local-s3://runs/main/acme/demo/dev/run-20261030T204319Z-e7abedc6?at=commit:62e12bdf..." --require-bundle
run-20261030T204319Z-e7abedc6: the evidence and the outputs verify, and match the committed bundle
```

Quote a URI that holds `?` on the command line. zsh reads an unquoted `?` as a wildcard, and fails with `no matches found`.

## Step 9: Call a real model (optional)

This step needs `ANTHROPIC_API_KEY` in the environment, and it costs a few cents.

1. Copy the run file to `/tmp/provrun-tour/real.yaml`.
2. In the copy, set `model` to `anthropic/claude-haiku-5-5`, and remove the `settings` section.
3. Run it, and read the answer.

   ```
   uv run provr run /tmp/provrun-tour/real.yaml --config $PROVCAT_CONFIG
   cat /tmp/provrun-tour/runs/<run ID>/output/response.txt
   ```

The model reads the restated report, so it should answer 45 million. The event `model_called` records the real token counts. The examples `claude-run.yaml` and `claude-agent.yaml` in `packages/provrun/examples` do the same with a local file, and the second runs an agent with tools.

## Clean up

The snapshots and the staged inputs are read-only, so give write permission before you delete them. Stop moto with Ctrl+C in its terminal.

```
chmod -R u+w /tmp/provrun-tour && rm -rf /tmp/provrun-tour
```

## Where each step lives in the code

| Step | File |
|---|---|
| Decide access | `packages/provrun/src/provrun/access.py` |
| Pin and make a snapshot | `packages/provrun/src/provrun/resolve.py`, which calls `Catalog.snapshot` in `packages/provcat/src/provcat/catalog.py` |
| Hash, clone and freeze | `packages/provcat/src/provcat/cache.py` |
| Stage and verify inputs | `packages/provrun/src/provrun/stagers.py` |
| Run the model | `packages/provrun/src/provrun/backends/litellm.py` |
| Write the events | `packages/provrun/src/provrun/evidence.py` |
| Commit the bundle | `packages/provrun/src/provrun/context.py` |
| The commands | `packages/provrun/src/provrun/cli.py` |
