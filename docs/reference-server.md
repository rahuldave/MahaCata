# The reference server

The reference server is one lakeFS that keeps several orgs apart. Each org reads and writes only its own repositories. provcat uses it as the lakeFS behind the logical schemes, with one store and one set of keys for each org.

It is a reference implementation. It shows that one lakeFS can keep orgs apart, and how. It runs on one machine, for development and tests.

## The parts

Two programs run side by side. Both are built from the fork [rahuldave/lakeFS](https://github.com/rahuldave/lakeFS), branch `provcat`.

```
  provcat (acme's keys)        provcat (globex's keys)
           |                            |
           v                            v
  +------------------------------------------------+
  | lakeFS 1.86.0, patched        127.0.0.1:8002   |
  |   S3 gateway and REST API                      |
  |   data, commits and branches in <dir>/data     |
  +------------------------------------------------+
           |  "may this user do this?"  (Bearer token)
           v
  +------------------------------------------------+
  | access-list server, patched   127.0.0.1:8011   |
  |   users, groups, policies and keys             |
  +------------------------------------------------+
```

- **lakeFS** holds the data, the commits and the branches. It runs with `auth.ui_config.rbac: external`, so it asks another server who may do what.
- **The access-list server** is the reference authorization server that lakeFS ships in `contrib/auth/acl`. It keeps the users, groups, policies and keys. lakeFS fetches each user's policies from it, and checks every call against them.
- **`packages/provcat/scripts/lakefs_acl_local.py`** in this project starts both, sets up the orgs, and stops both.

### Where the data lives

lakeFS keeps the objects on local disk, in `<dir>/data`, with `blockstore.type: local`. It is not in front of S3. provcat reaches the data only through lakeFS. It uses the S3-compatible gateway for objects, and the REST API for commits and branches.

lakeFS can keep the objects in S3, ADLS Gen2 or GCS instead. That takes a `blockstore` section with the bucket in `lakefs.yaml`, and the org policies' namespace rule then names that bucket, not `local://`. provcat would not change, because it talks only to lakeFS. The reference server does not do this yet.

Without lakeFS, provcat reads and writes S3, ADLS Gen2 and GCS buckets directly. A plain bucket has no branches, commits or pins, and the cloud's own access control keeps orgs apart.

### Why lakeFS 1.86.0, patched

lakeFS 1.87.0 removed the external authorization server and the access-list server. It also moved to the Business Source License 1.1, which is not an open-source license. So the reference server stays on 1.86.0, the last Apache 2.0 release, built from source so it can be changed.

Stock 1.86.0 did not keep orgs apart. The fork fixes that. `PROVCAT.md` at the root of the fork lists every change, why we made it, and its test. In short:

- **lakeFS:**
  - staging and linking can use storage outside the repository only with `fs:ImportFromStorage` on it, which no org has;
  - storage names cannot use `.` or `..` segments, or `@`, `?` or `#`;
  - meta-range IDs are plain names.
- **The access-list server:**
  - callers need a token;
  - the policy calls work;
  - deleting a user or a group takes its keys or its policy with it;
  - the Admins policy is guarded.

## Start and stop it

Build the fork first, as `docs/setup.md` explains under "Build lakeFS 1.86.0 from source". Then start the server:

```bash
uv run python packages/provcat/scripts/lakefs_acl_local.py
```

It runs in the foreground. Ctrl+C, `kill` or closing the terminal stops lakeFS, and then the access-list server. Start it again with the same command.

When it is ready, it prints two lines:

```
lakeFS on http://127.0.0.1:8002, with access control for acme, globex
export AWS_SHARED_CREDENTIALS_FILE=/Users/<you>/Catalog/lakefs-acl/credentials
```

The script has these options:

| Option | Default | What it sets |
|---|---|---|
| `--dir` | `~/Catalog/lakefs-acl` | The folder for the configs, the databases and the data. |
| `--lakefs` | `~/Projects/lakeFS/lakefs` | The lakeFS program. |
| `--acl-server` | `~/Projects/lakeFS/acl-server` | The access-list server program. |
| `--port` | `8002` | The lakeFS port on `127.0.0.1`. |
| `--acl-port` | `8011` | The access-list server port on `127.0.0.1`. |
| `--org` | `acme` and `globex` | An org to set up. Give it once for each org. |
| `--repair-admin` | off | Puts the admin back after a lockout, as "Repair the admin" explains. |
| `--wait` | `60` | Seconds to wait for each program to start. |
| `--cache-seconds` | `25` | After a repair, how long lakeFS may still answer from its policy cache. |
| `--admin-wait` | `90` | After a repair, how long to wait for lakeFS to take the new admin keys. |

The ports are fixed by the first run, because the config files hold them. A later run with other ports is refused. A port that something else already uses is refused too.

## The folder

The first run makes the folder and writes everything into it. Later runs reuse it.

| Path | Mode | What it holds |
|---|---|---|
| `lakefs.yaml` | 0600 | The lakeFS config, with a random secret key. |
| `acl.yaml` | 0600 | The access-list server config, with its own random secret key. |
| `api-token` | 0600 | The random token between lakeFS and the access-list server. |
| `credentials` | 0600 | An AWS credentials file with every key, as "Users and keys" explains. |
| `lakefs-db/` | 0700 | The lakeFS metadata: repositories, branches and commits. |
| `acl-db/` | 0700 | The access-list server's users, groups, policies and keys. |
| `data/` | 0700 | The objects of every repository, one folder for each. |
| `cache/` | | The lakeFS commit cache, kept apart from every other lakeFS on the machine. |

To start over, stop the server and delete the folder. The next run makes new keys for everyone.

The script passes four settings to the programs in the environment, not in the config files. So an older folder gets them too.

| Variable | To | What it sets |
|---|---|---|
| `ACLSERVER_API_TOKEN` | access-list server | The token every call must carry. |
| `ACLSERVER_CACHE_ENABLED=false` | access-list server | No cache. Its cache made a deleted user or key look present for 20 seconds. |
| `LAKEFS_AUTH_API_TOKEN` | lakeFS | The token lakeFS sends to the access-list server. |
| `LAKEFS_COMMITTED_LOCAL_CACHE_DIR` | lakeFS | Its own commit cache, in `<dir>/cache`. |

## Orgs, users and keys

Each org gets four things:

- **A group,** named after the org, for example `acme`.
- **A user,** `<org>-user`, in that group.
- **Keys** for that user, saved in `credentials` as the profile `lakefs-<org>`.
- **A policy,** `<org>-repos`, attached to the group.

The policy for acme is this:

```json
{"id": "acme-repos", "statement": [
  {"effect": "allow", "action": ["fs:*"],
   "resource": "arn:lakefs:fs:::repository/acme-*"},
  {"effect": "allow", "action": ["fs:AttachStorageNamespace"],
   "resource": "arn:lakefs:fs:::namespace/local://acme-*"}
]}
```

So acme can do anything in a repository whose name starts with `acme-`, and can make one only on storage named `local://acme-...`. An org name is 1 to 20 of `a-z` and `0-9`, with no `-`. So `acme-*` never matches another org's repositories.

The admin is the user `admin`, in the group `Admins`. Its keys are the profile `lakefs`. The admin can do everything, in every repository.

### What each org can and cannot do

| | Own repositories | Another org's repositories |
|---|---|---|
| Read, list, write and delete objects | Yes | Refused |
| Commit, branch, tag and merge | Yes | Refused |
| Make a repository | Yes, named `<org>-...` | Refused |
| List repositories | Sees only its own | Not shown |
| Manage users, groups, policies or keys | Refused | Refused |

The system test `packages/provcat/tests/system/test_lakefs_acl.py` checks reading, writing, committing, branching, merging, making and listing repositories, through the REST API, the S3 gateway and provcat. Tags follow from the same policy, but no test runs them.

### Add an org

Stop the server, then start it with every org you want, the old ones too:

```bash
uv run python packages/provcat/scripts/lakefs_acl_local.py --org acme --org globex --org initech
```

A run only adds. It keeps the keys of the orgs that already exist, and prints the new ones as `(new: initech)`. It also writes each org's policy again, so a change to the policy in the script reaches an existing server.

Each run also checks the saved key of every org it is given with `--org`. If that key no longer works, the run deletes all of the org's keys and makes a new one.

## Use it with provcat

Point provcat at the server and its keys:

```bash
export AWS_SHARED_CREDENTIALS_FILE=~/Catalog/lakefs-acl/credentials
```

```bash
export LAKEFS_S3_ENDPOINT=http://127.0.0.1:8002
```

The example configuration, `packages/provcat/config/provcat.example.yaml`, then works as it is. Its store `lakefs-acme` uses the profile `lakefs-acme`, and `lakefs-globex` uses `lakefs-globex`. The logical schemes pick the store from the org in the URI, so `data+table://acme/...` reads and writes with acme's keys only. The store `lakefs` uses the admin's keys.

Give each org a configuration with only its own store, and only its own profile in its credentials file. The mapping in one shared configuration routes requests; the keys are what keep orgs apart. `docs/design.md` explains this under "Tenants".

`AWS_SHARED_CREDENTIALS_FILE` hides `~/.aws/credentials` in that shell. So the plain S3 stores lose their keys there. Use another shell for them, or copy the profiles into `~/.aws/credentials`.

## Run it as the admin

The admin manages the server through the lakeFS REST API, with the keys of the profile `lakefs`. lakeFS passes the calls about users, groups and policies on to the access-list server. These calls are the useful ones:

| Task | Call |
|---|---|
| List users | `GET /api/v1/auth/users` |
| List an org's keys | `GET /api/v1/auth/users/acme-user/credentials` |
| Make a new key for an org | `POST /api/v1/auth/users/acme-user/credentials` |
| Delete a key | `DELETE /api/v1/auth/users/acme-user/credentials/<access key id>` |
| List policies | `GET /api/v1/auth/policies` |
| Show a group's policies | `GET /api/v1/auth/groups/acme/policies` |
| Take an org's access away | `DELETE /api/v1/auth/groups/acme/policies/acme-repos` |
| Give it back | `PUT /api/v1/auth/groups/acme/policies/acme-repos` |

For example, with `curl` and the admin keys from the profile:

```bash
curl -u "$ADMIN_KEY:$ADMIN_SECRET" http://127.0.0.1:8002/api/v1/auth/users
```

An org's user cannot list, make or delete its own keys. Only the admin can.

A change to a policy can take up to about 23 seconds to apply, because lakeFS caches each user's policies.

### Guards on the admin

The fork refuses calls that would leave the admin unable to do anything:

- deleting the `Admins` group;
- deleting the `ACL(_-_)` policy of a group that exists, or detaching it from its own group;
- making an `ACL(_-_)` policy for a group that does not exist;
- writing anything into `ACL(_-_)Admins` but the full-access statement.

Each refusal answers 400.

### Repair the admin

The admin can still lock itself out in other ways:

- leave the `Admins` group;
- join a group with a deny policy;
- attach a deny policy to itself or to `Admins`;
- delete its own keys or its own user.

To undo any of these, run the script with `--repair-admin`:

```bash
uv run python packages/provcat/scripts/lakefs_acl_local.py --repair-admin
```

With the server running, both ports in use, it repairs it in place and exits. Otherwise it starts the server and repairs it first. It works through the access-list server and the token, not through lakeFS, so a locked-out admin does not stop it. It does the same steps every time:

1. It makes the `Admins` group again if it is missing. It writes the `Admins` policy back to its full-access statement, and detaches every other policy from `Admins`, also ones that only allow.
2. It puts the admin back in `Admins`, takes it out of every other group, and detaches the policies on the admin itself.
3. It deletes the admin's keys, makes a new key, and saves it in `credentials`.
4. It waits until lakeFS takes the new key for policies and for repositories, after lakeFS's policy cache has expired.

It prints what it changed, for example `repaired the admin: put the admin back in Admins; made new admin keys`. It exits 1 if lakeFS still refuses the new key after `--admin-wait` seconds.

## What it protects, and what it does not

It protects each org's data from the other orgs:

- **Org keys.** An org's keys reach only that org's repositories. That covers objects, commits, branches, staged and linked addresses, storage names and meta-range IDs.
- **The access-list server.** It takes only calls that carry the token, and it listens on `127.0.0.1` only.
- **The secrets.** The config files, the token and the keys are readable by your user only.

It does not protect against these:

- **Your own user account.** Anyone who can read `<dir>` has the admin keys and the token.
- **Other machines.** Both programs listen on `127.0.0.1`. Do not expose them on a network without TLS and a firewall.
- **Revocation at once.** lakeFS caches each user's policies, so a revoked policy can work for up to about 23 seconds.
- **Server paths.** Error messages and stat replies show absolute paths on the server.
- **Every admin mistake.** The guards cover the admin's own policy, and `--repair-admin` undoes the rest. They do not stop the admin from deleting an org's data.
- **A rare race.** If a policy is deleted and made again at the very moment it is attached, a stale link can survive. It has not been reproduced.

## Test it

Integration tests run the script against fake servers, in about 40 seconds:

```bash
uv run pytest packages/provcat/tests/integration/test_lakefs_acl_local.py
```

The system tests run against the real server. Start it first, then:

```bash
PROVCAT_SYSTEM_LAKEFS_ACL=http://127.0.0.1:8002 \
PROVCAT_SYSTEM_LAKEFS_ACL_SERVER=http://127.0.0.1:8011 \
PROVCAT_SYSTEM_LAKEFS_ACL_DIR=~/Catalog/lakefs-acl \
PROVCAT_SYSTEM_LAKEFS_ACL_CREDENTIALS=~/Catalog/lakefs-acl/credentials \
PROVCAT_SYSTEM_LAKEFS_ACL_DATA=~/Catalog/lakefs-acl/data \
uv run pytest -m system packages/provcat/tests/system/test_lakefs_acl.py
```

They take about 3 minutes, most of it waiting out lakeFS's policy cache after each repair. They make repositories named `acme-provcat-test-...` and `globex-provcat-test-...`, and delete them at the end. The repair tests lock the admin out and repair it, so they change the admin's keys in `credentials`.

The fork has its own Go tests. From `~/Projects/lakeFS`:

```bash
GOTOOLCHAIN=go1.26.4 go test ./pkg/api ./pkg/graveler/... ./pkg/catalog/... ./pkg/pyramid/... ./contrib/auth/...
```

## When something goes wrong

| What you see | Why | What to do |
|---|---|---|
| `no program at .../lakefs` | The fork is not built. | Build it, as `docs/setup.md` explains. |
| `something already listens on 127.0.0.1:8002` | Another lakeFS, or an earlier run, holds the port. | Stop it. `lsof -i :8002` shows what holds it. |
| `... was made with other ports` | The ports differ from the first run. | Use the first run's ports, or edit `listen_address` and `endpoint` in the config files. |
| `... takes calls without the token` | The access-list server is a stock build. | Build `acl-server` from the fork. |
| `api-token ... can be read by other users` | Its mode changed. | `chmod 600 ~/Catalog/lakefs-acl/api-token`. |
| An org's keys get `insufficient permissions` | The org may not do this, or a policy changed less than 23 seconds ago. | Check the group's policies as the admin. |
| The admin gets 401 everywhere | The admin locked itself out. | Run with `--repair-admin`. |
| `lakeFS still refuses the new admin keys` | lakeFS did not take the new key in time. | Look at the lakeFS output, then run `--repair-admin` again. |

The lakeFS banner says to complete setup at `127.0.0.1:8000`. That line is stock lakeFS text and does not apply. The script has set everything up.

## Where to read more

- **The fork's changes:** `PROVCAT.md` at the root of [rahuldave/lakeFS](https://github.com/rahuldave/lakeFS/blob/provcat/PROVCAT.md).
- **Building lakeFS:** the section "Build lakeFS 1.86.0 from source" in `docs/setup.md`.
- **Tenancy:** the section "Tenants" in `docs/design.md`, with the holes in stock lakeFS.
- **Writes through provcat:** the section "Writes" in `docs/design.md`.
- **The script itself:** `packages/provcat/scripts/lakefs_acl_local.py`.
