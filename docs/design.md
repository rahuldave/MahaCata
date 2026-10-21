# Design of provcat

This page records what the original provcat did, what this version keeps, and how the interfaces fit together. Read the [README](../README.md) first for the commands.

## Terms

| Term | Meaning |
|---|---|
| Logical URI | A URI whose scheme the configuration defines. It names a thing, not a place. |
| Physical URI | A URI whose scheme names a store. It names a place. |
| Selector | The `?at=kind:value` part of a URI. It picks a version. |
| Resolver | The part that turns a logical URI into a physical URI. |
| Store | A named instance of a driver, with options. |
| Driver | A class that reads one kind of storage, for example S3. |
| Pin | Replace a moving name, such as a branch, with a fixed one, such as a commit. |
| ETag | The fingerprint that S3 gives each version of an object. |

## The original provcat

The first commit holds provcat as it was found in `~/Catalog/provcat`. The files date from 29 September to 3 October 2025. It had no commits.

It was a command, `provc`, for a catalog of datasets, experiments, runs, models and artifacts. Each thing had a logical URI, for example `model://org/team/env/project/name?at=version:3`. The data lived in lakeFS, which is a store with git-like branches over S3. Experiments and model versions went to MLflow. Tables went to Lance files.

The resolver worked in principle. It split a URI into parts, filled a repository name and a path from YAML templates, and returned `s3://repo/branch/path`. A second resolver read the same templates from etcd.

The rest was a draft:

- `provc.py` did not parse, because of bad indentation. It also used `os` but did not import it.
- The lakeFS branch commands returned fixed fake answers.
- The S3 writer fell back to `/mnt/data` without a word when S3 failed.
- Stage pointers lived in a JSON file under `/mnt/data`.
- The tests imported `provenance_resolver`, an older name of the package, and expected fields that the code no longer returned.
- The selectors `stage:`, `version:`, `commit:` and `ts:` were in the docs, but only `branch:` changed the result.

## What changed

This version keeps the URI shapes and the template idea, and drops the rest:

- **Kept:** the logical schemes, the YAML and etcd configuration, and stage pointers. Stage pointers now live in the configuration.
- **Added:** store drivers for local files, S3-compatible storage and GitHub, and a local cache.
- **Removed:** the lakeFS write commands, Lance tables, MLflow runs and the Jinja cards. They are in the first commit for later use.

lakeFS stays usable for reading. Its S3 gateway takes `s3://repo/ref/path`, so a store with the S3 driver and the lakeFS endpoint reads any branch or commit.

## The interfaces

provcat follows the Perl `URI` module, in which the scheme of a URI picks the class that handles it. Each interface has one or more implementations, and each one is replaceable.

| Interface | Implementations | File |
|---|---|---|
| `ConfigSource` | `YamlConfig`, `EtcdConfig`, `EmptyConfig` | `src/provcat/config.py` |
| `Resolver` | `TemplateResolver` | `src/provcat/resolver.py` |
| `Store` | `LocalStore`, `S3Store`, `GithubStore` | `src/provcat/stores/` |
| Cache | `LocalCache` | `src/provcat/cache.py` |

`Catalog` in `src/provcat/catalog.py` joins them. It loads a configuration, makes one store for each entry in `stores`, and makes the resolver from `schemes` and `stages`. A store name and a logical scheme cannot be the same.

### Configuration sources

A value can read the environment with `${oc.env:NAME,default}`. Each source has two methods. `raw()` returns the configuration as written. `load()` fills in every `${...}` from the environment of the reader.

`YamlConfig` reads a YAML file. `EtcdConfig` keeps each section as JSON under `<prefix>/<section>`. It talks to the etcd v3 HTTP gateway with the standard library, so it needs no etcd client package. It does not support TLS or etcd user accounts yet.

`provc config push` copies the raw form into etcd, so each reader fills in its own home folder and endpoints. The push is one etcd transaction. It writes the sections that the file has, and deletes the sections that the file does not have.

### The template resolver

Each logical scheme has these settings:

- **`parts`:** names for the first part and each segment, in order.
- **`rest`:** an optional name that takes all extra segments, joined with `/`.
- **`target`:** a physical URI with `{name}` fields. A field can be a part name, the rest name, `ref`, `version` or `at`.
- **`ref`:** the default for `{ref}` when the URI has no selector.
- **`pass_selector`:** if true, the physical URI gets the selector that the resolver used. A stage becomes the selector it points at. A scheme with a `ref` default passes `branch:<ref>` when the URI has no selector.

