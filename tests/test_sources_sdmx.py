"""Integration tests for sources/sdmx.py — ingest_sdmx and structural queries.

These tests require sdmxlib's storage tables and a real DuckDB connection.
They exercise the full ingest → query cycle, including JOINs into sdmxlib's
dsd_components and codes tables.
"""

import pathlib
from collections.abc import Iterator

import pytest
from sdmxlib import artefact_urn
from sdmxlib.model.codelist import Code, Codelist
from sdmxlib.model.dataflow import Dataflow
from sdmxlib.model.datastructure import DataStructure, Dimension
from sdmxlib.model.hierarchy import HierarchicalCode, Hierarchy, HierarchyAssociation
from sdmxlib.model.in_memory_registry import InMemoryRegistry
from sdmxlib.model.istring import InternationalString
from sdmxlib.model.ref import Ref
from sdmxlib.model.urn import SdmxUrn

import statcatlib as sc
import statcatlib.query as q

pytestmark = pytest.mark.integration


# ---------------------------------------------------------------------------
# Shared fixture helpers
# ---------------------------------------------------------------------------


def _i(en: str, fr: str | None = None) -> InternationalString:
    return InternationalString(en=en, fr=fr or en)


def _make_registry() -> tuple[InMemoryRegistry, str]:
    """Build a minimal registry with two dimensions and return (registry, flow_urn).

    Schema:
      CL_GEO: CA, US
      CL_FREQ: A (Annual), M (Monthly)
      DSD_TEST: dimensions GEO (→ CL_GEO), FREQ (→ CL_FREQ)
      DF_TEST: uses DSD_TEST
    """
    geo_cl = Codelist(
        id="CL_GEO",
        maintainer="TEST",
        name=_i("Geography", "Géographie"),
        codes=[
            Code(id="CA", name=_i("Canada")),
            Code(id="US", name=_i("United States", "États-Unis")),
        ],
    )
    freq_cl = Codelist(
        id="CL_FREQ",
        maintainer="TEST",
        name=_i("Frequency", "Fréquence"),
        codes=[
            Code(id="A", name=_i("Annual", "Annuel")),
            Code(id="M", name=_i("Monthly", "Mensuel")),
        ],
    )
    dsd = DataStructure(
        id="DSD_TEST",
        maintainer="TEST",
        name=_i("Test Data Structure"),
        dimensions=[
            Dimension(id="GEO", representation=geo_cl),
            Dimension(id="FREQ", representation=freq_cl),
        ],
    )
    flow = Dataflow(
        id="DF_TEST",
        maintainer="TEST",
        name=_i("Test Dataflow", "Flux de test"),
        description=_i("A test dataflow for integration testing"),
        structure=dsd,
    )
    registry = InMemoryRegistry()
    registry.add(geo_cl)
    registry.add(freq_cl)
    registry.add(dsd)
    registry.add(flow)
    flow_urn = str(artefact_urn(flow))
    return registry, flow_urn


@pytest.fixture
def sdmx_store() -> sc.Catalog:
    """A Catalog populated with a minimal SDMX registry."""
    store = sc.Catalog().open()
    registry, _ = _make_registry()
    sc.ingest_sdmx(store, registry)
    return store


@pytest.fixture(scope="module")
def indexed_sdmx_store(tmp_path_factory: pytest.TempPathFactory) -> Iterator[sc.Catalog]:
    """The same registry, folder-backed with a BM25 index built over it.

    `search()` needs somewhere to put ``search/``; the in-memory fixture
    above has nowhere, which is why `TestFTS` was skipped rather than
    failing (#117).
    """
    pytest.importorskip("lancedb", reason="search needs statcatlib[search]")
    path = tmp_path_factory.mktemp("sdmx_indexed") / "catalog"
    with sc.Catalog(path) as catalog:
        registry, _ = _make_registry()
        sc.ingest_sdmx(catalog, registry)
        catalog.build_index()
        yield catalog


# ---------------------------------------------------------------------------
# ingest_sdmx — basic ingest behaviour
# ---------------------------------------------------------------------------


