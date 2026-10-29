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
| `Store` | `LocalStore`, `GithubStore`, and the object stores `S3Store`, `GcsStore`, `AzureStore` | `src/provcat/stores/` |
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
- **`ref`:** the default branch for `{ref}` when the URI has no `branch:`, `tag:` or `commit:`. It can name parts, for example `"{env}"`, so that each environment is a branch. A `version:` selector keeps the default.
- **`pass_selector`:** if true, the physical URI gets the selector that the resolver used. A stage becomes the selector it points at. A scheme with a `ref` default passes `branch:<ref>` when the URI has no selector. With `version:`, only the version is passed.
- **`match`:** an optional regular expression for each part. provcat refuses a URI whose part does not match the whole expression.

A template can use only plain `{name}` fields, and only the names in the list above. provcat checks the fields when it loads the configuration. It refuses attribute access, indexes and format specs.

The resolver percent-encodes each value in the template, so a value can never add a path segment or a `?at=` to the physical URI. A ref that contains `/` fails when the ref fills one path segment, as it does for lakeFS.

### Stores

A driver class has four methods. Each method takes the cache.

- **`pin(uri, cache)`:** returns the URI with a fixed version. GitHub replaces a branch or tag with a commit and records it in the cache. Other drivers return the URI as it is.
- **`stat(uri, cache)`:** returns one `Entry`, a file or a folder, with a size and a version.
- **`list(uri, cache)`:** returns the entries directly inside a folder.
- **`fetch(uri, cache)`:** returns a local path that holds the file or folder.

To add a driver, write a subclass of `Store`, set `driver`, give its options and their types in `option_types()`, and decorate the class with `register_driver`. A store entry with `driver: <name>` then uses it.

S3, Google Cloud Storage and Azure Blob Storage share one base class, `ObjectStore` in `src/provcat/stores/objects.py`. It holds stat, list, folder fetch, the cache and offline mode. A driver for one more object store gives only four primitives:

- **`locate(uri)`:** the bucket and the key that a URI names.
- **`_head`:** the size and version of one object.
- **`_pages`:** the pages of a listing, with folder prefixes and objects.
- **`_get`:** writes the bytes of one object, and returns their version.

Each driver uses its cloud's Python SDK, never a command-line tool. The version of a file is the ETag on S3 and Azure, and the generation on GCS. The GCS and Azure SDKs are optional extras, so the drivers import them only when a store is used. An ADLS Gen2 account keeps folders as marker blobs, and the Azure driver leaves them out of listings.

### The cache

`LocalCache` writes every file to a temporary name under `.provcat/tmp` and then renames it, so a reader never sees half a file. The records under `.provcat` are named by a hash of the store and the key, so two keys never share or block a record:

- **`meta`:** the version of each cached file, with its size, modification time and SHA-256. If only the time changed, for example after a copy of the cache, provcat hashes the file and keeps it when the hash matches. If the bytes changed, provcat fetches the file again.
- **`done`:** a folder that was fetched whole.
- **`refs`:** a branch, tag or default branch pinned to a commit. The record includes the kind, so `tag:v1` and `branch:v1/x` do not touch each other.
- **`locks`:** one lock file for each folder. Fetches of one folder run one at a time, in threads and in processes.

Some disks, for example the default disks of macOS and Windows, store `Guide.md` and `guide.md` as one file. The same is true of one name in two Unicode forms. Both drivers refuse to fetch a folder that holds two such names, and they compare every level of each path. They refuse it on Linux too, so that every cache can move to any disk. The check also treats `straße` and `strasse` as one name. A single file with such a name still works, because the hash in its record shows when the other name overwrote it.

The GitHub driver checks the kind of a ref through the git refs API, so `tag:main` reads the tag and not the branch. It follows an annotated tag to its commit. It fetches a folder as one tarball of the commit. It skips links, devices and names with `..` in the tarball, and unpacks with the Python `data` filter. Offline mode serves a file inside a folder that was fetched whole. The token goes only to the host of `api_url`, and `gh auth token` is asked for that host.