A template can use only plain `{name}` fields, and only the names in the list above. provcat checks the fields when it loads the configuration. It refuses attribute access, indexes and format specs.

The resolver percent-encodes each value in the template, so a value can never add a path segment or a `?at=` to the physical URI. A ref that contains `/` fails when the ref fills one path segment, as it does for lakeFS.

### Stores

A driver class has four methods. Each method takes the cache.

- **`pin(uri, cache)`:** returns the URI with a fixed version. GitHub replaces a branch or tag with a commit and records it in the cache. Other drivers return the URI as it is.
- **`stat(uri, cache)`:** returns one `Entry`, a file or a folder, with a size and a version.
- **`list(uri, cache)`:** returns the entries directly inside a folder.
- **`fetch(uri, cache)`:** returns a local path that holds the file or folder.

To add a driver, write a subclass of `Store`, set `driver`, list its options in `option_names`, and decorate the class with `register_driver`. A store entry with `driver: <name>` then uses it.

### The cache

`LocalCache` writes every file to a temporary name under `.provcat/tmp` and then renames it, so a reader never sees half a file. The records under `.provcat` are named by a hash of the store and the key, so two keys never share or block a record:

- **`meta`:** the version of each cached file, with its size, modification time and SHA-256. If only the time changed, for example after a copy of the cache, provcat hashes the file and keeps it when the hash matches. If the bytes changed, provcat fetches the file again.
- **`done`:** a folder that was fetched whole.
- **`refs`:** a branch, tag or default branch pinned to a commit. The record includes the kind, so `tag:v1` and `branch:v1/x` do not touch each other.
- **`locks`:** one lock file for each folder. Fetches of one folder run one at a time, in threads and in processes.

Some disks, for example the default disks of macOS and Windows, store `Guide.md` and `guide.md` as one file. The same is true of one name in two Unicode forms. Both drivers refuse to fetch a folder that holds two such names, and they compare every level of each path. They refuse it on Linux too, so that every cache can move to any disk. The check also treats `straße` and `strasse` as one name. A single file with such a name still works, because the hash in its record shows when the other name overwrote it.

The GitHub driver checks the kind of a ref through the git refs API, so `tag:main` reads the tag and not the branch. It follows an annotated tag to its commit. It fetches a folder as one tarball of the commit. It skips links, devices and names with `..` in the tarball, and unpacks with the Python `data` filter. Offline mode serves a file inside a folder that was fetched whole. The token goes only to the host of `api_url`, and `gh auth token` is asked for that host.

The S3 driver fetches a folder object by object, and then deletes cached files whose objects no longer exist. It removes the folder marker first, and writes it again only after the whole folder is current. It refuses a folder that holds keys no URI can name, for example `a//b`. It records the ETag that came with the downloaded bytes. It does not send `If-Match`, because S3-compatible servers do not all support it.

## Known limits

| Limit | What to do |
|---|---|
| The GitHub contents API refuses files over 100 MB. | Fetch the folder that holds the file. The tarball has every file. |
| `provc ls` shows only the first 1000 entries of a GitHub folder. | Fetch the folder. The tarball has every file. |
| A crash can leave files in `.provcat/tmp`. | Delete them when no provcat process runs. |
| S3 lets `a` and `a/b` exist together, but a disk does not. | The second fetch fails with an error. Rename one key. |
| Lock files under `.provcat/locks` are never deleted. | Delete them when no provcat process runs. |
| A folder and a folder inside it can be fetched at the same time, under different locks. | Avoid it. If a fetch fails with an error, run it again. |
| A `${oc.env:...}` value in etcd reads the environment of each reader. | Let only trusted people write to the etcd prefix. |

## Next steps

- **ADLS:** add a driver for Azure Data Lake Storage for the Azure deployment. It follows the S3 driver, with `abfs://container/path` URIs and the `azure-storage-file-datalake` package.
- **Timestamps:** support `ts:` selectors, which need a list of lakeFS commits.
- **Writes:** bring back the lakeFS branch, commit and merge commands from the first commit, as a separate interface.
- **Containers:** mount the cache read-only into containers, and run `provc --offline` inside them.
