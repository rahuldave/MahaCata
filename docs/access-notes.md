# Notes on how code reads and writes data through provcat and provrun

These notes describe the intended way to use provcat and provrun from code. They are a design for review, and nothing in them is built yet. They cover the lookup, the lifecycle spots and their hooks, a file API in the style of fsspec and the evidence of a read. They end with a plan of pull requests and the open questions.

## Terms

| Term | Meaning |
|---|---|
| logical URI | The name that code uses, for example `data+table://acme/nlp/prod/corpora/wiki`. It names no backend. |
| physical URI | The address in a store, for example `hf://datasets/acme/wiki/data` or `lakefs-acme://...`. The lookup makes one from a logical URI. |
| lookup | The step that turns a logical URI into a physical URI. It reads the YAML file that the user gives to provc, or etcd. |
| scheme template | A lookup rule for every URI of one scheme, as in `schemes:` today. |
| stage | A named pin for one logical URI, as in `stages:` today. |
| catalog entry | A lookup rule for one logical URI or one prefix of it. Entries do not exist yet. |
| pin | The physical URI with a fixed version in place of a moving name, such as a branch. |
| principal | Who asks: the tenant, the actor and, in a run, the run ID. |
| app | The program that uses the catalog, for example `provc`, `provrun` or a notebook. |
| launcher | The process that starts an app and sets its app name and hooks, for example provrun for an agent. |
| lifecycle spot | One step of the catalog where hooks run, for example the lookup or the fetch. |
| hook | A function that runs at a lifecycle spot. A gate, a cache layer and a recorder are hooks. |
| hook chain | The hooks of one lifecycle spot, in order, around the work of the catalog. |
| base cache | The local cache that is always in the chain, right after the gates. It holds the fetched files, the folder records and the record of pins. |
| proxy configuration | A local file that gives each app its hooks. It stands in for the profile and the permissions, which come later. |
| broker | The part of provrun that decides whether a run may use a URI. It becomes a gate hook. |
| evidence | The chain of events that provrun writes for each run. It becomes a recorder hook. |
| bundle | The evidence and the outputs of a run, which provrun commits through provcat. [provrun.md](provrun.md) describes it. |
| fsspec | The Python library that gives one file system API for many stores. pandas, pyarrow, polars and Dask use it to open URLs. |
| Git LFS | The Git extension that keeps large files outside the repository. Git holds a small pointer file in place of each large file. |
| Xet | The storage under Hugging Face. It splits files into chunks. Its repositories keep the Git LFS pointer format. |

## The intended usage pattern

Code names data only by logical URI. It opens a URI, reads it and closes it, as it does a local file. The lookup decides where the bytes are, and any other facts go in the catalog, not in the URI.

```python
fs = catalog.fs()                      # an fsspec file system
with fs.open("data+table://acme/nlp/prod/corpora/wiki/part-0.parquet", "rb") as f:
    table = pyarrow.parquet.read_table(f)

df = pandas.read_parquet("data+table://acme/nlp/prod/corpora/wiki/part-0.parquet")
```

One target can be code under one name and data under another. The scheme of a name says what the thing is to its users. The lookup says where it is.

There is one catalog and one file system. The file system is a thin fsspec adapter over the catalog, and it has no logic and no cache of its own. Every method of the catalog runs through the lifecycle spots. So the catalog has no method that reads data without the hooks.

## The trust boundary

The hooks control the code that uses the catalog API. They do not stop code that goes around the API in the same process. Such code can call a store object of the catalog or open a file in the cache folder. It can also use boto3 or `HfFileSystem` with the same keys.

So a gate is a security control only when the gated code cannot reach the keys or the cache folder:

- **Now.** provrun runs the agent in its own process. The sandbox is a set of paths, `/input`, `/output` and `/scratch`, that the tools of the agent see. The keys and the cache folder are in the same process. So the sandbox holds only while the agent has no tool that runs code.
- **Step 6 of the plan of pull requests below.** The agent moves to its own process or container, which holds no keys and no cache folder. Code there asks the provrun process to open a URI, and the provrun process runs the catalog with the gate. Undeclared opens are a security control only after this move.
- **Later.** The access layer gives short-lived keys for each grant, so the keys themselves carry the limits.

Outside a sandbox, for example in a notebook, the hooks are a policy for code that cooperates. They are not a wall.

## The lifecycle spots

Each operation of the catalog passes through fixed spots:

