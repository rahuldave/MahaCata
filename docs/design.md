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
| `ConfigSource` | `YamlConfig`, `EtcdConfig`, `EmptyConfig` | `packages/provcat/src/provcat/config.py` |
| `Resolver` | `TemplateResolver` | `packages/provcat/src/provcat/resolver.py` |
| `Store` | `LocalStore`, `GithubStore`, and the object stores `S3Store`, `GcsStore`, `AzureStore` | `packages/provcat/src/provcat/stores/` |
| Cache | `LocalCache` | `packages/provcat/src/provcat/cache.py` |

`Catalog` in `packages/provcat/src/provcat/catalog.py` joins them. It loads a configuration, makes one store for each entry in `stores`, and makes the resolver from `schemes` and `stages`. A store name and a logical scheme cannot be the same.

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

A driver class has four methods for reading. Each method takes the cache.

- **`pin(uri, cache)`:** returns the URI with a fixed version. GitHub replaces a branch or tag with a commit and records it in the cache. Other drivers return the URI as it is.
- **`stat(uri, cache)`:** returns one `Entry`, a file or a folder, with a size and a version.
- **`list(uri, cache)`:** returns the entries directly inside a folder.
- **`fetch(uri, cache)`:** returns a local path that holds the file or folder.

A driver that can be written also has two methods for writing:

- **`put(uri, src, cache)`:** writes the local file `src` at the URI, and returns its `Entry`.
- **`delete(uri, cache)`:** deletes one object.

The `file`, `s3`, `gcs`, `azure` and `lakefs` drivers can be written. The `github` driver cannot, and the base class refuses with a `StoreError`. The lakeFS driver also has `commit` and `branch`, as "Writes" below explains.

To add a driver, write a subclass of `Store`, set `driver`, give its options and their types in `option_types()`, and decorate the class with `register_driver`. A store entry with `driver: <name>` then uses it.

S3, Google Cloud Storage and Azure Blob Storage share one base class, `ObjectStore` in `packages/provcat/src/provcat/stores/objects.py`. It holds stat, list, folder fetch, the cache and offline mode. A driver for one more object store gives only four primitives:

- **`locate(uri)`:** the bucket and the key that a URI names.
- **`_head`:** the size and version of one object.
- **`_pages`:** the pages of a listing, with folder prefixes and objects.
- **`_get`:** writes the bytes of one object, and returns their version.
- **`_put`:** writes a local file as one object, and returns its version.
- **`_delete`:** deletes one object, or raises `NotFound`.

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

The mapping is not access control. provcat does not know who runs it, and a physical URI, such as `lakefs-globex://...`, skips the mapping. The real boundary is the keys that a reader holds. Give each tenant a view with only its own stores, as the next section shows. Give it only its own profiles in its AWS files.

A shared cache is not a boundary either. Its folders, such as `<cache>/lakefs-globex/...`, are plain files. Give each tenant its own cache. Pins live under the store name, so a pin made through the `lakefs` store is not found through `lakefs-acme`.

The keys keep tenants apart only if the server checks them. lakeFS with its default settings has no access control. It answers 501 to the group and policy APIs, and every user can read every repository. With one open-source lakeFS, the stores for each org route the requests. They do not stop one org's keys from reading another org's repositories. To enforce the separation, use one of these:

- **A lakeFS for each org.** Point each org's store at its own lakeFS server, with `endpoint_url`. The system test `packages/provcat/tests/system/test_lakefs_isolation.py` checks this with two real servers. Each org reads its own data, and its store finds no repository of the other org. Each server refuses the other server's keys, at the S3 gateway and at the API.
- **One patched lakeFS 1.86.0 with the access-list server.** lakeFS 1.86.0 can ask an external server who may do what. Its reference server, the access-list server, keeps users, groups and policies. `packages/provcat/scripts/lakefs_acl_local.py` runs both, and gives each org a policy for the repositories whose names start with the org and a `-`. The system test `packages/provcat/tests/system/test_lakefs_acl.py` checks this on one real server:
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

The system test `packages/provcat/tests/system/test_lakefs_acl.py` tries each attack. The first two fail on stock 1.86.0 and pass on the patched build. A reviewer reproduced the third on a build with only the first two fixes. Open-source lakeFS at master has the same staging code. lakeFS Enterprise 1.103.0 made the same change to stage and link, and notes it as a security fix.