class TestIngestSdmx:
    def test_returns_statistical_datasets(self):
        store = sc.Catalog().open()
        registry, _ = _make_registry()
        result = sc.ingest_sdmx(store, registry)
        assert len(result) == 1
        assert all(isinstance(ds, sc.StatisticalDataset) for ds in result)

    def test_identifier_format(self):
        store = sc.Catalog().open()
        registry, _ = _make_registry()
        result = sc.ingest_sdmx(store, registry)
        assert result[0].identifier == "TEST:DF_TEST(1.0)"

    def test_sdmx_dataflow_urn_set(self):
        store = sc.Catalog().open()
        registry, flow_urn = _make_registry()
        result = sc.ingest_sdmx(store, registry)
        assert result[0].sdmx_dataflow_urn == flow_urn

    def test_title_and_description_populated(self):
        store = sc.Catalog().open()
        registry, _ = _make_registry()
        result = sc.ingest_sdmx(store, registry)
        ds = result[0]
        assert ds.title.get("en") == "Test Dataflow"
        assert ds.title.get("fr") == "Flux de test"
        assert ds.description.get("en") == "A test dataflow for integration testing"

    def test_dimension_names_populated(self):
        store = sc.Catalog().open()
        registry, _ = _make_registry()
        result = sc.ingest_sdmx(store, registry)
        ds = result[0]
        assert len(ds.dimension_names) == 2
        dim_ids = {d.get("en") for d in ds.dimension_names}
        assert "GEO" in dim_ids
        assert "FREQ" in dim_ids

    def test_sdmxlib_tables_populated(self):
        store = sc.Catalog().open()
        registry, _ = _make_registry()
        sc.ingest_sdmx(store, registry)
        rows = store.connection.execute("SELECT COUNT(*) FROM sdmx.code").fetchone()
        assert rows is not None
        assert rows[0] == 4  # CA, US, A, M

    def test_idempotent_reingest(self):
        """Re-ingesting the same registry should not duplicate datasets."""
        store = sc.Catalog().open()
        registry, _ = _make_registry()
        sc.ingest_sdmx(store, registry)
        sc.ingest_sdmx(store, registry)
        result = store.query(sc.StatisticalDataset).all()
        assert len(result) == 1


# ---------------------------------------------------------------------------
# Round-trip: store.get after ingest
# ---------------------------------------------------------------------------


class TestGetAfterIngest:
    def test_get_returns_statistical_dataset(self, sdmx_store: sc.Catalog):
        ds = sdmx_store.get(sc.StatisticalDataset, "TEST:DF_TEST(1.0)")
        assert ds is not None
        assert isinstance(ds, sc.StatisticalDataset)

    def test_get_preserves_urn(self, sdmx_store: sc.Catalog):
        _, flow_urn = _make_registry()
        ds = sdmx_store.get(sc.StatisticalDataset, "TEST:DF_TEST(1.0)")
        assert ds is not None
        assert ds.sdmx_dataflow_urn == flow_urn

    def test_get_preserves_dimension_names(self, sdmx_store: sc.Catalog):
        ds = sdmx_store.get(sc.StatisticalDataset, "TEST:DF_TEST(1.0)")
        assert ds is not None
        assert len(ds.dimension_names) == 2

    def test_get_wrong_type_returns_none(self, sdmx_store: sc.Catalog):
        # Dataset (base) does exist under that id — but StatisticalDataset lookup
        # on a plain Dataset id should return None. Here we test the reverse:
        # all ingested datasets are StatisticalDataset so get(Dataset) should work.
        ds = sdmx_store.get(sc.BaseDataset, "TEST:DF_TEST(1.0)")
        assert ds is not None

    def test_get_missing_returns_none(self, sdmx_store: sc.Catalog):
        assert sdmx_store.get(sc.StatisticalDataset, "NONEXISTENT") is None


# ---------------------------------------------------------------------------
# Structural queries — has_code and has_dimensions JOIN into sdmxlib tables
# ---------------------------------------------------------------------------


class TestStructuralQueries:
    def test_has_code_matches(self, sdmx_store: sc.Catalog):
        result = sdmx_store.query(sc.StatisticalDataset).filter(q.has_code("GEO", "CA")).execute()
        assert result.total == 1
        assert result.items[0].identifier == "TEST:DF_TEST(1.0)"

    def test_has_code_no_match(self, sdmx_store: sc.Catalog):
        result = sdmx_store.query(sc.StatisticalDataset).filter(q.has_code("GEO", "ZZ")).execute()
        assert result.total == 0

    def test_has_code_wrong_dimension(self, sdmx_store: sc.Catalog):
        # Code exists but under a different dimension
        result = sdmx_store.query(sc.StatisticalDataset).filter(q.has_code("FREQ", "CA")).execute()
        assert result.total == 0

    def test_has_dimensions_single_match(self, sdmx_store: sc.Catalog):
        result = sdmx_store.query(sc.StatisticalDataset).filter(q.has_dimensions(["GEO"])).execute()
        assert result.total == 1

    def test_has_dimensions_both_match(self, sdmx_store: sc.Catalog):
        result = sdmx_store.query(sc.StatisticalDataset).filter(q.has_dimensions(["GEO", "FREQ"])).execute()
        assert result.total == 1

    def test_has_dimensions_no_match(self, sdmx_store: sc.Catalog):
        result = sdmx_store.query(sc.StatisticalDataset).filter(q.has_dimensions(["GEO", "FREQ", "MISSING"])).execute()
        assert result.total == 0

    def test_has_code_plain_dataset_not_matched(self, sdmx_store: sc.Catalog):
        """Plain datasets with no sdmx_dataflow_urn should not match structural filters."""
        sdmx_store.put(
            sc.OpenDataset(
                identifier="PLAIN001",
                title=InternationalString(en="Plain"),
                description=InternationalString(en="No SDMX link"),
            )
        )
        result = sdmx_store.query(sc.BaseDataset).filter(q.has_code("GEO", "CA")).execute()
        # Only the AggregateDataset should match
        assert all(isinstance(ds, sc.AggregateDataset) for ds in result.items)


# ---------------------------------------------------------------------------
# Publisher mapping
# ---------------------------------------------------------------------------


