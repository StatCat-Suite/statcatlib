"""Build ``tests/fixtures/v3_catalog.tar.xz``: a catalog whose search indexes are format 3.

Run it with statcatlib 0.20.0, the last release that writes format 3, from a
directory outside the repository, so the environment cannot import this
checkout's ``src/``:

    cd "$(mktemp -d)"
    env -u VIRTUAL_ENV uv run --no-project --isolated --with "statcatlib[search]==0.20.0" \
        python -I /path/to/statcatlib/tools/make_v3_fixture.py

The archive's ``index.json`` files then record ``"version": 3`` and
``"written_by": "0.20.0"``.

The catalog holds two products. ``sealed`` has one live edition: an SDMX
dataflow with a coded dimension, its concepts and a reference-metadata set,
and a CKAN-shaped dataset with a publisher, spatial coverage and ``en-t-fr``
text. It is indexed with a deterministic stub encoder under the model name
``stub-encoder``, codes and reference metadata included, so its dataset,
code and concept rows carry vectors and ``text_sha256``. ``open`` has a live edition and an
unsealed working copy indexed under 0.20. Every table has the format 3
columns: ``text``, and no concept columns. The tests that read it
(``tests/test_index_v3.py``) unpack it into a temporary directory. It is
checked in as an archive: the DuckDB files are mostly empty pages, about 8 MB
unpacked and under 50 kB compressed (``docs/design/label-lookup.md``,
LL-D15).
"""

from __future__ import annotations

import hashlib
import math
import shutil
import tarfile
import tempfile
from pathlib import Path

import sdmxlib as sl

import statcatlib as sc

ARCHIVE = Path(__file__).resolve().parent.parent / "tests" / "fixtures" / "v3_catalog.tar.xz"
MODEL_NAME = "stub-encoder"


class StubEncoder:
    """Deterministic 8-dimension unit vectors from the SHA-256 of the text."""

    DIM = 8

    def encode(self, text: str | list[str]) -> list[float] | list[list[float]]:
        """Encode one text or a list of texts."""
        if isinstance(text, list):
            return [self._one(t) for t in text]
        return self._one(text)

    def _one(self, text: str) -> list[float]:
        digest = hashlib.sha256(text.encode()).digest()
        v = [b / 255.0 + 0.01 for b in digest[: self.DIM]]
        norm = math.sqrt(sum(x * x for x in v))
        return [x / norm for x in v]


def _i(**texts: str) -> sc.InternationalString:
    return sc.InternationalString.of({tag.replace("_", "-"): text for tag, text in texts.items()})


def _dataset(identifier: str, word: str) -> sc.OpenDataset:
    title = _i(en=f"{word} estimates", fr=f"Estimations {word}", en_t_fr=f"{word} totals")
    return sc.OpenDataset(identifier=identifier, title=title, description=title)


def _rich_dataset() -> sc.OpenDataset:
    """A CKAN-shaped dataset: a publisher, spatial coverage, and ``en-t-fr`` text."""
    return sc.OpenDataset(
        identifier="rain",
        title=_i(en_t_fr="Rainfall totals", fr="Totaux des précipitations"),
        description=_i(en="Monthly rainfall by province", fr="Précipitations mensuelles par province"),
        publisher=sc.Agent(id="STC", name=_i(en="Statistics Canada", fr="Statistique Canada")),
        spatial_coverage=[
            sc.SpatialCoverage(label=_i(en="Ontario", fr="Ontario")),
            sc.SpatialCoverage(uri="https://example.org/region/24", label=_i(fr="Québec")),
        ],
    )


def _concept_ref(scheme: sl.ConceptScheme, concept_id: str) -> sl.Ref[sl.Concept]:
    urn = sl.SdmxUrn.make(
        sl.Concept,
        package="conceptscheme",
        artefact_class="Concept",
        agency=scheme.agency_id,
        id=scheme.id,
        version=scheme.version,
        item_id=concept_id,
    )
    return sl.Ref.of(urn, scheme.concepts[concept_id])


