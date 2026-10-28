"""The object store suite against the real clouds and a real lakeFS.

Each backend runs only when its environment variable is set. Each test makes
its own bucket, or container, and the fixture deletes it afterwards, even when
the test fails. Everything is made and deleted with the Python SDKs.

- PROVCAT_SYSTEM_AWS=1: AWS S3 with the default credentials. PROVCAT_SYSTEM_AWS_PROFILE
  picks a profile.
- PROVCAT_SYSTEM_GCS=<project>: Google Cloud Storage in that project, with
  Application Default Credentials (gcloud auth application-default login).
- PROVCAT_SYSTEM_AZURE=<subscription id>: makes a resource group with one ADLS
  Gen2 storage account for the session, and deletes the group at the end.
  PROVCAT_SYSTEM_AZURE_LOCATION picks the region (default eastus). It uses
  DefaultAzureCredential, for example az login.
- PROVCAT_SYSTEM_LAKEFS=<url>: lakeFS, for example http://localhost:8000, with
  the keys of the AWS profile PROVCAT_SYSTEM_LAKEFS_PROFILE (default lakefs).
"""
import os
import uuid

import pytest

from object_store_suite import *  # noqa: F401,F403  (the tests and backends)
from object_store_suite import AzureBackend, GcsBackend, LakefsBackend, S3Backend


def _need(var):
    value = os.environ.get(var)
    if not value:
        pytest.skip(f"set {var} to run this against the real service")
    return value


@pytest.fixture(scope="session")
def azure_connection():
    subscription = _need("PROVCAT_SYSTEM_AZURE")
    from azure.identity import DefaultAzureCredential
    from azure.mgmt.resource.resources import ResourceManagementClient
    from azure.mgmt.storage import StorageManagementClient

    credential = DefaultAzureCredential()
    location = os.environ.get("PROVCAT_SYSTEM_AZURE_LOCATION", "eastus")
    suffix = uuid.uuid4().hex[:12]
    group, account = f"provcat-test-{suffix}", f"provcattest{suffix}"
    groups = ResourceManagementClient(credential, subscription).resource_groups
    accounts = StorageManagementClient(credential, subscription).storage_accounts
    try:
        groups.create_or_update(group, {"location": location, "tags": {"purpose": "provcat system test"}})
        from azure.mgmt.storage import models

        accounts.begin_create(group, account, models.StorageAccountCreateParameters(
            location=location, kind="StorageV2", sku=models.Sku(name="Standard_LRS"),
            properties=models.StorageAccountPropertiesCreateParameters(
                is_hns_enabled=True, minimum_tls_version="TLS1_2", allow_blob_public_access=False),
        )).result()
        key = accounts.list_keys(group, account).keys_property[0].value
        yield (f"DefaultEndpointsProtocol=https;AccountName={account};AccountKey={key};"
               "EndpointSuffix=core.windows.net")
    finally:
        # Delete the group only if it exists, so a failed setup shows its own error.
        if groups.check_existence(group):
            groups.begin_delete(group).result()


@pytest.fixture(params=["aws", "gcs", "azure", "lakefs"])
def backend(request, monkeypatch):
    if request.param == "aws":
        _need("PROVCAT_SYSTEM_AWS")
        b = S3Backend(profile=os.environ.get("PROVCAT_SYSTEM_AWS_PROFILE"))
    elif request.param == "gcs":
        b = GcsBackend(project=_need("PROVCAT_SYSTEM_GCS"))
    elif request.param == "azure":
        b = AzureBackend(request.getfixturevalue("azure_connection"), monkeypatch)
        b.hns = True
    else:
        b = LakefsBackend(_need("PROVCAT_SYSTEM_LAKEFS"),
                          os.environ.get("PROVCAT_SYSTEM_LAKEFS_PROFILE", "lakefs"))
    yield b
    b.cleanup()
