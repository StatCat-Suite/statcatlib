"""The CODR generator in `statcatlib.testing` (GH-51, S2, design decision O6).

`codr_structures` seals the three structure products and `codr_product` a
data product of one or more tables cut from them. The generator is
deterministic: one seed and shape give one set of artefact fingerprints.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import sdmxlib as sl
from sdmxlib.edition import read_manifest

import statcatlib as sc
from statcatlib import testing
from statcatlib.testing import STRUCTURE_PRODUCTS, CodrShape, codr_product, codr_structures

pytestmark = pytest.mark.integration

SHAPE = CodrShape(geography_codes=200, geography_depth=3, classification_codes=(40, 20, 10), observations=200)

#: DuckDB 2.0 stores ``metadata.duckdb`` in 256 KiB blocks, and every table and
#: index that holds a row takes at least one: the default shape's data product
#: takes 46 blocks (11.5 MiB), 35 of them sdmxlib's tables and indexes. The
#: design's target is 10 MB; this bound is the measured size, rounded up to 48
#: blocks, so a change that adds a table's worth of rows still fails here.
METADATA_BOUND = 48 * 256 * 1024


def _fingerprints(edition_dir: Path) -> dict[str, str]:
    return {a.urn: a.fingerprint for a in read_manifest(edition_dir).artefacts}


def test_a_default_shape_data_products_metadata_stays_within_its_block_bound(tmp_path: Path) -> None:
    root = tmp_path / "catalog"
    with sc.Catalog.create(root) as cat:
        edition = codr_product(cat, "table-14100287")

    size = (root / "products" / "table-14100287" / edition.edition_id / "metadata.duckdb").stat().st_size
    assert size <= METADATA_BOUND
    assert sum(a.partial for a in read_manifest(root / "products" / edition.product_id / edition.edition_id).artefacts)


def test_two_generations_with_one_seed_give_equal_fingerprints(tmp_path: Path) -> None:
    found: list[dict[str, dict[str, str]]] = []
    for n in (1, 2):
        root = tmp_path / f"catalog-{n}"
        with sc.Catalog.create(root) as cat:
            editions = codr_structures(cat, seed=7, shape=SHAPE)
            data = codr_product(cat, "table", seed=7, shape=SHAPE)
        dirs = {product_id: root / "products" / product_id / e.edition_id for product_id, e in editions.items()}
        dirs["table"] = root / "products" / "table" / data.edition_id
        found.append({product_id: _fingerprints(path) for product_id, path in dirs.items()})

    first, second = found
    assert sorted(first) == sorted([*STRUCTURE_PRODUCTS, "table"])
    assert first == second


def test_another_seed_gives_other_structures(tmp_path: Path) -> None:
    one, other = (testing._structures(seed, SHAPE) for seed in (1, 2))

    assert one.unit == other.unit, "the unit and status codelists are fixed"
    assert one.geography != other.geography


def test_a_two_table_product_holds_each_shared_partial_once_cut_to_both_tables_codes(tmp_path: Path) -> None:
    root = tmp_path / "catalog"
    with sc.Catalog.create(root) as cat:
        edition = codr_product(cat, "pair", tables=2, seed=5, shape=SHAPE)
        cat.refresh()
        view = cat.product("pair").view()
        stored = {
            urn: {
                code
                for (code,) in view._cursor()
                .execute("SELECT code_id FROM sdmx.code WHERE codelist_urn = $urn", {"urn": urn})
                .fetchall()
            }
            for urn in (
                "urn:sdmx:org.sdmx.infomodel.codelist.Codelist=CODR:CL_GEO(1.0)",
                "urn:sdmx:org.sdmx.infomodel.codelist.Codelist=CODR:CL_CLASS_1(1.0)",
            )
        }

    manifest = read_manifest(root / "products" / "pair" / edition.edition_id)
    urns = [a.urn for a in manifest.artefacts]
    assert len(urns) == len(set(urns)), "each artefact is listed once"
    assert sorted(u.split("=")[1] for u in urns if ":DF_" in u) == ["CODR:DF_pair_1(1.0)", "CODR:DF_pair_2(1.0)"]
    assert len(manifest.bindings) == 2

    structures = testing._structures(5, SHAPE)
    tables = [testing._observations("pair", n, 5, SHAPE, structures) for n in (1, 2)]
    for urn, column, codelist in (
        (str(structures.geography.urn), "GEO", structures.geography),
        (str(structures.classifications[0].urn), "CLASS_1", structures.classifications[0]),
    ):
        used = {value for table in tables for value in table.column(column).to_pylist()}
        assert stored[urn] == set(testing._with_ancestors(codelist, sorted(used)))
        one_table = set(testing._with_ancestors(codelist, sorted(set(tables[0].column(column).to_pylist()))))
        assert one_table < stored[urn], "the shared partial holds the second table's codes too"
        assert isinstance(codelist, sl.Codelist)
        assert len(stored[urn]) < len(codelist.codes)
