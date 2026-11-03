---
title: What provcat does
---

provcat is a Python library with a command, `provc`. Code names each dataset, model or file by a name that says what it is. provcat finds the data behind the name, pins its version, reads it and writes it.

Each section below shows one feature with examples and their real output. [The tour](tour.md) runs the same commands from a script, on your own machine. [The reference](reference.md) lists every command and option, and [the design](design.md) explains how the parts work.

## Terms

| Term | Meaning |
|---|---|
| logical URI | A name that code uses, such as `model://acme/ml/prod/churn/xgb`. It does not say where the data is. |
| physical URI | An address in one store, such as `s3://acme-sales/main/orders.csv`. |
| store | A named place that holds data, such as a bucket server, a lakeFS server or GitHub. Its name is the scheme of a physical URI. |
| selector | The `?at=` part of a URI. It picks a version, such as `?at=branch:dev` or `?at=commit:7fd1a60b`. |
| pin | The same URI with a fixed version in place of a name that moves, such as a branch. |
| configuration | The YAML file, or the prefix in etcd, a key-value server, that says which logical name goes to which store. |
| bucket scheme | A logical scheme whose names go to a bucket, in which each top folder is a branch. |
| provcat commit | A record that provcat writes in a bucket, of the version of every file in a folder. |

The examples that use `files://`, `tour-s3://` or `tour-files://` ran in the shell of [the tour](tour.md), after `source tours/provcat/start.sh`, in the order of its steps. The examples that use `$C` read the example configuration. Set these two variables first, so the examples use a scratch cache and not your own:

```
export C=packages/provcat/config/provcat.example.yaml
export PROVCAT_CACHE=/tmp/provcat-demo
```

## Name data by what it is

A logical URI names a thing by its parts, such as the org, the environment and the project. The configuration turns the parts into a physical URI. `provc resolve` shows the answer and each step it took. The examples use the configuration in `packages/provcat/config/provcat.example.yaml`.

A model version goes to a lakeFS repository of the org's ML project:

```
$ uv run provc --config $C resolve 'model://acme/ml/prod/churn/xgb?at=version:3'
{
  "logical": "model://acme/ml/prod/churn/xgb?at=version:3",
  "physical": "lakefs-acme://acme-ml-churn/prod/ml/models/xgb/v3",
  "via": [
    "model template -> lakefs-acme://acme-ml-churn/prod/ml/models/xgb/v3"
  ]
}
```

A folder of files goes to the org's bucket for the project, on the branch `dev`:

```
$ uv run provc --config $C resolve 'files://acme/sales/tables/agreements?at=branch:dev'
{
  "logical": "files://acme/sales/tables/agreements?at=branch:dev",
  "physical": "s3-acme://acme-sales/dev/tables/agreements",
  "via": [
    "files template -> s3-acme://acme-sales/dev/tables/agreements"
  ]
}
```

A stage is a name for one fixed version, such as `production`. The configuration holds it, so a team moves `production` by changing one line:

```
$ uv run provc --config $C resolve 'data+table://acme/shared/prod/agreements/qa_outputs?at=stage:production'
{
  "logical": "data+table://acme/shared/prod/agreements/qa_outputs?at=stage:production",
  "physical": "lakefs-acme://acme-data-shared/b8a2f9c123456/tables/agreements/qa_outputs",
  "via": [
    "stage:production -> commit:b8a2f9c123456",
    "data+table template -> lakefs-acme://acme-data-shared/b8a2f9c123456/tables/agreements/qa_outputs"
  ]
}
```

Each of these rules is a scheme in the configuration. The `model` scheme is a template with five parts:

```yaml
schemes:
  model:
    parts: [org, team, env, project, model_name]
    target: "lakefs-{org}://{org}-ml-{project}/{ref}/{team}/models/{model_name}/v{version}"
    ref: "{env}"
```

A name that does not fit is refused, and the error names the missing piece:

