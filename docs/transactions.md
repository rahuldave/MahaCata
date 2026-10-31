# Transactional guarantees in lakeFS and in provcat's direct writes

This page answers what a write and a commit guarantee in lakeFS, in provcat's direct writes to buckets, and in four other systems. It ends with what provcat's commits on versioned buckets guarantee, and why they are built as they are. The research was done on 30 October 2026.

## How to read the marks

Each claim carries one mark:

- **[src]** means that the claim was read in source code, at the file and line given.
- **[src]** paths in lakeFS are in the fork, branch `provcat`, at `~/Projects/lakeFS`.
- **[doc]** means that a research agent read the claim on the web page given, on 30 October 2026.
- **[not verified]** means that no source was found, or that the claim comes from reading code, with no test.

Terms used in this report:

- **Commit point.** The commit point is the one write that makes a commit visible. Before that write, readers see the old state. After it, readers see the new state.
- **Compare-and-set (CAS).** A CAS write succeeds only if the stored value is still the value that the writer read before. Otherwise it fails, and the writer retries.
- **Conditional write.** A conditional write is a bucket's form of CAS. The request carries a precondition, for example "the object must not exist", and the bucket refuses the write with HTTP 412 if the precondition is false.
- **Snapshot.** A snapshot is a set of file versions that all existed together at one moment.

## Summary

- Every bucket platform gives atomic writes of one object, and conditional writes on one object. No platform gives an atomic write of many objects.
- lakeFS does not use bucket conditional writes for its commits. Its commit point is a CAS on the branch record in its key-value store (KV store), which is Postgres, DynamoDB or a local Badger database.
- A lakeFS commit is all or nothing. Writes that race a commit are never lost, but a commit can hold writes that arrived after the commit call started.
- provcat's direct `put` and `delete` write one object at a time, with no conditions. The last writer wins.
- provcat's commits on versioned buckets take their file list from two equal listings, so the list is a snapshot. They number each folder's commits with a put-if-absent write, as Delta Lake does. The last section has the details.

## Bucket primitives

The table gives what each platform offers today.

| Primitive | AWS S3 | Google Cloud Storage (GCS) | Azure Blob |
|---|---|---|---|
| Strong read-after-write, also for list | Yes, since 1 Dec 2020 [doc] | Yes, list is strongly consistent [doc] | Yes [not verified in this session] |
| Create only if absent | `If-None-Match: *`, since 20 Aug 2024 [doc] | `ifGenerationMatch=0` [doc] | `If-None-Match: *` [not verified in this session] |
| Replace only if unchanged | `If-Match: <ETag>`, since 25 Nov 2024 [doc] | `ifGenerationMatch=<n>`, `ifMetagenerationMatch=<n>` [doc] | `If-Match: <ETag>` [not verified in this session] |
| Delete only if unchanged | `If-Match` on DeleteObject and DeleteObjects, general purpose buckets since Sept 2025 [doc] | `ifGenerationMatch` on delete [not verified] | `If-Match` on Delete Blob [not verified] |
| Enforce conditions by policy | `s3:if-match`, `s3:if-none-match` condition keys [doc] | No [not verified] | No [not verified] |
| Locks | No | No | Blob leases, 15 to 60 seconds or infinite [not verified in this session] |
| Atomic write of many objects | No | No | No |

### Notes on S3

- **Multipart uploads.** `If-None-Match` and `If-Match` work on CompleteMultipartUpload. An upload in progress does not count as an existing object. After a 412, the whole multipart upload must start again. [doc] https://docs.aws.amazon.com/AmazonS3/latest/userguide/conditional-writes.html
- **Versioned buckets.** `If-None-Match` checks only the current version of a key. If the current version is a delete marker, the write succeeds. [doc] Same page.
- **Announcements.** Consistency: https://aws.amazon.com/about-aws/whats-new/2020/12/amazon-s3-now-delivers-strong-read-after-write-consistency-automatically-for-all-applications/. If-None-Match: https://aws.amazon.com/about-aws/whats-new/2024/08/amazon-s3-conditional-writes. If-Match: https://aws.amazon.com/about-aws/whats-new/2024/11/amazon-s3-functionality-conditional-writes. Conditional deletes: https://docs.aws.amazon.com/AmazonS3/latest/userguide/conditional-deletes.html [doc]