The S3 driver fetches a folder object by object, and then deletes cached files whose objects no longer exist. It removes the folder marker first, and writes it again only after the whole folder is current. It refuses a folder that holds keys no URI can name, for example `a//b`. It records the ETag that came with the downloaded bytes. It does not send `If-Match`, because S3-compatible servers do not all support it.

## Tenants

The org is the tenant. It is the first part of every logical URI in the example configuration, as the owner is on GitHub. The mapping keeps the data of each org apart:

| Logical URI | Physical URI |
|---|---|
| `data+table://ORG/DOMAIN/ENV/FAMILY/NAME` | `lakefs-ORG://ORG-data-DOMAIN/ENV/tables/FAMILY/NAME` |
| `experiment://ORG/TEAM/ENV/PROJECT/PIPELINE` | `lakefs-ORG://ORG-ml-PROJECT/ENV/TEAM/experiments/PIPELINE` |
| `model://ORG/TEAM/ENV/PROJECT/NAME?at=version:N` | `lakefs-ORG://ORG-ml-PROJECT/ENV/TEAM/models/NAME/vN` |
| `artifact+prompt://ORG/TEAM/ENV/PROJECT/NAME` | `lakefs-ORG://ORG-ml-PROJECT/ENV/TEAM/prompts/NAME` |
| `artifact+card://ORG/TEAM/ENV/PROJECT/NAME` | `lakefs-ORG://ORG-ml-PROJECT/ENV/TEAM/cards/NAME` |
| `code://ORG/REPO/ENV/PATH` | `github://ORG/REPO/PATH?at=branch:ENV` |

lakeFS has no orgs, so each org gets its own repositories, named `ORG-data-DOMAIN` or `ORG-ml-PROJECT`. Each environment is a branch of the repository. For code, the environment is the GitHub branch, and the org is the GitHub owner. The `code` patterns follow the GitHub rules for owners, repositories and branch names, so an org there can have `-` and capital letters.

GitHub ignores case in owner names, so `Octocat` and `octocat` are two logical names for one owner, with two cache folders. A branch name with `/`, such as `feature/x`, cannot be one segment. Name it with a selector, for example `code://ORG/REPO/main/PATH?at=branch:feature/x`.

The `code` shape changed from `code://ORG/REPO/PATH`. An old URI still resolves, but its first path segment becomes the env. So rewrite old `code` URIs with an env.

So two orgs never share a lakeFS repository, a branch or a commit history.

In the lakeFS schemes, the `match` patterns keep the names apart and valid for lakeFS:

- **Org:** 1 to 20 lowercase letters and digits. It has no `-`, so the first `-` in a repository name always ends the org.
- **Kind:** the word `data` or `ml` after the org keeps a domain and a project with the same name apart.
- **Domain or project:** lowercase letters, digits and `-`, as lakeFS requires. The length limits keep each repository name within 63 characters.
- **Environment:** 1 to 20 lowercase letters, digits, `_` and `-`, so that it is a valid lakeFS branch name.

The environment part picks the branch only when the URI has no `branch:`, `tag:` or `commit:`. A URI such as `...prod/...?at=branch:dev` reads the branch `dev`. Use `commit:` or `stage:` to fix a version, and keep `branch:` for work on a branch outside the environments. A stage pointer belongs to one full URI, so each environment needs its own stage entries.

In the lakeFS schemes, each org also reads with its own keys. The template names the store `lakefs-{org}`, and the configuration has one store for each org, each with its own AWS profile. A logical URI of one org resolves only to that org's store. A logical URI of an org with no store fails, because it names a store that does not exist. `code://` URIs are different, because every org reads GitHub through the one `github` store.

The rules for a field in a store name keep this mapping safe:

