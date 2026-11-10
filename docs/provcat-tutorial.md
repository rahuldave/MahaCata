---
title: "Tutorial: name your files, and read them by name"
---

In this tutorial, you give your files names that do not say where they are. Then you read and write the files by those names. First you read a file on your own machine. Then you keep a file in a bucket, commit it, change it and read the old version back, all by the same name. Last, you read the file by its name from Python.

The tutorial takes about 10 minutes. It needs no account, no key and no network. Steps 1 and 2 need no server either.

## Why a file needs a name

Code that reads `s3://sales-bucket/main/orders/day1.csv` breaks when the file moves to another bucket or another cloud. Code that reads a name, such as `data://sales/orders/day1.csv`, keeps working, because provcat's configuration says where each name goes. The name also keeps working when you ask for an old version. You add a selector to the name, and provcat reads that version from the store.

## Terms

| Term | Meaning |
|---|---|
| store | A named place that holds data, such as a folder on this machine or a bucket server. |
| logical URI | A name that code uses, such as `data://sales/orders/day1.csv`. It does not say where the file is. |
| physical URI | An address in one store, such as `local-s3://sales/main/orders/day1.csv`. |
| scheme | A rule in the configuration that turns a logical URI into a physical URI. |
| selector | The `?at=` part of a URI, which picks a version or a branch, such as `?at=branch:dev`. |
| commit | A record of the version of every file in a folder, with an ID. |
| pinned URI | A URI with a fixed version, such as one that ends in `?at=version:...`. It always reads the same bytes. |
| cache | The local folder where provcat keeps the files that it read. |

## Before you start

