"""Tests for the CKAN source connector."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

import httpx
import pytest
from pytest_httpx import HTTPXMock
from sdmxlib import InternationalString

import statcatlib as sc
import statcatlib.query as q
from statcatlib.sources.ckan import (
    CkanClient,
    _ckan_package_to_dataset,
    _parse_dt,
    _parse_frequency,
    _parse_istring,
    _parse_keywords,
    _parse_spatial,
    _parse_subjects,
)

pytestmark = pytest.mark.unit

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

_CKAN_BASE = "https://open.canada.ca/data"
_SEARCH_URL = f"{_CKAN_BASE}/api/3/action/package_search"
_SCHEMA_URL = f"{_CKAN_BASE}/api/3/action/scheming_dataset_schema_show?type=dataset"

#: The pairs open.canada.ca's dataset schema declares.
_ALTERNATES = {"en": ["en-t-fr"], "fr": ["fr-t-en"]}

# Minimal valid CKAN package dict representing a bilingual StatCan dataset
_PKG: dict[str, Any] = {
    "id": "abc-123",
    "title": "Labour Force Survey",
    "title_translated": {"en": "Labour Force Survey", "fr": "Enquête sur la population active"},
    "notes": "Monthly survey of labour force.",
    "notes_translated": {
        "en": "Monthly survey of the Canadian labour force.",
        "fr": "Enquête mensuelle sur la population active canadienne.",
    },
    "organization": {
        "id": "statcan-statcan",
        "name": "statcan-statcan",
        "title": "Statistics Canada / Statistique Canada",
        "description": json.dumps({"en": "Statistics Canada", "fr": "Statistique Canada"}),
    },
    "metadata_created": "2022-01-15T10:00:00",
    "metadata_modified": "2024-06-01T08:30:00",
    "frequency": json.dumps({"en": "Monthly", "fr": "Mensuelle"}),
    "subject": json.dumps(["Labour", "Employment"]),
    "geographic_region": json.dumps(["Canada", "Provinces and territories"]),
    "time_period_coverage_start": "1976-01-01",
    "time_period_coverage_end": "2024-05-01",
    "keywords": {"en": ["labour", "employment", "survey"], "fr": ["travail", "emploi", "enquête"]},
    "resources": [
        {
            "id": "res-1",
            "name": "CSV Download",
            "name_translated": {"en": "CSV Download", "fr": "Téléchargement CSV"},
            "url": "https://example.com/data.csv",
            "url_type": "download",
            "format": "CSV",
            "mimetype": "text/csv",
            "size": "102400",
            "created": "2022-01-15T10:00:00",
            "last_modified": "2024-06-01T08:30:00",
        }
    ],
    "license_id": "ca-ogl-lgo",
    "license_title": "Open Government Licence - Canada",
    "license_url": "https://open.canada.ca/en/open-government-licence-canada",
    "language": json.dumps(["en", "fr"]),
    "url": "https://www150.statcan.gc.ca/t1/tbl1/en/tv.action?pid=1410028701",
}


#: A cut of open.canada.ca's ``geographic_region`` field: the
#: ``canada_geographic_region`` preset has 310 such choices.
_REGION_FIELD: dict[str, Any] = {
    "field_name": "geographic_region",
    "label": {"en": "Geographic Region Name", "fr": "Nom de la région géographique"},
    "preset": "canada_geographic_region",
    "choices": [
        {"value": "0", "label": {"en": "Canada", "fr": "Canada"}},
        {"value": "10", "label": {"en": "Newfoundland and Labrador", "fr": "Terre-Neuve-et-Labrador"}},
        {"value": "24", "label": {"en": "Quebec", "fr": "Québec"}},
    ],
}

_REGION_SCHEME = "https://open.canada.ca/data/scheming/dataset/geographic_region"


def _mock_schema(
    httpx_mock: HTTPXMock, alternates: object = _ALTERNATES, fields: list[dict[str, Any]] | None = None
) -> None:
    """Answer the dataset-schema request ``ingest_ckan`` makes after searching."""
    result: dict[str, Any] = {"dataset_type": "dataset", "alternate_languages": alternates}
    if fields is not None:
        result["dataset_fields"] = fields
    httpx_mock.add_response(url=_SCHEMA_URL, json={"success": True, "result": result})


def _make_search_response(packages: list[dict[str, Any]], count: int | None = None) -> dict[str, Any]:
    return {
        "success": True,
        "result": {
            "count": count if count is not None else len(packages),
            "results": packages,
        },
    }


# ---------------------------------------------------------------------------
# TestParseIstring
# ---------------------------------------------------------------------------


class TestParseIstring:
    def test_dict_bilingual(self):
        result = _parse_istring({"en": "Hello", "fr": "Bonjour"})
        assert result.get("en") == "Hello"
        assert result.get("fr") == "Bonjour"

    def test_dict_en_only(self):
        result = _parse_istring({"en": "Hello"})
        assert result.get("en") == "Hello"
        assert result.get("fr") is None

    def test_json_string_bilingual(self):
        raw = json.dumps({"en": "Hello", "fr": "Bonjour"})
        result = _parse_istring(raw)
        assert result.get("en") == "Hello"
        assert result.get("fr") == "Bonjour"

    def test_plain_string_is_undetermined(self):
        """CKAN declares no default language, so untagged text is ``und``, never English."""
        result = _parse_istring("Just English")
        assert result.as_dict() == {"und": "Just English"}

    def test_transformed_content_tags_kept(self):
        """open.canada.ca sends translated text under RFC 6497 tags; each is kept as given."""
        result = _parse_istring({"en-t-fr": "Hydrography", "fr": "Hydrographie"})
        assert result.as_dict() == {"en-t-fr": "Hydrography", "fr": "Hydrographie"}
        assert result.get("en") is None

    def test_json_string_with_transformed_tag(self):
        result = _parse_istring(json.dumps({"en": "Title", "fr-t-en": "Titre"}))
        assert result.as_dict() == {"en": "Title", "fr-t-en": "Titre"}

    def test_empty_values_dropped(self):
        result = _parse_istring({"en": "", "fr": "Bonjour", "de": None})
        assert result.as_dict() == {"fr": "Bonjour"}

    @pytest.mark.parametrize("raw", [None, "", "   ", {}, {"en": ""}, 42])
    def test_nothing_usable_is_empty(self, raw: object):
        assert not _parse_istring(raw)


# ---------------------------------------------------------------------------
# TestParseDt
# ---------------------------------------------------------------------------


class TestParseDt:
    def test_full_datetime(self):
        result = _parse_dt("2024-06-01T08:30:00")
        assert result == datetime(2024, 6, 1, 8, 30, 0)

    def test_date_only(self):
        result = _parse_dt("1976-01-01")
        assert result == datetime(1976, 1, 1)

    def test_none_returns_none(self):
        assert _parse_dt(None) is None

    def test_empty_string_returns_none(self):
        assert _parse_dt("") is None

    def test_garbage_returns_none(self):
        assert _parse_dt("not-a-date") is None


# ---------------------------------------------------------------------------
# TestParseFrequency
# ---------------------------------------------------------------------------


class TestParseFrequency:
    def test_json_bilingual_string(self):
        raw = json.dumps({"en": "Monthly", "fr": "Mensuelle"})
        freq = _parse_frequency(raw)
        assert freq is not None
        assert freq.id == "MONTHLY"
        assert freq.label is not None
        assert freq.label.get("fr") == "Mensuelle"

    def test_plain_string_label(self):
        freq = _parse_frequency("Annual")
        assert freq is not None
        assert freq.id == "ANNUAL"

    def test_plain_string_is_a_code(self):
        """open.canada.ca sends codes such as ``as_needed``; the label is ``zxx``, not English."""
        freq = _parse_frequency("as_needed")
        assert freq is not None
        assert freq.label is not None
        assert freq.label.as_dict() == {"zxx": "as_needed"}

    def test_dict_input(self):
        freq = _parse_frequency({"en": "Quarterly", "fr": "Trimestrielle"})
        assert freq is not None
        assert freq.id == "QUARTERLY"

    def test_none_returns_none(self):
        assert _parse_frequency(None) is None

    def test_empty_string_returns_none(self):
        assert _parse_frequency("") is None

    def test_unknown_label_uppercased(self):
        freq = _parse_frequency("Every decade")
        assert freq is not None
        assert freq.id == "EVERY_DECADE"


# ---------------------------------------------------------------------------
# TestParseKeywords
# ---------------------------------------------------------------------------


class TestParseKeywords:
    def test_bilingual_dict(self):
        raw = {"en": ["labour", "employment"], "fr": ["travail", "emploi"]}
        kws = _parse_keywords(raw)
        assert len(kws) == 2
        assert kws[0].get("en") == "labour"
        assert kws[0].get("fr") == "travail"

    def test_json_encoded_dict(self):
        raw = json.dumps({"en": ["a"], "fr": ["b"]})
        kws = _parse_keywords(raw)
        assert len(kws) == 1
        assert kws[0].get("en") == "a"

    def test_more_en_than_fr(self):
        raw = {"en": ["a", "b", "c"], "fr": ["x"]}
        kws = _parse_keywords(raw)
        assert len(kws) == 3
        assert kws[1].get("fr") is None
        assert kws[2].get("fr") is None

    def test_more_fr_than_en(self):
        raw: dict[str, list[str]] = {"en": [], "fr": ["x", "y"]}
        kws = _parse_keywords(raw)
        assert len(kws) == 2
        assert kws[0].get("fr") == "x"

    def test_transformed_content_tags_paired_by_position(self):
        raw = {"en-t-fr": ["water", "maps"], "fr": ["eau", "cartes"]}
        kws = _parse_keywords(raw)
        assert [kw.as_dict() for kw in kws] == [
            {"en-t-fr": "water", "fr": "eau"},
            {"en-t-fr": "maps", "fr": "cartes"},
        ]

    def test_empty_keywords_dropped(self):
        kws = _parse_keywords({"en": ["", "b"], "fr": ["", ""]})
        assert [kw.as_dict() for kw in kws] == [{"en": "b"}]

    def test_not_a_dict_returns_empty(self):
        assert _parse_keywords("not a dict") == []

    def test_none_returns_empty(self):
        assert _parse_keywords(None) == []


# ---------------------------------------------------------------------------
# TestParseSubjects
# ---------------------------------------------------------------------------


class TestParseSubjects:
    def test_json_list(self):
        raw = json.dumps(["Labour", "Employment"])
        refs = _parse_subjects(raw)
        assert len(refs) == 2
        assert refs[0].uri == "urn:ckan:tag:labour"
        # Subjects are the portal's codes (``government_and_politics``), not English text.
        assert refs[0].label.as_dict() == {"zxx": "Labour"}

    def test_plain_list(self):
        refs = _parse_subjects(["Health", "Demographics"])
        assert len(refs) == 2

    def test_plain_string_wrapped(self):
        refs = _parse_subjects("Economy")
        assert len(refs) == 1
        assert refs[0].uri == "urn:ckan:tag:economy"

    def test_empty_list(self):
        assert _parse_subjects([]) == []

    def test_none_returns_empty(self):
        assert _parse_subjects(None) == []


# ---------------------------------------------------------------------------
# TestParseSpatial
# ---------------------------------------------------------------------------


class TestParseSpatial:
    def test_json_list(self):
        raw = json.dumps(["Canada", "Provinces and territories"])
        regions = _parse_spatial(raw)
        assert len(regions) == 2
        # Free text in no declared language is stored under ``und``.
        assert regions[0] == sc.SpatialCoverage(label=InternationalString(und="Canada"))

    def test_plain_list(self):
        regions = _parse_spatial(["Canada"])
        assert len(regions) == 1

    def test_none_returns_empty(self):
        assert _parse_spatial(None) == []

    def test_a_known_code_becomes_the_region_identifier(self):
        regions = sc.ConceptScheme(
            uri=_REGION_SCHEME,
            label=InternationalString(en="Regions"),
            concepts={"10": sc.Concept(notation="10", label=InternationalString(en="Newfoundland and Labrador"))},
        )
        assert _parse_spatial(json.dumps(["10", "Nunavik"]), regions) == [
            sc.SpatialCoverage(uri=f"{_REGION_SCHEME}/10"),
            sc.SpatialCoverage(label=InternationalString(und="Nunavik")),
        ]


# ---------------------------------------------------------------------------
# TestCkanPackageToDataset
# ---------------------------------------------------------------------------


class TestCkanPackageToDataset:
    def test_identifier_is_ckan_id(self):
        ds = _ckan_package_to_dataset(_PKG)
        assert ds.identifier == "abc-123"

    def test_title_from_title_translated(self):
        ds = _ckan_package_to_dataset(_PKG)
        assert ds.title.get("en") == "Labour Force Survey"
        assert ds.title.get("fr") == "Enquête sur la population active"

    def test_title_fallback_to_plain_title(self):
        pkg = dict(_PKG)
        del pkg["title_translated"]
        ds = _ckan_package_to_dataset(pkg)
        assert ds.title.as_dict() == {"und": "Labour Force Survey"}

    def test_translated_title_keeps_tags_and_no_id_title(self):
        """A package with only ``en-t-fr`` English keeps it, and the id never becomes the English title."""
        pkg = {
            **_PKG,
            "title_translated": {"en-t-fr": "Hydrography of Montreal", "fr": "Hydrographie de Montréal"},
            "notes_translated": {"en-t-fr": "Watercourses.", "fr": "Cours d'eau."},
        }
        ds = _ckan_package_to_dataset(pkg)
        assert ds.title.as_dict() == {"en-t-fr": "Hydrography of Montreal", "fr": "Hydrographie de Montréal"}
        assert ds.description.as_dict() == {"en-t-fr": "Watercourses.", "fr": "Cours d'eau."}

    def test_no_title_is_empty(self):
        pkg = {k: v for k, v in _PKG.items() if k not in ("title", "title_translated")}
        ds = _ckan_package_to_dataset(pkg)
        assert not ds.title

    def test_description_bilingual(self):
        ds = _ckan_package_to_dataset(_PKG)
        assert ds.description.get("en") == "Monthly survey of the Canadian labour force."
        assert ds.description.get("fr") == "Enquête mensuelle sur la population active canadienne."

    def test_publisher_name(self):
        ds = _ckan_package_to_dataset(_PKG)
        assert ds.publisher is not None
        assert ds.publisher.id == "statcan-statcan"
        # A plain organization title carries no language.
        assert ds.publisher.name.as_dict() == {"und": "Statistics Canada / Statistique Canada"}

    def test_publisher_without_title_named_by_slug(self):
        pkg = {**_PKG, "organization": {"name": "statcan-statcan"}}
        ds = _ckan_package_to_dataset(pkg)
        assert ds.publisher is not None
        assert ds.publisher.name.as_dict() == {"zxx": "statcan-statcan"}

    def test_issued_parsed(self):
        ds = _ckan_package_to_dataset(_PKG)
        assert ds.issued == datetime(2022, 1, 15, 10, 0, 0)

    def test_modified_parsed(self):
        ds = _ckan_package_to_dataset(_PKG)
        assert ds.modified == datetime(2024, 6, 1, 8, 30, 0)

    def test_frequency_normalized(self):
        ds = _ckan_package_to_dataset(_PKG)
        assert ds.frequency is not None
        assert ds.frequency.id == "MONTHLY"
        assert ds.frequency.label is not None
        assert ds.frequency.label.get("fr") == "Mensuelle"

    def test_themes_from_subject(self):
        ds = _ckan_package_to_dataset(_PKG)
        assert len(ds.themes) == 2
        uris = {ref.uri for ref in ds.themes}
        assert "urn:ckan:tag:labour" in uris
        assert "urn:ckan:tag:employment" in uris

    def test_keywords_bilingual(self):
        ds = _ckan_package_to_dataset(_PKG)
        assert len(ds.keywords) == 3
        assert ds.keywords[0].get("en") == "labour"
        assert ds.keywords[0].get("fr") == "travail"

    def test_spatial_coverage(self):
        ds = _ckan_package_to_dataset(_PKG)
        assert len(ds.spatial_coverage) == 2
        labels = [cov.label.get("und") for cov in ds.spatial_coverage if cov.label]
        assert labels == ["Canada", "Provinces and territories"]

    def test_temporal_coverage(self):
        ds = _ckan_package_to_dataset(_PKG)
        assert ds.temporal_coverage is not None
        assert ds.temporal_coverage.start == datetime(1976, 1, 1)
        assert ds.temporal_coverage.end == datetime(2024, 5, 1)

    def test_distribution_created(self):
        ds = _ckan_package_to_dataset(_PKG)
        assert len(ds.distributions) == 1
        d = ds.distributions[0]
        assert d.id == "res-1"
        assert d.format == "CSV"
        assert d.download_url == "https://example.com/data.csv"
        assert d.byte_size == 102400

    def test_distribution_title_bilingual(self):
        ds = _ckan_package_to_dataset(_PKG)
        d = ds.distributions[0]
        assert d.title is not None
        assert d.title.get("en") == "CSV Download"
        assert d.title.get("fr") == "Téléchargement CSV"

    def test_licence(self):
        ds = _ckan_package_to_dataset(_PKG)
        assert ds.licence is not None
        assert ds.licence.id == "ca-ogl-lgo"
        assert ds.licence.uri == "https://open.canada.ca/en/open-government-licence-canada"
        assert ds.licence.label is not None
        assert ds.licence.label.as_dict() == {"und": "Open Government Licence - Canada"}

    def test_language(self):
        ds = _ckan_package_to_dataset(_PKG)
        assert "en" in ds.language
        assert "fr" in ds.language

    def test_landing_page(self):
        ds = _ckan_package_to_dataset(_PKG)
        assert ds.landing_page is not None

    def test_no_title_translated_uses_title(self):
        pkg = {**_PKG, "title": "Fallback Title"}
        del pkg["title_translated"]
        ds = _ckan_package_to_dataset(pkg)
        assert ds.title.as_dict() == {"und": "Fallback Title"}

    def test_empty_resources_gives_no_distributions(self):
        pkg: dict[str, object] = {**_PKG, "resources": []}
        ds = _ckan_package_to_dataset(pkg)
        assert ds.distributions == []

    def test_missing_temporal_fields_gives_none(self):
        pkg = {k: v for k, v in _PKG.items() if "time_period" not in k}
        ds = _ckan_package_to_dataset(pkg)
        assert ds.temporal_coverage is None

    def test_no_organization_gives_no_publisher(self):
        pkg = {**_PKG, "organization": None}
        ds = _ckan_package_to_dataset(pkg)
        assert ds.publisher is None


# ---------------------------------------------------------------------------
# TestCkanClient
# ---------------------------------------------------------------------------


class TestCkanClient:
    def test_package_search_returns_list(self, httpx_mock: HTTPXMock):
        httpx_mock.add_response(
            url=f"{_SEARCH_URL}?q=%2A%3A%2A&rows=100&start=0",
            json=_make_search_response([_PKG]),
        )
        with CkanClient(_CKAN_BASE) as client:
            results = client.package_search(rows=100)
        assert len(results) == 1
        assert results[0]["id"] == "abc-123"

    def test_package_search_organization_filter(self, httpx_mock: HTTPXMock):
        httpx_mock.add_response(
            url=f"{_SEARCH_URL}?q=organization%3Astatcan-statcan&rows=5&start=0",
            json=_make_search_response([_PKG], count=1),
        )
        with CkanClient(_CKAN_BASE) as client:
            results = client.package_search(organization="statcan-statcan", rows=5)
        assert len(results) == 1

    def test_package_search_stops_when_count_exhausted(self, httpx_mock: HTTPXMock):
        # Portal claims count=1 so pagination should stop after first page
        httpx_mock.add_response(
            url=f"{_SEARCH_URL}?q=%2A%3A%2A&rows=100&start=0",
            json=_make_search_response([_PKG], count=1),
        )
        with CkanClient(_CKAN_BASE) as client:
            results = client.package_search(rows=100)
        assert len(results) == 1

    def test_package_search_paginates(self, httpx_mock: HTTPXMock):
        pkg2 = {**_PKG, "id": "xyz-456"}
        # page_size = min(_ROWS_PER_PAGE=100, remaining=2) = 2 for first call.
        # First page returns only 1 result but count=2 → triggers a second call.
        httpx_mock.add_response(
            url=f"{_SEARCH_URL}?q=%2A%3A%2A&rows=2&start=0",
            json=_make_search_response([_PKG], count=2),
        )
        httpx_mock.add_response(
            url=f"{_SEARCH_URL}?q=%2A%3A%2A&rows=1&start=1",
            json=_make_search_response([pkg2], count=2),
        )
        with CkanClient(_CKAN_BASE) as client:
            results = client.package_search(rows=2)
        assert len(results) == 2
        assert {r["id"] for r in results} == {"abc-123", "xyz-456"}

    def test_get_raises_on_api_error(self, httpx_mock: HTTPXMock):
        httpx_mock.add_response(
            url=f"{_SEARCH_URL}?q=%2A%3A%2A&rows=100&start=0",
            json={"success": False, "error": {"message": "Not found"}},
        )
        with CkanClient(_CKAN_BASE) as client, pytest.raises(RuntimeError, match="CKAN API error"):
            client.package_search()

    def test_field_choices_is_a_concept_scheme(self, httpx_mock: HTTPXMock):
        _mock_schema(httpx_mock, fields=[{"field_name": "title"}, _REGION_FIELD])
        with CkanClient(_CKAN_BASE) as client:
            scheme = client.field_choices("geographic_region")
        assert scheme is not None
        assert scheme.uri == _REGION_SCHEME
        assert scheme.label.get("fr") == "Nom de la région géographique"
        assert list(scheme.concepts) == ["0", "10", "24"]
        assert scheme.concepts["10"].label == InternationalString(
            en="Newfoundland and Labrador", fr="Terre-Neuve-et-Labrador"
        )

    @pytest.mark.parametrize(
        "fields",
        [None, [{"field_name": "title"}], [{"field_name": "geographic_region", "choices": []}]],
        ids=["no-fields", "field-absent", "no-choices"],
    )
    def test_field_choices_none_without_choices(self, httpx_mock: HTTPXMock, fields: list[dict[str, Any]] | None):
        _mock_schema(httpx_mock, fields=fields)
        with CkanClient(_CKAN_BASE) as client:
            assert client.field_choices("geographic_region") is None

    def test_one_schema_request_serves_every_schema_read(self, httpx_mock: HTTPXMock):
        _mock_schema(httpx_mock, fields=[_REGION_FIELD])
        with CkanClient(_CKAN_BASE) as client:
            assert client.alternate_languages() == _ALTERNATES
            assert client.field_choices("geographic_region") is not None
        assert len(httpx_mock.get_requests()) == 1


# ---------------------------------------------------------------------------
# TestIngestCkan (end-to-end with in-memory store)
# ---------------------------------------------------------------------------


class TestIngestCkan:
    def test_ingests_datasets_into_store(self, httpx_mock: HTTPXMock):
        httpx_mock.add_response(
            url=f"{_SEARCH_URL}?q=%2A%3A%2A&rows=10&start=0",
            json=_make_search_response([_PKG], count=1),
        )
        _mock_schema(httpx_mock)
        store = sc.Catalog().open()
        datasets = sc.ingest_ckan(store, _CKAN_BASE, limit=10)
        assert len(datasets) == 1
        retrieved = store.get(sc.OpenDataset, "abc-123")
        assert retrieved is not None
        assert retrieved.title.get("en") == "Labour Force Survey"

    def test_returns_list_of_datasets(self, httpx_mock: HTTPXMock):
        httpx_mock.add_response(
            url=f"{_SEARCH_URL}?q=%2A%3A%2A&rows=5&start=0",
            json=_make_search_response([_PKG], count=1),
        )
        _mock_schema(httpx_mock)
        store = sc.Catalog().open()
        datasets = sc.ingest_ckan(store, _CKAN_BASE, limit=5)
        assert isinstance(datasets, list)
        assert all(isinstance(d, sc.OpenDataset) for d in datasets)

    def test_organization_filter_passed_to_client(self, httpx_mock: HTTPXMock):
        httpx_mock.add_response(
            url=f"{_SEARCH_URL}?q=organization%3Astatcan-statcan&rows=10&start=0",
            json=_make_search_response([_PKG], count=1),
        )
        _mock_schema(httpx_mock)
        store = sc.Catalog().open()
        datasets = sc.ingest_ckan(store, _CKAN_BASE, organization="statcan-statcan", limit=10)
        assert len(datasets) == 1

    def test_dataset_is_searchable_after_ingest(self, httpx_mock: HTTPXMock, tmp_path: Path):
        """An ingested CKAN dataset is findable by title.

        Folder-backed, because `search()` needs somewhere to put ``search/``
        and must be given an index to read — which is why this was skipped
        rather than failing.
        """
        pytest.importorskip("lancedb", reason="search needs statcatlib[search]")
        httpx_mock.add_response(
            url=f"{_SEARCH_URL}?q=%2A%3A%2A&rows=5&start=0",
            json=_make_search_response([_PKG], count=1),
        )
        _mock_schema(httpx_mock)
        with sc.Catalog(tmp_path / "catalog") as store:
            sc.ingest_ckan(store, _CKAN_BASE, limit=5)
            store.build_index()
            result = store.search("Labour Force Survey")
        assert result.total >= 1

    def test_ingest_is_idempotent(self, httpx_mock: HTTPXMock):
        httpx_mock.add_response(
            url=f"{_SEARCH_URL}?q=%2A%3A%2A&rows=5&start=0",
            json=_make_search_response([_PKG], count=1),
        )
        httpx_mock.add_response(
            url=f"{_SEARCH_URL}?q=%2A%3A%2A&rows=5&start=0",
            json=_make_search_response([_PKG], count=1),
        )
        _mock_schema(httpx_mock)
        _mock_schema(httpx_mock)
        store = sc.Catalog().open()
        sc.ingest_ckan(store, _CKAN_BASE, limit=5)
        sc.ingest_ckan(store, _CKAN_BASE, limit=5)
        count = store.query(sc.OpenDataset).count()
        assert count == 1  # upsert, not duplicate
        assert len(store.language_alternates()) == 2  # recorded once, not twice

    def test_records_declared_language_pairs(self, httpx_mock: HTTPXMock):
        httpx_mock.add_response(
            url=f"{_SEARCH_URL}?q=%2A%3A%2A&rows=5&start=0",
            json=_make_search_response([_PKG], count=1),
        )
        _mock_schema(httpx_mock)
        store = sc.Catalog().open()
        sc.ingest_ckan(store, _CKAN_BASE, limit=5)
        assert store.language_alternates() == [
            {"primary_tag": "en", "alternate_tag": "en-t-fr", "source": _CKAN_BASE},
            {"primary_tag": "fr", "alternate_tag": "fr-t-en", "source": _CKAN_BASE},
        ]

    @pytest.mark.parametrize("status", [400, 404])
    def test_portal_without_schema_records_no_pairs(self, httpx_mock: HTTPXMock, status: int):
        """A portal without ckanext-scheming does not know the action; ingest still succeeds."""
        httpx_mock.add_response(
            url=f"{_SEARCH_URL}?q=%2A%3A%2A&rows=5&start=0",
            json=_make_search_response([_PKG], count=1),
        )
        httpx_mock.add_response(url=_SCHEMA_URL, status_code=status, json={"success": False})
        store = sc.Catalog().open()
        datasets = sc.ingest_ckan(store, _CKAN_BASE, limit=5)
        assert len(datasets) == 1
        assert store.language_alternates() == []

    def test_schema_without_pairs_records_none(self, httpx_mock: HTTPXMock):
        httpx_mock.add_response(
            url=f"{_SEARCH_URL}?q=%2A%3A%2A&rows=5&start=0",
            json=_make_search_response([_PKG], count=1),
        )
        _mock_schema(httpx_mock, alternates=None)
        store = sc.Catalog().open()
        sc.ingest_ckan(store, _CKAN_BASE, limit=5)
        assert store.language_alternates() == []


class TestIngestRegionCodes:
    """``geographic_region`` codes become identifiers labelled by their concepts (GH-83)."""

    @pytest.fixture
    def store(self, httpx_mock: HTTPXMock) -> sc.Catalog:
        httpx_mock.add_response(
            url=f"{_SEARCH_URL}?q=%2A%3A%2A&rows=5&start=0",
            json=_make_search_response(
                [
                    {**_PKG, "id": "nl", "geographic_region": json.dumps(["0", "10"])},
                    {**_PKG, "id": "qc", "geographic_region": ["0", "24"]},
                ],
                count=2,
            ),
        )
        _mock_schema(httpx_mock, fields=[_REGION_FIELD])
        store = sc.Catalog().open()
        sc.ingest_ckan(store, _CKAN_BASE, limit=5)
        return store

    def test_the_choices_are_stored_as_a_concept_scheme(self, store: sc.Catalog):
        scheme = store.get_concept_scheme(_REGION_SCHEME)
        assert scheme is not None
        assert scheme.concepts["10"].label.get("fr") == "Terre-Neuve-et-Labrador"

    def test_a_code_is_stored_as_the_region_uri_with_no_label(self, store: sc.Catalog):
        ds = store.get(sc.OpenDataset, "nl")
        assert ds is not None
        assert ds.spatial_coverage == [
            sc.SpatialCoverage(uri=f"{_REGION_SCHEME}/0"),
            sc.SpatialCoverage(uri=f"{_REGION_SCHEME}/10"),
        ]

    def test_the_spatial_facet_is_keyed_by_region_uri(self, store: sc.Catalog):
        assert store.facets("spatial")["spatial"] == {
            f"{_REGION_SCHEME}/0": 2,
            f"{_REGION_SCHEME}/10": 1,
            f"{_REGION_SCHEME}/24": 1,
        }

    @pytest.mark.parametrize(
        ("lang", "label"), [("en", "Newfoundland and Labrador"), ("fr", "Terre-Neuve-et-Labrador")]
    )
    def test_spatial_labels_come_from_the_concepts(self, store: sc.Catalog, lang: str, label: str):
        assert store.spatial_labels(lang)[f"{_REGION_SCHEME}/10"] == label

    def test_has_spatial_matches_a_concept_label(self, store: sc.Catalog):
        found = store.query(sc.OpenDataset).filter(q.has_spatial("Québec", lang="fr")).collect()
        assert [ds.identifier for ds in found] == ["qc"]

    def test_query_facets_are_keyed_by_region_uri(self, store: sc.Catalog):
        result = store.query(sc.OpenDataset).filter(q.has_spatial("Québec", lang="fr")).with_facets("spatial").execute()
        assert result.facets["spatial"] == {f"{_REGION_SCHEME}/0": 1, f"{_REGION_SCHEME}/24": 1}

    def test_a_facet_key_filters_back_to_its_datasets(self, store: sc.Catalog):
        key = f"{_REGION_SCHEME}/24"
        found = store.query(sc.OpenDataset).filter(q.has_spatial_uri(key)).collect()
        assert [ds.identifier for ds in found] == ["qc"]

    def test_free_text_regions_are_counted_under_their_text(self, httpx_mock: HTTPXMock):
        httpx_mock.add_response(
            url=f"{_SEARCH_URL}?q=%2A%3A%2A&rows=5&start=0",
            json=_make_search_response([{**_PKG, "geographic_region": ["10", "Nunavik"]}], count=1),
        )
        _mock_schema(httpx_mock, fields=[_REGION_FIELD])
        store = sc.Catalog().open()
        sc.ingest_ckan(store, _CKAN_BASE, limit=5)
        assert store.facets("spatial")["spatial"] == {f"{_REGION_SCHEME}/10": 1, "Nunavik": 1}
        assert store.spatial_labels("fr")["Nunavik"] == "Nunavik"
        found = store.query(sc.OpenDataset).filter(q.has_spatial("Nunavik", lang="und")).collect()
        assert [ds.identifier for ds in found] == ["abc-123"]

    def test_a_failed_schema_request_raises(self, httpx_mock: HTTPXMock):
        """A 503 is reported: read as "no schema" it would store region codes as text."""
        httpx_mock.add_response(
            url=f"{_SEARCH_URL}?q=%2A%3A%2A&rows=5&start=0",
            json=_make_search_response([_PKG], count=1),
        )
        httpx_mock.add_response(url=_SCHEMA_URL, status_code=503)
        with pytest.raises(httpx.HTTPStatusError, match="503"):
            sc.ingest_ckan(sc.Catalog().open(), _CKAN_BASE, limit=5)