class TestPublisherMapping:
    def test_publisher_set_from_agency(self, sdmx_store: sc.Catalog):
        ds = sdmx_store.get(sc.StatisticalDataset, "TEST:DF_TEST(1.0)")
        assert ds is not None
        assert ds.publisher is not None
        assert ds.publisher.id == "TEST"

    def test_publisher_name_fallback_to_id(self, sdmx_store: sc.Catalog):
        """When agency has no name, publisher.name.get('en') should fall back to agency id."""
        ds = sdmx_store.get(sc.StatisticalDataset, "TEST:DF_TEST(1.0)")
        assert ds is not None
        assert ds.publisher is not None
        assert ds.publisher.name.get("en") is not None

    def test_facets_publisher_works_after_ingest(self, sdmx_store: sc.Catalog):
        counts = sdmx_store.facets("publisher")
        assert "publisher" in counts
        assert len(counts["publisher"]) > 0


# ---------------------------------------------------------------------------
# Full-text search
# ---------------------------------------------------------------------------


class TestFTS:
    def test_search_hits_title(self, indexed_sdmx_store: sc.Catalog):
        result = indexed_sdmx_store.search("Dataflow")
        assert result.total >= 1
        # `search()` yields `Hit` wrappers; the entity is on `.payload`.
        # This read the entity directly and would have raised if the class
        # were ever unskipped — see the stale skip above (#117).
        assert any(
            "DF_TEST" in hit.payload.identifier for hit in result.items if isinstance(hit.payload, sc.BaseDataset)
        )

    def test_search_hits_description(self, indexed_sdmx_store: sc.Catalog):
        result = indexed_sdmx_store.search("integration testing")
        assert result.total >= 1

    def test_search_no_match(self, indexed_sdmx_store: sc.Catalog):
        result = indexed_sdmx_store.search("xyzzy_nonexistent_token_1234")
        assert result.total == 0

    def test_query_with_search(self, indexed_sdmx_store: sc.Catalog):
        result = indexed_sdmx_store.query(sc.StatisticalDataset).search("Dataflow").execute()
        assert result.total >= 1


# ---------------------------------------------------------------------------
# Persistent storage
# ---------------------------------------------------------------------------


class TestPersistentStorage:
    def test_write_close_reopen_query(self, tmp_path: object) -> None:
        db_path = pathlib.Path(str(tmp_path)) / "bundle"

        # Write
        registry, flow_urn = _make_registry()
        with sc.Catalog(db_path) as store:
            sc.ingest_sdmx(store, registry)
            assert store.get(sc.StatisticalDataset, "TEST:DF_TEST(1.0)") is not None

        # Reopen and query — catalog tables must survive close/open
        with sc.Catalog(db_path) as store:
            ds = store.get(sc.StatisticalDataset, "TEST:DF_TEST(1.0)")
            assert ds is not None
            assert ds.sdmx_dataflow_urn == flow_urn
            assert ds.publisher is not None
            assert ds.publisher.id == "TEST"

    def test_structural_query_survives_reopen(self, tmp_path: object) -> None:
        db_path = pathlib.Path(str(tmp_path)) / "bundle"

        registry, _ = _make_registry()
        with sc.Catalog(db_path) as store:
            sc.ingest_sdmx(store, registry)

        with sc.Catalog(db_path) as store:
            result = store.query(sc.StatisticalDataset).filter(q.has_code("GEO", "CA")).execute()
            assert result.total == 1


# ---------------------------------------------------------------------------
# Multi-Hierarchy ingest — issue #93 item 1
# ---------------------------------------------------------------------------
#
# Models the CL_GEOGRAPHY admin-vs-statistical case from
# Admin vs statistical geography: one Codelist (CA / ON / QC / TOR) with two
# Hierarchy artefacts giving the same codes different parent graphs.
# H_GEO_ADMIN nests TOR under ON; H_GEO_STAT treats TOR as a sibling of
# ON / QC directly under CA.
#
# The claim under test: statcatlib.ingest_sdmx() plumbs Hierarchy artefacts
# from an InMemoryRegistry through sdmxlib's Registry.ingest() into
# sdmx.hierarchy_node with populated parent_node_id / depth /
# ancestor_ids. No statcatlib-side hierarchy code needed — this confirms that
# item 1 of #93 ("codelist loader reads sibling Hierarchy
# artefacts") is effectively a no-op because sdmxlib already does it.
# Mirrors the SDMX-ML 3.0 round-trip fixture in sdmxlib at
# tests/fixtures/sdmx_ml30/with_multiple_hierarchies.xml.


