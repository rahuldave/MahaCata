---
title: A runnable tour of provcat
---

This tour runs provcat on your own machine. It needs no account, no key and no network, except in the last step, which is optional. It uses moto, a local server that speaks the S3 protocol, in place of a cloud bucket. [What provcat does](provcat-features.md) shows the features first. Read it before the tour if provcat is new to you.

A script, `tours/provcat/run.sh`, runs every step and prints each command before its output. The output below is from that script. The commit IDs and version IDs differ on each run.

## Terms

| Term | Meaning |
|---|---|
| store | A named place that holds data, such as a bucket server or a folder. |
| logical URI | A name that code uses, such as `files://sales/orders/day1.csv`. A scheme of the configuration turns it into a URI of a store. |
| physical URI | An address in one store, such as `tour-s3://tour-sales/main/orders/day1.csv`. |
| selector | The `?at=` part of a URI, which picks a version, such as `?at=branch:dev`. |
| commit | A record of the version of every file in a folder, with an ID. |
| pin | The same URI with a fixed version, such as a commit, in place of a branch. |
| cache | The local folder where provcat keeps the files it fetched. |

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

Run the script. It takes about 15 seconds.

```
bash tours/provcat/run.sh
```

Add `--online` to run Step 11 too, which reads GitHub and Hugging Face. The script starts moto, runs the steps, stops moto and deletes its scratch folder.

## Or run it step by step

1. Start the tour in your shell.

   ```
   source tours/provcat/start.sh
   ```

   The script does these things:

   - It makes the scratch folder `/tmp/provcat-tour`, or the folder in `TOUR_DIR`. It stops with an error if that folder is already there. When an earlier tour made the folder, the error says to run `source tours/provcat/stop.sh` first.
   - It points `PROVCAT_CONFIG` and `PROVCAT_CACHE` at that folder, so the tour does not touch your own cache.
   - It sets fake AWS keys for moto, and unsets `AWS_PROFILE`, `AWS_SESSION_TOKEN` and the provcat variables `PROVCAT_HOOKS`, `PROVCAT_APP` and `PROVCAT_TENANT`.
   - It starts moto on port 5055, or the port in `TOUR_S3_PORT`. It refuses a port that is in use.
   - It makes the bucket `tour-sales`, with versioning on, and two local files.
   - It refuses to start a second tour in a shell that runs one, and to run outside the top folder of the repository. If it fails after its checks, it undoes what it did.

2. Copy each command of the steps below into the same shell, in order. Copy only the text after `$ `.

3. Stop the tour when you finish, in the same shell.

   ```
   source tours/provcat/stop.sh
   ```

   It stops moto, deletes the scratch folder, and gives each variable that `start.sh` changed its old value back. In a new shell, it stops the tour in `/tmp/provcat-tour`, or in the folder that `TOUR_DIR` names.

## The tour's configuration

The configuration, `tours/provcat/provcat.yaml`, defines two stores, three logical schemes and one entry. A logical scheme turns a name into a URI of a store. Step 6 shows the entry.

| Name | Kind | What it is |
|---|---|---|
| `tour-files` | store | Local files under `$TOUR_DIR/files`. |
| `tour-s3` | store | moto, on `localhost`, at port 5055 or the port in `TOUR_S3_PORT`. |
| `notes://project/path` | scheme | A local file of `tour-files`. |
| `files://project/path` | scheme | The bucket `tour-PROJECT` on `tour-s3`, on the branch `main` unless a selector names another branch. |
| `code://owner/repo/branch/path` | scheme | A file on GitHub. Only Step 11 uses it. |

The `files` scheme looks like this:

```yaml
schemes:
  files:
    parts: [project]
    rest: path
    target: "tour-s3://tour-{project}/{ref}/{path}"
    ref: main
```

## Step 1: Read a local file by its name

The scheme `notes` sends a name to a local file under the tour's folder. `resolve` shows where the name goes, and `cat` prints the file.

