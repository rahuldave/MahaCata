"""The object store suite against local servers: moto for S3,
fake-gcs-server for GCS and Azurite for Azure."""
import pytest

from object_store_suite import *  # noqa: F401,F403  (the tests and backends)
from object_store_suite import AzureBackend, GcsBackend, S3Backend


@pytest.fixture(params=["s3", "gcs", "azure"])
def backend(request, monkeypatch):
    if request.param == "s3":
        return S3Backend(request.getfixturevalue("s3_endpoint"))
    if request.param == "gcs":
        return GcsBackend(request.getfixturevalue("gcs_endpoint"))
    return AzureBackend(request.getfixturevalue("azurite_connection"), monkeypatch)