def _make_multi_hierarchy_registry() -> tuple[InMemoryRegistry, str, str]:
    """Build a registry with two Hierarchies over one Codelist.

    Returns (registry, h_admin_urn, h_stat_urn) — the hierarchy URNs are
    what get stored in sdmx.hierarchy_node.hierarchy_urn.
    """
    cl_area = Codelist(
        id="CL_AREA",
        maintainer="TEST",
        name=InternationalString(en="Area"),
        codes=[
            Code(id="CA", name=InternationalString(en="Canada")),
            Code(id="ON", name=InternationalString(en="Ontario")),
            Code(id="QC", name=InternationalString(en="Quebec")),
            Code(id="TOR", name=InternationalString(en="Toronto")),
        ],
    )
    dsd = DataStructure(
        id="DSD_GEO",
        maintainer="TEST",
        name=InternationalString(en="Geo DSD"),
        dimensions=[Dimension(id="REF_AREA", representation=cl_area)],
    )
    flow = Dataflow(
        id="DF_GEO",
        maintainer="TEST",
        name=InternationalString(en="Geo dataflow"),
        structure=dsd,
    )
    h_admin = Hierarchy(
        id="H_GEO_ADMIN",
        maintainer="TEST",
        version="1.0",
        name=InternationalString(en="Administrative geo hierarchy"),
        tree=[
            HierarchicalCode(
                id="CA",
                children=[
                    HierarchicalCode(
                        id="ON",
                        children=[HierarchicalCode(id="TOR")],
                    ),
                    HierarchicalCode(id="QC"),
                ],
            ),
        ],
    )
    h_stat = Hierarchy(
        id="H_GEO_STAT",
        maintainer="TEST",
        version="1.0",
        name=InternationalString(en="Statistical geo hierarchy"),
        tree=[
            HierarchicalCode(
                id="CA",
                children=[
                    HierarchicalCode(id="ON"),
                    HierarchicalCode(id="QC"),
                    HierarchicalCode(id="TOR"),
                ],
            ),
        ],
    )
    registry = InMemoryRegistry()
    for artefact in (cl_area, dsd, flow, h_admin, h_stat):
        registry.add(artefact)
    return registry, str(artefact_urn(h_admin)), str(artefact_urn(h_stat))


class TestHierarchyIngest:
    """ingest_sdmx() persists Hierarchy artefacts to sdmx.hierarchy_node.

    Confirms that distinct parent graphs over the same codelist survive
    ingest with the expected (parent_node_id, depth, ancestor_ids) shape
    per hierarchy.
    """

    def test_both_hierarchies_persisted(self):
        store = sc.Catalog().open()
        registry, h_admin_urn, h_stat_urn = _make_multi_hierarchy_registry()
        sc.ingest_sdmx(store, registry)

        rows = store.connection.execute(
            "SELECT hierarchy_urn, COUNT(*) FROM sdmx.hierarchy_node "
            "WHERE hierarchy_urn IN (?, ?) "
            "GROUP BY hierarchy_urn ORDER BY hierarchy_urn",
            [h_admin_urn, h_stat_urn],
        ).fetchall()
        # 4 nodes per hierarchy (CA, ON, QC, TOR) across 2 hierarchies.
        assert {row[0] for row in rows} == {h_admin_urn, h_stat_urn}
        assert all(row[1] == 4 for row in rows)

    def test_distinct_parent_graphs_per_hierarchy(self):
        """TOR's parent differs: ON under admin, CA under statistical."""
        store = sc.Catalog().open()
        registry, h_admin_urn, h_stat_urn = _make_multi_hierarchy_registry()
        sc.ingest_sdmx(store, registry)

        def _parent_of(hierarchy_urn: str, node_id: str) -> str | None:
            row = store.connection.execute(
                "SELECT parent_node_id FROM sdmx.hierarchy_node WHERE hierarchy_urn = ? AND node_id = ?",
                [hierarchy_urn, node_id],
            ).fetchone()
            assert row is not None, f"missing node {node_id} in {hierarchy_urn}"
            parent = row[0]
            # Not `str(row[0])`: a NULL parent must stay `None`, not
            # become the string "None" — the #114 defect, in a test.
            return None if parent is None else str(parent)

        # Admin: TOR → ON → CA (root)
        assert _parent_of(h_admin_urn, "CA") is None
        assert _parent_of(h_admin_urn, "ON") == "CA"
        assert _parent_of(h_admin_urn, "TOR") == "ON"
        # Statistical: TOR is a sibling of ON / QC under CA — different parent.
        assert _parent_of(h_stat_urn, "TOR") == "CA"
        assert _parent_of(h_stat_urn, "ON") == "CA"

    def test_depth_and_ancestor_ids_populated(self):
        """Pre-materialised columns let consumers query without a recursive CTE."""
        store = sc.Catalog().open()
        registry, h_admin_urn, _ = _make_multi_hierarchy_registry()
        sc.ingest_sdmx(store, registry)

        rows = store.connection.execute(
            "SELECT node_id, depth, ancestor_ids FROM sdmx.hierarchy_node WHERE hierarchy_urn = ? ORDER BY node_id",
            [h_admin_urn],
        ).fetchall()
        by_id = {row[0]: (row[1], row[2]) for row in rows}

        ca_depth, ca_ancestors = by_id["CA"]
        on_depth, on_ancestors = by_id["ON"]
        tor_depth, tor_ancestors = by_id["TOR"]
        assert ca_depth == 0
        assert on_depth == 1
        assert tor_depth == 2
        # ancestor_ids walks root-ward; exact order may vary by writer
        # convention. Assert the *set* of ancestors per node, plus the
        # invariant that count == depth.
        assert ca_ancestors in (None, [])
        assert set(on_ancestors or []) == {"CA"}
        assert set(tor_ancestors or []) == {"CA", "ON"}
        assert len(tor_ancestors or []) == tor_depth

    def test_hierarchies_survive_reopen(self, tmp_path: object) -> None:
        """Persistent path: hierarchy_node rows present after close + reopen."""
        db_path = pathlib.Path(str(tmp_path)) / "bundle"
        registry, h_admin_urn, _ = _make_multi_hierarchy_registry()

        with sc.Catalog(db_path) as store:
            sc.ingest_sdmx(store, registry)

        with sc.Catalog(db_path) as store:
            row = store.connection.execute(
                "SELECT COUNT(*) FROM sdmx.hierarchy_node WHERE hierarchy_urn = ?",
                [h_admin_urn],
            ).fetchone()
            assert row is not None
            assert row[0] == 4