You need Python 3.13, [uv](https://docs.astral.sh/uv/), `curl` and `bash`.

1. Clone the repository, install the packages, and stay in the top folder of the repository for every command.

   ```sh
   git clone https://github.com/rahuldave/MahaCata
   cd MahaCata
   uv sync
   ```

2. Clear the variables that your shell may hold from your own work or from an earlier tutorial. The first line clears those of provcat and provrun. It also clears the AWS profile, which would send requests to a real account. The tutorial sets each one that it needs. The second line clears the variables of telemetry, Postgres and proxies.

   ```sh
   unset PROVCAT_CONFIG PROVCAT_CACHE PROVCAT_HOOKS PROVCAT_APP PROVCAT_TENANT PROVRUN_RUNS PROVRUN_TELEMETRY_POLICY PYDANTIC_AI_NO_BANNER AWS_PROFILE AWS_SESSION_TOKEN
   for v in $(env | command grep -oiE '^((OTEL|LOGFIRE|MLFLOW|LITELLM)_|PG|[A-Z_]*PROXY)[A-Z0-9_]*'); do unset $v; done
   ```

   The second line also removes your shell's proxy. A proxy would send the requests for the local server elsewhere. If you reach the internet only through a proxy, set it again before you run `uv` for a download.

3. Choose a scratch folder and a port for the local S3 server, which Step 3 starts.

   ```sh
   export TOUR_DIR=/tmp/provcat-tutorial TOUR_S3_PORT=5055
   mkdir $TOUR_DIR
   ```

A script runs the whole tutorial in one go, with `bash tours/provcat-tutorial/run.sh`. It needs only the first step above, and a folder that does not exist yet. It starts its own moto. Stop yours first, or set `TOUR_S3_PORT` to another port.

It stops its moto and deletes its folder at the end. The output below comes from that script. The version IDs and the commit ID differ on each run.

## Step 1: Write the configuration

provcat reads its configuration from a YAML file. The tutorial's file names two stores and two schemes:

```yaml
# The catalog of the provcat tutorial: two stores and two names.
cache:
  root: ${oc.env:TOUR_DIR}/cache

stores:
  # Files on this machine, under one folder.
  local-files:
    driver: file
    root: ${oc.env:TOUR_DIR}/files
  # moto, a local server that speaks the S3 protocol.
  local-s3:
    driver: s3
    endpoint_url: http://localhost:${oc.env:TOUR_S3_PORT,5055}

schemes:
  # notes://project/path is a file under the folder files.
  notes:
    parts: [project]
    rest: path
    target: "local-files:///{project}/{path}"
  # data://project/path is a file in the bucket of the project, on a branch.
  data:
    parts: [project]
    rest: path
    target: "local-s3://{project}/{ref}/{path}"
    ref: main
```

Each store has a driver, which is the code that talks to that kind of store. The `file` driver reads and writes only under its `root`. Each scheme names its parts, and its `target` puts the parts into a physical URI. `${oc.env:TOUR_DIR}` takes the value of the variable `TOUR_DIR`.

Copy the file, and point provcat at it and at a cache of its own:

```
$ cp tours/provcat-tutorial/provcat.yaml $TOUR_DIR/

$ export PROVCAT_CONFIG=$TOUR_DIR/provcat.yaml PROVCAT_CACHE=$TOUR_DIR/cache
```

## Step 2: Read a local file by its name

Write a small file under the folder of the store `local-files`:

```
$ mkdir -p $TOUR_DIR/files/sales

$ printf 'region,target\neast,40\nwest,25\n' > $TOUR_DIR/files/sales/targets.csv
```

`provc resolve` shows where a name goes, and the rule that sent it there. `provc cat` reads the file by its name.

```
$ uv run provc resolve notes://sales/targets.csv
{
  "logical": "notes://sales/targets.csv",
  "physical": "local-files:///sales/targets.csv",
  "via": [
    "notes template -> local-files:///sales/targets.csv"
  ]
}

$ uv run provc cat notes://sales/targets.csv
region,target
east,40
west,25
```

The project `sales` became the first folder under the root, and the rest of the name became the path under it.

## Step 3: Make a bucket, and write a file to it

A folder on one machine keeps no versions. A bucket with versioning on keeps each version of each file, so the next steps use one. Open a second terminal in the same folder, and start moto there, on the port in `TOUR_S3_PORT`. Leave it running.

```sh
uv run moto_server -p 5055
```

moto accepts any keys. Set fake ones, so that the AWS library does not look for yours. Then make the bucket `sales`, with versioning on.

```
$ export AWS_ACCESS_KEY_ID=tutorial AWS_SECRET_ACCESS_KEY=tutorial AWS_DEFAULT_REGION=us-east-1 AWS_CONFIG_FILE=/dev/null

$ uv run python tours/provcat-tutorial/make_bucket.py sales
```

Write a file of orders, and put it in the bucket by its name. The scheme `data` takes the bucket from the project, and puts the branch `main` first in the path.

```
$ printf 'order,region,amount\n1,east,30\n2,west,12\n' > $TOUR_DIR/day1.csv

$ uv run provc put data://sales/orders/day1.csv $TOUR_DIR/day1.csv
[
  {
    "uri": "local-s3://sales/main/orders/day1.csv",
    "kind": "file",
    "size": 40,
    "version": "ee5762b26311c199582a32b684f862a4"
  }
]

$ uv run provc resolve data://sales/orders/day1.csv
{
  "logical": "data://sales/orders/day1.csv",
  "physical": "local-s3://sales/main/orders/day1.csv",
  "via": [
    "data template -> local-s3://sales/main/orders/day1.csv"
  ]
}

$ uv run provc ls data://sales/orders
[
  {
    "uri": "local-s3://sales/main/orders/day1.csv",
    "kind": "file",
    "size": 40,
    "version": "ee5762b26311c199582a32b684f862a4"
  }
]
```

The `version` that `put` and `ls` print is the ETag of the object, a hash that S3 gives its content.

## Step 4: Commit the folder

A commit records the version of every file in a folder, with an ID. `provc commit` prints the URI pinned to the new commit.

```
$ uv run provc commit data://sales/orders -m 'first day'
local-s3://sales/main/orders?at=commit:20ab0d6bffb65978ac6052dcf954f6ae6a52bf30d81f8c4ccf0d4021484b7f2e
```

A commit is useful when several files must stay together, such as the files of one day. This tutorial reads one file, so the next step pins the file itself.

## Step 5: Pin the file, change it, and read the old version

`provc pin` gives the version that a name holds now. The bucket gives each write a version ID, and the pinned URI names it. Keep it in `PINNED`:

```
$ PINNED=$(uv run provc pin data://sales/orders/day1.csv)

$ echo "$PINNED"
local-s3://sales/main/orders/day1.csv?at=version:92a458b0-3e64-4e15-b97d-61f492909609
```

Now change the amount of the second order, and put the file again. The name reads the new file, and the pinned URI still reads the old one.

```
$ printf 'order,region,amount\n1,east,30\n2,west,15\n' > $TOUR_DIR/day1.csv

$ uv run provc put data://sales/orders/day1.csv $TOUR_DIR/day1.csv
[
  {
    "uri": "local-s3://sales/main/orders/day1.csv",
    "kind": "file",
    "size": 40,
    "version": "67fd407bbf80fc532b35d6f2b914e3f1"
  }
]

$ uv run provc cat data://sales/orders/day1.csv
order,region,amount
1,east,30
2,west,15

$ uv run provc cat "$PINNED"
order,region,amount
1,east,30
2,west,12
```

A program that records the pinned URI of each file that it read can read the same bytes again later. provrun records them in the evidence of each run.

## Step 6: Make a branch, and change it

A branch is a copy of the files in which you can make changes, and leave `main` as it is. In a bucket, the branch is the first folder of the path. `provc branch` copies the branch `main` to a new branch `dev`:

```
$ uv run provc branch 'data://sales?at=branch:dev' --from main
local-s3://sales/dev
```

The selector `?at=branch:dev` sends a name to the branch `dev`. Add a third order on `dev`, and read the file on both branches.

```
$ printf 'order,region,amount\n1,east,30\n2,west,15\n3,north,8\n' > $TOUR_DIR/day1.csv

$ uv run provc put 'data://sales/orders/day1.csv?at=branch:dev' $TOUR_DIR/day1.csv
[
  {
    "uri": "local-s3://sales/dev/orders/day1.csv",
    "kind": "file",
    "size": 50,
    "version": "db9d103e03426ae52b4506187f6f4dc3"
  }
]

$ uv run provc cat 'data://sales/orders/day1.csv?at=branch:dev'
order,region,amount
1,east,30
2,west,15
3,north,8

$ uv run provc cat data://sales/orders/day1.csv
order,region,amount
1,east,30
2,west,15
```

The name without a selector still reads `main`, so code that reads `main` sees no change until you copy the work back.

## Step 7: Read from the cache, without asking the store

provcat keeps the files that it reads in its cache. `provc fetch` puts a file there, and prints the local path, so that a program that wants a path can read the file.

```
$ uv run provc fetch data://sales/orders/day1.csv
/tmp/provcat-tutorial/cache/local-s3/sales/main/orders/day1.csv
```

`--offline` reads only the cache, and never asks the store. A pinned version never changes, so the copy that Step 5 read is still right:

```
$ uv run provc --offline cat "$PINNED"
order,region,amount
1,east,30
2,west,12
```

## Step 8: Read by name from Python

Code reads names through the class `Catalog`. `Catalog.open()` reads the configuration in `PROVCAT_CONFIG`. The tutorial's script `read.py` resolves a name, pins it and reads it:

```python
"""Read a file by its logical URI from Python, and pin it."""
import sys

from provcat import Catalog

cat = Catalog.open()  # the file in PROVCAT_CONFIG
uri = sys.argv[1]
print("physical:", cat.resolve(uri).physical)
print("pinned:  ", cat.pin(uri))
print("content: ", cat.read(uri).decode().strip())
```

```
$ uv run python tours/provcat-tutorial/read.py data://sales/orders/day1.csv
physical: local-s3://sales/main/orders/day1.csv
pinned:   local-s3://sales/main/orders/day1.csv?at=version:078b0319-8cf9-4186-ba9b-1cbd504109f5
content:  order,region,amount
1,east,30
2,west,15
```

The pin is a new version ID, because Step 5 put the file again after the first pin.

## What you built

You gave two stores two names, and read and wrote files only by those names:

- **A local file** by the name `notes://sales/targets.csv`, with no server.
- **A versioned file** in a bucket by the name `data://sales/orders/day1.csv`. You committed its folder, pinned it, and read the old version after a change.
- **A branch** `dev`, which changed while `main` stayed as it was.
- **The cache**, which read a pinned version with no store.

The tutorial leaves out several features. provcat also reads and writes lakeFS, GCS and Azure, and reads GitHub and Hugging Face. One name can go to another store than the rest of its scheme. Hooks can record or refuse each request of an app, and a snapshot gives a program a read-only copy of a commit.

## Clean up

Stop moto with Ctrl-C in its terminal. Then delete the folder:

```sh
chmod -R u+w $TOUR_DIR
rm -rf $TOUR_DIR
unset AWS_ACCESS_KEY_ID AWS_SECRET_ACCESS_KEY AWS_DEFAULT_REGION AWS_CONFIG_FILE PROVCAT_CONFIG PROVCAT_CACHE TOUR_DIR TOUR_S3_PORT
```

## Where to go next

| To do this | Read |
|---|---|
| Run an agent on a named file, and check what it did | [The tutorial of the three packages](tutorial.md) |
| See every feature of provcat on one machine | [The provcat tour](tour.md) |
| Look up the `provc` commands, the URI forms and the configuration | [The provcat reference](reference.md) |
| Learn how the parts of provcat fit together | [Design](design.md) |