```
$ uv run provc --config $C resolve 'data+table://initech/shared/prod/a/b'
provc: data+table://initech/shared/prod/a/b resolves to lakefs-initech://initech-data-shared/prod/tables/a/b, but there is no store named 'lakefs-initech'; add one to the configuration
```

## Read from many kinds of storage

The same commands work on every store. A physical URI skips the configuration, so `provc` reads it as it is.

| Store | URI | Reads | Writes |
|---|---|---|---|
| Local files | `file:///abs/path` | yes | yes |
| S3, and servers that speak S3, such as MinIO | `s3://bucket/key` | yes | yes |
| lakeFS | a store that the configuration names, such as `lakefs-acme://repository/branch/path` | yes | yes |
| Google Cloud Storage | `gs://bucket/key` | yes | yes |
| Azure Blob Storage | `az://account/container/key` | yes | yes |
| GitHub | `github://owner/repo/path` | yes | no |
| Hugging Face | `hf://models/owner/repo/path` | yes | no |

A file on GitHub:

```
$ uv run provc ls 'github://octocat/Hello-World'
[
  {
    "uri": "github://octocat/Hello-World/README?at=commit:7fd1a60b01f91b314f59955a4e4d4e80d8edf11d",
    "kind": "file",
    "size": 13,
    "version": "7fd1a60b01f91b314f59955a4e4d4e80d8edf11d"
  }
]
```

A file of a model on Hugging Face:

```
$ uv run provc stat 'hf://models/hf-internal-testing/tiny-random-gpt2/config.json?at=ts:2023-01-01'
{
  "uri": "hf://models/hf-internal-testing/tiny-random-gpt2/config.json?at=commit:937b4d23b6648f5a1a0d1247b939b26981798903",
  "kind": "file",
  "size": 807,
  "version": "937b4d23b6648f5a1a0d1247b939b26981798903"
}
```

A file in a bucket, on moto, a local S3 server. The tour's configuration names the store `tour-s3`:

```
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

Google Cloud Storage, Azure and Hugging Face each need an extra of the package, `gcs`, `azure` or `hf`. A clone with `uv sync` has all of them. Each store takes its keys from the usual place for that service, such as an AWS profile, a `gcloud` login or `gh auth token`. The configuration never holds keys. [The reference](reference.md#stores) lists the options of each store.

## Pin a version

`provc pin` replaces a name that moves with one that does not. A program that reads the pinned URI later reads the same bytes.

A GitHub branch becomes a commit:

```
$ uv run provc --config $C pin 'code://octocat/Hello-World/master/README'
github://octocat/Hello-World/README?at=commit:7fd1a60b01f91b314f59955a4e4d4e80d8edf11d
```

A time becomes the commit that the branch had then:

```
$ uv run provc pin 'hf://models/hf-internal-testing/tiny-random-gpt2?at=ts:2023-01-01'
hf://models/hf-internal-testing/tiny-random-gpt2?at=commit:937b4d23b6648f5a1a0d1247b939b26981798903
```

A file in a bucket with versioning on becomes its version ID, which S3 gives each version of an object. A folder becomes its newest provcat commit. That commit does not hold changes made after it, so run `provc commit` first to pin what the folder holds now:

```
$ uv run provc pin files://sales/orders/day1.csv
tour-s3://tour-sales/main/orders/day1.csv?at=version:09adc054-a05a-4659-b76a-b5e8306feeec