# ---------------------------------------------------------------------------
# Hierarchy-aware q.has_code — issue #93 items 2 + 3
# ---------------------------------------------------------------------------
#
# Today's q.has_code('GEO', 'ON') is an exact-match filter. Items 2 + 3 of
# #93 expand it to accept an sdmxlib code predicate, so a caller can find
# datasets that have ANY descendant of a root code on a given dimension
# under a chosen SDMX 3.0 Hierarchy — the canonical
# use case ("small areas in this province").
#
# Three paths under test:
#   (a) literal-string code — unchanged exact match
#   (b) q.descendants_of(root, hierarchy=h) — explicit hierarchy
#   (c) q.descendants_of(root) — statcatlib resolves the default via
#       sdmx.hierarchy_association per dataflow (dataflow-scoped wins over
#       unscoped) and falls back to the codelist's parent chain when no
#       association exists.


def _make_descent_registry(
    *, with_ha: bool, scoped: bool = True, divergent: bool = False
) -> tuple[InMemoryRegistry, str, Hierarchy]:
    """Build a registry shaped for descent-filter testing.

    Codelist: CA → ON → TOR (parent_id chain — populates ancestor_ids).
    Hierarchy H_ADMIN: CA → ON → TOR (same shape via SDMX 3.0 Hierarchy).
    When ``with_ha`` is True, a HierarchyAssociation binds REF_AREA on the
    DSD to H_ADMIN, so a hierarchy-less descendants_of resolves to it.
    ``scoped`` controls whether that association names the dataflow as its
    context. An unscoped one applies to the component wherever it is used,
    and outranks nothing — ``rank == 1`` picks it only when no scoped
    association exists.

    ``divergent`` builds H_ADMIN as CA -> TOR, skipping ON, while the
    codelist keeps CA -> ON -> TOR. The two then disagree: "descendants of
    ON" is empty in the hierarchy and non-empty in the parent chain, so a
    test can tell which path answered. With the default aligned shape both
    give the same answer, which is why the pre-existing tests here cannot
    distinguish them.

    Returns (registry, dataflow_urn, h_admin).
    """
    cl_area = Codelist(
        id="CL_AREA",
        maintainer="TEST",
        name=InternationalString(en="Area"),
        codes=[
            Code(id="CA", name=InternationalString(en="Canada")),
            Code(id="ON", name=InternationalString(en="Ontario"), parent_id="CA"),
            Code(id="TOR", name=InternationalString(en="Toronto"), parent_id="ON"),
        ],
    )

    def _code_ref(code_id: str) -> Ref[Code]:
        # Codes aren't maintainable — build the URN explicitly with the
        # codelist's coordinates as the parent identity.
        urn = SdmxUrn.make(
            Code,
            package="codelist",
            artefact_class="Code",
            agency="TEST",
            id="CL_AREA",
            version="1.0",
            item_id=code_id,
        )
        return Ref[Code].unresolved(urn)

    dsd = DataStructure(
        id="DSD_GEO",
        maintainer="TEST",
        name=InternationalString(en="Geo DSD"),
        dimensions=[Dimension(id="REF_AREA", representation=cl_area)],
    )
    flow = Dataflow(
        id="DF_GEO",
        maintainer="TEST",
        name=InternationalString(en="Geo dataflow"),
        structure=dsd,
    )
    # Each HierarchicalCode needs an explicit ``code=`` ref so the
    # writer populates ``sdmx.hierarchy_node.code_urn`` — sdmxlib's
    # ``descendants_of`` predicate JOINs via that column, not via the
    # node_id == code_id convention.
    h_admin = Hierarchy(
        id="H_GEO_ADMIN",
        maintainer="TEST",
        version="1.0",
        name=InternationalString(en="Admin hierarchy"),
        tree=[
            HierarchicalCode(
                id="CA",
                code=_code_ref("CA"),
                children=(
                    [HierarchicalCode(id="TOR", code=_code_ref("TOR"))]
                    if divergent
                    else [
                        HierarchicalCode(
                            id="ON",
                            code=_code_ref("ON"),
                            children=[HierarchicalCode(id="TOR", code=_code_ref("TOR"))],
                        ),
                    ]
                ),
            ),
        ],
    )
    registry = InMemoryRegistry()
    for art in (cl_area, dsd, flow, h_admin):
        registry.add(art)
    if with_ha:
        # Build the Dimension URN by hand so the HA's linked_object resolves
        # to the REF_AREA dim on DSD_GEO, as an upstream structure producer
        # would emit it.
        dsd_urn = artefact_urn(dsd)
        dim_urn = SdmxUrn.make(
            Dimension,
            package="datastructure",
            artefact_class="Dimension",
            agency=dsd_urn.agency,
            id=dsd_urn.id,
            version=dsd_urn.version,
            item_id="REF_AREA",
        )
        ha = HierarchyAssociation(
            id="HA_REF_AREA_ADMIN",
            maintainer="TEST",
            version="1.0",
            name=InternationalString(en="REF_AREA under H_GEO_ADMIN"),
            linked_hierarchy=Ref.to(h_admin),
            linked_object=Ref[Dimension].unresolved(dim_urn),
            context_object=Ref.to(flow) if scoped else None,
        )
        registry.add(ha)
    return registry, str(artefact_urn(flow)), h_admin


