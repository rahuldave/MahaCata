# Setting up provcat and its services

This page lists the tools that provcat and its tests need, and how to set up each service. Read the [README](../README.md) first for the commands and the kinds of test.

## What each part needs

| Part | Needs |
|---|---|
| provcat itself | Python 3.13 and [uv](https://docs.astral.sh/uv/). |
| Unit tests | Nothing more. |
| Integration tests | Docker, for the GCS and Azure emulators. Without Docker, those tests skip. |
| lakeFS | The lakeFS server, and Postgres if you want its metadata in a database. |
| Shared configuration | An etcd server. |
| System tests | A login to each cloud you test, and the services above. |

## Where command-line tools are used

provcat reads every store through a Python SDK. It uses boto3 for S3 and lakeFS, `google-cloud-storage` for GCS, and `azure-storage-blob` for Azure. The system tests also make and delete their buckets, projects and accounts with Python SDKs. They make lakeFS repositories through the lakeFS REST API. Command-line tools have these jobs only:

| Tool | Job |
|---|---|
| `gh` | provcat asks `gh auth token` for a GitHub token when `GITHUB_TOKEN` and `GH_TOKEN` are not set. |
| `gcloud` | Logs you in to Google Cloud. The Python SDK then reads that login. |
| `az` | Logs you in to Azure. The Python SDK then reads that login. |
| `docker` | The integration tests start the GCS and Azure emulators with it. |
| `lakefs`, `etcd` | The servers themselves. |

You do not need the `aws` command. The AWS SDK reads `~/.aws/credentials` and `~/.aws/config`.

## Install and run lakeFS

lakeFS is a server that keeps git-like branches and commits over object storage. provcat reads it through its S3 gateway.

1. Build lakeFS 1.86.0 from source, as the next section shows.
2. Put `lakefs` on your `PATH`, or give its path to the script with `--lakefs`.
3. Choose how lakeFS keeps its metadata, as the next sections show.
4. Get the keys of an admin user.
5. Save the keys as an AWS profile named `lakefs`.

### Build lakeFS 1.86.0 from source

Use lakeFS 1.86.0, and no later version. It is the last release under the Apache 2.0 license, and the last with pluggable access control:

- **The license.** lakeFS 1.87.0 moved to the Business Source License 1.1, which is not an open-source license.
- **Access control.** lakeFS 1.87.0 removed the external authorization server and its reference server, the access-list server. With them, one lakeFS can keep each org to its own repositories. Since 1.77.0, a user who lists repositories sees only the ones their policies let them list. The filter works only with an external authorization server, not with the built-in one.

Build it from source, from the fork [rahuldave/lakeFS](https://github.com/rahuldave/lakeFS), branch `provcat`. The branch is v1.86.0 with our changes, which `PROVCAT.md` at the root of the fork lists. Without them, access control does not keep orgs apart, as the design document explains. You need Git, Node.js and Go 1.23 or later. Go downloads the Go 1.26.4 that lakeFS needs.

```
git clone --branch provcat --single-branch https://github.com/rahuldave/lakeFS.git ~/Projects/lakeFS
cd ~/Projects/lakeFS
export GOTOOLCHAIN=go1.26.4
make gen-ui gen-code
git checkout webui/package-lock.json
make build-binaries VERSION=1.86.0
(cd contrib/auth/acl && go build -o ../../../acl-server ./cmd/acl)
echo /acl-server >> .git/info/exclude
```

The build makes three programs in `~/Projects/lakeFS`:

- `lakefs`, the server.
- `lakectl`, its command-line client.
- `acl-server`, the reference access-list server.

Skip `make build` and `make gen`. They also build the clients for other languages, which needs Docker images that provcat does not use.

`make gen-ui` changes `webui/package-lock.json`. The `git checkout` puts it back before `make build-binaries`, which stamps the version. With any change in the working tree, the version ends in `.with.local.changes`. The `.gitignore` of lakeFS covers `lakefs` and `lakectl` but not `acl-server`, so the last line keeps it out of `git status`.

`make gen-code` downloads Go tools such as `goimports`, so it needs the network.

Check the version:

```
./lakefs --version
```

It prints `lakefs version 1.86.0-` and the short hash of the commit at the head of `provcat`. Each new commit on the branch changes the hash.

lakeFS 1.70.1 and 1.86.0 use the same database schema, version 4, so 1.86.0 opens a 1.70.1 database with no migration. Back up the database before the switch all the same, for example with `pg_dump`. To check a database, run `lakefs --config <file> migrate version`.

### Run lakeFS without a database

To try lakeFS, run it with its built-in settings. It keeps the metadata and the data on local disk.

```
lakefs run --quickstart
```

The quickstart makes its admin user with fixed example keys, which are the same in every lakeFS. Use them only on your own machine. Save them as shown in "Make keys and save them", and skip the `lakefs setup` step there.

```
access key: AKIAIOSFOLQUICKSTART
secret key: wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY
```

### Run lakeFS with Postgres

For lasting use, keep the metadata in Postgres.

1. Start Postgres in Docker, with its port on `127.0.0.1`. Choose your own password.

   ```
   docker run -d --name lakefs-postgres -e POSTGRES_PASSWORD=<password> -p 127.0.0.1:5432:5432 postgres:16
   ```

2. Write a lakeFS configuration file, for example `lakefs.yaml`. Replace the two values in angle brackets.

   ```yaml
   database:
     type: postgres
     postgres:
       connection_string: "postgresql://postgres:<password>@127.0.0.1:5432/postgres"
   auth:
     encrypt:
       secret_key: "<a long random string>"
   blockstore:
     type: local
     local:
       path: ~/lakefs/data
   ```

3. Wait until Postgres takes connections, and then start lakeFS. lakeFS stops at once if it cannot reach the database.

   ```
   lakefs run --config lakefs.yaml
   ```

Use the `127.0.0.1` port of the container, not its Docker host name. On OrbStack, a name such as `some-postgres.orb.local` can stop working while the container runs. A port on `127.0.0.1` keeps working.

### Start lakeFS with the script

After the first start, `scripts/lakefs_local.py` starts lakeFS again in one command. It needs these things:

- **A container.** The Postgres container from step 1 must exist. The script starts it, and does not make it.
- **The program.** `--lakefs` gives the `lakefs` program, as a path or as a name on your `PATH`. The default is `~/Projects/lakeFS/lakefs`, the build from source.
- **A folder.** `--lakefs-dir` gives the folder with the configuration, named `config.yaml`. lakeFS runs in this folder, so relative paths in the configuration start there. The default is `~/Catalog/lakefs`.
- **Relative paths.** A relative `--lakefs` or `--lakefs-dir` starts from the folder you run the script in.
- **The port.** Give the port that the container publishes on `127.0.0.1`.

```
uv run python scripts/lakefs_local.py --lakefs <program> --lakefs-dir <folder> --container lakefs-postgres --port 5432
```

The script waits until Postgres answers, and then runs lakeFS in the foreground. Ctrl+C, `kill` or closing the terminal asks lakeFS to stop, and waits up to 30 seconds for it. Then the script stops the container, but only if the script started it. The exit code is the exit code of lakeFS.

The script reads the connection string from `config.yaml`. It changes only the host and port, to `127.0.0.1` and the port of the container. It passes the result in the environment variable `LAKEFS_DATABASE_POSTGRES_CONNECTION_STRING`, so it never changes `config.yaml`. Its messages never show the connection string or a line of the file. If the password has a `/`, `?` or `#` that is not percent-encoded, the script stops and asks you to encode it.

### Run one lakeFS for many orgs

`scripts/lakefs_acl_local.py` runs lakeFS 1.86.0 with access control for each org. It runs two programs from the source build. The access-list server keeps users, groups and policies. lakeFS asks it who may do what. lakeFS keeps its metadata in a local database, so this needs no Postgres.

```
uv run python scripts/lakefs_acl_local.py
```

The first run makes the folder `~/Catalog/lakefs-acl`, and writes these files there:

- `acl.yaml` and `lakefs.yaml`, each with a new random secret key.
- `api-token`, the random token between lakeFS and the access-list server.
- The databases of both programs, and the lakeFS data, in the `data` folder.
- `credentials`, an AWS credentials file.

The credentials file has these profiles:

- `lakefs`, the admin, who reads every repository.
- `lakefs-acme` and `lakefs-globex`, the keys of each org.

Each org may do anything in the repositories whose names start with the org and a `-`, such as `acme-data-shared`, and nothing else. Give `--org` once for each org, to choose other orgs. A later run keeps the keys, and adds any org that is new.

lakeFS listens on `127.0.0.1:8002`, and the access-list server on `127.0.0.1:8011`. `--port` and `--acl-port` change them on the first run. Later runs must use the same ports. `--dir`, `--lakefs` and `--acl-server` change the folder and the two programs.

An org user cannot list, make or delete its own keys. Only the admin can, through the lakeFS API.

To use it with provcat and the example configuration:

```
export AWS_SHARED_CREDENTIALS_FILE=~/Catalog/lakefs-acl/credentials
export LAKEFS_S3_ENDPOINT=http://127.0.0.1:8002
```

If the admin locks itself out, for example by leaving the Admins group, run the script again with `--repair-admin`. With the setup running, it repairs it in place and exits. Either way it writes new admin keys to `credentials`, and detaches every policy from Admins except its own.

Setting `AWS_SHARED_CREDENTIALS_FILE` hides `~/.aws/credentials` from provcat in that shell, so the S3 stores lose their keys there. Use another shell for them, or copy the profiles into `~/.aws/credentials`.

Ctrl+C, `kill` or closing the terminal stops lakeFS, and then the access-list server.

The access-list server takes only calls that carry the token in `api-token`, in the same folder. lakeFS sends it, and so does the script. The server listens on `127.0.0.1` only. The design document lists the other limits of this route.

### Make keys and save them

With Postgres, make the first admin user. The command prints an access key and a secret key.

```
lakefs setup --config lakefs.yaml --user-name admin
```

Save them in `~/.aws/credentials`:

```
[lakefs]
aws_access_key_id = <access key>
aws_secret_access_key = <secret key>
```

The example configuration also has one store for each org, `lakefs-acme` and `lakefs-globex`, each with its own profile. To use them, add a profile with each org's keys:

```
[lakefs-acme]
aws_access_key_id = <acme access key>
aws_secret_access_key = <acme secret key>
```

provcat stops with "no AWS profile named 'lakefs-acme'" when the profile is missing.

The example provcat configuration has a store `lakefs` that uses this profile, with the endpoint `http://localhost:8000`. The environment variable `LAKEFS_S3_ENDPOINT` changes the endpoint.

A lakeFS repository has its own storage. When you delete a repository, lakeFS keeps that storage. With the local blockstore, delete the folder of the repository under the blockstore path.

## Run etcd

Install etcd, for example with `brew install etcd` on macOS. Then run it with a data folder of your choice.

```
etcd --data-dir ~/etcd-data
```

It listens on `localhost:2379`. Step 6 of the [tour](tour.md) shows how to copy a configuration into it.

## Pull the emulator images

The integration tests use one emulator for GCS and one for Azure. Pull both images once.

```
docker pull fsouza/fake-gcs-server:1.52.2
docker pull mcr.microsoft.com/azure-storage/azurite:3.35.0
```

The tests start and stop the containers. fake-gcs-server has two faults, and the tests skip the checks that meet them:

- **Folder markers.** It drops an object whose name ends in `/`.
- **Pages.** It returns only the first page when a listing asks for a page size.

The system tests check both on real GCS.

## Set up each cloud for the system tests

Each system test makes its own bucket and deletes it afterwards, even when the test fails. The README lists the environment variable for each service.

### AWS

Put your keys in `~/.aws/credentials`, or use a profile with `PROVCAT_SYSTEM_AWS_PROFILE`. The tests make buckets named `provcat-test-...` in `us-east-1`.

### Google Cloud

1. Log in, and give the Python SDK your login.

   ```
   gcloud auth login
   gcloud auth application-default login
   ```

2. Make a project for the tests. The script makes the project, links it to a billing account, and turns on the Cloud Storage API. It uses the Google REST APIs, not `gcloud`.

   ```
   uv run python scripts/gcs_test_project.py --billing-from <a project that has billing>
   ```

3. Run the tests with the project id that the script prints.

   ```
   PROVCAT_SYSTEM_GCS=<project id> uv run pytest -m system
   ```

The project holds no data between runs. GCS can keep deleted objects for a few days under its soft-delete setting, which costs a little. To remove the project, run the script with `--delete <project id>`. If a step fails after the project exists, run the script again with `--project <project id>` to finish its setup.

### Azure

1. Install the Azure test packages.

   ```
   uv sync --group system
   ```

2. Log in to your tenant.

   ```
   az login --tenant <tenant id>
   ```

3. Run the tests with your subscription id. The tests make one resource group with one ADLS Gen2 storage account, and delete the group at the end.

   ```
   PROVCAT_SYSTEM_AZURE=<subscription id> uv run pytest -m system
   ```

If `az` says that the request is malformed, the saved login is stale. Run `az account clear`, and log in again.

### lakeFS

Start lakeFS and save its keys as the profile `lakefs`, as above. The tests make repositories named `provcat-test-...` with the local blockstore, and delete them at the end. lakeFS keeps the storage folder of each one. Set `PROVCAT_SYSTEM_LAKEFS_DATA` to the blockstore path, and the tests delete those folders too. They delete only a folder that they made, inside that path.

```
PROVCAT_SYSTEM_LAKEFS=http://localhost:8000 PROVCAT_SYSTEM_LAKEFS_DATA=~/lakefs/data uv run pytest -m system
```

To test isolation between orgs, run a second lakeFS:

1. Give it its own configuration file, with its own port, for example `listen_address: "127.0.0.1:8001"`. Give it its own metadata, in a local database folder or its own Postgres database. Give it its own blockstore path.
2. Start it with `lakefs run --config <file>`. Do not use `--quickstart` for either server. Every quickstart has the same fixed keys, so each server would accept the other's keys.
3. Make its admin user through its setup API. `lakefs setup` cannot run while a server with a local database runs.

   ```
   curl -X POST -H 'Content-Type: application/json' -d '{"username": "admin"}' http://127.0.0.1:8001/api/v1/setup_lakefs
   ```

4. Save the access key and secret key from the reply as the profile `lakefs-b` in `~/.aws/credentials`.
5. Set `PROVCAT_SYSTEM_LAKEFS_B=http://127.0.0.1:8001`, and `PROVCAT_SYSTEM_LAKEFS_B_DATA` to its blockstore path.

`PROVCAT_SYSTEM_LAKEFS_PROFILE` and `PROVCAT_SYSTEM_LAKEFS_B_PROFILE` change the two profile names, which are `lakefs` and `lakefs-b` by default.

To test isolation on one lakeFS with access control, start `scripts/lakefs_acl_local.py`, and set five variables:

```
PROVCAT_SYSTEM_LAKEFS_ACL=http://127.0.0.1:8002 \
PROVCAT_SYSTEM_LAKEFS_ACL_SERVER=http://127.0.0.1:8011 \
PROVCAT_SYSTEM_LAKEFS_ACL_DIR=~/Catalog/lakefs-acl \
PROVCAT_SYSTEM_LAKEFS_ACL_CREDENTIALS=~/Catalog/lakefs-acl/credentials \
PROVCAT_SYSTEM_LAKEFS_ACL_DATA=~/Catalog/lakefs-acl/data \
uv run pytest -m system tests/system/test_lakefs_acl.py
```

The test makes repositories named `acme-provcat-test-...` and `globex-provcat-test-...` with each org's keys, and deletes them at the end.