### S3-compatible servers

- **MinIO.** MinIO supports `If-Match` and `If-None-Match` on uploads since PR 16551, merged 7 Feb 2023. Fixes for multipart uploads came in Sept 2025. An open issue, 19442, says a matching ETag returns 304 and not 412. [doc] https://github.com/minio/minio/pull/16551
- **moto.** moto honours `If-None-Match` on `put_object` from 5.0.15, and on multipart completion from 5.0.23. It honours `If-Match` on `put_object` from 5.1.5, and on `delete_object` from 5.1.22. [doc] https://github.com/getmoto/moto/blob/master/CHANGELOG.md
- **lakeFS S3 gateway.** The gateway accepts `If-None-Match: *` on PutObject. It answers `If-Match` with "not implemented". [src] `~/Projects/lakeFS/pkg/api/apiutil/conditions.go:16-36`, `pkg/gateway/operations/putobject.go:332-358`

### GCS

GCS gives every object a generation number, which changes on every write, and a metageneration, which changes on every metadata change. A request with `ifGenerationMatch=0` proceeds only if no live object has the name. Noncurrent versions do not block it. The XML API uses `x-goog-if-generation-match`. [doc] https://docs.cloud.google.com/storage/docs/request-preconditions

### Azure Blob and ADLS Gen2

Azure Blob takes `If-Match`, `If-None-Match`, `If-Modified-Since` and `If-Unmodified-Since` on writes, and gives leases on blobs. I did not open these pages in this session, so the details are [not verified]. The pages are https://learn.microsoft.com/rest/api/storageservices/specifying-conditional-headers-for-blob-service-operations and https://learn.microsoft.com/rest/api/storageservices/lease-blob.

**Blob versioning does not work on accounts with a hierarchical namespace (ADLS Gen2).** The versioning overview, dated 2 Oct 2026, says that such accounts "aren't currently supported". The feature support page marks blob versioning "Not yet supported" for hierarchical namespace accounts. [doc] https://learn.microsoft.com/en-us/azure/storage/blobs/versioning-overview and https://learn.microsoft.com/en-us/azure/storage/blobs/storage-feature-support-in-storage-accounts

### What no platform gives

No platform writes or deletes two objects as one atomic step. Every system below therefore builds a multi-object commit in the same way. It writes immutable data objects first, and then it makes one atomic single-key write, the commit point, that names them.

## How lakeFS makes a commit atomic

The source is `~/Projects/lakeFS`, branch `provcat`. All paths below are relative to it.

### Where the branch pointer lives

A branch is one record in the KV store, not an object in the bucket. Its key is `branches/<name>` in the partition of the repository. [src] `pkg/graveler/model.go:43-61`. The record holds the commit ID, the current staging token and the list of sealed staging tokens. [src] `pkg/graveler/graveler.go:555-556`

Every change to a branch goes through `BranchUpdate`. It reads the record together with a predicate, applies a function, and writes the result with `SetMsgIf`. [src] `pkg/graveler/ref/manager.go:452-483`. The predicate is the bytes of the record that it read, so the write is a CAS on the whole record. [src] `pkg/kv/store.go:96-100`

Each KV driver implements `SetIf` as an atomic CAS:

- **Postgres.** `UPDATE ... WHERE key=$2 AND partition_key=$1 AND value=$4`, and a failure if no row changed. A create uses `INSERT ... ON CONFLICT DO NOTHING`. [src] `pkg/kv/postgres/store.go:268-299`
- **DynamoDB.** `PutItem` with a condition expression `ItemValue = :predicate`, or `attribute_not_exists` for a create. Reads use `ConsistentRead`. [src] `pkg/kv/dynamodb/store.go:241`, `277-330`
- **Local (Badger).** A read and a compare inside one Badger transaction. [src] `pkg/kv/local/store.go:106-150`