```
$ uv run provc resolve notes://sales/targets.csv
{
  "logical": "notes://sales/targets.csv",
  "physical": "tour-files:///sales/targets.csv",
  "via": [
    "notes template -> tour-files:///sales/targets.csv"
  ]
}

$ uv run provc cat notes://sales/targets.csv
region,target
east,120
west,95
```

Code that reads `notes://sales/targets.csv` does not know the folder. A change to the configuration can move the file without a change to the code.

## Step 2: Write to a bucket

The scheme `files` sends a name to a bucket on moto. Each project has its own bucket, and the first folder of a key is the branch, `main` by default. Write a file of orders, then read it back by its name.

```
$ printf 'order,region,amount\n1,east,30\n2,west,12\n' > $TOUR_DIR/day1.csv

$ uv run provc put files://sales/orders/day1.csv $TOUR_DIR/day1.csv
[
  {
    "uri": "tour-s3://tour-sales/main/orders/day1.csv",
    "kind": "file",
    "size": 40,
    "version": "ee5762b26311c199582a32b684f862a4"
  }
]

$ uv run provc resolve files://sales/orders/day1.csv
{
  "logical": "files://sales/orders/day1.csv",
  "physical": "tour-s3://tour-sales/main/orders/day1.csv",
  "via": [
    "files template -> tour-s3://tour-sales/main/orders/day1.csv"
  ]
}

$ uv run provc cat files://sales/orders/day1.csv
order,region,amount
1,east,30
2,west,12
```

`put` prints the entry of the file it wrote. Its `version` field is the ETag of the object, a hash of its bytes, so the same bytes give the same ETag. Step 4 shows the version ID that a pin uses, which is a different value.

## Step 3: Commit a folder, and read the commit after a change

A commit records the version of every file in a folder. Commit the orders, then change a line of the file.

```
$ FIRST=$(uv run provc commit files://sales/orders -m "first day" --meta source=till)

$ echo "$FIRST"
tour-s3://tour-sales/main/orders?at=commit:0537ea725a5756b72794cdfdcb4a8f2ae9ab7932a133b8a2e3351f4e8c3973ac

$ printf 'order,region,amount\n1,east,30\n2,west,15\n' > $TOUR_DIR/day1.csv

$ uv run provc put files://sales/orders/day1.csv $TOUR_DIR/day1.csv
[
  {
    "uri": "tour-s3://tour-sales/main/orders/day1.csv",
    "kind": "file",
    "size": 40,
    "version": "67fd407bbf80fc532b35d6f2b914e3f1"
  }
]

$ uv run provc cat files://sales/orders/day1.csv
order,region,amount
1,east,30
2,west,15

$ ID=${FIRST#*commit:}

$ uv run provc cat "tour-s3://tour-sales/main/orders/day1.csv?at=commit:$ID"
order,region,amount
1,east,30
2,west,12
```

The name reads the new file. The commit still reads the file as it was. `$FIRST` holds the URI that the commit printed, and `$ID` holds its ID.

## Step 4: Pin a name to a fixed version

`pin` turns a name into a URI that always reads the same bytes. The name itself can read a fixed version too, with `?at=`.

```
$ uv run provc pin files://sales/orders/day1.csv
tour-s3://tour-sales/main/orders/day1.csv?at=version:09adc054-a05a-4659-b76a-b5e8306feeec

$ uv run provc pin files://sales/orders
tour-s3://tour-sales/main/orders?at=commit:0537ea725a5756b72794cdfdcb4a8f2ae9ab7932a133b8a2e3351f4e8c3973ac

$ VERSION=$(uv run provc pin files://sales/orders/day1.csv); VERSION=${VERSION#*version:}

$ uv run provc cat "files://sales/orders/day1.csv?at=version:$VERSION"
order,region,amount
1,east,30
2,west,15

$ uv run provc cat "files://sales/orders/day1.csv?at=commit:$ID"
order,region,amount
1,east,30
2,west,12

$ uv run provc cat 'files://sales/orders/day1.csv?at=tag:v1' || true
provc: files://sales/orders/day1.csv?at=tag:v1: files goes to a bucket, which has no tags; use commit: or version:
```