def _sdmx_artefacts() -> list[object]:
    """A dataflow with a coded dimension, its concepts, and a reference-metadata set."""
    concepts = sl.ConceptScheme(
        id="CS_V3",
        maintainer="V3",
        version="1.0",
        name=_i(en="Concepts"),
        concepts=[
            sl.Concept(id="GEO", name=_i(en="Geography", fr="Géographie"), description=_i(en="Where it was counted")),
            sl.Concept(
                id="RAIN",
                name=_i(en="Rainfall", fr="Précipitations", en_t_fr="Rain amount"),
                description=_i(en="Millimetres of rain in a month"),
            ),
            sl.Concept(id="TOPIC", name=_i(en="Topic")),
        ],
    )
    geo = sl.Codelist(
        id="CL_GEO",
        maintainer="V3",
        version="1.0",
        name=_i(en="Geography"),
        codes=[
            sl.Code(id="ON", name=_i(en="Ontario", fr="Ontario")),
            sl.Code(id="QC", name=_i(en="Quebec", fr="Québec", en_t_fr="Quebec province")),
        ],
    )
    dsd = sl.DataStructure(
        id="DSD_RAIN",
        maintainer="V3",
        version="1.0",
        name=_i(en="Rainfall"),
        dimensions=[sl.Dimension(id="GEO", concept=_concept_ref(concepts, "GEO"), representation=geo)],
    )
    flow = sl.Dataflow(
        id="DF_RAIN",
        maintainer="V3",
        version="1.0",
        name=_i(en="Rainfall by province", fr="Précipitations par province"),
        structure=dsd,
    )
    msd = sl.MetadataStructure(
        id="MSD_V3",
        maintainer="V3",
        version="1.0",
        name=_i(en="Guide"),
        attributes=[
            sl.MetadataAttribute(
                id="TOPIC",
                concept=_concept_ref(concepts, "TOPIC"),
                representation=sl.Facet(type=sl.DataType.XHTML),
            )
        ],
    )
    mdf = sl.Metadataflow(id="MDF_V3", maintainer="V3", version="1.0", name=_i(en="Guide"), structure=msd)
    guide = sl.MetadataSet(
        id="MS_V3",
        maintainer="V3",
        version="1.0",
        structure=mdf,
        name=_i(en="Rainfall guide"),
        targets=[sl.artefact_urn(flow)],
        attributes=[
            sl.ReportedAttribute(
                id="TOPIC",
                value=_i(en="Rainfall is measured at each weather station and summed for the month."),
            )
        ],
    )
    return [concepts, geo, dsd, flow, msd, mdf, guide]


def main() -> None:
    """Write the fixture archive, replacing any earlier one."""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp) / "v3_catalog"
        _build(root)
        ARCHIVE.parent.mkdir(parents=True, exist_ok=True)
        with tarfile.open(ARCHIVE, "w:xz") as tar:
            tar.add(root, arcname="v3_catalog")
        shutil.rmtree(root)


def _build(root: Path) -> None:
    model = StubEncoder()
    with sc.Catalog.create(root) as cat:
        wc = cat.product("sealed").working_copy(embedding_model=model)
        registry = sl.InMemoryRegistry()
        artefacts = _sdmx_artefacts()
        registry.add_all(artefacts)
        sc.ingest_sdmx(wc, registry)
        wc.registry.add_all(artefacts[4:])
        wc.put(_rich_dataset())
        wc.build_index(embedding_model=model, model_name=MODEL_NAME, embed_codes=True, include_reference_metadata=True)
        wc.seal()

        wc = cat.product("open").working_copy(embedding_model=model)
        wc.put(_dataset("snow", "snowfall"))
        wc.build_index(embedding_model=model, model_name=MODEL_NAME)
        wc.seal()

        wc = cat.product("open").working_copy(embedding_model=model)
        wc.put(_dataset("hail", "hailfall"))
        wc.build_index(embedding_model=model, model_name=MODEL_NAME)
        wc.close()


if __name__ == "__main__":
    main()