Each lakeFS also needs its own commit cache. The default, `~/lakefs/data/cache`, is shared by every lakeFS of the same user, and names its files by storage namespace only. `packages/provcat/scripts/lakefs_acl_local.py` sets `LAKEFS_COMMITTED_LOCAL_CACHE_DIR` to a folder of its own. Error messages and stat replies show absolute server paths, which help an attacker build addresses.

The access-list server route has these limits:

- **It ends with 1.86.0.** lakeFS 1.87.0 removed the external authorization server and the access-list server, and moved to the Business Source License 1.1. So this route stays on 1.86.0, built from source.
- **The server needs a token.** Stock 1.86.0's access-list server took every caller, so anyone who reached its port could make an admin. On the fork it takes `api_token`, and refuses a call without `Authorization: Bearer <token>`. lakeFS sends its `auth.api.token` that way. The script makes one token for both, and keeps the server on `127.0.0.1` as well.
- **Policies.** Stock 1.86.0's access-list server answered 501 to listing, deleting and detaching policies, and to policies on users. The fork implements them. To take an org's access away, detach its policy from its group, or delete the policy. lakeFS caches each user's policies for up to 23 seconds, so a change can take that long to apply. The script turns off the access-list server's own cache, which made a deleted user or key look present. The script rewrites each org's policy on every run.
- **Group policies are guarded.** Each group made through the access-list levels has a policy named `ACL(_-_)<group>`. Deleting the Admins one locked the admin out, with no way back through the API. So the fork refuses these:
  - deleting the Admins group;
  - deleting the `ACL(_-_)` policy of a group that exists, or detaching it from its own group;
  - making an `ACL(_-_)` policy for a group that does not exist;
  - writing anything into the Admins policy but the full-access statement it holds.

  Deleting any other group also deletes its `ACL(_-_)` policy.
- **The admin can still lock itself out.** It can do so in any of these ways:
  - leave the Admins group, or delete it;
  - join a group with a deny policy;
  - attach a deny policy to itself or to Admins;
  - delete its own keys or its own user.

  Run `packages/provcat/scripts/lakefs_acl_local.py --repair-admin` to undo any of these. It works through the access-list server and its token, while the setup runs or as it starts. It does the same steps every time:
  1. It makes the Admins group again if it is missing. It writes the Admins policy back to its full-access statement, and detaches every other policy from Admins, also ones that only allow.
  2. It puts the admin back in Admins, takes it out of every other group, and detaches the policies on the admin itself.
  3. It replaces the admin's keys in `credentials`.
  4. It waits until lakeFS takes the new keys for policies and for repositories, after lakeFS's policy cache has expired.
- **Org users cannot manage their own keys.** Only the admin can make or delete an org's keys.
- **The admin is made in the access-list server.** In this mode the lakeFS setup call makes no user, and `lakefs superuser` refuses to run. The script makes the admin in the access-list server, in its `Admins` group.
- **A missing repository looks like a refused one.** For a user with limited access, the S3 gateway answers a repository that does not exist with 403. The `lakefs` driver then asks the API, which tells the two apart.

### A configuration for each tenant

One configuration holds every tenant. The schemes are shared, so every tenant maps URIs by the same rules. Two sections hold the parts that are not shared:

| Section | What it holds | Who sees it |
|---|---|---|
| `tenants.<name>` | The tenant's stores, stages and cache root. | That tenant and the admin. |
| `admin` | Stores, stages and a cache root for the admin, such as the store `lakefs`, whose keys read every repository. | The admin only. |

`provc --tenant acme`, or `PROVCAT_TENANT=acme`, opens acme's view. It holds the shared sections and acme's part. provcat cuts the view before it fills in `${...}` values, so another tenant's part is never read. Without a tenant, `provc` opens the admin's view, which holds every part. `provc config show --tenant acme` prints a view.

A scheme names the part that says whose a URI is, with `tenant: org`. In acme's view, a URI of such a scheme with another org is refused. A scheme without `tenant`, such as `code`, serves every tenant.

Three rules keep one tenant from passing for another:

- **Store names.** A tenant's store names end with `-<tenant>`, as in `lakefs-acme` and `s3-acme`. So no tenant can define `lakefs-globex`, which globex's URIs resolve to.
- **Stages.** A tenant's stages must be URIs of that tenant. So acme cannot point globex's `stage:production` anywhere.
- **One name, one place.** A store or stage named in two parts is an error. Two ways of writing one stage URI, such as with a trailing `/`, count as one name.
- **The tenant picks the store.** In a scheme whose store name has a field, as in `lakefs-{org}`, the tenant part must be that field.