| Spot | When | What a hook gets |
|---|---|---|
| `session` | when an app opens the catalog, and when it closes it | the principal and the app |
| `resolve` | the lookup | the logical URI before the lookup, and the physical URI after it |
| `pin` | a moving name becomes a version | the physical URI, and then the pinned URI |
| `stat` and `list` | a question about a file or a folder | the URI, and then the entries |
| `fetch` | the bytes of a pinned version come to local disk | the pinned URI, and then the local path and its SHA-256 |
| `snapshot` | a fetched version becomes a read-only copy | the pinned URI, and then the path and the SHA-256 |
| `open` and `close` | a handle opens on a pinned version, and closes | the pinned URI, the mode, and at close the byte ranges read |
| `read_block` | one byte range of a large file | the pinned URI and the range |
| `put` and `delete` | a write or a delete in a store | the physical URI, and then the entry |
| `commit`, `branch` and `merge` | a change to the history of a store | the URI, the message and the new pin |

Each spot gets the logical URI that started the operation too, also at `fetch` and `snapshot`. So a hook can decide by the logical name at every spot.

The catalog calls the `pin` spot before every `fetch`, `snapshot` and `open` for reading. The drivers pin inside `fetch` now.

Step 4 of the plan of pull requests below moves that pin out of the drivers. Then a hook at `pin` sees the pin of every read. `stat` and `list` do not pin first. A file that a session put on a lakeFS branch, and did not commit yet, is on the branch but in no commit. So a `stat` or a `list` of the branch must still find it.

## Hooks

A hook at a spot wraps the work of the catalog, as middleware wraps a request in a web framework. The hook gets the request and a function `next`, which runs the rest of the chain. One shape covers every kind of hook:

| Kind | What it does with `next` | Example |
|---|---|---|
| recorder | It calls `next`, and then it records the request and the answer, or the error. | The provrun evidence. A log for a notebook. |
| gate | It raises an error and does not call `next`, or it calls `next`. | The provrun broker, at every spot. |
| cache layer | It returns a stored answer and does not call `next`. Otherwise it calls `next` and stores the answer. | A shared cache in S3 at `fetch` and `read_block`. |

```python
def broker_gate(ctx, req, next):
    # At resolve, req.physical is None, because the lookup has not run yet.
    if not broker.allows(ctx.principal, req.spot, req.logical, req.physical):
        raise AccessDenied(req.logical)
    return next(req)
```

The order of a chain is recorder, then gate, then the base cache, then the cache layers, then the store. So the recorder sees every request, also one that the gate refuses. provrun records `access_denied` today, and it keeps doing so. A gate sees every request that reaches the data, and a read from the base cache or a cache layer passes the gate too. A principal that may not read a URI cannot read, through the catalog, a copy that another principal fetched.

## The base cache

The base cache is always there, right after the gates in every hook chain. A proxy configuration cannot remove it or move it. The drivers need it. A fetch returns a path inside it, and a folder fetch installs into it under a lock. Offline mode reads the record of pins from it. The drivers now call the cache in about 30 places, and those calls stay as they are.

A cache layer adds a level between the base cache and the store. A cache in S3 is such a layer. A fetch then looks in the base cache first, then in the shared cache, and then in the store. A pinned version is safe to share between machines, because it never changes. The record of pins stays in the base cache, because each machine pins at its own time.

The base cache has these levels, and the notes add the third one:

1. **A copy by version.** The cache keeps a pinned URI in a path that names the version. No later fetch rewrites the copy.
2. **A snapshot.** provrun stages each declared input from a read-only snapshot, which is named by its SHA-256.
3. **Blocks of a large file (new).** A reader of a Parquet file often reads a few byte ranges of a large file. The cache keeps each block under the pinned URI. The fsspec adapter keeps no blocks of its own, so the cache and its hooks see every block.

`cache.root` stays in the configuration, as the root of the base cache for users who have no hooks file. The proxy configuration can set another root.

## The proxy configuration

Hooks come later from the profile of the principal and the access layer. Until then, a proxy configuration gives each app its hooks. It is a local file, and it is never in etcd. A configuration in etcd describes the data. The hooks describe who reads it and how, so they belong to the machine and the app.

```yaml
# ~/.config/provcat/hooks.yaml, or the file in PROVCAT_HOOKS
apps:
  default: {}
  provrun:
    cache_root: /var/cache/provcat-runs
    "*": [provrun.evidence, provrun.broker]
    fetch: [cache.s3]
    read_block: [cache.s3]
```