- **One field.** The store name holds one field, next to fixed text with a `-`, as in `lakefs-{org}`. So no value can make a built-in name such as `file` or `s3`.
- **A plain value.** The value must match its `match` pattern, and must be lowercase letters and digits only. So two different values never give one store name.

The mapping is not access control. provcat does not know who runs it, and a physical URI, such as `lakefs-globex://...`, skips the mapping. The real boundary is the keys that a reader holds. Give each tenant a configuration with only its own store, and only its own profile in its AWS files. The example configuration also has the admin store `lakefs`, which reads every repository. Leave it out of a tenant's configuration.

A shared cache is not a boundary either. Its folders, such as `<cache>/lakefs-globex/...`, are plain files. Give each tenant its own cache. Pins live under the store name, so a pin made through the `lakefs` store is not found through `lakefs-acme`.

The keys keep tenants apart only if the server checks them. lakeFS with its default settings has no access control. It answers 501 to the group and policy APIs, and every user can read every repository. With one open-source lakeFS, the stores for each org route the requests. They do not stop one org's keys from reading another org's repositories. To enforce the separation, use one of these:

- **A lakeFS for each org.** Point each org's store at its own lakeFS server, with `endpoint_url`. The system test `tests/system/test_lakefs_isolation.py` checks this with two real servers. Each org reads its own data, and its store finds no repository of the other org. Each server refuses the other server's keys, at the S3 gateway and at the API.
- **One patched lakeFS 1.86.0 with the access-list server.** lakeFS 1.86.0 can ask an external server who may do what. Its reference server, the access-list server, keeps users, groups and policies. `scripts/lakefs_acl_local.py` runs both, and gives each org a policy for the repositories whose names start with the org and a `-`. The system test `tests/system/test_lakefs_acl.py` checks this on one real server:
  - Each org reads, pins and lists its own data.
  - Each org lists only its own repositories, through the API and the S3 gateway.
  - Each org is refused the other's repositories, and cannot make a repository under the other's name or on its storage.
- **lakeFS with access control from Treeverse.** lakeFS Cloud and lakeFS Enterprise can limit a user to its own repositories.