In etcd, each part has its own keys:

| Key | Holds |
|---|---|
| `<prefix>/schemes`, `/stores`, `/stages`, `/cache` | The shared sections. |
| `<prefix>/tenants` | The list of tenant names, for the admin. |
| `<prefix>/tenants/<name>/<section>` | One section of a tenant's part. |
| `<prefix>/tenants/<name>/name` | The tenant's name, which marks that the tenant exists, even with an empty part. |
| `<prefix>/admin/<section>` | One section of the admin's part. |

A reader for acme reads the shared keys and the keys under `<prefix>/tenants/acme/` only. It does not read the list of names. So etcd's own access control can keep the tenants apart. Give acme's etcd user read access to those keys only. Grant the prefix with its trailing `/`, as `<prefix>/tenants/acme/`, or the grant also covers `acme2`. The configuration holds no keys, only profile names, but it does show other tenants' stores and endpoints.

The views are not access control on their own. A tenant who can read the YAML file, or the etcd keys of another tenant, can read that part. The boundary stays the keys each tenant holds.

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

## Writes

provcat writes as well as reads. `Catalog` has five methods for writing, and `provc` has a command for each.

| Method | Command | What it does |
|---|---|---|
| `put(uri, src)` | `provc put URI PATH` | Writes a local file, a local folder, or bytes. A folder is written file by file under the URI. `-` reads standard input. |
| `delete(uri)` | `provc rm URI` | Deletes one object. |
| `commit(uri, message, metadata)` | `provc commit URI -m MSG --meta K=V` | Commits the lakeFS branch that the URI is on, or a folder in a versioned bucket. It returns the URI pinned to the new commit. |
| `branch(uri, source)` | `provc branch URI --from REF` | Makes a lakeFS branch from a branch, a tag or a commit. In a bucket, it copies a branch or a commit to a new branch. |
| `merge(uri, source, message, metadata, strategy)` | `provc merge URI --from REF -m MSG --meta K=V --strategy S` | Merges a branch, a tag or a commit into the lakeFS branch that the URI is on. It returns the URI pinned to the merge commit. Buckets have no merge. |

A write takes a logical or a physical URI, and goes to a branch:

- **No selector.** A logical URI writes to its scheme's default branch, which is the environment in the example configuration. So `data+table://acme/shared/dev/...` writes to branch `dev`.
- **A branch selector.** `?at=branch:NAME` writes to that branch.
- **Any other selector.** A commit, a tag, a version or a time names a fixed version, which cannot change, so the write is refused. A stage is refused too, even one that points at a branch. Write to the branch itself.
- **A scheme must use the branch.** If a scheme's target has no `{ref}`, `?at=branch:NAME` would be dropped and the write would go to the default branch. So the write is refused.

On lakeFS, a write is not seen in a pinned URI until it is committed. The commit gets metadata, and a commit through a logical URI adds `provcat.uri`, the logical URI it was made through. So a commit records where its data belongs in the catalog. lakeFS refuses a write to a commit or a tag. The driver checks that the ref is a branch first, so the error says so.

A local file is written through a temporary file in the same folder, so a reader never sees half of it. The file keeps the mode of the file it replaces. The `root` option confines writes as it confines reads. A folder upload checks every name before it writes the first file, and it includes hidden files.

### Merges

`provc merge lakefs://repo/main --from feature` merges `feature` into `main` with the lakeFS merge API. It merges the last commit of the source. So writes on `feature` that are not committed stay on `feature`. Commit them first.

lakeFS refuses some merges, and provcat says why:

- **A conflict.** Both branches changed the same file since they split. lakeFS refuses the merge, unless `--strategy` picks a side. `source-wins` keeps the source's file, and `dest-wins` keeps the branch's file.
- **Nothing to merge.** The branch already has every change in the source.
- **Uncommitted changes.** The branch that receives the merge has writes that are not committed. Commit them, or delete them, first.

A merge through a logical URI adds `provcat.uri` to the merge commit's metadata, as a commit does. lakeFS adds its own key, `.lakefs.merge.strategy`.

The cache needs no change for writes. An online read asks the store for the version first, so it never serves a copy that a write has replaced.

Access control is the store's. With the shared lakeFS, each org's keys write, commit, branch and merge only its own repositories. The system test `packages/provcat/tests/system/test_lakefs_acl.py` checks this.

## Commits in a bucket