- **The hooks.** Each name is a Python entry point in the group `provcat.hooks`. A package registers its hooks when it is installed. So the file names hooks, and it holds no code and no path to code. If two packages register one name, provcat refuses to start.
- **The spots.** The hooks under `"*"` run at every spot, first. The hooks under a spot run after them. A name can be in a chain only once.
- **The app.** The launcher sets the app name and the path of the hooks file when it starts the process, through `PROVCAT_APP` and `PROVCAT_HOOKS`. provcat reads them once, when the process opens its first catalog. A later `Catalog.open(app=...)` can name only that app. Code in the process cannot pick an app with fewer hooks.
- **The writer.** The person who runs the machine writes the hooks file. The principal whose reads it controls must not be able to write it.
- **The root of the base cache.** `cache_root` in the section of an app takes the place of `cache.root` in the configuration. Two apps with two roots keep two records of pins, so `--offline` in one app does not see the pins of the other.
- **No app.** If `PROVCAT_APP` is not set and the file exists, the process gets the `default` section.
- **No file.** If `PROVCAT_APP` is not set and the file does not exist, every app gets the base cache and no hooks, as provcat works now. If `PROVCAT_APP` names an app and the file is missing or has no section for it, provcat refuses to start. So a missing file never turns a gate off without an error.

Later, the profile and the permissions give the hook chain of each principal. Then the proxy configuration goes away, and the spots and the hooks stay as they are.

## The lookup

A scheme template sends every URI of a scheme to one place. So today every `code://` URI goes to GitHub, and every `data+table://` URI goes to lakeFS. A catalog entry adds a rule for one name or one prefix:

```yaml
entries:
  "code://acme/tokenizer/prod":
    target: "github://acme/tokenizer/{path}?at=branch:prod"
  "code://acme/tokenizer/prod/weights":
    target: "hf://models/acme/tokenizer/{path}"
```

These rules apply to entries:

- **Longest prefix.** The longest prefix wins. A prefix matches whole path segments only, so `code://acme/tok` does not match `code://acme/tokenizer`. If no entry matches, the scheme template applies, as now.
- **Fields.** A target can use one field, `{path}`, which is the rest of the URI after the prefix. provcat encodes each segment of `{path}` as the templates do now, so a path cannot add a selector or climb with `..`.
- **Selectors.** A selector on the URI, for example `?at=commit:ID`, takes the place of the selector in the target. So a run can fix a version of any name. A `ts:` selector keeps the branch of the target, as the templates do now. So `?at=ts:2026-01-01` on the first entry above becomes `ts:prod@2026-01-01` on GitHub.
- **Stages.** A stage of a URI comes before an entry, as it comes before a template now.
- **The scheme rules.** The prefix must match the `parts` and the `match` patterns of its scheme, up to its length. A key cannot name a store, because the catalog does not look up a physical URI.
- **Tenants.** Entries live in the shared part or in a tenant's part of the configuration. provcat checks an entry of a tenant as it checks a stage. The scheme names one part of the key with its `tenant:` key, and that part must be the tenant. The store of the target must end with `-` and the tenant. The tenant view of the configuration must take in `entries` for this, and today it takes in only `stores` and `stages`.

## Physical URIs

The catalog also takes a physical URI, for example `github://owner/repo/path`. It does no lookup for one, so a hook sees no logical name. A gate can refuse physical URIs. The provrun broker already does so outside the `dev` environment.

The fsspec adapter registers the logical schemes as fsspec protocols. fsspec keeps one registration for each protocol in a process. So one catalog per process registers them, and provcat refuses a second catalog that tries to register them. Other catalogs in the process work through their own `catalog.fs()`.

The adapter does not register the stores. fsspec and `huggingface_hub` already register `github` and `hf` for their own file systems, and provcat does not replace them. Code reaches a physical URI through `catalog.fs()` only.

## Reads pin at open

A read must give the bytes that the evidence names. So `open` does the lookup once and pins once. Every read through that handle reads the pinned version, even if the branch moves during the read.

A hook at `pin` can hold the pins of a session. Then two opens of the same URI give the same version. provrun puts such a hook on a run, so the evidence of a run names one version for each URI. A run that must see a newer version opens the URI with a selector, for example `?at=commit:ID`, and the evidence shows the change.

The record of a read holds the logical URI, the physical URI, the pinned URI and an identity of the bytes:

- **A whole file.** The identity is the SHA-256.
- **Blocks of a file.** The identity is the version that the store gives, for example a lakeFS checksum, an S3 version ID or a Git commit. The record also holds the byte ranges that the reader read.
- **A store without versions.** `file://` pins a URI to itself. Then the identity is the size and the time of the last change. Such a read is not fixed, because the bytes can change during the read. The record says so. A bucket without versioning cannot be pinned, so it cannot be read in a run, as now.

Code in a run can open a URI that the run did not declare. The gate decides at that time, and the evidence records the open as it records a declared input. Declared inputs are still staged before the agent starts, as now.

## Writes

`open(uri, "wb")` writes to a temporary file. `close` runs the `put` spot, so the hooks see the write before the file goes into the store. Nothing is committed until the code calls `commit`, as with `provc put` now. In a run, the evidence hook records each written file as an output, and the commit of the bundle takes them all.

## fsspec under the drivers

fsspec has implementations for most of the stores that provcat reads:

| Store | fsspec implementation | Use it in provcat |
|---|---|---|
| S3, GCS, Azure | s3fs, gcsfs, adlfs | No, not now. The provcat drivers hold the commit logic, the version checks and the folder rules that the tests cover. |
| Hugging Face | `HfFileSystem` in `huggingface_hub` | Yes, inside the `hf` driver. It takes a revision and handles Xet. The driver adds the pin and `ts:`. |
| lakeFS | lakefs-spec | No. The provcat driver already reads, pins, commits and merges. |
| DVC | `DVCFileSystem` in dvc | Maybe, for a later DVC driver. |
| GitHub | `GithubFileSystem` in fsspec | No. But its way of reading Git LFS files is useful, see below. |

A driver that uses an fsspec implementation stays behind the lifecycle spots. So the hooks apply to it as to every other driver. The `hf` driver turns off the caches of `HfFileSystem` and does not use the cache of `huggingface_hub` under `~/.cache/huggingface`. So the base cache is the only cache of its files.

The provcat GitHub driver does not handle Git LFS now. A live test confirmed the problem. `provc fetch github://araines/serverless-php/php` writes the 133-byte pointer file, but `provc stat` gives the size of the content, 27,841,495 bytes. The fsspec GitHub file system reads the same contents API. If the content is a pointer, it downloads the `download_url` that GitHub gives for the file.

## Plan of pull requests

1. **Git LFS in the GitHub driver.** The driver replaces each pointer file with its content, in a single file and in a folder. It gets the content through the Git LFS batch API, and it checks the size and the SHA-256 against the pointer. A copy of a pointer file in the cache from before the fix is fetched again. A snapshot of such a copy named the pointer, so the evidence of a run that used it named the pointer too.
2. **The `hf` driver.** It reads Hugging Face models, datasets and Spaces through `HfFileSystem`. It pins to a commit, and it takes `branch:`, `tag:`, `commit:` and `ts:`. It is read-only at first.
3. **Catalog entries.** The lookup takes entries by the rules above, in the YAML file and in etcd.
4. **The lifecycle spots and the hooks.** The catalog runs each operation through its spots, and it pins before each read. The proxy configuration and the entry points come in. Nothing changes for users who have no hooks file.
5. **The fsspec file system.** `catalog.fs()` gives the adapter, with a pin at open and the cache of blocks. The logical schemes register as fsspec protocols.
6. **provrun on the hooks.** The broker becomes a gate hook, and the evidence becomes a recorder hook. The agent moves to its own process or container, and undeclared opens go from it to the provrun process. This step changes provrun, so it needs agreement on provrun's design first.

Steps 1 to 3 do not depend on each other. Step 5 needs step 4, and step 6 needs steps 4 and 5.

## Decisions

- **Undeclared opens.** Code in a run can open a URI that the run did not declare. The gate decides at that time.
- **No gates in etcd.** Hooks come from the proxy configuration now, and from the profile and the permissions later.
- **The identity of a partial read.** The evidence of a read of blocks holds the version from the store and the byte ranges that the reader read. A hash of the whole file would make the reader download the whole file.
- **The cache root.** `cache.root` stays in the configuration, as the default for users who have no hooks file. The proxy configuration can set another root.

## Open questions

- **The place of the repository type on Hugging Face.** The notes put it in the physical URI, as in `hf://datasets/...`, which is the form that `HfFileSystem` uses. The other choice is an option of the store.