The file pins to its version ID in the bucket, which names this version of the object and no other. The folder pins to its newest commit, the one from Step 3. That commit does not hold the change of Step 3, so run `provc commit` first to pin what the name reads now.

`$VERSION` holds the version ID that `pin` printed, and `$ID` holds the commit of Step 3. Through the name, the version reads the file as it is now, and the commit reads it as it was. provcat passes `?at=` on to the bucket, on the scheme's default branch. A bucket has no tags, so provcat refuses `tag:`.

## Step 5: Make a branch

In a bucket, each top folder is a branch, such as `main`. `branch` copies `main` to a new branch, `dev`. List the orders on `dev`.

```
$ uv run provc branch 'files://sales?at=branch:dev' --from main
tour-s3://tour-sales/dev

$ uv run provc ls 'files://sales/orders?at=branch:dev'
[
  {
    "uri": "tour-s3://tour-sales/dev/orders/day1.csv",
    "kind": "file",
    "size": 40,
    "version": "67fd407bbf80fc532b35d6f2b914e3f1"
  }
]
```

A selector, `?at=branch:dev`, picks the branch on any command. Work on `dev` does not change `main`.

## Step 6: Send one name somewhere else

An entry is a rule of the configuration for one name, or for every name under a prefix. The tour's configuration has one entry. It sends `files://sales/prices` to a local file, while the rest of `files://sales` stays in the bucket.

```
$ uv run provc resolve files://sales/prices
{
  "logical": "files://sales/prices",
  "physical": "tour-files:///shared/prices.csv",
  "via": [
    "entry files://sales/prices -> tour-files:///shared/prices.csv"
  ]
}

$ uv run provc cat files://sales/prices
item,price
pen,2
book,12
```

The `via` list names the entry. An entry moves one name, or every name under a prefix, without a change to the code that reads it.

## Step 7: Fetch into the cache, and read offline

`fetch` puts a folder in the local cache and prints its path. With `--offline`, provcat reads only the cache.

```
$ uv run provc fetch files://sales/orders
/tmp/provcat-tour/cache/tour-s3/tour-sales/main/orders

$ find $TOUR_DIR/cache -type f -not -path '*/.provcat/*' | sort
/tmp/provcat-tour/cache/tour-s3/tour-sales/main/orders/day1.csv
/tmp/provcat-tour/cache/tour-s3@commit-0537ea725a5756b72794cdfdcb4a8f2ae9ab7932a133b8a2e3351f4e8c3973ac/tour-sales/main/orders/day1.csv
/tmp/provcat-tour/cache/tour-s3@version-09adc054-a05a-4659-b76a-b5e8306feeec/tour-sales/main/orders/day1.csv

$ uv run provc --offline cat files://sales/orders/day1.csv
order,region,amount
1,east,30
2,west,15

$ uv run provc --offline cat "tour-s3://tour-sales/main/orders/day1.csv?at=commit:$ID"
order,region,amount
1,east,30
2,west,12
```

The cache keeps the live folder, the commit and the version apart, so the cached file of a fixed version never changes. The read of the commit in Step 3 put the commit there, and Step 4 put the version. A read through the name and a read through the physical URI share one copy. Offline, the name reads the copy it fetched last, and the commit reads its own copy.

## Step 8: Give a program a snapshot

A snapshot is a read-only copy of one pinned version, in a folder named by the SHA-256 of its content. `tours/provcat/snapshot.py` makes one of the commit from Step 3, and lists its files.

```
$ uv run python tours/provcat/snapshot.py "$FIRST"
pinned: tour-s3://tour-sales/main/orders?at=commit:0537ea725a5756b72794cdfdcb4a8f2ae9ab7932a133b8a2e3351f4e8c3973ac
digest: feb1e4a1980a1b554363cd5e3f80fecc1c43050d0d8ffe90373e495070880597
day1.csv 0o444
```

The mode `0o444` means that nobody can write the file. Give a program a snapshot, not a path in the cache. A later fetch can change a cached file.

## Step 9: Read through fsspec

