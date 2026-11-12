"""Fakes for the release job's clients, for the unit and the stores tiers (Phase 5 Part 3).

`FakeListing` stands for `statcatlib.access.AzureReaderListing`, so no test of the
job sends a request to Azure Resource Manager. `RecordingRest` stands for
`statcatlib.store.blob.AzureBlobRest`: it answers from the test's own store with the
size and the MD5 Azure would keep, and records each path asked.
"""

from __future__ import annotations

import base64
import hashlib
from typing import TYPE_CHECKING

from sdmxlib.edition import RemoteFile

from statcatlib.access import AccessListingError, ReaderGrant, ReaderReport
from statcatlib.store.blob import BlobStoreError, _ObjectOps

if TYPE_CHECKING:
    from collections.abc import Iterable

    from obstore.store import ObjectStore

#: The scope every fake assignment is made at.
ACCOUNT = "/subscriptions/sub/resourceGroups/rg/providers/Microsoft.Storage/storageAccounts/acct"

#: A sentinel assignment at the subscription, which `listing` lists.
SENTINEL = "/subscriptions/sub/providers/Microsoft.Authorization/roleAssignments/sentinel-subscription"


def grant(principal_id: str, *, principal_type: str = "ServicePrincipal", n: int = 0) -> ReaderGrant:
    """A blob-read grant to *principal_id* at the account, as Storage Blob Data Reader."""
    return ReaderGrant(
        assignment_id=f"{ACCOUNT}/providers/Microsoft.Authorization/roleAssignments/{principal_id}-{n}",
        principal_id=principal_id,
        principal_type=principal_type,
        role="Storage Blob Data Reader",
        scope=ACCOUNT,
    )


class FakeListing:
    """A `ReaderListing` that returns the report it is given, or raises the error it is given."""

    def __init__(self, grants: Iterable[ReaderGrant] = (), *, sentinels: Iterable[str] = (SENTINEL,)) -> None:
        self.grants = tuple(grants)
        self.sentinels = tuple(sentinels)
        self.error: str | None = None
        self.calls = 0

    def readers(self) -> ReaderReport:
        self.calls += 1
        if self.error is not None:
            raise AccessListingError(self.error)
        ids = frozenset({*(one.assignment_id for one in self.grants), *self.sentinels})
        return ReaderReport(assignment_ids=ids, grants=self.grants)


class RecordingRest:
    """The digest read over any store: the size and the MD5 Azure would keep, and the paths asked.

    A path in `fail` raises `BlobStoreError`, as a store fault does.
    """

    def __init__(self, store: ObjectStore) -> None:
        self._ops = _ObjectOps(store)
        self.asked: list[str] = []
        self.fail: set[str] = set()

    def head(self, path: str) -> RemoteFile | None:
        self.asked.append(path)
        if path in self.fail:
            msg = f"read the properties of {path}: refused by the test"
            raise BlobStoreError(msg, path=path)
        got = self._ops.read(path)
        if got is None:
            return None
        md5 = hashlib.md5(got[0], usedforsecurity=False).digest()
        return RemoteFile(len(got[0]), {"md5": base64.b64encode(md5).decode()})