$ uv run provc pin files://sales/orders
tour-s3://tour-sales/main/orders?at=commit:0537ea725a5756b72794cdfdcb4a8f2ae9ab7932a133b8a2e3351f4e8c3973ac
```

A lakeFS branch becomes a commit, which lakeFS keeps in the path:

```
$ uv run provc --config $C pin data+table://acme/sales/main/orders/2026-11
lakefs-acme://acme-data-sales/66e31b0b4f4106b90daacf5a46a349884414697fc9ef3f190b05537b1885af49/tables/orders/2026-11
```

| Selector | Picks |
|---|---|
| `branch:X`, `tag:X`, `commit:X` | That git or lakeFS ref. On a bucket, `commit:ID` is a provcat commit. |
| `version:N` | On a logical URI, version N of a model, through the template. On a bucket, the version ID of a file. |
| `stage:NAME` | The version that the stage names in the configuration. |
| `ts:TIME` | The commit that the branch had at TIME, on lakeFS, GitHub and Hugging Face. |

On a bucket scheme, such as `files`, a logical URI takes `branch:`, `commit:` and `version:`. A branch goes in the path. A commit or a version goes on to the physical URI as `?at=`, on the default branch of the scheme. provcat refuses `tag:`, because a bucket has no tags. It passes `ts:` on to the bucket, which refuses it. A commit made on another branch reads only through its physical URI, which `provc commit` prints.

In [the tour](tour.md), the last line of `day1.csv` goes from `2,west,12` to `2,west,15` after the folder's commit. `$ID` holds the commit, and `$VERSION` takes the version ID that `pin` prints. The name then reads the version, which is the file as it is now, and the commit, which is the file as it was. It refuses a tag:

```
$ ID=0537ea725a5756b72794cdfdcb4a8f2ae9ab7932a133b8a2e3351f4e8c3973ac

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

## Write, commit, branch and merge

provcat writes to every store but GitHub and Hugging Face. On lakeFS it commits, branches and merges. This example puts a month of orders on `main`, corrects it on a branch, and merges the correction.

The example uses the store `lakefs-acme` of the example configuration. That store needs a lakeFS at the address in `LAKEFS_S3_ENDPOINT`, the keys of the AWS profile `lakefs-acme`, and a repository `acme-data-sales`. [The reference lakeFS target](lakefs-target.md) sets these up. A lakeFS commit and a lakeFS merge take every change on the branch, including files that the URI does not name.

```
$ printf 'id,total\n1,30\n2,12\n' > orders.csv

$ uv run provc --config $C put data+table://acme/sales/main/orders/2026-11 orders.csv
[
  {
    "uri": "lakefs-acme://acme-data-sales/main/tables/orders/2026-11",
    "kind": "file",
    "size": 19,
    "version": "b4727814e5f1c81f79dbef4805ec1564"
  }
]

$ uv run provc --config $C commit data+table://acme/sales/main/orders/2026-11 -m 'November orders' --meta source=till
lakefs-acme://acme-data-sales/44477cd9c1b20092359c3331dba3e67914e2f1e79a386b9189f3a41b9c67e8a0/tables/orders/2026-11

$ uv run provc --config $C branch lakefs-acme://acme-data-sales/fix --from main
lakefs-acme://acme-data-sales/fix

$ printf 'id,total\n1,30\n2,15\n3,8\n' > orders2.csv

$ uv run provc --config $C put 'data+table://acme/sales/main/orders/2026-11?at=branch:fix' orders2.csv
[
  {
    "uri": "lakefs-acme://acme-data-sales/fix/tables/orders/2026-11",
    "kind": "file",
    "size": 23,
    "version": "94bb9ed7e6f3ac23be2a0a561628e9ef"
  }
]

$ uv run provc --config $C commit 'data+table://acme/sales/main/orders/2026-11?at=branch:fix' -m 'Add order 3'
lakefs-acme://acme-data-sales/9daf64c1bf41f3920e1d8cea0f756410e1009487e39107ebb836e3ad26cdff91/tables/orders/2026-11

$ uv run provc --config $C merge data+table://acme/sales/main/orders/2026-11 --from fix -m 'Take the fix'
lakefs-acme://acme-data-sales/66e31b0b4f4106b90daacf5a46a349884414697fc9ef3f190b05537b1885af49/tables/orders/2026-11
```

The branch `main` now has the correction, and the first commit still has the first file:

```
$ uv run provc --config $C cat data+table://acme/sales/main/orders/2026-11
id,total
1,30
2,15
3,8

$ uv run provc --config $C cat 'data+table://acme/sales/main/orders/2026-11?at=commit:44477cd9c1b20092359c3331dba3e67914e2f1e79a386b9189f3a41b9c67e8a0'
id,total
1,30
2,12
```

A bucket has no commits of its own. In a bucket with versioning on, provcat commits a folder and copies a branch. Steps 3 and 5 of [the tour](tour.md) do this on moto. [Transactions](transactions.md) says what a commit guarantees in each kind of store.

## Keep a local cache, and work offline

`provc fetch` puts a file or a folder in a local cache and prints its path. The path of a GitHub or Hugging Face file holds its commit:

```
$ uv run provc --config $C fetch 'code://octocat/Hello-World/master/README'
/tmp/provcat-demo/github/octocat/Hello-World/7fd1a60b01f91b314f59955a4e4d4e80d8edf11d/README
```

With `--offline`, provcat reads only the cache. A branch that it pinned before gives the same commit. A branch that it never pinned fails:

```
$ uv run provc --offline --config $C cat 'code://octocat/Hello-World/master/README'
Hello World!

$ uv run provc --offline --config $C cat 'code://octocat/Hello-World/test/README'
provc: github://octocat/Hello-World/README?at=branch:test: offline, and branch:test was never pinned
```

## Give a program a read-only snapshot

`Catalog.snapshot` gives a read-only copy of one pinned version of a file or a folder. A later fetch never changes it, so give a program or a container a snapshot to read. The tour's script `tours/provcat/snapshot.py` makes one of the commit from Step 3 of the tour, which `$FIRST` holds:

```
$ uv run python tours/provcat/snapshot.py "$FIRST"
pinned: tour-s3://tour-sales/main/orders?at=commit:0537ea725a5756b72794cdfdcb4a8f2ae9ab7932a133b8a2e3351f4e8c3973ac
digest: feb1e4a1980a1b554363cd5e3f80fecc1c43050d0d8ffe90373e495070880597
day1.csv 0o444
```

provrun, the package that runs agents on data that provcat names, gives each run its inputs as snapshots.

## Send one name somewhere else

A scheme sends every name of the scheme to one place. An entry sends one name, or every name under a prefix, to another place. Code that uses the name does not change when the entry moves the data. In the example configuration, the weights of a code repository live on Hugging Face:

```yaml
entries:
  "code://octocat/Hello-World/master/weights":
    target: "hf://models/hf-internal-testing/tiny-random-gpt2/{path}"
```

```
$ uv run provc --config $C resolve 'code://octocat/Hello-World/master/weights/config.json'
{
  "logical": "code://octocat/Hello-World/master/weights/config.json",
  "physical": "hf://models/hf-internal-testing/tiny-random-gpt2/config.json",
  "via": [
    "entry code://octocat/Hello-World/master/weights -> hf://models/hf-internal-testing/tiny-random-gpt2/config.json"
  ]
}
```

In the tour, one name of a bucket scheme goes to a local file:

```
$ uv run provc resolve files://sales/prices
{
  "logical": "files://sales/prices",
  "physical": "tour-files:///shared/prices.csv",
  "via": [
    "entry files://sales/prices -> tour-files:///shared/prices.csv"
  ]
}
```

## Keep tenants apart

A tenant is an org that shares the configuration with other orgs. Each tenant has its own stores and its own keys. A catalog opened for one tenant sees the shared schemes and that tenant's part, and refuses the names of another tenant:

```
$ uv run provc --config $C --tenant acme resolve 'data+table://acme/shared/prod/agreements/qa_outputs'
{
  "logical": "data+table://acme/shared/prod/agreements/qa_outputs",
  "physical": "lakefs-acme://acme-data-shared/prod/tables/agreements/qa_outputs",
  "via": [
    "data+table template -> lakefs-acme://acme-data-shared/prod/tables/agreements/qa_outputs"
  ]
}

$ uv run provc --config $C --tenant acme resolve 'data+table://globex/shared/prod/agreements/qa_outputs'
provc: data+table://globex/shared/prod/agreements/qa_outputs: org 'globex' is another tenant; this catalog is for acme
```