A bucket on S3, GCS or Azure Blob has no branches and no commits. provcat keeps both by a layout and by the bucket's versions.

### The layout

A bucket holds one project. Its first folders are the branches, `main` among them:

```
s3://acme-sales/main/tables/agreements/part-0.parquet
s3://acme-sales/dev/tables/agreements/part-0.parquet
```

A scheme maps a logical URI onto that layout with `{ref}`. This needs no new code in the resolver:

```yaml
schemes:
  data:
    parts: [org, project]
    rest: path
    target: "s3://{org}-{project}/{ref}/{path}"
    ref: main
    match: {org: "[a-z0-9]+", project: "[a-z0-9-]+"}
```

So `data://acme/sales/tables/agreements?at=branch:dev` is `s3://acme-sales/dev/tables/agreements`. An org has many buckets, one for each project, and the cloud's access control works on each bucket.

### Fixed versions

A bucket with versioning on keeps every version of an object, and gives each an ID. Two selectors name fixed versions:

- **`?at=version:ID`** names one version of one file. `provc pin` of a file gives it.
- **`?at=commit:ID`** names a provcat commit of a folder. It works on the folder and on every file and folder in it.

A pin of a folder gives the newest commit of that folder, or of a folder above it, that holds the folder. A folder with no commit cannot be pinned. A bucket without versioning cannot pin or commit, and provcat says so.

### What a commit is

`provc commit s3://acme-sales/main/tables -m "qa run 42"` writes two objects at the top of the bucket:

| Object | What it holds |
|---|---|
| `.provcat/commits/<ID>.json` | The folder, the commit number, the parent commit, the message, the metadata, the time, and each file's version ID and size. The ID is the SHA-256 of this object. |
| `.provcat/log/<SHA-256 of the folder>/<10^15 - N>` | The ID of commit N of the folder. |

A reader checks that the commit object hashes to its ID, so a changed commit is refused. A read at a commit gets each file's listed version. The cache keeps it apart from the live files.

`provc branch s3://acme-sales/dev --from main` copies every file of `main` to `dev`. `--from <commit ID>` copies the versions in a commit. S3 and GCS copy inside the cloud. Azure downloads and uploads.

### What is guaranteed

A bucket writes one object at a time. It never writes several objects in one step. So a commit is made safe in two ways:

- **The file list is a snapshot.** A commit lists every version under the folder twice, deleted versions too. Every write makes a new version ID. So if the two listings are equal, nothing changed between them, and every listed version was current at one moment. If they differ, the commit lists again, up to five times, and then fails.
- **Each folder's commits have one order.** The commit creates log entry N only if no object has that name. S3 and Azure do this with `If-None-Match: *`, and GCS with `ifGenerationMatch=0`. Of two commits at once, one gets N. The other lists the files again and takes N + 1, or finds nothing to commit. Each commit names its parent.

lakeFS works differently. It keeps each branch pointer in its database, and a commit moves the pointer with one compare-and-swap. [Transactions](transactions.md) has the details, and compares Nessie, Delta Lake, Iceberg and SlateDB.

What a bucket commit does not give:

- **Several files written as one step.** Writers write files one by one, and a reader of the live folder can see half a change. Read at a commit to get one moment. Write through lakeFS to make several files appear at once.
- **Commits that outlive their versions.** A lifecycle rule that deletes old versions, or a person who deletes one, breaks the commits that name it. A read of such a file fails with "not found". Keep old versions while their commits matter.
- **One order across folders.** Each folder has its own log. A pin compares the commits of a folder and of the folders above it by their time, which comes from each writer's clock.
- **ADLS Gen2.** An account with a hierarchical namespace keeps no blob versions, so it can neither pin nor commit.
- **An atomic branch.** A branch copies file by file. If it fails halfway, the new branch is half made. Delete it and copy again.

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
| `ts:` works on lakeFS only. GitHub and buckets refuse it. | Pin a GitHub ref with `commit:`, and a bucket folder with a provcat commit. |
| A bucket commit lists every version under its folder, twice, so it slows as history grows. | Commit small folders, or delete old versions that no commit names. |
| A `ts:` search reads at most 100,000 commits of a branch. | Use `commit:` or a stage for older versions. |

## Next steps

- **Partial commits:** a lakeFS commit takes everything staged on the branch. A commit of one folder would need a temporary branch and a merge.
- **Containers:** mount the cache read-only into containers, and run `provc --offline` inside them.