`retryBranchUpdate` retries a failed CAS with backoff, up to 10 times. [src] `pkg/graveler/graveler.go:54`, `2457-2478`

### The steps of a commit

`Graveler.Commit` runs these steps. [src] `pkg/graveler/graveler.go:2255-2420`

1. **Seal.** A first CAS moves the current staging token to the front of the sealed list. It also gives the branch a new, empty staging token (lines 2318-2335). New writes go to the new token from this moment.
2. **Build the tree.** A second, retried CAS update reads the branch again. It merges the sealed tokens into the parent commit's metarange, and writes new ranges and a new metarange to the bucket (lines 2337-2390).
3. **Record the commit.** It writes the commit record to the KV store under its content hash, with "create if absent". A duplicate is not an error, because the same hash means the same commit. [src] `pkg/graveler/ref/manager.go:623-633`
4. **Move the branch.** The function returns the branch with the new commit ID and an empty sealed list. `BranchUpdate` writes it with CAS. **This CAS is the commit point.**
5. **Clean up.** It drops the sealed tokens' staged entries (line 2414).

### Ranges and metaranges in the bucket

Ranges and metaranges are SSTable files, named by the SHA-256 of their contents. [src] `pkg/graveler/sstable/writer.go:129-153`, `pkg/catalog/catalog.go:61`. The tier file system uploads each one with a plain `adapter.Put` and no condition. [src] `pkg/pyramid/tier_fs.go:207-232`. A name is a hash of the contents, so two writers of one name write the same bytes, and a condition adds nothing.

User data objects get a new, unique physical path on every upload, built from an `xid`. lakeFS therefore never overwrites a data object. [src] `pkg/upload/path_provider.go:102-106`, `pkg/gateway/operations/putobject.go:364`

I found no use of bucket conditional writes in the block adapters, the tier file system or graveler. A search for `IfNoneMatch`, `IfMatch` and `IfGenerationMatch` under `pkg/block`, `pkg/pyramid` and `pkg/graveler` found nothing. [src]

### Writes that race a commit

An object write calls `safeBranchWrite`. It reads the branch, writes the entry to that staging token, and reads the branch again. If the staging token changed in between, it writes the entry again to the new token, up to a fixed number of tries. [src] `pkg/graveler/graveler.go:1952-1995`

The result is that a racing write is never lost:

- If the commit's iterator read the entry in the sealed token, the commit holds it. The entry also sits in the new staging token, with the same value, and the next commit sees no change.
- If the iterator did not read it, the entry is in the new staging token, and the next commit holds it.

Also, two commits can merge. Suppose commit A seals token T0, and commit B then seals T1 before A builds its tree. A's step 2 reads both sealed tokens and commits both. B then finds no sealed tokens and fails with "no changes". [src, by reading `graveler.go:2329` and `2382-2407`, not tested]. A commit therefore holds every write that was staged before its last branch read in step 2. It is not limited to the writes before the commit call.

### What a reader gets

- **A commit is all or nothing.** A reader of a branch sees the old commit ID or the new one, because one KV record holds it. A commit ID names an immutable metarange, so a read of a commit ID is a snapshot. [src]
- **Uncommitted reads.** A read of a branch merges the staging token, the sealed tokens and the committed metarange. [src] `graveler.go:1784`, `2192-2215`. A write is visible to every reader of the branch at once, with no isolation between writers on one branch.
- **No snapshot across keys before a commit.** A listing of a branch during writes can show one new file and one old file. The branch is the unit of isolation. Readers who need a fixed state read a commit ID. [not verified by test]
- **Crash before the commit point.** Suppose lakeFS stops after it writes ranges, a metarange or a commit record, but before the branch CAS. Then the branch still points to the old commit. The sealed tokens stay on the branch, so the next commit includes their writes. The files and the record that it wrote are orphans. [src, by reading]. I did not check whether lakeFS garbage collection removes orphan ranges. [not verified]
- **The KV store and the bucket cannot disagree in a harmful direction.** The branch moves only after its metarange exists. A metarange without a branch is garbage, not an error. A branch that names a missing metarange would need someone to delete it from the bucket by hand. [src, by reading]

