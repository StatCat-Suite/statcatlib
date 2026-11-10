"""`fold_lifecycle`: the lifecycle the release job's fold records an uploaded edition with (Phase 5 D3, Q7, S13).

The leaf's lifecycle wins for a product the Inventory records, and the
``lifecycle`` key of the edition's ``statcat`` section names it for a new
product. Each of the design's five rows is one test.
"""

from __future__ import annotations

from datetime import UTC, datetime

import msgspec
import pytest
from sdmxlib.edition import CANONICAL_FORM, DATA_PATH, Lifecycle, Manifest, Producer
from sdmxlib.inventory import Leaf, record
from sdmxlib.storage.schema import SCHEMA_VERSION

from statcatlib.bundle import STATCAT_SECTION
from statcatlib.store.inventory import InventoryRuleError
from statcatlib.store.working_copy import fold_lifecycle

pytestmark = pytest.mark.unit

_NOW = datetime(2026, 11, 10, 12, 0, tzinfo=UTC)
_RECORDED = "01a18e00-0000-7000-8000-000000000001"
_UPLOADED = "01a18e00-0000-7000-8000-000000000002"


def _leaf(lifecycle: Lifecycle) -> Leaf:
    """Product ``p``'s leaf, recording one live edition with *lifecycle*."""
    return record(
        None,
        product_id="p",
        kind="data",
        lifecycle=lifecycle,
        edition_id=_RECORDED,
        previous=None,
        manifest_sha256="0" * 64,
        schema_version=SCHEMA_VERSION,
        embargo=False,
        now=_NOW,
    )


def _manifest(**section: object) -> Manifest:
    """An uploaded edition of ``p`` whose ``statcat`` section holds *section*."""
    return Manifest(
        format_version="3.0",
        product_id="p",
        edition_id=_UPLOADED,
        previous=_RECORDED,
        created_at=_NOW,
        producer=Producer(name="statcatlib", version="0.17.0"),
        sdmxlib_version="0.87.0",
        schema_version=SCHEMA_VERSION,
        duckdb_version="v1.5.0",
        duckdb_storage_version=69,
        ducklake_version=None,
        ducklake_spec_version=None,
        data_path=DATA_PATH,
        files=(),
        bindings=(),
        artefacts=(),
        canonical_form=CANONICAL_FORM,
        extensions={STATCAT_SECTION: {"format": 3, "kind": "data", **section}},
    )


class TestRecordedProduct:
    def test_a_section_with_no_lifecycle_takes_the_leafs(self) -> None:
        assert fold_lifecycle(_leaf("final"), _manifest()) == "final"

    def test_a_section_naming_the_leafs_lifecycle_takes_it(self) -> None:
        assert fold_lifecycle(_leaf("revisable"), _manifest(lifecycle="revisable")) == "revisable"

    def test_a_section_naming_another_lifecycle_is_refused_naming_both(self) -> None:
        with pytest.raises(InventoryRuleError) as caught:
            fold_lifecycle(_leaf("final"), _manifest(lifecycle="revisable"))

        assert caught.value.rule == "lifecycle"
        assert (caught.value.product, caught.value.edition) == ("p", _UPLOADED)
        message = str(caught.value)
        assert "'revisable'" in message
        assert "final product" in message


class TestNewProduct:
    @pytest.mark.parametrize("lifecycle", ["final", "revisable"])
    def test_the_sections_lifecycle_is_recorded(self, lifecycle: Lifecycle) -> None:
        assert fold_lifecycle(None, _manifest(lifecycle=lifecycle)) == lifecycle

    def test_a_section_with_no_lifecycle_is_refused_naming_statcatlib_0_17(self) -> None:
        with pytest.raises(InventoryRuleError, match=r"statcatlib 0\.17") as caught:
            fold_lifecycle(None, _manifest())

        assert caught.value.rule == "lifecycle"
        assert (caught.value.product, caught.value.edition) == ("p", _UPLOADED)
        assert "names no lifecycle" in str(caught.value)

    def test_a_section_naming_neither_value_is_refused(self) -> None:
        with pytest.raises(InventoryRuleError, match="'live', which is neither final nor revisable") as caught:
            fold_lifecycle(None, _manifest(lifecycle="live"))

        assert caught.value.rule == "lifecycle"

    def test_a_manifest_with_no_statcat_section_is_refused(self) -> None:
        bare = msgspec.structs.replace(_manifest(), extensions={})

        with pytest.raises(InventoryRuleError, match="names no lifecycle"):
            fold_lifecycle(None, bare)
