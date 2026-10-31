---
title: provcat reference
---

This page lists the `provc` commands, the forms of a URI, the stores and their options, the configuration and the cache. [The tour](tour.md) shows them at work, and [the design](design.md) explains how the parts fit together.

## Install

provcat needs Python 3.13 and [uv](https://docs.astral.sh/uv/). It is not on PyPI, so install it from the repository on GitHub:

```
git clone https://github.com/rahuldave/MahaCata
cd MahaCata
uv sync
uv run provc --help
```

The base package reads local files, GitHub, S3 and lakeFS. Google Cloud Storage, Azure and Hugging Face each need an extra, which installs the Python SDK of that service:

In another project, install provcat from GitHub, with the extras that you need in the brackets:

```
pip install 'provcat[hf] @ git+https://github.com/rahuldave/MahaCata#subdirectory=packages/provcat'
```

In a clone, `uv sync` installs all three.

## Commands

Every command but `config` takes a URI, logical or physical. These options go before the command name:

| Option | Meaning |
|---|---|
| `--config SOURCE` | A YAML file or `etcd://host:port/prefix`. "Configuration" below gives the default. |
| `--cache FOLDER` | The cache folder. "The cache" below gives the default. |
| `--offline` | Read the stores only from the cache. provcat still reads the configuration from its source, which can be etcd. |
| `--tenant NAME` | See only the shared part of the configuration and this tenant's part. The default is `PROVCAT_TENANT`. |

| Command | Output |
|---|---|
| `provc resolve URI` | The physical URI and the steps taken, as JSON. |
| `provc pin URI` | The URI with a branch or tag replaced by its commit. In a versioned bucket, a file gets its version and a folder its newest commit. |
| `provc stat URI` | One entry with its kind, size and version, as JSON. |
| `provc ls URI` | The entries in a folder, as JSON. |
| `provc fetch URI` | The local path of the file or folder. |
| `provc cat URI` | The bytes of a file. |
| `provc put URI PATH` | Writes a local file or folder at the URI, or standard input for `-`. |
| `provc rm URI` | Deletes one object. |
| `provc commit URI -m MSG [--meta K=V]` | Commits a lakeFS branch or a folder in a versioned bucket, and prints the URI pinned to the new commit. |
| `provc branch URI --from REF` | Makes a lakeFS branch from a branch, a tag or a commit. In a bucket, copies a branch or a commit to a new branch. See [Commits in a bucket](design.md#commits-in-a-bucket). |
| `provc merge URI --from REF [-m MSG] [--meta K=V] [--strategy S]` | Merges a branch, a tag or a commit into a lakeFS branch, and prints the URI pinned to the merge commit. `--strategy` is `source-wins` or `dest-wins`. |
| `provc config show` | The configuration and its source. |
| `provc config push --to etcd://...` | Copies the configuration into etcd. |

## Catalog URIs

A URI has the form `scheme://first/part/part?at=kind:value`. The part after `?at=` is the selector. It picks a version.

A **logical URI** has a scheme that the configuration defines, for example `data+table` or `code`. A **physical URI** has a scheme that names a store, for example `s3`, `github` or `file`. provcat resolves a logical URI to a physical URI in one step.

```
data+table://acme/shared/prod/agreements/qa_outputs?at=stage:production
  -> lakefs-acme://acme-data-shared/b8a2f9c123456/tables/agreements/qa_outputs

code://octocat/Hello-World/master/README
  -> github://octocat/Hello-World/README?at=branch:master
```

In the example configuration, the org is the tenant and the first part of every logical URI, as the owner is on GitHub. Each org gets its own lakeFS repositories, and each environment is a branch. Read the section "Tenants" in [the design](design.md).

| Selector | Meaning |
|---|---|
| `branch:X`, `tag:X`, `commit:X` | Use the git or lakeFS ref X. |
| `version:N` | Fill `{version}` in the template, for a model version. |
| `stage:NAME` | Look up NAME in the `stages` section and use the selector stored there. |
| `ts:TIME` | Use the newest commit of the branch made at or before TIME. TIME is a date, a date and time, or Unix seconds. The `lakefs`, `github` and `hf` drivers resolve it. A GitHub or Hugging Face URI names a branch other than the default as `ts:BRANCH@TIME`. |

## Stores

A store is a named instance of a driver. Every driver reads. All but `github` and `hf` also write, as [the design](design.md#writes) explains. The stores `file`, `s3`, `github`, `gs`, `az` and `hf` exist without configuration. So a logical scheme cannot use one of those six names. The configuration can add more, for example a store `lakefs` that uses the `lakefs` driver with the lakeFS endpoint.

| Driver | URI | What it does |
|---|---|---|
| `file` | `file:///abs/path` | Reads and writes local files, with no cache. `fetch` returns the path itself. Option `root` confines the store to one folder. |
| `s3` | `s3://bucket/key` | Reads and writes AWS S3 or any S3-compatible server, for example MinIO. It branches in any bucket. In a bucket with versioning on, it also pins and commits. Options are `endpoint_url`, `region`, `profile` and `addressing_style`. |
| `lakefs` | `lakefs://repo/ref/path?at=ts:TIME` | Reads and writes lakeFS through its S3 gateway. It pins, commits, branches and merges through its REST API. `pin` turns a branch or a time into a commit. `ls` of an empty branch gives `[]`, and `ls lakefs://repo` lists the branches. Options are those of `s3`, plus `api_url`. |
| `github` | `github://owner/repo/path?at=branch:main` | Reads a file or a folder of a GitHub repository at a commit. `pin` turns a branch, a tag or a time into a commit. Options are `api_url`, `token_env`, `use_gh_cli` and `timeout`. |
| `hf` | `hf://datasets/owner/repo/path?at=branch:main` | Reads a file or a folder of a Hugging Face model, dataset or Space at a commit. The first part is `models`, `datasets` or `spaces`. `pin` turns a branch, a tag or a time into a commit. Options are `endpoint`, `token_env` and `timeout`. Needs the `hf` extra. |
| `gcs` | `gs://bucket/key` | Reads and writes Google Cloud Storage. It branches in any bucket. In a bucket with versioning on, it also pins and commits. Options are `project`, `endpoint_url` and `anonymous`. Needs the `gcs` extra. |
| `azure` | `az://account/container/key` | Reads and writes Azure Blob Storage and ADLS Gen2. It branches in any container. In a Blob Storage account with versioning on, it also pins and commits. ADLS Gen2 has no versions. Options are `account_url` and `connection_string_env`. Needs the `azure` extra. |

Each cloud driver uses that cloud's Python SDK and takes credentials from the usual places:

- **S3:** the environment, a profile or an instance role.
- **GCS:** Application Default Credentials.
- **Azure:** `DefaultAzureCredential`, for example `az login`, or a connection string in the environment variable that `connection_string_env` names.

Do not put keys in the provcat configuration. The GitHub driver reads a token from `GITHUB_TOKEN` or `GH_TOKEN`. If neither is set, it runs `gh auth token`. The Hugging Face driver lets `huggingface_hub` find a token, in `HF_TOKEN` or in the file that `hf auth login` writes.

## Configuration

provcat reads its configuration from the first of these sources that exists:

1. The `--config` option of `provc`.
2. The environment variable `PROVCAT_CONFIG`.
3. The file `provcat.yaml` in the current folder.
4. The built-in defaults, which have the six stores and no logical schemes.

A source that starts with `etcd://host:port/prefix` is etcd. Any other source is a YAML file. [packages/provcat/config/provcat.example.yaml](https://github.com/rahuldave/MahaCata/blob/main/packages/provcat/config/provcat.example.yaml) has every section, the logical schemes from the old provcat docs and one entry. A value can read the environment with `${oc.env:NAME,default}`.

To copy a YAML configuration into etcd, run the command below. It copies the `${...}` references as written, so each reader fills them in from its own environment. It also deletes the sections in etcd that the file does not have.

```
uv run provc --config packages/provcat/config/provcat.example.yaml config push --to etcd://localhost:2379/provcat
```

A configuration can hold a part for each tenant, in its `tenants` section, and a part for the admin, in its `admin` section. `provc --tenant acme`, or `PROVCAT_TENANT=acme`, sees the shared sections and acme's part only. Without a tenant, `provc` is the admin, and sees every part. Read "A configuration for each tenant" in [the design](design.md).

## Entries

A scheme template sends every name of a scheme to one place. An entry sends one name, or every name under a prefix, somewhere else. The `entries` section of the configuration holds them:

```yaml
entries:
  "code://acme/tokenizer/prod":
    target: "github://acme/tokenizer/{path}?at=branch:prod"
  "code://acme/tokenizer/prod/weights":
    target: "hf://models/acme/tokenizer/{path}"
  "data+table://acme/shared/prod/corpora/wiki":
    target: "lakefs-acme://acme-corpora/{ref}/wiki"
    ref: main
```

With these entries, `code://acme/tokenizer/prod/src/a.py` reads GitHub, and `code://acme/tokenizer/prod/weights/model.bin` reads Hugging Face. The code that uses the names does not change when an entry moves them.

| Rule | What it means |
|---|---|
| The longest key wins. | A key matches whole parts of the URI, so `code://acme/tok` does not match `code://acme/tokenizer`. A URI that no key matches goes to the template of its scheme. |
| `{path}` is the rest of the URI. | It holds the parts after the key, each one encoded, so a name cannot add a `?at=` or climb with `..`. A target without `{path}` serves only the URI that is its key. |
| A selector on the URI wins. | `?at=branch:`, `tag:` or `commit:` takes the place of the `?at=` of the target. So a run can fix the version of any name. |
| `{ref}` is for a branch in the path. | lakeFS keeps the branch in the path. A target with `{ref}` needs a `ref`, its default branch, and the selector of the URI fills `{ref}`. Such a target has no `?at=`. |
| `ts:` keeps the branch. | `?at=ts:TIME` asks for the commit that the branch of the target had at TIME. `version:` is refused. |
| A fixed target stays fixed. | A target whose `?at=` names a tag or a commit takes only `commit:` from the URI. It refuses `branch:`, `tag:` and `ts:`, which would move it. |
| A stage comes first. | `?at=stage:NAME` looks up the stage of the URI, and its selector then applies as above. |
| A key fits its scheme. | A key is a URI of a logical scheme, with no `?at=`. Its parts must match the `parts` and `match` of the scheme, as far as the key goes. A URI under the key must still fit the scheme. |
| The place is fixed. | The store name of a target, and the bucket or repository after `//`, are plain text. The store must exist. A `ref` is one part of a path, so it has no `/`. |

`provc resolve` shows the entry that a URI went through, in its `via` list. A tenant's part of the configuration can hold entries too. Each key must be a URI of that tenant, of a scheme that names its tenant part with `tenant:`. Each target must go to a store whose name ends with `-` and the tenant, as in `s3-acme`. Its bucket or repository must be named for the tenant, as `acme` or `acme-...`, as the templates name them. A tenant's entry cannot hold `${...}` values, because provcat checks it before it fills them in.

## Hooks

A hook is a function that runs at a lifecycle spot of the catalog. A recorder writes down each request. A gate refuses a request. The proxy configuration names the hooks of each app. It is a local YAML file, never in etcd:

```yaml
# ~/.config/provcat/hooks.yaml, or the file in PROVCAT_HOOKS
apps:
  default: {}
  notebook:
    cache_root: ~/.cache/provcat-notebook
    "*": [provcat.log]
    fetch: [provcat.logical_only]
```

The spots are `session`, `resolve`, `pin`, `stat`, `list`, `fetch`, `snapshot`, `put`, `delete`, `commit`, `branch` and `merge`.

| Rule | What it means |
|---|---|
| The app comes from `PROVCAT_APP`. | A process reads it and the hooks file once, when it opens its first catalog. `Catalog(app=...)` can name only that app. A later change to `PROVCAT_HOOKS` or to the file has no effect. |
| No app gets `default`. | With no `PROVCAT_APP`, the process gets the `default` section of the file. If the file has no `default` section, it gets no hooks. |
| No file means no hooks. | With no `PROVCAT_APP`, no `PROVCAT_HOOKS` and no file at `~/.config/provcat/hooks.yaml`, provcat runs no hooks. |
| A missing file or app is an error. | provcat refuses to start when `PROVCAT_APP` names an app that the file does not have. It also refuses when `PROVCAT_APP` or `PROVCAT_HOOKS` is set and the file is missing. So a missing file never turns a gate off quietly. |
| Names are entry points. | Each name is a Python entry point in the group `provcat.hooks`. If two packages register one name, provcat refuses to start. |
| `"*"` runs first. | The hooks under `"*"` run at every spot, before the hooks under the spot. A name runs once at most in a chain. |
| Hooks come before the cache. | A read from the cache passes the gates too. |
| A fetch from GitHub or Hugging Face pins first. | It runs the `pin` spot and asks the store for the commit, and then it runs `fetch`. So a gate that must stop every call to the store goes under `"*"` or `pin`. |
| `cache_root` moves the cache. | It takes the place of `cache.root` for that app. |

provcat comes with two hooks:

| Name | Kind | What it does |
|---|---|---|
| `provcat.log` | recorder | It writes one JSON line on stderr for each request: the spot, the logical and physical URIs, and the answer or the error. |
| `provcat.logical_only` | gate | It refuses a physical URI, so the app reads and writes only through names. The names come from the configuration that the app opens. |

A hook takes three arguments, `hook(ctx, req, next)`. `ctx` holds the app, the principal and the tenant. `req` holds the spot, the logical URI, the physical URI and the other arguments. `next(req)` runs the rest of the chain and returns the answer. A gate raises `provcat.AccessDenied`.

Hooks hold back code that uses the catalog. Code that calls a store directly, or writes its own configuration, gets past them. "Hooks" in [the design](design.md#hooks) explains the rest.

## The cache

The cache folder is `~/.cache/provcat`, or `$XDG_CACHE_HOME/provcat` if that variable is set. The `--cache` option changes it. So does the `cache_root` of the app in the hooks file, the `cache.root` setting or the environment variable `PROVCAT_CACHE`. The first of these four that is set wins, in that order. The example configuration sets `cache.root` to `PROVCAT_CACHE` or `~/.cache/provcat`, so it ignores `XDG_CACHE_HOME`. The files have readable paths.

```
<cache>/github/<owner>/<repo>/<commit>/<path>
<cache>/hf/<kind>/<owner>/<repo>/<commit>/<path>
<cache>/<store name>/<bucket>/<key>                   an object store; for lakeFS the key holds the commit
<cache>/<store name>@version-<ID>/<bucket>/<key>      a fixed version of a file
<cache>/<store name>@commit-<ID>/<bucket>/<key>       a file or folder at a provcat commit
<cache>/.provcat/        versions, finished folders and pinned refs
```

provcat stores a GitHub or Hugging Face file under its commit, so the file never goes stale. provcat checks a file of an object store against its version before each use, and downloads it again when the version changed. The version is the ETag on S3 and Azure, and the generation on GCS. With `--offline`, provcat reads only the cache. For a branch, it uses the last commit that it pinned.

A fetch can rewrite a cached file in place when a branch moves. So do not mount a cached path into a container. Mount a snapshot instead. `Catalog.snapshot(uri)` pins the URI, fetches that version, and copies it into `<cache>/.provcat/snapshots/<kind>/<sha256>/<name>`. The kind is `file` or `folder`, the folder name is the SHA-256 of the content, and the copy is read-only. Before provcat uses a snapshot again, it checks the digest, and it replaces a snapshot that someone changed.