The keys keep the tenants apart, and the configuration only routes. "Tenants" in [the design](design.md#tenants) explains this. [The reference lakeFS target](lakefs-target.md) is one lakeFS set up so that each org reads and writes only its own repositories.

## Watch and limit what an app reads

A hook runs at each step of a request, such as the lookup, the pin and the fetch. A recorder writes each request down. A gate refuses a request. A hooks file names the hooks of each app. The tour's file gives the app `tour` the two hooks that come with provcat:

```yaml
apps:
  tour:
    "*": [provcat.log, provcat.logical_only]
```

`provcat.log` writes a line for each step. `provcat.logical_only` refuses a physical URI, so the app reads only through names. These commands ran in Step 10 of the tour, after this line set the app and its hooks file:

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
```

A program can pass its own hooks to each `Catalog`. provrun records every read of a run with a hook. "Hooks" in [the reference](reference.md#hooks) gives the rules.

## Use it from Python and fsspec

Each command has a method of `Catalog`. The methods are `resolve`, `pin`, `stat`, `list`, `fetch`, `read`, `snapshot`, `put`, `delete`, `commit`, `branch` and `merge`. `catalog.fs()` gives a file system for fsspec, the library that pandas and many others use to open files. The file system takes logical URIs. The tour's script `tours/provcat/read_fsspec.py` holds this code:

```python
import fsspec

from provcat import Catalog

cat = Catalog.open()
fs = cat.fs()  # also lets fsspec.open take the logical schemes
print(fs.ls("files://sales/orders", detail=False))
with fsspec.open("files://sales/orders/day1.csv", "rt") as f:
    print(f.readline().strip())
```

```
$ uv run python tours/provcat/read_fsspec.py
['files://sales/orders/day1.csv']
order,region,amount
```

An open file reads one pinned version from start to end. "Files and fsspec" in [the reference](reference.md#files-and-fsspec) gives the rules.

## Share one configuration through etcd

A team can keep one configuration in etcd, a key-value server, in place of a YAML file in each place. `provc config push` copies a file into etcd, and every command reads it back:

```
$ uv run provc --config $C config push --to etcd://localhost:2379/provcat
{
  "ok": true,
  "from": "packages/provcat/config/provcat.example.yaml",
  "to": "etcd://localhost:2379/provcat",
  "sections": [
    "admin",
    "cache",
    "entries",
    "schemes",
    "tenants"
  ]
}

$ uv run provc --config etcd://localhost:2379/provcat resolve 'model://acme/ml/prod/churn/xgb?at=version:3'
{
  "logical": "model://acme/ml/prod/churn/xgb?at=version:3",
  "physical": "lakefs-acme://acme-ml-churn/prod/ml/models/xgb/v3",
  "via": [
    "model template -> lakefs-acme://acme-ml-churn/prod/ml/models/xgb/v3"
  ]
}
```

A value such as `${oc.env:HOME}` stays as written in etcd, and each reader fills it in from its own environment. So give write access to the etcd prefix only to people you trust.

## Where the examples come from

| Examples | Where they ran |
|---|---|
| `files://`, `tour-s3://`, `tour-files://`, snapshots, hooks, fsspec | The tour's script, `bash tours/provcat/run.sh`, with moto. |
| `code://`, `github://`, `hf://` | The tour's script with `--online`, or the example configuration. They need the network. |
| `model://`, the stage, tenants, the missing store | The example configuration in `$C`. `resolve` reads no data, so they need no server. |
| `data+table://acme/sales/...` | A lakeFS 1.86.0 on the same machine as `provc`, with a store `lakefs-acme` and a repository `acme-data-sales`. [Setup](setup.md) shows how to run lakeFS. |
| etcd | An etcd on `localhost:2379`, as [Setup](setup.md) starts it. |

The commit IDs and version IDs differ on each run.
