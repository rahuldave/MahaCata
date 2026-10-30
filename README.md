# provcat

provcat turns a catalog URI into a local path. A catalog URI names a dataset, a model, a prompt or some code in words that stay the same when the storage changes. provcat resolves the URI to a physical location, downloads the object into a local cache, and returns the path.

The first commit of this repository is the provcat code as it was found in `~/Catalog/provcat`. Read [docs/tour.md](docs/tour.md) for a guided tour with commands to run. Read [docs/setup.md](docs/setup.md) to install lakeFS, etcd and the tools that the tests need. Read [docs/design.md](docs/design.md) for what that code did, what changed and how the parts fit together. Read [docs/reference-server.md](docs/reference-server.md) to run and operate the shared lakeFS that keeps orgs apart.

## Install and run

provcat needs Python 3.13 and [uv](https://docs.astral.sh/uv/).

```
uv sync
uv run provc --help
uv run pytest
```

The base package reads local files, GitHub, S3 and lakeFS. Google Cloud Storage and Azure each need an extra, which installs that cloud's Python SDK:

```
pip install 'provcat[gcs]'
pip install 'provcat[azure]'
```

`uv sync` installs both for development.

## Catalog URIs

A URI has the form `scheme://first/part/part?at=kind:value`. The part after `?at=` is the selector. It picks a version.

A **logical URI** has a scheme that the configuration defines, for example `data+table` or `code`. A **physical URI** has a scheme that names a store, for example `s3`, `github` or `file`. provcat resolves a logical URI to a physical URI in one step.

```
data+table://acme/shared/prod/agreements/qa_outputs?at=stage:production
  -> lakefs-acme://acme-data-shared/b8a2f9c123456/tables/agreements/qa_outputs

code://octocat/Hello-World/master/README
  -> github://octocat/Hello-World/README?at=branch:master
```

In the example configuration, the org is the tenant and the first part of every logical URI, as the owner is on GitHub. Each org gets its own lakeFS repositories, and each environment is a branch. Read the section "Tenants" in [docs/design.md](docs/design.md).

| Selector | Meaning |
|---|---|
| `branch:X`, `tag:X`, `commit:X` | Use the git or lakeFS ref X. |
| `version:N` | Fill `{version}` in the template, for a model version. |
| `stage:NAME` | Look up NAME in the `stages` section and use the selector stored there. |
| `ts:TIME` | Use the newest commit of the branch made at or before TIME. TIME is a date, a date and time, or Unix seconds. The `lakefs` driver resolves it. |

## Stores

A store is a named instance of a driver. Every driver reads. All but `github` also write, as docs/design.md explains under "Writes". The stores `file`, `s3`, `github`, `gs` and `az` exist without configuration. So a logical scheme cannot use one of those five names. The configuration can add more, for example a store `lakefs` that uses the `lakefs` driver with the lakeFS endpoint.

| Driver | URI | What it does |
|---|---|---|
| `file` | `file:///abs/path` | Returns the local path. It does not copy into the cache. Option `root` confines the store to one folder. |
| `s3` | `s3://bucket/key` | Reads AWS S3 or any S3-compatible server, for example MinIO or the lakeFS S3 gateway. Options are `endpoint_url`, `region`, `profile` and `addressing_style`. |
| `lakefs` | `lakefs://repo/ref/path?at=ts:TIME` | Reads lakeFS through its S3 gateway, and asks its REST API for commits. `pin` turns a branch or a time into a commit. `ls` of an empty branch gives `[]`, and `ls lakefs://repo` lists the branches. Options are those of `s3`, plus `api_url`. |
| `github` | `github://owner/repo/path?at=branch:main` | Reads a file or a folder of a GitHub repository at a commit. Options are `api_url`, `token_env`, `use_gh_cli` and `timeout`. |
| `gcs` | `gs://bucket/key` | Reads Google Cloud Storage. Options are `project`, `endpoint_url` and `anonymous`. Needs the `gcs` extra. |
| `azure` | `az://account/container/key` | Reads Azure Blob Storage and ADLS Gen2. Options are `account_url` and `connection_string_env`. Needs the `azure` extra. |

Each cloud driver uses that cloud's Python SDK and takes credentials from the usual places:

- **S3:** the environment, a profile or an instance role.
- **GCS:** Application Default Credentials.
- **Azure:** `DefaultAzureCredential`, for example `az login`, or a connection string in the environment variable that `connection_string_env` names.

Do not put keys in the provcat configuration. The GitHub driver reads a token from `GITHUB_TOKEN` or `GH_TOKEN`. If neither is set, it runs `gh auth token`.

## Configuration

provcat reads its configuration from the first of these sources that exists:

1. The `--config` option of `provc`.
2. The environment variable `PROVCAT_CONFIG`.
3. The file `provcat.yaml` in the current folder.
4. The built-in defaults, which have the five stores and no logical schemes.

A source that starts with `etcd://host:port/prefix` is etcd. Any other source is a YAML file. [config/provcat.example.yaml](config/provcat.example.yaml) has every section and the logical schemes from the old provcat docs. A value can read the environment with `${oc.env:NAME,default}`.

To copy a YAML configuration into etcd, run the command below. It copies the `${...}` references as written, so each reader fills them in from its own environment. It also deletes the sections in etcd that the file does not have.

```
uv run provc --config config/provcat.example.yaml config push --to etcd://localhost:2379/provcat
```

## The cache

The cache folder is `~/.cache/provcat`. The environment variable `PROVCAT_CACHE`, the `cache.root` setting or the `--cache` option can change it. The files have readable paths, so a container can mount a part of the cache.

```
<cache>/github/<owner>/<repo>/<commit>/<path>
<cache>/<s3 store name>/<bucket>/<key>
<cache>/.provcat/        versions, finished folders and pinned refs
```

provcat stores a GitHub file under its commit, so the file never goes stale. provcat checks an S3 file against its ETag before each use, and downloads it again when the ETag changed. With `--offline`, provcat reads only the cache. For a branch, it uses the last commit that it pinned.

## Commands

| Command | Output |
|---|---|
| `provc resolve URI` | The physical URI and the steps taken, as JSON. |
| `provc pin URI` | The URI with a branch or tag replaced by its commit. |
| `provc stat URI` | One entry with its kind, size and version, as JSON. |
| `provc ls URI` | The entries in a folder, as JSON. |
| `provc fetch URI` | The local path of the file or folder. |
| `provc cat URI` | The bytes of a file. |
| `provc put URI PATH` | Writes a local file or folder at the URI, or standard input for `-`. |
| `provc rm URI` | Deletes one object. |
| `provc commit URI -m MSG [--meta K=V]` | Commits a lakeFS branch, and prints the URI pinned to the new commit. |
| `provc branch URI --from REF` | Makes a lakeFS branch from a branch, a tag or a commit. |
| `provc config show` | The configuration and its source. |
| `provc config push --to etcd://...` | Copies the configuration into etcd. |

## Tests

The tests are in three folders, and each test gets the marker of its folder:

| Folder | Marker | What the tests use |
|---|---|---|
| `tests/unit` | `unit` | Only the code and temporary files. |
| `tests/integration` | `integration` | Servers on this machine: a fake GitHub API and a fake etcd in the test process, moto for S3, and emulators in Docker for GCS and Azure. |
| `tests/system` | `system` | Real services on the network. |

`uv run pytest` runs the unit and integration tests. They need no network. The GCS and Azure tests need Docker and two images. If an image is missing, those tests skip and name the image to pull:

```
docker pull fsouza/fake-gcs-server:1.52.2
docker pull mcr.microsoft.com/azure-storage/azurite:3.35.0
```

`tests/object_store_suite.py` holds one set of tests for every object store. The integration tests run it against moto, fake-gcs-server and Azurite. The system tests run it against real AWS, GCS, Azure and lakeFS. Each system test makes its own bucket and deletes it afterwards. It uses the cloud's Python SDK, or the REST API for lakeFS.

Run the system tests with `-m system`. [docs/setup.md](docs/setup.md) shows how to set up each service. Each service runs only if you set its variable:

| Variable | Service |
|---|---|
| `PROVCAT_SYSTEM_AWS=1` | AWS S3 with the default credentials. `PROVCAT_SYSTEM_AWS_PROFILE` picks a profile. |
| `PROVCAT_SYSTEM_GCS=<project>` | Google Cloud Storage, with Application Default Credentials. |
| `PROVCAT_SYSTEM_AZURE=<subscription id>` | Azure. The tests make a resource group with one ADLS Gen2 account, and delete the group afterwards. They need `uv sync --group system`. |
| `PROVCAT_SYSTEM_LAKEFS=<url>` | lakeFS, with the keys of the AWS profile `lakefs`. Set `PROVCAT_SYSTEM_LAKEFS_DATA` to its blockstore path, and the tests also delete the storage folders that they made. |
| `PROVCAT_SYSTEM_LAKEFS_B=<url>` | A second lakeFS, with the keys of the profile `lakefs-b`. With both lakeFS variables set, a test checks that two servers keep two orgs apart. `PROVCAT_SYSTEM_LAKEFS_B_DATA` names its blockstore path. |
| `PROVCAT_SYSTEM_LAKEFS_ACL=<url>` | A lakeFS run by `scripts/lakefs_acl_local.py`, with access control for each org. A test checks that one server keeps two orgs apart. `PROVCAT_SYSTEM_LAKEFS_ACL_CREDENTIALS` names the credentials file, `PROVCAT_SYSTEM_LAKEFS_ACL_DATA` the blockstore path, `PROVCAT_SYSTEM_LAKEFS_ACL_SERVER` the access-list server, and `PROVCAT_SYSTEM_LAKEFS_ACL_DIR` the folder of the setup. |
| `PROVCAT_SYSTEM_GITHUB=1` | github.com. |
| `PROVCAT_SYSTEM_ETCD=<host:port>` | etcd. |

For example:

```
PROVCAT_SYSTEM_AWS=1 uv run pytest -m system
```
