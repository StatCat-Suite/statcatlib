"""CUJ 1 against a real SDMX registry: an analyst builds a catalog on a laptop (Phase 2a).

The steps are the design's: `Catalog.create`, ``product().working_copy()``,
`ingest_sdmx`, a query on the working copy's view, ``seal()``; then
`Catalog.open` and the same query on the sealed edition's view. Live: it
fetches Eurostat's NAMA_10_GDP dataflow with its structures.

Run with: just test-live
"""

from __future__ import annotations

from pathlib import Path

import pytest
import sdmxlib as sl

import statcatlib as sc
import statcatlib.query as q

pytestmark = pytest.mark.live


def _query(view: sc.EditionView) -> list[str]:
    found = view.query(sc.AggregateDataset).filter(q.has_dimensions(["geo"])).collect()
    return sorted(d.identifier for d in found)


def test_cuj_1_build_seal_open(tmp_path: Path) -> None:
    root = tmp_path / "stats"
    with sc.Catalog.create(root) as cat:
        wc = cat.product("nama_10_gdp").working_copy()
        with sl.RestRegistry(sl.Provider.ESTAT) as reg:
            reg.get(sl.Dataflow.urn_for(agency="ESTAT", id="NAMA_10_GDP"), resolve=True)
            sc.ingest_sdmx(wc, reg.registry)
        before = _query(wc.view())
        assert before
        edition = wc.seal()

    assert sorted(p.name for p in root.iterdir()) == ["CURRENT", "catalog.json", "inventory", "products"]
    assert (root / "inventory" / "000000000001.json").is_file()
    assert (root / "products" / "nama_10_gdp" / edition.edition_id / "manifest.json").is_file()

    with sc.Catalog.open(root) as cat:
        product = cat.product("nama_10_gdp")
        assert _query(product.view()) == before
        assert product.editions() == [edition]
