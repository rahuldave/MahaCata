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

1. Download the lakeFS binary for your machine from the [lakeFS releases](https://github.com/treeverse/lakeFS/releases).
2. Unpack it, and put `lakefs` on your `PATH`.
3. Choose how lakeFS keeps its metadata, as the next sections show.
4. Get the keys of an admin user.
5. Save the keys as an AWS profile named `lakefs`.

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

The example provcat configuration has a store `lakefs` that uses this profile, with the endpoint `http://localhost:8000`. The environment variable `LAKEFS_S3_ENDPOINT` changes the endpoint.

A lakeFS repository has its own storage. When you delete a repository, lakeFS keeps that storage. With the local blockstore, delete the folder of the repository under the blockstore path by hand.

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

Start lakeFS and save its keys as the profile `lakefs`, as above. The tests make repositories named `provcat-test-...` with the local blockstore, and delete them at the end. lakeFS keeps their storage folders, so delete those by hand.

```
PROVCAT_SYSTEM_LAKEFS=http://localhost:8000 uv run pytest -m system
```