### A possible bug in conditional puts

`If-None-Match: *` runs through `handleUpdate`. It checks the condition against the branch, and writes the staging entry with CAS. [src] `graveler.go:1918-1949`, `pkg/graveler/staging/manager.go:117-136`. Suppose a commit seals the token between the first write and the token check. Then `safeBranchWrite` runs the update again on the new token. The second check finds the entry from the first try in the sealed token, and fails. The client gets 412, but its object is written. [not verified, from reading only]

### lakeFS documentation

The lakeFS internals page says that references and uncommitted metadata live in the KV store. It also says that a commit uses optimistic locking, with a CAS on the branch at the end. The reason is that the KV interface has no transactions. [doc] https://docs.lakefs.io/concepts/internals/ The design of the KV store is at https://github.com/treeverse/lakeFS/blob/master/design/accepted/metadata_kv/index.md [doc]

## Other systems

### SlateDB

SlateDB keeps its state in a manifest in the bucket. Manifests are numbered files. A client reads the newest one and writes the next number with a conditional put, so the conditional put is the commit point. A writer raises `writer_epoch` in the manifest when it starts, and a writer with a lower epoch is a "zombie" that must stop. A new writer fences old ones by writing to the next slot of the write-ahead log. The first design used DynamoDB for the CAS on S3, because S3 had no conditional writes then. [doc] https://github.com/slatedb/slatedb/blob/main/rfcs/0001-manifest.md

### Project Nessie

Nessie keeps commits and references in a database, not in the bucket. The backends include PostgreSQL, MySQL, MariaDB, DynamoDB, Bigtable, Cassandra and RocksDB. The commit point is `Persist.updateReferencePointer`, a CAS on the reference in that database. The bucket holds only immutable table files, which Iceberg or Delta writes.

Sources [doc]: https://projectnessie.org/guides/introduction/ and https://github.com/projectnessie/nessie/blob/main/versioned/storage/common/src/main/java/org/projectnessie/versioned/storage/common/persist/Persist.java. Nessie and lakeFS use the same pattern, with a different store.

### Delta Lake

A Delta table commits by creating `_delta_log/<N>.json`, where N is the next version number. The storage must let only one writer create a given file. The creation of that file is the commit point. [doc] https://docs.delta.io/latest/delta-storage.html

S3 had no put-if-absent, so multi-cluster writes on S3 needed `S3DynamoDBLogStore`, which kept the lock in DynamoDB. delta-rs 1.0.0 removed that store and uses S3 conditional writes by default. Its writers can no longer coordinate with Delta Spark writers that use DynamoDB. [doc] https://github.com/delta-io/delta-rs/discussions/4482

I did not find whether Delta Spark uses S3 conditional writes. [not verified]

### Apache Iceberg

An Iceberg table commits by swapping the catalog's pointer from the old metadata file to the new one. The swap is a CAS in the catalog, for example a database row, the Hive metastore or a REST catalog. If the swap fails, the writer retries on the new base. [doc] https://iceberg.apache.org/spec/. The metadata files and data files are immutable objects in the bucket.

### Comparison

| System | Commit point | Primitive |
|---|---|---|
| lakeFS | Branch record in the KV store | `SetIf` CAS in Postgres, DynamoDB or Badger |
| Nessie | Reference in the version store | CAS in the database |
| Iceberg | Metadata pointer in the catalog | CAS in the catalog |
| Delta Lake | Creation of `_delta_log/<N>.json` | Put-if-absent, or a DynamoDB lock on old S3 |
| SlateDB | Creation of the next manifest file | Conditional put in the bucket |

