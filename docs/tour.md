# A guided tour of provcat

This tour follows catalog URIs through provcat, one step at a time. Each step gives commands to run and the output they print. After each command, the tour says what happened inside. The [README](../README.md) is the reference for the commands, and [design.md](design.md) is the reference for the parts.

## Before you start

You need Python 3.13, [uv](https://docs.astral.sh/uv/) and a GitHub login for `gh`. Run every command from the top folder of the repository.

1. Install the package and its test tools.

   ```
   uv sync
   ```

2. Point the cache at a scratch folder, so the tour does not touch your real cache.

   ```
   export PROVCAT_CACHE=/tmp/provcat-tour
   ```

3. Keep the example configuration in a variable, because most steps use it.

   ```
   export C=config/provcat.example.yaml
   ```

## The path of one URI

Every command takes a URI and sends it through the same parts, in the same order. The picture below shows the parts. The names in brackets are the files that hold them.

```
   "code://octocat/Hello-World/master/README"
                     |
                     v
   +-------------------------------+
   | parse        (uri.py)         |  text -> Uri(scheme, authority, segments, selector)
   +-------------------------------+
                     |
                     v
   +-------------------------------+     config: stores, schemes, stages
   | resolve      (resolver.py)    | <-- (config.py: YAML file or etcd)
   +-------------------------------+
                     |  logical URI -> physical URI
                     |  "github://octocat/Hello-World/README?at=branch:master"
                     v
   +-------------------------------+
   | pick store by scheme          |  file -> LocalStore, s3 -> S3Store,
   |              (catalog.py)     |  github -> GithubStore, lakefs -> LakefsStore
   +-------------------------------+
                     |
                     v
   +-------------------------------+     +----------------------------------+
   | store: pin, stat, list, fetch | --> | LocalCache   (cache.py)          |
   |              (stores/*.py)    | <-- | /tmp/provcat-tour/github/...     |
   +-------------------------------+     +----------------------------------+
                     |
                     v
   /tmp/provcat-tour/github/octocat/Hello-World/7fd1a60b.../README
```

A URI can skip the resolve step. If its scheme already names a store, for example `github`, provcat uses it as it is. A URI like that is a **physical URI**. A URI whose scheme the configuration defines, for example `code`, is a **logical URI**, and it goes through the resolver once.

## Step 1: Read one file from GitHub

Start with a physical URI, so the resolver does nothing. Ask provcat to pin it.

```
$ uv run provc pin 'github://octocat/Hello-World/README?at=branch:master'
github://octocat/Hello-World/README?at=commit:7fd1a60b01f91b314f59955a4e4d4e80d8edf11d
```

**What happened:** `GithubStore.pin` asked the GitHub API where the branch `master` points. The answer is a commit. provcat wrote the branch and the commit in a ref record under `/tmp/provcat-tour/.provcat/refs`. A pin turns a name that moves, such as a branch, into a name that does not move.

Now fetch the file.

```
$ uv run provc fetch 'github://octocat/Hello-World/README?at=branch:master'
/tmp/provcat-tour/github/octocat/Hello-World/7fd1a60b01f91b314f59955a4e4d4e80d8edf11d/README
```

**What happened:** the store pinned the branch again and got the same commit. It then asked the cache for the file at that commit. The cache did not have it, so the store asked GitHub whether the path is a file or a folder. For a file, the store wrote the bytes into a temporary file that the cache gave it. The cache renamed the file into place. It also wrote a meta record with the version, the size, the time and the SHA-256 of the file.

The path holds the commit, so a file in the cache never goes stale. A new commit on `master` gets a new folder.

Print the file, and look at its entry.

```
$ uv run provc cat 'github://octocat/Hello-World/README?at=branch:master'
Hello World!

$ uv run provc stat 'github://octocat/Hello-World/README?at=branch:master'
{
  "uri": "github://octocat/Hello-World/README?at=commit:7fd1a60b01f91b314f59955a4e4d4e80d8edf11d",
  "kind": "file",
  "size": 13,
  "version": "7fd1a60b01f91b314f59955a4e4d4e80d8edf11d"
}
```

An **entry** is the answer of `stat` and `ls`. It holds the pinned URI, the kind, the size and the version. For GitHub, the version is the commit.

## Step 2: Use a logical URI

A logical URI names a thing, and the configuration says where the thing lives. The example configuration has a scheme `code` for GitHub code:

```yaml
schemes:
  code:
    parts: [org, repo, env]
    rest: path
    target: "github://{org}/{repo}/{path}"
    ref: "{env}"
    pass_selector: true
```

In the scheme, the parts of the URI are `org`, `repo` and `env`, in that order. Every segment after them goes into `path`. The `target` is a template for the physical URI. The `env` is the branch, through `ref: "{env}"`. `pass_selector: true` puts that branch on the physical URI as `?at=branch:...`.

The scheme also has `match` patterns that refuse names GitHub does not allow. A branch name with `/`, such as `feature/x`, cannot be one segment. Name it with a selector, as in `code://octocat/Hello-World/master/README?at=branch:feature/x`. The env must still be there and match its pattern.

Resolve a `code` URI.

```
$ uv run provc --config $C resolve 'code://octocat/Hello-World/master/README'
{
  "logical": "code://octocat/Hello-World/master/README",
  "physical": "github://octocat/Hello-World/README?at=branch:master",
  "via": [
    "code template -> github://octocat/Hello-World/README?at=branch:master"
  ]
}
```

**What happened:** `TemplateResolver.resolve` matched the parts to names, made the branch from `env`, filled the template, and parsed the result as a URI. A selector such as `?at=tag:v1` replaces the branch. The `via` list records each step. The resolver percent-encodes every value before it fills the template. So a value can never add a path segment or a second `?at=`.

Every other command takes the logical URI too. `fetch` and `cat` resolve it first, then do what Step 1 showed.

```
$ uv run provc --config $C cat 'code://octocat/Hello-World/master/README'
Hello World!
```

## Step 3: Use tenants, stages and versions

The rest of the example configuration maps the logical schemes of the old provcat onto a new lakeFS layout. lakeFS is a store with git-like branches over S3. Its S3 gateway reads `s3://repository/branch-or-commit/path`. You do not need lakeFS for this step, because `resolve` does not read any data.

The org is the tenant. It is the first part of every logical URI, as the owner is on GitHub. lakeFS has no orgs, so each org gets its own repositories, and each environment is a branch:

```yaml
schemes:
  data+table:
    parts: [org, domain, env, family, name]
    target: "lakefs-{org}://{org}-data-{domain}/{ref}/tables/{family}/{name}"
    ref: "{env}"
    match:
      org: "[a-z0-9]{1,20}"
      env: "[a-z0-9][a-z0-9_-]{0,19}"
      domain: "[a-z0-9][a-z0-9-]{0,36}"
```

So `data+table://acme/shared/prod/agreements/qa_outputs` reads the repository `acme-data-shared` on the branch `prod`. The same names for the org `globex` read `globex-data-shared`. The ML schemes use repositories named `{org}-ml-{project}`.

In the lakeFS schemes, the `match` patterns refuse a name that lakeFS does not allow, or that could run into another name. They also keep each repository name within the 63 characters that lakeFS allows. An org has no `-`, so the first `-` in a repository name always ends the org. Two orgs never share a repository, a branch or a commit history.

A **stage** is a name, such as `production`, that points at a fixed version of one thing. The `stages` section of the configuration holds the pointers:

```yaml
stages:
  "data+table://acme/shared/prod/agreements/qa_outputs":
    production: "commit:b8a2f9c123456"
```

Resolve the stage.

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

**What happened:** the resolver looked up `production` for this URI and found `commit:b8a2f9c123456`. The commit filled `{ref}`, which is the second part of a lakeFS path. Without a selector, `{ref}` takes the default `{env}` from the scheme, which is the branch `prod` here. To move `production` to a new commit, change one line in the configuration.

A `version:` selector fills `{version}` instead, and `{ref}` keeps its default. The `model` scheme uses it.

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

The scheme `lakefs-acme` in both answers is a store, not a driver. The template names the store `lakefs-{org}`, so each org reads through its own store, with its own keys. The configuration defines the store for `acme` as the `lakefs` driver with the lakeFS endpoint:

```yaml
stores:
  lakefs-acme:
    driver: lakefs
    endpoint_url: ${oc.env:LAKEFS_S3_ENDPOINT,http://localhost:8000}
    profile: lakefs-acme
```

A logical URI of an org with no store fails. For example, `data+table://initech/...` fails with "there is no store named 'lakefs-initech'".

A **driver** is a class that reads one kind of storage. A **store** is a driver with options and a name. The name becomes a URI scheme. The `lakefs` driver is the S3 driver plus the lakeFS REST API, which turns a branch or a time into a commit. So `?at=ts:2026-10-01` on a lakeFS URI reads the branch as it was on that day.

## Step 4: Read from an S3-compatible server

Step 3 stopped at the physical URI. To read the data, provcat needs a server that speaks the S3 protocol. The tour uses moto, a local S3 server that the tests also use. The same commands work against lakeFS or MinIO with a different `endpoint_url`.

1. Start moto in a second terminal.

   ```
   uv run moto_server -p 5055
   ```

2. Give moto any keys. It does not check them.

   ```
   export AWS_ACCESS_KEY_ID=test AWS_SECRET_ACCESS_KEY=test
   ```

3. Make a bucket with two files.

   ```
   uv run python -c "
   import boto3
   s = boto3.client('s3', endpoint_url='http://localhost:5055', region_name='us-east-1')
   s.create_bucket(Bucket='demo')
   s.put_object(Bucket='demo', Key='main/data/a.csv', Body=b'x,y\n1,2\n')
   s.put_object(Bucket='demo', Key='main/data/b.csv', Body=b'x,y\n3,4\n')"
   ```

4. Write a configuration with one store, `local-s3`, in `/tmp/tour.yaml`.

   ```yaml
   stores:
     local-s3:
       driver: s3
       endpoint_url: http://localhost:5055
   ```

List the folder, then fetch it.

```
$ uv run provc --config /tmp/tour.yaml ls local-s3://demo/main/data
[
  {
    "uri": "local-s3://demo/main/data/a.csv",
    "kind": "file",
    "size": 8,
    "version": "344fd2323dd33d5c058b7cf27de029e8"
  },
  {
    "uri": "local-s3://demo/main/data/b.csv",
    "kind": "file",
    "size": 8,
    "version": "c28e7e30d1dd46f320e0c3c1bc967c77"
  }
]

$ uv run provc --config /tmp/tour.yaml fetch local-s3://demo/main/data
/tmp/provcat-tour/local-s3/demo/main/data
```

**What happened:** for S3, the version of a file is its ETag. An ETag is the fingerprint that S3 gives each version of an object. The fetch of a folder works in this order:

1. It asks S3 whether the path is a file or a folder.
2. It takes the lock of the folder, and removes the mark that says the folder is done.
3. It lists every object below the folder.
4. It downloads each file that the cache does not hold with the same ETag and the same bytes.
5. It deletes cached files whose objects are gone.
6. It marks the folder as done.

S3 has no commits, so an S3 path can change under the same name. Online, provcat checks the ETag before each use, and downloads the file again when the ETag changed. Offline, it uses the cached file without a check. Stop moto with Ctrl+C when you finish.

## Step 5: Work offline

The cache has readable paths, so a program in a container can read the files directly. To use provcat inside the container, mount the whole cache folder, `.provcat` included. Then run provcat with `--offline`. It reads only the cache.

```
$ uv run provc --offline --config $C fetch 'code://octocat/Hello-World/master/README'
/tmp/provcat-tour/github/octocat/Hello-World/7fd1a60b01f91b314f59955a4e4d4e80d8edf11d/README
```

**What happened:** offline, provcat cannot ask GitHub where `master` points. It read the ref record from Step 1 and used that commit. A branch that was never pinned fails with a clear error:

```
$ uv run provc --offline --config $C fetch 'code://octocat/Hello-World/test/README'
provc: github://octocat/Hello-World/README?at=branch:test: offline, and branch:test was never pinned
```

Do not mount only a part, such as `github/`. Offline provcat needs the records under `.provcat` to find a pinned commit and to trust a cached file.

Look at the cache to see every file that the tour wrote.

```
$ find /tmp/provcat-tour -type f
/tmp/provcat-tour/.provcat/meta/a32d46c2....json
/tmp/provcat-tour/.provcat/refs/2fe8388b....json
/tmp/provcat-tour/github/octocat/Hello-World/7fd1a60b01f91b314f59955a4e4d4e80d8edf11d/README
...
```

The records under `.provcat` have hashed names, so any key fits in a file name. The data files keep their real names.

## Step 6: Share the configuration through etcd

A team can keep one configuration in etcd, a key-value server, instead of a YAML file in each place. You need a running etcd for this step. Its default address is `localhost:2379`.

1. Copy the example configuration into etcd, under the prefix `/provcat`.

   ```
   uv run provc --config $C config push --to etcd://localhost:2379/provcat
   ```

2. Read it back.

   ```
   uv run provc --config etcd://localhost:2379/provcat config show
   ```

3. Use it with any command.

   ```
   uv run provc --config etcd://localhost:2379/provcat resolve 'model://acme/ml/prod/churn/xgb?at=version:3'
   ```

The push copies each `${oc.env:...}` value as written. Each reader fills the value in from its own environment, so one entry in etcd gives each person their own home folder. Do not give write access to the etcd prefix to people you do not trust. A value there can read the environment of every reader.

## Where each step lives in the code

| Step | Files |
|---|---|
| Parse a URI and its selector | `src/provcat/uri.py` |
| Read the configuration | `src/provcat/config.py` |
| Resolve a logical URI | `src/provcat/resolver.py` |
| Join the parts and pick the store | `src/provcat/catalog.py` |
| Read GitHub | `src/provcat/stores/github.py` |
| Read S3, lakeFS and MinIO | `src/provcat/stores/s3.py` |
| Read local files | `src/provcat/stores/local.py` |
| Keep files, records and locks | `src/provcat/cache.py` |
| The `provc` command | `src/provcat/cli.py` |

The tests in `tests/` are a second set of examples. They run without a network, except for two live tests that run only when you set an environment variable. The README lists them.

## Where to go next

- **Use provcat from Python.** `Catalog` has the methods `resolve`, `pin`, `stat`, `list`, `fetch` and `read`. `list` is `provc ls`, and `read` returns the bytes that `provc cat` prints. For example, `Catalog.open("config/provcat.example.yaml").fetch(uri)` returns a path.
- **Add a driver.** The section "Stores" in [design.md](design.md) lists the methods that a driver needs.
- **Learn the limits.** The section "Known limits" in [design.md](design.md) lists what does not work yet, and what to do about each limit.
