"""`AzureReaderListing` against `pytest-httpx`, and `check_access`: the release job's access precondition (Phase 5 D8).

Every request here is answered by `pytest-httpx`, which fails a request it was
given no answer for, so no test reaches Azure Resource Manager. The real-account
check of 2026-11-12 (design handoff row 6a) proved the listing on Azure.
"""

from __future__ import annotations

import httpx
import pytest
from pytest_httpx import HTTPXMock

from statcatlib import access
from statcatlib.access import (
    ARM_SCOPE,
    BLOB_READ,
    AccessListingError,
    AzureReaderListing,
    ReaderGrant,
    ReaderReport,
    check_access,
    reads_blobs,
)

pytestmark = pytest.mark.unit

_ARM = "https://management.azure.com"
_SUB = "/subscriptions/sub"
_RG = f"{_SUB}/resourceGroups/rg"
_ACCOUNT = f"{_RG}/providers/Microsoft.Storage/storageAccounts/acct"
_CONTAINER = f"{_ACCOUNT}/blobServices/default/containers/catalogs"
_AUTH = "providers/Microsoft.Authorization"
_QUERY = "%24filter=atScope%28%29&api-version=2022-04-01"
_LIST = f"{_ARM}{_CONTAINER}/{_AUTH}/roleAssignments?{_QUERY}"

#: The built-in roles' ids, and two custom ones.
_READER, _CONTRIBUTOR, _OWNER_DATA = "2a2b9908", "ba92f5b4", "b7e6dc6d"
_SUB_OWNER, _CUSTOM_READ, _CUSTOM_NOT = "8e3af657", "c0000001", "c0000002"

_DEFINITIONS: dict[str, tuple[str, list[dict[str, list[str]]]]] = {
    _READER: ("Storage Blob Data Reader", [{"dataActions": [BLOB_READ], "notDataActions": []}]),
    _CONTRIBUTOR: (
        "Storage Blob Data Contributor",
        [
            {
                "dataActions": [
                    "Microsoft.Storage/storageAccounts/blobServices/containers/blobs/delete",
                    "Microsoft.Storage/storageAccounts/blobServices/containers/blobs/read",
                    "Microsoft.Storage/storageAccounts/blobServices/containers/blobs/write",
                ],
                "notDataActions": [],
            }
        ],
    ),
    _OWNER_DATA: (
        "Storage Blob Data Owner",
        [{"dataActions": ["Microsoft.Storage/storageAccounts/blobServices/containers/*"], "notDataActions": []}],
    ),
    _SUB_OWNER: ("Owner", [{"actions": ["*"], "dataActions": [], "notDataActions": []}]),
    _CUSTOM_READ: ("lake reader", [{"dataActions": ["Microsoft.Storage/*/read"], "notDataActions": []}]),
    _CUSTOM_NOT: (
        "queue worker",
        [{"dataActions": ["Microsoft.Storage/*"], "notDataActions": ["Microsoft.Storage/*/blobs/*"]}],
    ),
}


def _assignment(
    name: str, principal: str, role: str, scope: str, *, kind: str = "ServicePrincipal"
) -> dict[str, object]:
    return {
        "id": f"{scope}/{_AUTH}/roleAssignments/{name}",
        "properties": {
            "roleDefinitionId": f"{_SUB}/{_AUTH}/roleDefinitions/{role}",
            "principalId": principal,
            "principalType": kind,
            "scope": scope,
        },
    }


def _define(httpx_mock: HTTPXMock, *roles: str) -> None:
    for role in roles:
        name, permissions = _DEFINITIONS[role]
        httpx_mock.add_response(
            url=f"{_ARM}{_ACCOUNT}/{_AUTH}/roleDefinitions/{role}?api-version=2022-04-01",
            json={"properties": {"roleName": name, "permissions": permissions}},
        )


def _listing(*, scopes: tuple[str, ...] = ()) -> AzureReaderListing:
    return AzureReaderListing(_ACCOUNT, "catalogs", arm_token=lambda: "arm-token", scopes=scopes)