## provcat's direct writes

### What put and delete guarantee

- **One object at a time.** `put` writes one object, and `delete` removes one object. Each is atomic on its own. [src] `packages/provcat/src/provcat/stores/objects.py`
- **Last writer wins.** No driver sends a condition on `put`. [src]
- **The S3 version of a small file is the one written.** A file up to 64 MB goes in one `PutObject`, whose reply gives its ETag. A larger file goes in a multipart upload, which gives no ETag, so a HEAD reads it after. A write by someone else in between gives their ETag. [src] `packages/provcat/src/provcat/stores/s3.py`
- **The S3 delete checks and then deletes.** A write between the HEAD and the DeleteObject is deleted. [src]
- **Several files are never one step.** A reader of a live folder can see half a change. Read at a commit for one moment, or write through lakeFS. [src]

### What a commit is

A commit of a folder writes two objects at the top of the bucket. The design page has the layout. [src] `packages/provcat/src/provcat/stores/objects.py`

1. It finds the folder's newest commit, N.
2. It lists every version under the folder, deleted ones too, twice. It goes on only when the two listings are equal, and tries up to five times.
   It lists after it reads N, because commit N listed before it made its entry. So the list is never older than commit N's.
3. If the file list equals commit N's list, there is nothing to commit.
4. It writes the commit object, `.provcat/commits/<ID>.json`, with put-if-absent. The ID is the SHA-256 of the object, so an object already there must hold the same bytes, and provcat checks that.
5. It creates log entry N + 1, `.provcat/log/<SHA-256 of the folder>/<10^15 - (N + 1)>`, with put-if-absent. **This create is the commit point.** If another commit took N + 1 first, it goes back to step 1. The losing commit object stays in the bucket, but no log entry names it, so nothing reads it.

### Why the file list is a snapshot

Every write makes a new version ID, and every delete makes a delete marker on S3, or a deletion time on GCS. Suppose the two listings are equal. Then no key changed between its read in the first listing and its read in the second. So every listed version was current at one moment, between the end of the first listing and the start of the second. Snapshot algorithms call the method "double collect". The unit test `packages/provcat/tests/unit/test_bucket_commits.py` checks that a change between the listings makes the commit list again. [src]

An upload in progress is not an object, so the commit leaves it out. The commit is then older than the upload, but it is still a snapshot. [doc, for S3]

### Why a numbered log, not a head pointer

A head pointer, `.provcat/heads/<folder>`, updated with `If-Match`, would give the same order. The numbered log is kept for three reasons:

- It never overwrites anything, so every commit stays listed.
- A failed writer learns the new state by listing, which it does anyway.
- Each commit names its parent, so the history is a chain.

On S3, `If-None-Match` checks only the current version. Someone who deletes log entry N lets a later writer make N again. Treat any delete in `.provcat/` as damage. [doc]

### What is not guaranteed

- **Commits that outlive their versions.** A lifecycle rule that expires old versions, or a person who deletes one, breaks the commits that name it. A read then fails with "not found". [src]
- **One order across folders.** Each folder has its own log. A pin compares the commits of a folder and of the folders above it by their time, which comes from each writer's clock. A commit records its time to the microsecond. If a folder and a folder above it have the same time, the folder above counts as newer. So a pin gives the same answer every time, but the answer is wrong when the folder below came second. When two writers' clocks differ, a later commit can also get an earlier time and sort as older. [src]
- **ADLS Gen2.** Blob versioning does not work on accounts with a hierarchical namespace, so provcat cannot pin or commit there, and says so. [doc]
- **An atomic branch.** A branch copies file by file. [src]
- **Cost.** A commit lists every version under the folder twice, so it slows as the history grows. [src]

For writes of several files that must appear at once, write through lakeFS. It stages the writes and moves the branch with one CAS.
