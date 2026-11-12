"""The release job's access precondition: who holds a role that reads the catalog's blobs (Phase 5 D8).

Every principal with a role that reads blobs in the catalog's container reads
embargoed bytes (spec §10.1). Before a release step runs, the release job lists
those principals and compares them with the reviewed allowlist of the jobs
configuration (``[access]``, `statcatlib.jobs.AccessPolicy`):

1. a `ReaderListing` returns a `ReaderReport`: the id of every role assignment
   that applies to the container, inherited ones included, and a `ReaderGrant`
   for each assignment whose role reads blobs;
2. `check_access` passes when every grant's principal is on the allowlist and
   every sentinel assignment is in the listing.

`AzureReaderListing` is the listing over Azure Resource Manager, through
`httpx`. It needs one custom role on the release identity, assigned at the
storage account, with two actions:
``Microsoft.Authorization/roleAssignments/read`` and
``Microsoft.Authorization/roleDefinitions/read``. It reads no directory object,
so a group is one principal, compared by its own object id.

Outside the check, and reviewed by the deployment's owner: the members of an
allowlisted group, PIM-eligible assignments that are not active, the account's
shared-key and anonymous-access settings, and POSIX ACLs on a hierarchical
namespace.

The sentinels are assignment ids the deployment knows exist at each scope above
the account. A listing that lacks one did not see that scope, so the check
fails closed.
"""

from __future__ import annotations

import re
import time
from typing import TYPE_CHECKING, Protocol
from urllib.parse import urlsplit

import httpx
from attrs import frozen

if TYPE_CHECKING:
    from collections.abc import Callable, Collection, Iterable, Mapping

__all__ = [
    "ARM_SCOPE",
    "BLOB_READ",
    "AccessListingError",
    "AccessResult",
    "AzureReaderListing",
    "ReaderGrant",
    "ReaderListing",
    "ReaderReport",
    "check_access",
    "reads_blobs",
]

#: The token scope `AzureReaderListing`'s *arm_token* is asked for.
ARM_SCOPE = "https://management.azure.com/.default"

#: The data action that reads a blob (spec §10.1).
BLOB_READ = "Microsoft.Storage/storageAccounts/blobServices/containers/blobs/read"

#: Azure Resource Manager's endpoint; a ``nextLink`` on any other host is refused.
_ARM = "https://management.azure.com"

#: The ``api-version`` of the role assignment and role definition reads.
_API_VERSION = "2022-04-01"

#: How long one request may take, in seconds.
_TIMEOUT = 30.0

#: The seconds to wait before each retry of a request answered 429 or 5xx.
_BACKOFF = (1.0, 2.0, 4.0)

#: The pages one listing follows before it gives up on a ``nextLink`` chain.
_MAX_PAGES = 1000


class AccessListingError(RuntimeError):
    """A `ReaderListing` could not list the role assignments or read a role definition.

    The release job catches it, fails the access check closed, and logs
    ``access.unchecked``.
    """


@frozen
class ReaderGrant:
    """One role assignment whose role reads blobs in the catalog's container.

    Attributes:
        assignment_id: The assignment's resource id.
        principal_id: The object id of the user, group, service principal or
            managed identity it names. A group is one principal.
        principal_type: The kind of principal, as the listing reports it, such
            as ``ServicePrincipal`` or ``Group``.
        role: The role's name.
        scope: The scope the assignment is made at: the container or a scope
            above it.
    """

    assignment_id: str
    principal_id: str
    principal_type: str
    role: str
    scope: str


@frozen
class ReaderReport:
    """What a `ReaderListing` found.

    Attributes:
        assignment_ids: The id of every assignment listed, whatever its role;
            the sentinels are looked for here.
        grants: The assignments whose role reads blobs.
    """

    assignment_ids: frozenset[str]
    grants: tuple[ReaderGrant, ...]