class TestHasCodeWithPredicate:
    """``q.has_code(dim, q.descendants_of(...))`` covers descent over Hierarchies.

    The literal-string path (``q.has_code('GEO', 'ON')``) still works
    untouched — its dedicated coverage already lives in
    ``TestStructuralQueries`` above. These tests focus on the descent path.

    Descent resolves through ``sdmx.hierarchy_applicability`` (sdmxlib
    0.61); the fallback to the codelist's own parent chain, where no
    association applies, stays statcatlib's.
    """

    def test_descendants_of_with_explicit_hierarchy_matches_dataflow(self):
        """``descendants_of(root, hierarchy=h)`` finds dataflows whose codelist has descendants."""
        store = sc.Catalog().open()
        registry, df_urn, h_admin = _make_descent_registry(with_ha=False)
        sc.ingest_sdmx(store, registry)

        result = (
            store.query(sc.StatisticalDataset)
            .filter(q.has_code("REF_AREA", q.descendants_of("CA", hierarchy=h_admin)))
            .execute()
        )

        assert result.total == 1
        assert result.items[0].sdmx_dataflow_urn == df_urn

    def test_descendants_of_with_explicit_hierarchy_no_match(self):
        """Root code with no descendants in the hierarchy returns no datasets."""
        store = sc.Catalog().open()
        registry, _df_urn, h_admin = _make_descent_registry(with_ha=False)
        sc.ingest_sdmx(store, registry)

        # TOR is a leaf — has no descendants in H_GEO_ADMIN.
        result = (
            store.query(sc.StatisticalDataset)
            .filter(q.has_code("REF_AREA", q.descendants_of("TOR", hierarchy=h_admin)))
            .execute()
        )
        assert result.total == 0

    def test_descendants_of_no_hierarchy_falls_back_to_codelist_parent_chain(self):
        """Without a HierarchyAssociation, statcatlib falls back to ancestor_ids on sdmx.code."""
        store = sc.Catalog().open()
        registry, df_urn, _h = _make_descent_registry(with_ha=False)
        sc.ingest_sdmx(store, registry)

        # CL_AREA has CA→ON→TOR via parent_id, so ON/TOR have CA in ancestor_ids.
        result = store.query(sc.StatisticalDataset).filter(q.has_code("REF_AREA", q.descendants_of("CA"))).execute()
        assert result.total == 1
        assert result.items[0].sdmx_dataflow_urn == df_urn

    def test_descendants_of_no_hierarchy_resolves_via_hierarchy_association(self):
        """With a HierarchyAssociation in the registry, statcatlib routes through hierarchy_node."""
        store = sc.Catalog().open()
        registry, df_urn, _h = _make_descent_registry(with_ha=True)
        sc.ingest_sdmx(store, registry)

        # Identical query shape to the no-HA case — but now the SQL takes
        # the HA-resolution branch (CASE WHEN COALESCE(...) IS NOT NULL).
        # Both branches should produce the same final answer for this
        # parent-aligned fixture; the test asserts the HA path doesn't
        # break it.
        result = store.query(sc.StatisticalDataset).filter(q.has_code("REF_AREA", q.descendants_of("CA"))).execute()
        assert result.total == 1
        assert result.items[0].sdmx_dataflow_urn == df_urn

    def test_descendants_of_leaf_no_hierarchy_no_match(self):
        """Leaf code descended-of yields nothing — covers the ELSE branch's negative case."""
        store = sc.Catalog().open()
        registry, _df_urn, _h = _make_descent_registry(with_ha=False)
        sc.ingest_sdmx(store, registry)

        result = store.query(sc.StatisticalDataset).filter(q.has_code("REF_AREA", q.descendants_of("TOR"))).execute()
        assert result.total == 0

    def test_unscoped_association_answers_rather_than_the_parent_chain(self):
        """An association with no context object applies wherever the component is used.

        This path never worked before sdmxlib 0.61. statcatlib matched unscoped
        associations with ``context_object_urn IS NULL``, and the storage
        writer stored absence as ``''`` — so the join found nothing and
        every unscoped association fell through to the parent chain. Only
        the scoped case was covered here, which is why nobody noticed.

        The fixture is divergent (H_ADMIN is CA -> TOR, the codelist is
        CA -> ON -> TOR) so the two paths give opposite answers and this
        actually witnesses which one ran.
        """
        store = sc.Catalog().open()
        registry, _df_urn, _h = _make_descent_registry(with_ha=True, scoped=False, divergent=True)
        sc.ingest_sdmx(store, registry)

        # ON has no descendants in H_ADMIN, so the association's answer is
        # "no" — where the parent chain would have said "yes, TOR".
        result = store.query(sc.StatisticalDataset).filter(q.has_code("REF_AREA", q.descendants_of("ON"))).execute()
        assert result.total == 0

    def test_parent_chain_answers_when_no_association_applies(self):
        """The same question, no association: the codelist's own tree answers.

        Paired with the test above on the identical divergent fixture —
        together they pin down which branch ran, which neither can do alone.
        """
        store = sc.Catalog().open()
        registry, df_urn, _h = _make_descent_registry(with_ha=False, divergent=True)
        sc.ingest_sdmx(store, registry)

        result = store.query(sc.StatisticalDataset).filter(q.has_code("REF_AREA", q.descendants_of("ON"))).execute()
        assert result.total == 1
        assert result.items[0].sdmx_dataflow_urn == df_urn

    def test_descent_is_strict_but_include_self_widens_it(self):
        """Descent excludes the root; ``include_self=True`` admits it.

        TOR is a leaf, so it has no descendants — strict descent finds
        nothing and self-inclusive descent finds the dataflow. Matches
        sdmxlib's ``q.hcode.ancestors``, which excludes the node itself.
        """
        store = sc.Catalog().open()
        registry, df_urn, h_admin = _make_descent_registry(with_ha=False)
        sc.ingest_sdmx(store, registry)

        strict = (
            store.query(sc.StatisticalDataset)
            .filter(q.has_code("REF_AREA", q.descendants_of("TOR", hierarchy=h_admin)))
            .execute()
        )
        assert strict.total == 0

        inclusive = (
            store.query(sc.StatisticalDataset)
            .filter(q.has_code("REF_AREA", q.descendants_of("TOR", hierarchy=h_admin, include_self=True)))
            .execute()
        )
        assert inclusive.total == 1
        assert inclusive.items[0].sdmx_dataflow_urn == df_urn