@pytest.fixture(autouse=True)
def _no_backoff(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(access, "_BACKOFF", (0.0, 0.0, 0.0))


#: The six assignments of the real-account check: one at the subscription, one at
#: the resource group, three at the account, one at the container.
_SIX = [
    _assignment("at-sub", "owner", _SUB_OWNER, _SUB, kind="User"),
    _assignment("at-rg", "analysts", _READER, _RG, kind="Group"),
    _assignment("at-acct-1", "release", _CONTRIBUTOR, _ACCOUNT),
    _assignment("at-acct-2", "pods", _READER, _ACCOUNT),
    _assignment("at-acct-3", "gc", _OWNER_DATA, _ACCOUNT),
    _assignment("at-container", "preview", _CUSTOM_READ, _CONTAINER),
]


class TestTheListing:
    def test_it_lists_at_the_container_scope_and_keeps_the_inherited_assignments(self, httpx_mock: HTTPXMock) -> None:
        httpx_mock.add_response(url=_LIST, json={"value": _SIX})
        _define(httpx_mock, _SUB_OWNER, _READER, _CONTRIBUTOR, _OWNER_DATA, _CUSTOM_READ)

        report = _listing().readers()

        assert len(report.assignment_ids) == 6
        assert f"{_SUB}/{_AUTH}/roleAssignments/at-sub" in report.assignment_ids
        assert {(g.principal_id, g.role, g.scope) for g in report.grants} == {
            ("analysts", "Storage Blob Data Reader", _RG),
            ("release", "Storage Blob Data Contributor", _ACCOUNT),
            ("pods", "Storage Blob Data Reader", _ACCOUNT),
            ("gc", "Storage Blob Data Owner", _ACCOUNT),
            ("preview", "lake reader", _CONTAINER),
        }
        assert len(report.grants) == 5
        requests = httpx_mock.get_requests()
        assert all(request.headers["authorization"] == "Bearer arm-token" for request in requests)
        assert all(request.url.host == "management.azure.com" for request in requests)
        assert requests[0].url.params["$filter"] == "atScope()"

    def test_a_group_is_one_principal_and_its_members_are_not_read(self, httpx_mock: HTTPXMock) -> None:
        httpx_mock.add_response(url=_LIST, json={"value": [_assignment("g", "analysts", _READER, _RG, kind="Group")]})
        _define(httpx_mock, _READER)

        [grant] = _listing().readers().grants

        assert (grant.principal_id, grant.principal_type) == ("analysts", "Group")
        assert len(httpx_mock.get_requests()) == 2

    def test_every_next_link_is_followed(self, httpx_mock: HTTPXMock) -> None:
        second = f"{_ARM}{_CONTAINER}/{_AUTH}/roleAssignments?api-version=2022-04-01&$skiptoken=abc"
        httpx_mock.add_response(url=_LIST, json={"value": _SIX[:2], "nextLink": second})
        httpx_mock.add_response(url=second, json={"value": _SIX[2:4], "nextLink": None})
        _define(httpx_mock, _SUB_OWNER, _READER, _CONTRIBUTOR)

        report = _listing().readers()

        assert len(report.assignment_ids) == 4
        assert {g.principal_id for g in report.grants} == {"analysts", "release", "pods"}

    def test_a_next_link_on_another_host_is_refused_and_gets_no_token(self, httpx_mock: HTTPXMock) -> None:
        httpx_mock.add_response(url=_LIST, json={"value": [], "nextLink": "https://example.org/next"})

        with pytest.raises(AccessListingError, match=r"leaves https://management\.azure\.com"):
            _listing().readers()

        assert len(httpx_mock.get_requests()) == 1

    def test_each_role_definition_is_read_once_at_the_account_scope(self, httpx_mock: HTTPXMock) -> None:
        both = [_assignment("a", "pods", _READER, _ACCOUNT), _assignment("b", "search", _READER.upper(), _CONTAINER)]
        httpx_mock.add_response(url=_LIST, json={"value": both})
        _define(httpx_mock, _READER)

        report = _listing().readers()

        assert {g.principal_id for g in report.grants} == {"pods", "search"}
        definitions = [r for r in httpx_mock.get_requests() if "roleDefinitions" in r.url.path]
        assert [r.url.path for r in definitions] == [f"{_ACCOUNT}/{_AUTH}/roleDefinitions/{_READER}"]

    def test_scopes_are_listed_too_and_an_assignment_seen_twice_counts_once(self, httpx_mock: HTTPXMock) -> None:
        at_sub = f"{_ARM}{_SUB}/{_AUTH}/roleAssignments?{_QUERY}"
        httpx_mock.add_response(url=_LIST, json={"value": _SIX[2:4]})
        httpx_mock.add_response(url=at_sub, json={"value": [_SIX[0], _SIX[2]]})
        _define(httpx_mock, _SUB_OWNER, _CONTRIBUTOR, _READER)

        report = _listing(scopes=(_SUB,)).readers()

        assert len(report.assignment_ids) == 3
        assert [g.principal_id for g in report.grants] == ["release", "pods"]

    def test_a_custom_role_with_a_not_data_action_over_blobs_is_no_reader(self, httpx_mock: HTTPXMock) -> None:
        httpx_mock.add_response(url=_LIST, json={"value": [_assignment("q", "worker", _CUSTOM_NOT, _ACCOUNT)]})
        _define(httpx_mock, _CUSTOM_NOT)

        report = _listing().readers()

        assert report.grants == ()
        assert len(report.assignment_ids) == 1


class TestFailures:
    @pytest.mark.parametrize("status", [429, 500, 503])
    def test_a_throttled_or_failed_request_is_retried_three_times(self, httpx_mock: HTTPXMock, status: int) -> None:
        for _ in range(3):
            httpx_mock.add_response(url=_LIST, status_code=status)
        httpx_mock.add_response(url=_LIST, json={"value": []})

        assert _listing().readers() == ReaderReport(frozenset(), ())
        assert len(httpx_mock.get_requests()) == 4

    def test_a_failure_that_persists_fails_the_listing(self, httpx_mock: HTTPXMock) -> None:
        for _ in range(4):
            httpx_mock.add_response(url=_LIST, status_code=503)

        with pytest.raises(AccessListingError, match="HTTP 503"):
            _listing().readers()

    def test_a_403_fails_at_once_and_names_the_role(self, httpx_mock: HTTPXMock) -> None:
        httpx_mock.add_response(url=_LIST, status_code=403)

        with pytest.raises(AccessListingError, match=r"HTTP 403.*roleAssignments/read"):
            _listing().readers()

        assert len(httpx_mock.get_requests()) == 1

    def test_a_role_definition_that_cannot_be_read_fails_the_listing(self, httpx_mock: HTTPXMock) -> None:
        httpx_mock.add_response(url=_LIST, json={"value": _SIX[:1]})
        httpx_mock.add_response(
            url=f"{_ARM}{_ACCOUNT}/{_AUTH}/roleDefinitions/{_SUB_OWNER}?api-version=2022-04-01", status_code=404
        )

        with pytest.raises(AccessListingError, match=f"the role definition {_SUB_OWNER}: HTTP 404"):
            _listing().readers()

    def test_a_transport_failure_fails_the_listing(self, httpx_mock: HTTPXMock) -> None:
        httpx_mock.add_exception(httpx.ConnectTimeout("timed out"), url=_LIST)

        with pytest.raises(AccessListingError, match="ConnectTimeout"):
            _listing().readers()

    @pytest.mark.parametrize(
        "body",
        [
            {"values": []},
            {"value": ["no object"]},
            {"value": [{"id": "x", "properties": {"principalId": "p", "scope": "/s"}}]},
            ["a list"],
        ],
        ids=["no value", "an item that is no object", "no roleDefinitionId", "no object"],
    )
    def test_an_answer_that_is_not_the_documented_json_fails_the_listing(
        self, httpx_mock: HTTPXMock, body: object
    ) -> None:
        httpx_mock.add_response(url=_LIST, json=body)

        with pytest.raises(AccessListingError):
            _listing().readers()

    def test_a_body_that_is_not_json_fails_the_listing(self, httpx_mock: HTTPXMock) -> None:
        httpx_mock.add_response(url=_LIST, text="<html>")

        with pytest.raises(AccessListingError, match="not JSON"):
            _listing().readers()

    @pytest.mark.parametrize(
        ("account", "container", "scopes"),
        [("subscriptions/sub", "catalogs", ()), (_ACCOUNT, "", ()), (_ACCOUNT, "a/b", ()), (_ACCOUNT, "c", ("x",))],
    )
    def test_a_bad_resource_id_or_container_raises(self, account: str, container: str, scopes: tuple[str, ...]) -> None:
        with pytest.raises(ValueError, match=r"resource id|container"):
            AzureReaderListing(account, container, arm_token=lambda: "t", scopes=scopes)

    def test_the_token_scope_is_resource_managers(self) -> None:
        assert ARM_SCOPE == "https://management.azure.com/.default"


class TestReadsBlobs:
    @pytest.mark.parametrize(
        ("permissions", "reads"),
        [
            ([{"dataActions": [BLOB_READ]}], True),
            ([{"dataActions": [BLOB_READ.upper()]}], True),
            ([{"dataActions": ["*"]}], True),
            ([{"dataActions": ["Microsoft.Storage/storageAccounts/blobServices/containers/blobs/*"]}], True),
            ([{"dataActions": ["Microsoft.Storage/storageAccounts/blobServices/containers/blobs/write"]}], False),
            ([{"dataActions": ["Microsoft.Storage/storageAccounts/queueServices/*"]}], False),
            ([{"actions": ["*"], "dataActions": []}], False),
            ([{"dataActions": ["*"], "notDataActions": [BLOB_READ]}], False),
            ([{"dataActions": ["*"], "notDataActions": ["*/read"]}], False),
            ([{"dataActions": ["*"], "notDataActions": ["*"]}, {"dataActions": [BLOB_READ]}], True),
            ([], False),
        ],
    )
    def test_a_data_action_matches_with_wildcards_unless_a_not_data_action_does(
        self, permissions: list[dict[str, object]], *, reads: bool
    ) -> None:
        assert reads_blobs(permissions) is reads


def _grant(principal: str, name: str = "a") -> ReaderGrant:
    return ReaderGrant(f"{_ACCOUNT}/{_AUTH}/roleAssignments/{name}", principal, "ServicePrincipal", "reader", _ACCOUNT)


class TestCheckAccess:
    def test_every_principal_on_the_allowlist_passes(self) -> None:
        report = ReaderReport(frozenset({"/a/1", "/SENTINEL/2"}), (_grant("OID-Pods"),))

        result = check_access(report, ["oid-pods", "oid-search"], ["/sentinel/2"])

        assert result.passed
        assert result.outsiders == ()
        assert result.unused == ("oid-search",)
        assert result.missing_sentinels == ()

    def test_a_principal_off_the_allowlist_fails(self) -> None:
        outsider = _grant("analyst", "b")
        report = ReaderReport(frozenset(), (_grant("oid-pods"), outsider))

        result = check_access(report, ["oid-pods"], [])

        assert not result.passed
        assert result.outsiders == (outsider,)

    def test_a_group_passes_only_by_its_own_object_id(self) -> None:
        group = ReaderGrant("/a/g", "group-oid", "Group", "reader", _RG)

        assert not check_access(ReaderReport(frozenset(), (group,)), ["member-oid"], []).passed
        assert check_access(ReaderReport(frozenset(), (group,)), ["group-oid"], []).passed

    def test_a_missing_sentinel_fails_closed(self) -> None:
        report = ReaderReport(frozenset({"/a/1"}), (_grant("oid-pods"),))

        result = check_access(report, ["oid-pods"], ["/a/1", "/sub/owner"])

        assert not result.passed
        assert result.missing_sentinels == ("/sub/owner",)
        assert result.outsiders == ()
