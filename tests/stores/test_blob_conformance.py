"""The blob Inventory writer passes sdmxlib's conformance suite over Azurite (GH-98)."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime

import pytest
from sdmxlib.inventory import leaf_ref, record, root_path
from sdmxlib.inventory.conformance import (
    CHECKS,
    HISTORY_CHECKS,
    SCHEMA_VERSION,
    Backend,
    HistoryBackend,
    check_the_routing_fields_survive_a_commit_and_a_reinstate,
    run,
    run_history,
)

from statcatlib.store.blob import BlobLocation
from tests.stores._backend import BlobBackend

pytestmark = pytest.mark.stores


@pytest.mark.parametrize("check", CHECKS, ids=[c.__name__.removeprefix("check_") for c in CHECKS])
def test_the_writer_conforms(
    check: Callable[[Callable[[], Backend]], None], new_location: Callable[[], BlobLocation]
) -> None:
    check(lambda: BlobBackend(new_location()))


def test_the_routing_fields_survive_the_writer(new_location: Callable[[], BlobLocation]) -> None:
    check_the_routing_fields_survive_a_commit_and_a_reinstate(lambda: BlobBackend(new_location()))


def test_run_passes_every_check(new_location: Callable[[], BlobLocation]) -> None:
    assert run(lambda: BlobBackend(new_location())) == [c.__name__ for c in CHECKS]


@pytest.mark.parametrize("check", HISTORY_CHECKS, ids=[c.__name__.removeprefix("check_") for c in HISTORY_CHECKS])
def test_the_writer_conforms_to_the_history_checks(
    check: Callable[[Callable[[], HistoryBackend]], None], new_location: Callable[[], BlobLocation]
) -> None:
    check(lambda: BlobBackend(new_location()))


def test_run_history_passes_every_check(new_location: Callable[[], BlobLocation]) -> None:
    assert run_history(lambda: BlobBackend(new_location())) == [c.__name__ for c in HISTORY_CHECKS]


def test_a_writer_killed_between_the_leaf_and_the_root_leaves_the_head_readable_and_writable(
    location: BlobLocation,
) -> None:
    def sealed(product: str, n: int):
        return record(
            None,
            product_id=product,
            kind="data",
            lifecycle="revisable",
            edition_id=f"01a18e00-0000-7000-8000-{n:012d}",
            previous=None,
            manifest_sha256="0" * 64,
            schema_version=SCHEMA_VERSION,
            embargo=False,
            now=datetime.now(UTC),
        )

    backend = BlobBackend(location)
    base = backend.commit(None, {"a": sealed("a", 1)})
    orphan = sealed("b", 2)
    backend.commit_interrupted(base, {"b": orphan})

    assert backend.fetch(leaf_ref(orphan)[1].leaf) is not None
    assert backend.fetch(root_path(base.seq + 1)) is None
    head = backend.head()
    assert head is not None
    assert head.seq == base.seq
    assert set(head.leaves) == {"a"}

    after = backend.commit(head, {"c": sealed("c", 3)})
    assert after.seq == base.seq + 1
    assert set(after.leaves) == {"a", "c"}