# ---------------------------------------------------------------------------
# Descent at scale — the shape of the join, not just its answer
# ---------------------------------------------------------------------------


def _make_wide_descent_registry(n_flows: int, n_prov: int, n_sub: int) -> InMemoryRegistry:
    """N dataflows over one deep geography codelist, each with a scoped HA.

    Deliberately larger than the fixtures above, which have three codes and
    one dataflow — a size at which a nested loop and a hash join are
    indistinguishable.
    """
    codes = [Code(id="CA", name=InternationalString(en="Canada"))]
    for p_i in range(n_prov):
        pid = f"P{p_i:02d}"
        codes.append(Code(id=pid, name=InternationalString(en=pid), parent_id="CA"))
        codes.extend(
            Code(id=f"{pid}-{k:03d}", name=InternationalString(en=f"{pid}-{k}"), parent_id=pid) for k in range(n_sub)
        )
    cl = Codelist(id="CL_AREA", maintainer="TEST", version="1.0", name=InternationalString(en="Area"), codes=codes)

    def _cref(code_id: str) -> Ref[Code]:
        return Ref[Code].unresolved(
            SdmxUrn.make(
                Code,
                package="codelist",
                artefact_class="Code",
                agency="TEST",
                id="CL_AREA",
                version="1.0",
                item_id=code_id,
            )
        )

    h = Hierarchy(
        id="H_ADMIN",
        maintainer="TEST",
        version="1.0",
        name=InternationalString(en="Admin"),
        tree=[
            HierarchicalCode(
                id="CA",
                name=InternationalString(en="CA"),
                code=_cref("CA"),
                children=[
                    HierarchicalCode(
                        id=f"P{p_i:02d}",
                        name=InternationalString(en="p"),
                        code=_cref(f"P{p_i:02d}"),
                        children=[
                            HierarchicalCode(
                                id=f"P{p_i:02d}-{k:03d}",
                                name=InternationalString(en="s"),
                                code=_cref(f"P{p_i:02d}-{k:03d}"),
                            )
                            for k in range(n_sub)
                        ],
                    )
                    for p_i in range(n_prov)
                ],
            )
        ],
    )
    registry = InMemoryRegistry()
    registry.add(cl)
    registry.add(h)
    for i in range(n_flows):
        dsd = DataStructure(
            id=f"DSD{i:03d}",
            maintainer="TEST",
            version="1.0",
            name=InternationalString(en="dsd"),
            dimensions=[Dimension(id="REF_AREA", representation=cl)],
        )
        flow = Dataflow(
            id=f"DF{i:03d}",
            maintainer="TEST",
            version="1.0",
            name=InternationalString(en="flow"),
            structure=dsd,
        )
        dsd_urn = artefact_urn(dsd)
        dim_urn = SdmxUrn.make(
            Dimension,
            package="datastructure",
            artefact_class="Dimension",
            agency=dsd_urn.agency,
            id=dsd_urn.id,
            version=dsd_urn.version,
            item_id="REF_AREA",
        )
        registry.add(dsd)
        registry.add(flow)
        registry.add(
            HierarchyAssociation(
                id=f"HA{i:03d}",
                maintainer="TEST",
                version="1.0",
                name=InternationalString(en="ha"),
                linked_hierarchy=Ref.to(h),
                linked_object=Ref[Dimension].unresolved(dim_urn),
                context_object=Ref.to(flow),
            )
        )
    return registry