@frozen
class AccessResult:
    """The outcome of the access precondition.

    Attributes:
        passed: Whether a release may run: no outsider, no missing sentinel
            and no listing error.
        outsiders: The grants whose principal is off the allowlist.
        unused: The allowlisted object ids that hold no blob-read role, for
            the allowlist's next review.
        missing_sentinels: The sentinel assignment ids the listing lacks.
        errors: Why the listing could not be made.
    """

    passed: bool
    outsiders: tuple[ReaderGrant, ...] = ()
    unused: tuple[str, ...] = ()
    missing_sentinels: tuple[str, ...] = ()
    errors: tuple[str, ...] = ()


class ReaderListing(Protocol):
    """Lists who can read the catalog's blobs; `AzureReaderListing` in a deployment, a fake in tests."""

    def readers(self) -> ReaderReport:
        """Every role assignment that applies to the container, and those whose role reads blobs.

        A listing that cannot be made raises `AccessListingError`.
        """
        ...


def check_access(report: ReaderReport, allowlist: Collection[str], sentinels: Collection[str]) -> AccessResult:
    """Compare a listing with the reviewed allowlist (spec §10.1).

    Ids compare without regard to case, as Azure compares them.

    Args:
        report: What the listing found.
        allowlist: The object ids allowed a blob-read role; a group's own
            object id stands for the group.
        sentinels: Assignment ids the listing must hold, one at each scope
            above the account.

    Returns:
        The result, passed when every grant's principal is on *allowlist* and
        every sentinel is in the listing.
    """
    allowed = {object_id.lower() for object_id in allowlist}
    outsiders = tuple(grant for grant in report.grants if grant.principal_id.lower() not in allowed)
    holders = {grant.principal_id.lower() for grant in report.grants}
    unused = tuple(sorted(object_id for object_id in allowlist if object_id.lower() not in holders))
    listed = {assignment_id.lower() for assignment_id in report.assignment_ids}
    missing = tuple(sorted(sentinel for sentinel in sentinels if sentinel.lower() not in listed))
    return AccessResult(
        passed=not outsiders and not missing, outsiders=outsiders, unused=unused, missing_sentinels=missing
    )


def _matches(pattern: str, action: str) -> bool:
    """Whether the action pattern *pattern* covers *action*: ``*`` matches any run of characters, case ignored."""
    expression = ".*".join(re.escape(part) for part in pattern.split("*"))
    return re.fullmatch(expression, action, flags=re.IGNORECASE) is not None


def reads_blobs(permissions: Iterable[Mapping[str, object]]) -> bool:
    """Whether a role definition's ``permissions`` grant the blob read data action.

    A permission block grants it when one of its ``dataActions`` matches
    `BLOB_READ` and none of its ``notDataActions`` does, with ``*`` matching
    any run of characters. The role reads blobs when any block grants it, so
    the built-in Storage Blob Data Reader, Contributor and Owner do, and any
    custom role with the data action.

    Args:
        permissions: The definition's ``properties.permissions``.

    Returns:
        Whether the role reads blobs.
    """
    for block in permissions:
        granted = _strings(block.get("dataActions"))
        withheld = _strings(block.get("notDataActions"))
        if any(_matches(pattern, BLOB_READ) for pattern in granted) and not any(
            _matches(pattern, BLOB_READ) for pattern in withheld
        ):
            return True
    return False


def _strings(value: object) -> list[str]:
    """The strings of a JSON array, or none when *value* is no array."""
    return [item for item in value if isinstance(item, str)] if isinstance(value, list) else []


def _object(value: object, what: str) -> dict[str, object]:
    """*value* as a JSON object with string keys; anything else raises `AccessListingError` naming *what*."""
    if not isinstance(value, dict):
        msg = f"{what} is no JSON object"
        raise AccessListingError(msg)
    return {str(key): item for key, item in value.items()}


def _text(properties: Mapping[str, object], key: str, what: str) -> str:
    """The non-empty string at *key*; anything else raises `AccessListingError` naming *what*."""
    value = properties.get(key)
    if not isinstance(value, str) or not value:
        msg = f"{what} has no {key}"
        raise AccessListingError(msg)
    return value


@frozen
class _Assignment:
    """One role assignment, as the listing returns it."""

    assignment_id: str
    principal_id: str
    principal_type: str
    role_definition_id: str
    scope: str


