"""`AzureBlobRest.head`: the bearer path, with `pytest-httpx`. The real-account checklist proves it on Azure (W9)."""

from __future__ import annotations

from datetime import UTC, datetime

import httpx
import pytest
from obstore.store import AzureStore, MemoryStore, ObjectStore, S3Store
from pytest_httpx import HTTPXMock
from sdmxlib.edition import RemoteFile

from statcatlib.store.blob import AzureBlobRest, BlobLocation, BlobStoreError, TokenIdentity

pytestmark = pytest.mark.unit

_URL = "https://acct.blob.core.windows.net/catalogs/stc/products/p/e/manifest.json"


def _provider() -> dict[str, object]:
    return {"token": "the-token", "expires_at": datetime(2030, 1, 1, tzinfo=UTC)}


def _rest(*, prefix: str | None = "stc") -> AzureBlobRest:
    store = AzureStore("catalogs", prefix=prefix, account_name="acct", credential_provider=TokenIdentity(_provider))
    return AzureBlobRest(BlobLocation("az://catalogs/stc/", store))


def test_it_sends_the_token_and_version_and_reads_the_size_and_md5(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(method="HEAD", url=_URL, headers={"Content-Length": "12", "Content-MD5": "abc="})

    with _rest() as rest:
        found = rest.head("products/p/e/manifest.json")

    assert found == RemoteFile(12, {"md5": "abc="})
    request = httpx_mock.get_request()
    assert request is not None
    assert request.headers["authorization"] == "Bearer the-token"
    assert request.headers["x-ms-version"] == "2023-11-03"


def test_a_blob_with_no_kept_digest_reports_none(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(method="HEAD", url=_URL, headers={"Content-Length": "12"})

    with _rest() as rest:
        assert rest.head("products/p/e/manifest.json") == RemoteFile(12)


def test_a_404_is_none(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(method="HEAD", url=_URL, status_code=404)

    with _rest() as rest:
        assert rest.head("products/p/e/manifest.json") is None


@pytest.mark.parametrize("status", [400, 401, 403, 409, 500, 503])
def test_any_other_status_raises_so_a_missing_role_never_reads_as_a_missing_file(
    httpx_mock: HTTPXMock, status: int
) -> None:
    httpx_mock.add_response(method="HEAD", url=_URL, status_code=status)

    with _rest() as rest, pytest.raises(BlobStoreError, match=f"HTTP {status}") as caught:
        rest.head("products/p/e/manifest.json")

    assert caught.value.path == "products/p/e/manifest.json"
    assert "the-token" not in str(caught.value)


def test_a_transport_failure_raises(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_exception(httpx.ConnectError("refused"))

    with _rest() as rest, pytest.raises(BlobStoreError, match="ConnectError"):
        rest.head("products/p/e/manifest.json")


def test_an_unreadable_size_raises(httpx_mock: HTTPXMock) -> None:
    httpx_mock.add_response(method="HEAD", url=_URL, headers={"Content-Length": "many"})

    with _rest() as rest, pytest.raises(BlobStoreError, match="Content-Length"):
        rest.head("products/p/e/manifest.json")


def test_a_path_is_quoted_and_a_store_with_no_prefix_has_none(httpx_mock: HTTPXMock) -> None:
    url = "https://acct.blob.core.windows.net/catalogs/products/p/a%20b%3Dc.parquet"
    httpx_mock.add_response(method="HEAD", url=url, headers={"Content-Length": "1"})

    with _rest(prefix=None) as rest:
        assert rest.head("products/p/a b=c.parquet") == RemoteFile(1)


def test_a_custom_endpoint_is_used(httpx_mock: HTTPXMock) -> None:
    store = AzureStore(
        "catalogs",
        account_name="acct",
        endpoint="http://127.0.0.1:1/acct",
        credential_provider=TokenIdentity(_provider),
        client_options={"allow_http": True},
    )
    httpx_mock.add_response(method="HEAD", url="http://127.0.0.1:1/acct/catalogs/x", headers={"Content-Length": "1"})

    with AzureBlobRest(BlobLocation("az://catalogs/", store)) as rest:
        assert rest.head("x") == RemoteFile(1)


def test_a_store_with_no_token_identity_is_refused() -> None:
    store = AzureStore("catalogs", account_name="acct", account_key="a2V5")

    with AzureBlobRest(BlobLocation("az://catalogs/", store)) as rest, pytest.raises(ValueError, match="TokenIdentity"):
        rest.head("x")


def test_a_store_with_no_account_in_its_config_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AZURE_STORAGE_ACCOUNT_NAME", "from-the-environment")
    store = AzureStore("catalogs", credential_provider=TokenIdentity(_provider))

    with pytest.raises(ValueError, match="account_name"):
        AzureBlobRest(BlobLocation("az://catalogs/", store))


@pytest.mark.parametrize(
    "store",
    [
        S3Store("bucket", region="us-east-1", skip_signature=True),
        S3Store("bucket", region="r", endpoint="http://127.0.0.1:1", client_options={"allow_http": True}),
        MemoryStore(),
    ],
    ids=["s3-region", "s3-endpoint", "memory"],
)
def test_a_store_with_no_container_is_refused(store: ObjectStore) -> None:
    with pytest.raises(ValueError, match="container_name"):
        AzureBlobRest(BlobLocation("az://catalogs/", store))
