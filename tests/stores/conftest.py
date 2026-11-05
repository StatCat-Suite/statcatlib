"""The `stores` tier: the blob Inventory writer and the upload against Azurite in a container (GH-98).

Part of `just ci`. The pre-push hook is statcatlib's only gate, so a machine
with no container runtime fails this tier and names the fix (W6), where
sdmxlib's tier skips.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Iterator
from datetime import UTC, datetime

import pytest

from statcatlib.store.blob import BlobInventoryStorage, BlobLocation
from statcatlib.store.inventory import InventoryWriter
from tests.stores import _stores


@pytest.fixture(scope="session")
def azurite() -> Iterator[_stores.Running]:
    """Azurite, running for the session; no container runtime fails the test, naming the fix."""
    missing = _stores.runtime_missing()
    if missing is not None:
        pytest.fail(f"the stores tier needs a container runtime, and `just ci` runs this tier: {missing}")
    running = _stores.start()
    yield running
    _stores.stop(running)


@pytest.fixture
def new_location(azurite: _stores.Running) -> Callable[[], BlobLocation]:
    """Makes a location at a fresh prefix."""
    return lambda: _stores.location(azurite, uuid.uuid4().hex)


@pytest.fixture
def location(new_location: Callable[[], BlobLocation]) -> BlobLocation:
    """A catalog location at a fresh prefix."""
    return new_location()


@pytest.fixture
def writer(location: BlobLocation) -> InventoryWriter:
    """A writer over a new catalog, on this machine's clock, so a lagging container VM fails no commit."""
    made = InventoryWriter(BlobInventoryStorage(location, actor="tests", clock=lambda: datetime.now(UTC)))
    made.write_catalog_json()
    return made
