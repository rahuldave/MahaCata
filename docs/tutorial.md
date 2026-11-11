---
title: "Tutorial: run an agent on a named report, and check what it did"
---

In this tutorial, you ask an agent a question about a report, and then you answer the questions that come later. You find which version of the report the agent read, what it answered, and whether anyone has changed the record since. All three packages of MahaCata take part. provcat names the report and keeps its versions, provrun runs the agent, and provevi keeps the evidence of each run and checks it.

The tutorial takes about 15 minutes. It needs no account and no key, because the agent's answer is fixed in the run file. The last step is optional, and asks a real model.

## Why the evidence of a run is useful

A month after an agent answers a question, someone asks how it got its answer. The report may have changed since then, and so may the prompt and the model. Without a record, nobody can say which version the agent read. MahaCata records each step of a run as it happens, with the version of each input. Each event holds the hash of the event before it, so a later change to the record shows up when you check it.

## Terms

| Term | Meaning |
|---|---|
| logical URI | A name that code uses, such as `data://acme/finance/dev/reports/q3.txt`. It does not say where the data is. |
| physical URI | An address in one store, such as `local-s3://data/main/acme/finance/dev/reports/q3.txt`. |
| scheme | A rule in provcat's configuration that turns a logical URI into a physical URI. |
| branch | A line of versions in a store, such as `main`. In a bucket, the branch is the first folder of the path. |
| pinned URI | A URI with a fixed version, such as one that ends in `?at=version:...`. |
| run file | A YAML file that says what to run: the agent, its question and its inputs. |
| run folder | The folder of one run. It holds the input that the agent read, its output and the evidence. |
| evidence | The events of a run, in order. Each one holds the SHA-256 hash of the one before it. |
| evidence store | Where provevi keeps the evidence. This tutorial uses a SQLite file, which answers questions across runs. |

## Before you start