The route needs a patch to lakeFS, kept on the branch `provcat` of the fork [rahuldave/lakeFS](https://github.com/rahuldave/lakeFS). Without it, stock lakeFS 1.86.0 lets one org read another's data:

- **Linking any address.** The stage and link calls let a user who may write to its own repository add an object at any address on the same storage. lakeFS checks the repository, not the address. The addresses follow a pattern that can be guessed. So acme could link one of globex's objects into an acme repository, and read it.
- **Dots in names.** A storage namespace such as `local://acme-x/../globex-y` matches a policy on `namespace/local://acme-*`, but names globex's storage.
- **Any file as a commit's contents.** A commit can name its metarange, the file that lists its objects. lakeFS opened that name in its cache without a check, so `../` reached any file on the server, another org's metaranges among them. A bad name also broke the branch.

The patch closes all three:

- **Addresses outside the repository.** An address outside the repository's own storage needs `fs:ImportFromStorage` on that address, as an import does. The org policies do not grant it.
- **Dot segments.** lakeFS refuses `.` and `..` segments in addresses and storage namespaces, also when they are percent-encoded. It also refuses `@`, `?` and `#` in storage namespaces. The commit cache reads `local://acme-x@globex-y` as storage of `globex-y`.
- **Metarange names.** A metarange ID from a caller must be letters, digits, `_` and `-`, and a commit's metarange must exist. The cache also refuses `.` and `..` in any file name.

The system test `tests/system/test_lakefs_acl.py` tries each attack. The first two fail on stock 1.86.0 and pass on the patched build. A reviewer reproduced the third on a build with only the first two fixes. Open-source lakeFS at master has the same staging code. lakeFS Enterprise 1.103.0 made the same change to stage and link, and notes it as a security fix.

Each lakeFS also needs its own commit cache. The default, `~/lakefs/data/cache`, is shared by every lakeFS of the same user, and names its files by storage namespace only. `scripts/lakefs_acl_local.py` sets `LAKEFS_COMMITTED_LOCAL_CACHE_DIR` to a folder of its own. Error messages and stat replies show absolute server paths, which help an attacker build addresses.

The access-list server route has these limits:

- **It ends with 1.86.0.** lakeFS 1.87.0 removed the external authorization server and the access-list server, and moved to the Business Source License 1.1. So this route stays on 1.86.0, built from source.
- **The server trusts every caller.** The access-list server does not check who calls it. Anyone who reaches its port can make an admin. The script keeps it on `127.0.0.1`.
- **Policies cannot be listed, deleted or detached.** The access-list server answers 501 to these, and lakeFS passes that on as 500. A policy can be changed, and the script rewrites each org's policy on every run. To take an org's access away, remove its user from its group, or delete its keys.
- **Org users cannot manage their own keys.** Only the admin can make or delete an org's keys.
- **The admin is made in the access-list server.** In this mode the lakeFS setup call makes no user, and `lakefs superuser` refuses to run. The script makes the admin in the access-list server, in its `Admins` group.
- **A missing repository looks like a refused one.** For a user with limited access, the S3 gateway answers a repository that does not exist with 403. The `lakefs` driver then asks the API, which tells the two apart.

These parts of tenancy are not built yet:

- **A configuration for each tenant.** All tenants share one configuration. An etcd prefix for each org, for example `etcd://host/provcat/acme`, works today but has no tooling.

### The lakeFS driver

The `lakefs` driver is the S3 driver plus the lakeFS REST API. Data comes through the S3 gateway, where the first key segment is a ref. A ref is a branch, a tag or a commit. The REST API answers what the gateway cannot:

- **Pin.** `pin` turns `lakefs://repo/main/path` into `lakefs://repo/<commit>/path`. The commit is the head of the branch. Data that is not committed shows on the branch, but not on the pinned URI.
- **Time.** `lakefs://repo/main/path?at=ts:TIME` names the newest commit of `main` made at or before TIME. stat, ls and fetch pin it first, and then read that commit. A time in the future is refused, because its commit could still change.
- **Refs.** The API says whether a ref exists. So `ls` of an empty branch gives `[]`, and a missing branch or repository gives an error that names it. `ls lakefs://repo` lists the branches.

A logical URI keeps its default branch with `ts:`, and the resolver passes `?at=ts:TIME` on to the physical URI. So `data+table://acme/shared/prod/...?at=ts:2026-10-01` reads branch `prod` as it was on that day. A stage can point at a time, for example `audit: "ts:2026-03-31T23:59:59Z"`.

Each pin goes into the cache, so `--offline` uses the commit that the last online pin found. A pin of a time less than 10 minutes ago is not kept. A commit under way, or a server clock that runs behind, can still add a commit before that time.

A time search reads the first-parent log of the branch, 1000 commits a page, from the newest commit back. The first-parent log holds the commits of the branch itself. A commit of a feature branch that was merged in is left out, because the branch never pointed at it. The search takes the first commit made at or before TIME. lakeFS records when each commit was made, not where a branch pointed, so the answer can differ from the history in two cases:

- **A reset.** A branch that was reset to an older commit pointed at that commit with no new commit to show it.
- **A set date.** The lakeFS commit API can set the date of a commit, so a commit can claim an earlier time.

The API takes the same keys as the S3 gateway. provcat never follows a redirect from the API, because a redirect would send the keys to the host that it names.

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
| `ts:` works on lakeFS only. GitHub refuses it. | Pin a GitHub ref with `commit:`. |
| A `ts:` search reads at most 100,000 commits of a branch. | Use `commit:` or a stage for older versions. |

## Next steps

- **Writes:** bring back the lakeFS branch, commit and merge commands from the first commit, as a separate interface.
- **Containers:** mount the cache read-only into containers, and run `provc --offline` inside them.
