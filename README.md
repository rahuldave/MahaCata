# provcat

provcat turns a catalog URI into a local path. A catalog URI names a dataset, a model, a prompt or some code in words that stay the same when the storage changes. provcat resolves the URI to a physical location, downloads the object into a local cache, and returns the path.

The first commit of this repository is the provcat code as it was found in `~/Catalog/provcat`. Read [docs/design.md](docs/design.md) for what that code did, what changed and how the parts fit together.

## Install and run

provcat needs Python 3.13 and [uv](https://docs.astral.sh/uv/).

```
uv sync
uv run provc --help
uv run pytest
```

## Catalog URIs

A URI has the form `scheme://first/part/part?at=kind:value`. The part after `?at=` is the selector. It picks a version.

A **logical URI** has a scheme that the configuration defines, for example `data+table` or `code`. A **physical URI** has a scheme that names a store, for example `s3`, `github` or `file`. provcat resolves a logical URI to a physical URI in one step.

```
data+table://acme/shared/prod/agreements/qa_outputs?at=stage:production
  -> lakefs://agreements-qa_outputs-data/b8a2f9c123456/acme/shared/prod/agreements/qa_outputs

code://octocat/Hello-World/README?at=branch:master
  -> github://octocat/Hello-World/README?at=branch:master
```

| Selector | Meaning |
|---|---|
| `branch:X`, `tag:X`, `commit:X` | Use the git or lakeFS ref X. |
| `version:N` | Fill `{version}` in the template, for a model version. |
| `stage:NAME` | Look up NAME in the `stages` section and use the selector stored there. |
| `ts:TIME` | Refused for now. |

## Stores

A store is a named instance of a driver. The stores `file`, `s3` and `github` exist without configuration. The configuration can add more, for example a store `lakefs` that uses the S3 driver with the lakeFS endpoint.

| Driver | URI | What it does |
|---|---|---|
| `file` | `file:///abs/path` | Returns the local path. It does not copy into the cache. Option `root` confines the store to one folder. |
| `s3` | `s3://bucket/key` | Reads AWS S3 or any S3-compatible server, for example MinIO or the lakeFS S3 gateway. Options are `endpoint_url`, `region`, `profile` and `addressing_style`. |
| `github` | `github://owner/repo/path?at=branch:main` | Reads a file or a folder of a GitHub repository at a commit. Options are `api_url`, `token_env`, `use_gh_cli` and `timeout`. |

The S3 driver takes credentials from the usual AWS places, which are the environment, a profile and an instance role. Do not put keys in the provcat configuration. The GitHub driver reads a token from `GITHUB_TOKEN` or `GH_TOKEN`. If neither is set, it runs `gh auth token`.

## Configuration

provcat reads its configuration from the first of these sources that exists:

1. The `--config` option of `provc`.
2. The environment variable `PROVCAT_CONFIG`.
3. The file `provcat.yaml` in the current folder.
4. The built-in defaults, which have the three stores and no logical schemes.

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
| `provc config show` | The configuration and its source. |
| `provc config push --to etcd://...` | Copies the configuration into etcd. |

## Tests

The tests use a fake GitHub API, the moto S3 server and a fake etcd gateway, so they need no network. Two live tests run only if you set an environment variable:

```
PROVCAT_TEST_GITHUB=1 uv run pytest -m live
PROVCAT_TEST_ETCD=localhost:2379 uv run pytest -m live
```