fsspec is the library that pandas and many other libraries use to open files. `tours/provcat/read_fsspec.py` opens a logical URI with it.

```
$ uv run python tours/provcat/read_fsspec.py
['files://sales/orders/day1.csv']
order,region,amount
```

`cat.fs()` gives fsspec the logical schemes of the catalog, so any library built on fsspec reads the names.

## Step 10: Watch and limit an app with hooks

A hook is a function that runs at each step of a request, such as the lookup of the name and the fetch. A recorder is a hook that writes the request down, and a gate is a hook that can refuse it. `tours/provcat/hooks.yaml` gives the app `tour` two hooks. `provcat.log` writes one line for each step, and `provcat.logical_only` refuses a physical URI.

```
$ export PROVCAT_HOOKS=tours/provcat/hooks.yaml PROVCAT_APP=tour

$ uv run provc cat files://sales/prices
{"app": "tour", "spot": "session", "logical": null, "physical": null, "event": "open"}
{"app": "tour", "spot": "resolve", "logical": "files://sales/prices", "physical": null, "answer": "tour-files:///shared/prices.csv"}
{"app": "tour", "spot": "fetch", "logical": "files://sales/prices", "physical": "tour-files:///shared/prices.csv", "answer": "/private/tmp/provcat-tour/files/shared/prices.csv"}
item,price
pen,2
book,12
{"app": "tour", "spot": "session", "logical": null, "physical": null, "event": "close"}

$ uv run provc cat tour-files:///shared/prices.csv || true
{"app": "tour", "spot": "session", "logical": null, "physical": null, "event": "open"}
{"app": "tour", "spot": "fetch", "logical": null, "physical": "tour-files:///shared/prices.csv", "error": "AccessDenied: tour-files:///shared/prices.csv: app tour reads and writes only logical URIs"}
provc: tour-files:///shared/prices.csv: app tour reads and writes only logical URIs
{"app": "tour", "spot": "session", "logical": null, "physical": null, "event": "close"}

$ unset PROVCAT_HOOKS PROVCAT_APP
```

The first read went through the lookup and the fetch, and each line names the logical URI. The second read had no logical URI, so the gate refused it.

## Step 11: Read GitHub and Hugging Face (needs the network)

This step runs only with `--online`. The scheme `code` sends a name to GitHub, and its third part is the branch. A Hugging Face URI can take a time as its selector.

```
$ uv run provc pin 'code://octocat/Hello-World/master/README'
github://octocat/Hello-World/README?at=commit:7fd1a60b01f91b314f59955a4e4d4e80d8edf11d

$ uv run provc cat 'code://octocat/Hello-World/master/README'
Hello World!

$ uv run provc pin 'hf://models/hf-internal-testing/tiny-random-gpt2?at=ts:2023-01-01'
hf://models/hf-internal-testing/tiny-random-gpt2?at=commit:937b4d23b6648f5a1a0d1247b939b26981798903

$ uv run provc stat 'hf://models/hf-internal-testing/tiny-random-gpt2/config.json?at=ts:2023-01-01'
{
  "uri": "hf://models/hf-internal-testing/tiny-random-gpt2/config.json?at=commit:937b4d23b6648f5a1a0d1247b939b26981798903",
  "kind": "file",
  "size": 807,
  "version": "937b4d23b6648f5a1a0d1247b939b26981798903"
}
```

A branch and a time both pin to a commit. A program that reads the pinned URI next year reads the same file.

## Clean up

If you ran the steps by hand, stop moto and delete the scratch folder:

```
source tours/provcat/stop.sh
```

`run.sh` does this itself.

## Where to go next

- [What provcat does](provcat-features.md) shows lakeFS commits, branches and merges, tenants, stages and etcd, with examples.
- [The reference](reference.md) lists the commands and options.
- [The design](design.md) explains how the parts work, and lists the known limits.
- [The reference lakeFS target](lakefs-target.md) is a lakeFS that keeps orgs apart.
- [Setup](setup.md) shows how to run the tests, and how to set up each service.