You need Python 3.13, [uv](https://docs.astral.sh/uv/), `curl` and `bash`.

1. Clone the repository, install the packages, and stay in the top folder of the repository for every command.

   ```sh
   git clone https://github.com/rahuldave/MahaCata
   cd MahaCata
   uv sync
   ```

2. Clear the variables that your shell may hold from your own work or from an earlier tutorial. The first line clears those of provcat and provrun. It also clears the AWS profile, which would send requests to a real account. The tutorial sets each one that it needs. The second line clears those of OpenTelemetry, Logfire, MLflow, LiteLLM, Postgres and proxies. Each of them would change what a run records, and so the output.

   ```sh
   unset PROVCAT_CONFIG PROVCAT_CACHE PROVCAT_HOOKS PROVCAT_APP PROVCAT_TENANT PROVRUN_RUNS PROVRUN_TELEMETRY_POLICY PYDANTIC_AI_NO_BANNER AWS_PROFILE AWS_SESSION_TOKEN
   for v in $(env | command grep -oiE '^((OTEL|LOGFIRE|MLFLOW|LITELLM)_|PG|[A-Z_]*PROXY)[A-Z0-9_]*'); do unset $v; done
   ```

   The second line also removes your shell's proxy. If you reach the internet only through a proxy, set it again before you run `uv` for a download, or before Step 10.

3. Choose a scratch folder and a port for the local S3 server.

   ```sh
   export TOUR_DIR=/tmp/mahacata-tutorial TOUR_S3_PORT=5055
   mkdir $TOUR_DIR
   ```

4. Open a second terminal in the same folder, and start moto there. moto is a local server that speaks the S3 protocol, so it takes the place of a cloud bucket. If you chose another port, use it in place of 5055. Leave it running.

   ```sh
   uv run moto_server -p 5055
   ```

A script runs the whole tutorial in one go, with `bash tours/tutorial/run.sh`. It needs only the first step above, and a folder that does not exist yet. It starts its own moto. Stop yours first, or set `TOUR_S3_PORT` to another port.

It stops its moto and deletes its folder at the end. The output below comes from that script. The run IDs, version IDs and commit IDs differ on each run.

A run can print a warning from LiteLLM, that the coroutine `Logging.async_success_handler` was never awaited. LiteLLM writes it after the run ends, and it does not change the run. The script hides it with `PYTHONWARNINGS`.

## Step 1: Make a bucket

moto accepts any keys. Set fake ones, so that the AWS library does not look for yours. Then make the bucket `data`, with versioning on, so that the bucket keeps each version of each file.

```
$ export AWS_ACCESS_KEY_ID=tutorial AWS_SECRET_ACCESS_KEY=tutorial AWS_DEFAULT_REGION=us-east-1 AWS_CONFIG_FILE=/dev/null

$ uv run python tours/tutorial/make_bucket.py data
```

## Step 2: Name the report

provcat reads its configuration from a YAML file. The tutorial's file names one store, the moto server, and one scheme, `data`:

```yaml
# The catalog of the tutorial: one store and one name.
cache:
  root: ${oc.env:TOUR_DIR}/cache

stores:
  # moto, a local server that speaks the S3 protocol.
  local-s3:
    driver: s3
    endpoint_url: http://localhost:${oc.env:TOUR_S3_PORT,5055}

schemes:
  # data://org/domain/env/path is the file at that path in the bucket data.
  data:
    parts: [org, domain, env]
    rest: path
    target: "local-s3://data/{ref}/{org}/{domain}/{env}/{path}"
    ref: main
```

The scheme says that a logical URI has three parts, the org, the domain and the environment, and then a path. It puts the file under those parts in the bucket `data`, on the branch `main`. Copy the file, and point provcat at it. Then ask provcat where a name goes.

```
$ cp tours/tutorial/provcat.yaml $TOUR_DIR/

$ export PROVCAT_CONFIG=$TOUR_DIR/provcat.yaml PROVCAT_CACHE=$TOUR_DIR/cache

$ uv run provc resolve data://acme/finance/dev/reports/q3.txt
{
  "logical": "data://acme/finance/dev/reports/q3.txt",
  "physical": "local-s3://data/main/acme/finance/dev/reports/q3.txt",
  "via": [
    "data template -> local-s3://data/main/acme/finance/dev/reports/q3.txt"
  ]
}
```

Code and run files use the logical URI. If the report moves to another store, only the scheme changes.

## Step 3: Put the report in the bucket, and commit it

Write a one-line report, and put it in the bucket by its name. The `version` that `put` prints is the ETag of the object, a hash that S3 gives its content. Then commit the folder. A commit records the version of every file in the folder, with an ID. Files that belong together can then be read together later. This tutorial reads one file, so it pins the file itself, after the commit.

```
$ echo "Revenue in the third quarter was 42 million." > $TOUR_DIR/q3.txt

$ uv run provc put data://acme/finance/dev/reports/q3.txt $TOUR_DIR/q3.txt
[
  {
    "uri": "local-s3://data/main/acme/finance/dev/reports/q3.txt",
    "kind": "file",
    "size": 45,
    "version": "e49f6d6c6af456f911b96ac19c00251d"
  }
]

$ uv run provc commit data://acme/finance/dev/reports -m 'first report'
local-s3://data/main/acme/finance/dev/reports?at=commit:9342c0bdc9daa29e278efa7367e9fc219133472ef3eae2da271816d478b08731
```

A pin gives the version that the name holds now. The bucket gives each write a version ID, and the pinned URI names it. The version ID is not the ETag. A pinned URI always reads the same bytes, even after the file changes.

```
$ uv run provc pin data://acme/finance/dev/reports/q3.txt
local-s3://data/main/acme/finance/dev/reports/q3.txt?at=version:53a57dd7-0515-4998-8091-1e265bc3c389
```

## Step 4: Write the run file

The run file says who runs the agent, and where the run belongs. Then it gives the agent's question and its input, by the logical URI:

```yaml
# One question about one report. mock_response fixes the answer, so the
# tutorial needs no model and no key.
run:
  org: acme
  domain: finance
  env: dev
  actor: you@example.com
agent:
  name: revenue-question
  backend: litellm
  model: anthropic/claude-haiku-5-5
  instructions: Answer from the report only.
  prompt: What was revenue in the third quarter?
  inputs:
    - data://acme/finance/dev/reports/q3.txt
  settings:
    mock_response: Revenue in the third quarter was 42 million.
```

`mock_response` makes LiteLLM give that answer without calling the model. Step 10 removes it, to ask a real model. Copy the file:

```
$ cp tours/tutorial/run.yaml $TOUR_DIR/
```

## Step 5: Run the agent

`PROVRUN_RUNS` says where provrun makes the run folders. `--store` names the evidence store, here a SQLite file. Run the agent:

```
$ export PROVRUN_RUNS=$TOUR_DIR/runs S=sqlite:///$TOUR_DIR/evidence.db

$ uv run provr --store $S run $TOUR_DIR/run.yaml | tee $TOUR_DIR/r1.json
{
  "run_id": "run-20261110T192903Z-bd0514b6",
  "status": "completed",
  "folder": "/tmp/mahacata-tutorial/runs/run-20261110T192903Z-bd0514b6",
  "bundle": null,
  "pinned": null,
  "note": "no store for runs is set; the bundle stays in the run folder",
  "error": null
}
```

The run ID names the run everywhere from now on. Keep it in `R1`:

```
$ R1=$(uv run python tours/tutorial/field.py $TOUR_DIR/r1.json run_id)
```

The note says that the run folder is the only copy of the run. A run file can also name a store for runs, and provrun then commits a copy of the run folder there. [The provrun tour](provrun-tour.md) does that.

## Step 6: Look at what the agent read

The run folder holds the input that the agent read, in `input/`, and its answer, in `output/`. The evidence is in `evidence/`. `bundle.json` holds the `bundle`, `pinned` and `note` that `provr run` printed, with `bundle` under the key `uri`. The three fields say where provrun committed a copy of the run folder, when a run file names a store for runs. The files in `payloads/` hold the prompt and the answer, and each one is named by the SHA-256 hash of its content.

```
$ find $TOUR_DIR/runs/$R1 -type f | sort
/tmp/mahacata-tutorial/runs/run-20261110T192903Z-bd0514b6/bundle.json
/tmp/mahacata-tutorial/runs/run-20261110T192903Z-bd0514b6/evidence/events.jsonl
/tmp/mahacata-tutorial/runs/run-20261110T192903Z-bd0514b6/evidence/manifest.json
/tmp/mahacata-tutorial/runs/run-20261110T192903Z-bd0514b6/evidence/payloads/6ead0d42d7681b5eb0759a1769a13617d6bb7ea14341edd79a59c56aa63fcd25
/tmp/mahacata-tutorial/runs/run-20261110T192903Z-bd0514b6/evidence/payloads/d092ca7582c9211ace9c28b2de0ff2da92138c756b2cb2c961ca507c715efb87
/tmp/mahacata-tutorial/runs/run-20261110T192903Z-bd0514b6/input/q3.txt
/tmp/mahacata-tutorial/runs/run-20261110T192903Z-bd0514b6/output/response.txt

$ cat $TOUR_DIR/runs/$R1/input/q3.txt
Revenue in the third quarter was 42 million.
```

provrun gave the agent the version that it pinned, and not the file in the bucket. The copy is read-only, so the agent cannot change its own input:

```
$ uv run python tours/tutorial/modes.py $TOUR_DIR/runs/$R1/input $TOUR_DIR/runs/$R1/input/q3.txt
dr-xr-xr-x /tmp/mahacata-tutorial/runs/run-20261110T192903Z-bd0514b6/input
-r--r--r-- /tmp/mahacata-tutorial/runs/run-20261110T192903Z-bd0514b6/input/q3.txt
```

## Step 7: Read the evidence

`prove show` prints each event of a run. The `awk` keeps three columns, the number of the event, its type and the part of provrun that wrote it.

```
$ uv run prove --store $S show $R1 | awk '{print $1, $3, $4}'
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
```

The events follow the run in order:

- **Events 1 and 2.** provrun asked the broker whether the run may read the report, and the broker said yes. The broker decides who may read what.
- **Events 3 to 5.** provcat pinned the report, and provrun gave the agent a read-only copy of that version.
- **Events 7 and 8.** These record the prompt and the model's answer, and the payloads hold their text.
- **Event 10.** provrun checked that the input had not changed while the agent ran.

`prove access` prints the broker's decisions. This tutorial uses the stub broker. It grants access to each input that the run file names, if the input's org and environment are those of the run.

```
$ uv run prove --store $S access $R1
   2 granted  mounted_snapshot   data://acme/finance/dev/reports/q3.txt  in the allow list; org and environment match
```

## Step 8: Change the report, and run again

Restate the report, put it in the bucket, and commit it. Then run the same run file again.

```
$ echo "Revenue in the third quarter was 45 million, after a restatement." > $TOUR_DIR/q3.txt

$ uv run provc put data://acme/finance/dev/reports/q3.txt $TOUR_DIR/q3.txt
[
  {
    "uri": "local-s3://data/main/acme/finance/dev/reports/q3.txt",
    "kind": "file",
    "size": 66,
    "version": "a9cfab73a754a1fb3968102e701fc582"
  }
]

$ uv run provc commit data://acme/finance/dev/reports -m 'restated report'
local-s3://data/main/acme/finance/dev/reports?at=commit:f0402413542ee9c88c09b0981913a0be24abecd2c2c18a8139212fcb9c422fd7

$ uv run provr --store $S run $TOUR_DIR/run.yaml > $TOUR_DIR/r2.json

$ R2=$(uv run python tours/tutorial/field.py $TOUR_DIR/r2.json run_id)
```

Now ask the evidence store which runs read the report. Both runs name it by the same logical URI.

```
$ uv run prove --store $S runs --reading data://acme/finance/dev/reports/q3.txt
run-20261110T192903Z-bd0514b6
run-20261110T192913Z-471bea5e
```

The store also keeps the version that each run read, and the SHA-256 hash of its bytes. The two runs read two versions:

```
$ uv run python tours/tutorial/pins.py $S $R1 $R2
run-20261110T192903Z-bd0514b6  local-s3://data/main/acme/finance/dev/reports/q3.txt?at=version:53a57dd7-0515-4998-8091-1e265bc3c389
                               sha256 6314ca369e7e9f83c0b19536ead44eb549623f81507fec6b3dfab3f9b16a0ada
run-20261110T192913Z-471bea5e  local-s3://data/main/acme/finance/dev/reports/q3.txt?at=version:e74792a8-d120-4b94-8d83-45d589502727
                               sha256 d9936f11a8ac07f8c5beea6ae8e5b982a85b8b5528d80b647bf1dca2c25d5a80
```

The first run's pinned URI still reads the first report. A run file that names that pinned URI, in place of the logical URI, runs the agent on the first report again.

Both runs gave the same answer, because `mock_response` fixes it. A real model reads the input, as Step 10 shows.

## Step 9: Check the runs, and break one

`prove verify` checks the hash chain of the events, the payloads and the outputs of a run. The first run verifies:

```
$ uv run prove --store $S verify $R1
run-20261110T192903Z-bd0514b6: the evidence and the outputs verify; no committed bundle to compare with
```

Now change the answer of the second run, as someone who edits the record would. The check finds the change, and exits with status 1.

```
$ echo "Revenue was 99 million." > $TOUR_DIR/runs/$R2/output/response.txt

$ uv run prove --store $S verify $R2 || echo "exit status $?"
prove: output response.txt was changed
exit status 1
```

The evidence holds the SHA-256 hash of each output, so a changed output no longer matches its event. A changed event breaks the hash chain in the same way.

## Step 10: Ask a real model (needs ANTHROPIC_API_KEY)

This step is optional. It calls Anthropic's API, and each call costs a fraction of a cent. Set `ANTHROPIC_API_KEY`, and make a run file without `mock_response`:

```
$ grep -v mock_response $TOUR_DIR/run.yaml | grep -v "^  settings:" > $TOUR_DIR/real.yaml

$ uv run provr --store $S run $TOUR_DIR/real.yaml > $TOUR_DIR/r3.json

$ R3=$(uv run python tours/tutorial/field.py $TOUR_DIR/r3.json run_id)

$ cat $TOUR_DIR/runs/$R3/output/response.txt
Revenue in the third quarter was **45 million**, after a restatement. The report does not give the figure from before the restatement.
```

The model read the restated report, which is the version that the name held when the run started. Its words differ on each run. `bash tours/tutorial/run.sh --online` runs this step too.

## What you built

You built the whole path of one question:

- **provcat** named the report, kept each version, and pinned the version that each run read.
- **provrun** asked the broker for the report, gave the agent a read-only copy of the pinned version, and recorded each step.
- **provevi** kept the evidence in SQLite, found the runs that read the report and the version that each one read, and found the changed answer.

The tutorial leaves out several features. A run can commit a copy of its folder to a store, so that a check can compare the run folder with the copy. A telemetry policy says which libraries may send data from a run, and where. A run can also stream the answer to a live viewer, and name the experiment that it belongs to.

## Clean up

Stop moto with Ctrl-C in its terminal. The inputs in the run folders are read-only, so give write access back before you delete the folder:

```sh
chmod -R u+w $TOUR_DIR
rm -rf $TOUR_DIR
unset AWS_ACCESS_KEY_ID AWS_SECRET_ACCESS_KEY AWS_DEFAULT_REGION AWS_CONFIG_FILE PROVCAT_CONFIG PROVCAT_CACHE PROVRUN_RUNS S TOUR_DIR TOUR_S3_PORT
```

## Where to go next

| To do this | Read |
|---|---|
| Learn provcat alone, from a local file to a bucket | [The provcat tutorial](provcat-tutorial.md) |
| Learn provrun alone, from a run file to its evidence | [The provrun tutorial](provrun-tutorial.md) |
| Learn provevi alone, from a store to a broken run | [The provevi tutorial](provevi-tutorial.md) |
| See every feature of the three packages on one machine | [The provrun tour](provrun-tour.md) |
| See how the three packages share the work | [What MahaCata does](overview.md) |