@frozen
class _Role:
    """One role definition: its name, and whether it reads blobs."""

    name: str
    reads: bool


class AzureReaderListing:
    """The `ReaderListing` over Azure Resource Manager, with the two-action custom role (D8).

    `readers` sends, each with the bearer token of *arm_token*:

    1. ``GET {container scope}/providers/Microsoft.Authorization/roleAssignments?$filter=atScope()``,
       where the container scope is
       ``{storage_account}/blobServices/default/containers/{container}``, and
       the same request at each of *scopes*. ``atScope()`` returns the
       assignments at the scope and those inherited from above it. Every
       ``nextLink`` is followed.
    2. ``GET {storage_account}/providers/Microsoft.Authorization/roleDefinitions/{guid}``
       for each distinct role definition the assignments name, and judges its
       ``permissions`` with `reads_blobs`.

    A request answered 429 or 5xx is tried again three times, after 1, 2 and
    4 seconds. Any other failure raises `AccessListingError`: a 403 (the
    custom role is missing), any other status, a transport failure, a body
    that is not the documented JSON, or a ``nextLink`` on another host than
    Azure Resource Manager's.
    """

    def __init__(
        self,
        storage_account: str,
        container: str,
        *,
        arm_token: Callable[[], str],
        scopes: Iterable[str] = (),
    ) -> None:
        """List the assignments that apply to *container* of *storage_account*.

        Args:
            storage_account: The storage account's resource id,
                ``/subscriptions/.../storageAccounts/<name>``.
            container: The catalog's container.
            arm_token: Returns a bearer token for `ARM_SCOPE`. The command
                line builds it from its credential; the listing reads no
                environment variable.
            scopes: Scopes above the account to list at as well, for a tenant
                where the account-scoped role does not list inherited
                assignments.

        Raises:
            ValueError: *storage_account* or a scope is no resource id
                starting with ``/``, or *container* is empty or holds ``/``.
        """
        account = storage_account.rstrip("/")
        for scope in (account, *scopes):
            if not scope.startswith("/") or set(scope) & set("?#"):
                msg = f"{scope!r} is no Azure resource id; write it from /subscriptions/ or /providers/ on"
                raise ValueError(msg)
        if not container or "/" in container:
            msg = f"{container!r} is no container name"
            raise ValueError(msg)
        self._account = account
        self._listed = (f"{account}/blobServices/default/containers/{container}", *(s.rstrip("/") for s in scopes))
        self._arm_token = arm_token

    def readers(self) -> ReaderReport:
        """List the assignments and read each role named.

        Returns:
            Every assignment's id, and a grant for each whose role reads blobs,
            by assignment id.

        A request that failed, or an answer that is not the documented JSON,
        raises `AccessListingError`.
        """
        with httpx.Client(timeout=_TIMEOUT) as client:
            headers = {"Authorization": f"Bearer {self._arm_token()}"}
            found: dict[str, _Assignment] = {}
            for scope in self._listed:
                for assignment in self._assignments(client, headers, scope):
                    found.setdefault(assignment.assignment_id.lower(), assignment)
            roles: dict[str, _Role] = {}
            grants: list[ReaderGrant] = []
            for assignment in sorted(found.values(), key=lambda one: one.assignment_id.lower()):
                guid = assignment.role_definition_id.rstrip("/").rsplit("/", 1)[-1].lower()
                if guid not in roles:
                    roles[guid] = self._role(client, headers, guid)
                role = roles[guid]
                if role.reads:
                    grants.append(
                        ReaderGrant(
                            assignment_id=assignment.assignment_id,
                            principal_id=assignment.principal_id,
                            principal_type=assignment.principal_type,
                            role=role.name,
                            scope=assignment.scope,
                        )
                    )
        return ReaderReport(assignment_ids=frozenset(one.assignment_id for one in found.values()), grants=tuple(grants))

    def _assignments(self, client: httpx.Client, headers: Mapping[str, str], scope: str) -> list[_Assignment]:
        """Every assignment that applies to *scope*, over every page."""
        url: str | None = f"{_ARM}{scope}/providers/Microsoft.Authorization/roleAssignments"
        params: dict[str, str] | None = {"$filter": "atScope()", "api-version": _API_VERSION}
        found: list[_Assignment] = []
        for _ in range(_MAX_PAGES):
            if url is None:
                return found
            page = self._get(client, headers, url, params, f"the role assignments at {scope}")
            listed = page.get("value")
            if not isinstance(listed, list):
                msg = f"the role assignments at {scope}: the answer has no value array"
                raise AccessListingError(msg)
            found += [self._assignment(item, scope) for item in listed]
            url, params = self._next_link(page.get("nextLink"), scope), None
        msg = f"the role assignments at {scope}: more than {_MAX_PAGES} pages"
        raise AccessListingError(msg)

    @staticmethod
    def _next_link(link: object, scope: str) -> str | None:
        """The next page's URL, or ``None`` at the last page; a link off Azure Resource Manager is refused."""
        if link is None or link == "":
            return None
        if not isinstance(link, str) or f"{urlsplit(link).scheme}://{urlsplit(link).netloc}" != _ARM:
            msg = f"the role assignments at {scope}: nextLink {link!r} leaves {_ARM}; the token is sent nowhere else"
            raise AccessListingError(msg)
        return link

    @staticmethod
    def _assignment(item: object, scope: str) -> _Assignment:
        what = f"a role assignment at {scope}"
        body = _object(item, what)
        assignment_id = _text(body, "id", what)
        properties = _object(body.get("properties"), f"{assignment_id}'s properties")
        principal_type = properties.get("principalType")
        return _Assignment(
            assignment_id=assignment_id,
            principal_id=_text(properties, "principalId", assignment_id),
            principal_type=principal_type if isinstance(principal_type, str) and principal_type else "Unknown",
            role_definition_id=_text(properties, "roleDefinitionId", assignment_id),
            scope=_text(properties, "scope", assignment_id),
        )

    def _role(self, client: httpx.Client, headers: Mapping[str, str], guid: str) -> _Role:
        """The role definition *guid*, read at the storage account's scope."""
        what = f"the role definition {guid}"
        url = f"{_ARM}{self._account}/providers/Microsoft.Authorization/roleDefinitions/{guid}"
        body = self._get(client, headers, url, {"api-version": _API_VERSION}, what)
        properties = _object(body.get("properties"), f"{what}'s properties")
        permissions = properties.get("permissions")
        if not isinstance(permissions, list):
            msg = f"{what} has no permissions array"
            raise AccessListingError(msg)
        blocks = [_object(block, f"a permission block of {what}") for block in permissions]
        name = properties.get("roleName")
        return _Role(name=name if isinstance(name, str) and name else guid, reads=reads_blobs(blocks))

    @staticmethod
    def _get(
        client: httpx.Client, headers: Mapping[str, str], url: str, params: Mapping[str, str] | None, what: str
    ) -> dict[str, object]:
        """One GET with its retries, and the JSON object it answers with."""
        response: httpx.Response | None = None
        for wait in (0.0, *_BACKOFF):
            if wait:
                time.sleep(wait)
            try:
                response = client.get(url, params=params, headers=dict(headers))
            except httpx.HTTPError as exc:
                msg = f"{what}: {type(exc).__name__}: {exc}"
                raise AccessListingError(msg) from exc
            if response.status_code != httpx.codes.TOO_MANY_REQUESTS and not response.is_server_error:
                break
        if response is None or not response.is_success:
            status = "no answer" if response is None else f"HTTP {response.status_code}"
            hint = ""
            if response is not None and response.status_code == httpx.codes.FORBIDDEN:
                hint = (
                    "; the release identity needs the custom role with roleAssignments/read and "
                    "roleDefinitions/read at the storage account"
                )
            msg = f"{what}: {status}{hint}"
            raise AccessListingError(msg)
        try:
            return _object(response.json(), f"{what}: the answer")
        except ValueError as exc:
            msg = f"{what}: the answer is not JSON: {exc}"
            raise AccessListingError(msg) from exc