class TestDescentJoinShape:
    """The descendant set must be *joined on*, never scanned per row.

    Joining the applicability relation on ``dataflow_urn`` alone and testing
    ``list_contains(codes, code_id)`` gives the right answer and is a nested
    loop: one linear scan of the descendant list per (dataflow x code) row.
    Measured at 100 dataflows over a 3k-code geography, that was 7.2s
    against 49ms for the ``unnest`` + equality form — and the gap widens
    with catalog size. Every other test here uses three codes and one
    dataflow, where the two are indistinguishable.
    """

    def test_compiles_to_a_join_on_code_id_not_a_list_scan(self):
        from statcatlib.store.store import _QueryContext

        store = sc.Catalog().open()
        registry, _df_urn, _h = _make_descent_registry(with_ha=True)
        sc.ingest_sdmx(store, registry)

        ctx = _QueryContext(row_alias="d", registry=store.registry)
        sql, _params = q.within("REF_AREA", "CA").to_sql(ctx)

        assert "unnest(codes)" in sql, "descendant set is not unnested to rows"
        assert "code_id = ef1_c.code_id" in sql, "no equality join onto the code alias"
        assert "list_contains" not in sql, (
            "descendant set is scanned per row rather than joined — see the "
            "docstring on SdmxCodeFilter._compile_descent"
        )

    @pytest.mark.parametrize(("root", "expected"), [("CA", 40), ("P00", 40), ("P00-000", 0)])
    def test_answers_correctly_over_a_wide_catalog(self, root, expected):
        """40 dataflows, ~470 codes. Same answers the small fixtures give."""
        store = sc.Catalog().open()
        sc.ingest_sdmx(store, _make_wide_descent_registry(n_flows=40, n_prov=15, n_sub=30))

        result = store.query(sc.StatisticalDataset).filter(q.within("REF_AREA", root)).execute()
        assert result.total == expected

    def test_stays_fast_enough_to_be_a_join(self):
        """A generous ceiling that only a per-row scan can breach.

        ~46ms when joined, ~7s when scanned, so the bound is ~40x over the
        real cost and ~3x under the regression. Timing assertions are
        fragile; this one is here because the structural check above cannot
        see a scan reintroduced further down the query.
        """
        import time

        store = sc.Catalog().open()
        sc.ingest_sdmx(store, _make_wide_descent_registry(n_flows=100, n_prov=30, n_sub=100))

        start = time.perf_counter()
        result = store.query(sc.StatisticalDataset).filter(q.within("REF_AREA", "CA")).execute()
        elapsed = time.perf_counter() - start

        assert result.total == 100
        assert elapsed < 2.0, f"descent took {elapsed:.2f}s — the join shape has regressed"


class TestWithin:
    """``q.within(dim, root, hierarchy=h)`` shortcut over ``has_code(..., descendants_of(...))``.

    Lets callers avoid importing sdmxlib for the common census-descent case.
    Semantics must match the underlying ``has_code`` + ``descendants_of`` chain.
    """

    def test_within_with_explicit_hierarchy_matches(self):
        """``within(dim, root, hierarchy=h)`` matches dataflows with descendants."""
        store = sc.Catalog().open()
        registry, df_urn, h_admin = _make_descent_registry(with_ha=False)
        sc.ingest_sdmx(store, registry)

        result = store.query(sc.StatisticalDataset).filter(q.within("REF_AREA", "CA", hierarchy=h_admin)).execute()

        assert result.total == 1
        assert result.items[0].sdmx_dataflow_urn == df_urn

    def test_within_no_hierarchy_falls_back_to_codelist_parent_chain(self):
        """Without a HierarchyAssociation, ``within`` walks ``ancestor_ids``."""
        store = sc.Catalog().open()
        registry, df_urn, _h = _make_descent_registry(with_ha=False)
        sc.ingest_sdmx(store, registry)

        result = store.query(sc.StatisticalDataset).filter(q.within("REF_AREA", "CA")).execute()

        assert result.total == 1
        assert result.items[0].sdmx_dataflow_urn == df_urn

    def test_within_leaf_no_match(self):
        """Leaf code has no descendants — empty result."""
        store = sc.Catalog().open()
        registry, _df_urn, h_admin = _make_descent_registry(with_ha=False)
        sc.ingest_sdmx(store, registry)

        result = store.query(sc.StatisticalDataset).filter(q.within("REF_AREA", "TOR", hierarchy=h_admin)).execute()
        assert result.total == 0
