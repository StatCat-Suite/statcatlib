"""Generic CKAN source connector for statcatlib.

Ingests datasets from any CKAN portal (open.canada.ca, data.gov, etc.)
into the catalog as OpenDataset objects.

The open.canada.ca portal stores many metadata fields as JSON-encoded
strings rather than native JSON objects. The helpers here handle both
representations defensively.

Language tags are kept as the portal sends them, including RFC 6497
transformed-content tags such as ``en-t-fr`` (English translated from
French). CKAN declares no default language, so a plain string is stored
under ``und`` and a code stored as text under ``zxx``. No field is given an
English value the portal did not send (GH-84).
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import datetime
from typing import TYPE_CHECKING, Self

import httpx
from sdmxlib import InternationalString

from statcatlib.model.agents import Agent
from statcatlib.model.dataset import OpenDataset
from statcatlib.model.distribution import Distribution
from statcatlib.model.licence import LicenceDocument
from statcatlib.model.skos import ConceptRef
from statcatlib.model.spatial import SpatialCoverage
from statcatlib.model.temporal import Frequency, TemporalCoverage

if TYPE_CHECKING:
    from statcatlib.store.store import Catalog

__all__ = ["CkanClient", "ingest_ckan"]

# Frequency label (lowercase English) → normalized catalog ID
_FREQ_LABEL_TO_ID: dict[str, str] = {
    "daily": "DAILY",
    "weekly": "WEEKLY",
    "biweekly": "BIWEEKLY",
    "bi-weekly": "BIWEEKLY",
    "monthly": "MONTHLY",
    "quarterly": "QUARTERLY",
    "semi-annual": "SEMIANNUAL",
    "semi-annually": "SEMIANNUAL",
    "annual": "ANNUAL",
    "annually": "ANNUAL",
    "occasional": "OCCASIONAL",
    "as needed": "IRREGULAR",
    "irregular": "IRREGULAR",
    "not planned": "NOT_PLANNED",
    "unknown": "UNKNOWN",
}

_ROWS_PER_PAGE = 100

#: BCP 47 tag for text the portal sends with no language: undetermined.
_UNDETERMINED = "und"

#: BCP 47 tag for a code stored as text: no linguistic content.
_NO_LANGUAGE = "zxx"


class CkanClient:
    """HTTP client for CKAN portals.

    Uses the CKAN Action API v3 (``/api/3/action/``). Handles pagination
    internally — callers see a flat list up to their requested limit.
    """

    def __init__(self, base_url: str, *, timeout: float = 30.0) -> None:
        self._base = base_url.rstrip("/")
        self._client = httpx.Client(timeout=timeout)

    def package_search(
        self,
        *,
        organization: str | None = None,
        subject: str | None = None,
        rows: int = 100,
        start: int = 0,
    ) -> list[dict[str, object]]:
        """Search for packages (datasets) and return raw CKAN package dicts.

        Paginates automatically until ``rows`` results are collected or
        the portal has no more results.

        Args:
            organization: Filter by CKAN organization name slug.
            subject: Additional free-text filter appended to the query.
            rows: Maximum total results to return.
            start: Offset into the result set (for external pagination).

        Returns:
            The raw ``result.results`` list from CKAN's ``package_search``
            endpoint — package dicts, unmodelled.
        """
        parts: list[str] = []
        if organization:
            parts.append(f"organization:{organization}")
        if subject:
            parts.append(subject)
        q = " ".join(parts) if parts else "*:*"

        results: list[dict[str, object]] = []
        offset = start
        remaining = rows

        while remaining > 0:
            page_size = min(_ROWS_PER_PAGE, remaining)
            page = self._get("package_search", {"q": q, "rows": page_size, "start": offset})
            if not isinstance(page, Mapping):
                break
            batch = [pkg for pkg in _seq(page.get("results")) if isinstance(pkg, dict)]
            if not batch:
                break
            results.extend(batch)
            remaining -= len(batch)
            offset += len(batch)
            # Stop if we've exhausted the portal's result set
            if len(results) + start >= (_i(page.get("count")) or 0):
                break

        return results

    def alternate_languages(self, dataset_type: str = "dataset") -> dict[str, list[str]]:
        """Return the language pairs the portal's dataset schema declares.

        Portals running ckanext-scheming with ckanext-fluent publish
        ``alternate_languages`` in ``scheming_dataset_schema_show``;
        open.canada.ca declares ``{"en": ["en-t-fr"], "fr": ["fr-t-en"]}``.

        Args:
            dataset_type: The CKAN dataset type whose schema to read.

        Returns:
            Each primary tag mapped to its alternate tags. Empty when the
            portal has no scheming schema or the schema declares no pairs.
        """
        try:
            schema = self._get("scheming_dataset_schema_show", {"type": dataset_type})
        except httpx.HTTPStatusError, RuntimeError:
            return {}
        if not isinstance(schema, Mapping):
            return {}
        declared = schema.get("alternate_languages")
        if not isinstance(declared, Mapping):
            return {}
        return {
            primary: [alt for alt in _seq(alts) if isinstance(alt, str) and alt.strip()]
            for primary, alts in declared.items()
            if isinstance(primary, str) and primary.strip() and _seq(alts)
        }

    def _get(self, action: str, params: dict[str, str | int]) -> object:
        url = f"{self._base}/api/3/action/{action}"
        r = self._client.get(url, params=params)
        r.raise_for_status()
        # `r.json()` is `Any`; annotating the receiving local is what keeps it
        # from riding out through the return.
        body: object = r.json()
        if not isinstance(body, Mapping):
            msg = f"CKAN API returned {type(body).__name__} for {action!r}, expected an object"
            raise TypeError(msg)
        if not body.get("success"):
            msg = f"CKAN API error for {action!r}: {body.get('error', body)}"
            raise RuntimeError(msg)
        return body.get("result")

    def close(self) -> None:
        """Close the underlying HTTP client."""
        self._client.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()


# ---------------------------------------------------------------------------
# Internal mapper helpers
# ---------------------------------------------------------------------------

# ── Narrowing accessors for the untyped shell ─────────────────────────────
#
# CKAN's payloads are `object` at this boundary, deliberately: a raw JSON dict
# typed `Any` would hand an unchecked value to every mapper below and out into
# the model. These narrow instead — "narrow, don't cast" — so a portal that
# returns a number where a string is expected produces `None` here rather than
# a `TypeError` three frames away.


def _s(d: Mapping[str, object], key: str) -> str | None:
    """The `str` at *key*, or ``None`` when absent, empty, or another type."""
    v = d.get(key)
    return v if isinstance(v, str) and v else None


def _i(v: object) -> int | None:
    """*v* as an `int`, or ``None`` when it is neither an int nor an int-ish str."""
    if isinstance(v, bool):  # bool is an int; a flag is not a size
        return None
    if isinstance(v, int):
        return v
    if isinstance(v, str):
        try:
            return int(v)
        except ValueError:
            return None
    return None


def _d(d: Mapping[str, object], key: str) -> Mapping[str, object]:
    """The nested mapping at *key*, or an empty one."""
    v = d.get(key)
    return v if isinstance(v, Mapping) else {}


def _seq(v: object) -> list[object]:
    """*v* as a list, or empty. A `str` is not a sequence of items here."""
    return list(v) if isinstance(v, list) else []


def _lang_map(raw: Mapping[object, object]) -> dict[str, str]:
    """The non-empty string values of *raw*, keyed by language tag as given."""
    return {
        tag.strip(): text
        for tag, text in raw.items()
        if isinstance(tag, str) and tag.strip() and isinstance(text, str) and text.strip()
    }


def _parse_istring(raw: object) -> InternationalString:
    """Parse a multilingual field: a dict, a JSON-encoded dict, or a plain string.

    Every language key is kept under its own tag, ``en-t-fr`` and ``fr-t-en``
    included. A plain string is stored under ``und``. Empty values are dropped,
    and a field with none left is the empty `InternationalString`.
    """
    if isinstance(raw, str):
        try:
            parsed: object = json.loads(raw)
        except json.JSONDecodeError, ValueError:
            parsed = None
        if not isinstance(parsed, dict):
            return InternationalString.of({_UNDETERMINED: raw} if raw.strip() else {})
        raw = parsed
    if isinstance(raw, Mapping):
        return InternationalString.of(_lang_map(raw))
    return InternationalString.EMPTY


def _parse_dt(raw: object) -> datetime | None:
    """Parse an ISO 8601 date/datetime string, returning None on failure."""
    if not isinstance(raw, str) or not raw.strip():
        return None
    s = raw.strip()
    # Try common formats
    for fmt in ("%Y-%m-%dT%H:%M:%S.%f", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(s, fmt)  # noqa: DTZ007
        except ValueError:
            continue
    return None


def _parse_keywords(raw: object) -> list[InternationalString]:
    """Parse CKAN keywords: ``{tag: [keyword, ...]}``, native or JSON-encoded.

    Keywords are paired across languages by position, as the portal lists
    them: the i-th keyword under each tag forms one keyword. Every tag is kept,
    ``en-t-fr`` and ``fr-t-en`` included.
    """
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError, ValueError:
            return []
    if not isinstance(raw, dict):
        return []
    words_by_tag = {tag: _seq(words) for tag, words in raw.items() if isinstance(tag, str)}
    longest = max((len(words) for words in words_by_tag.values()), default=0)
    keywords = (
        InternationalString.of(_lang_map({tag: words[i] for tag, words in words_by_tag.items() if i < len(words)}))
        for i in range(longest)
    )
    return [kw for kw in keywords if kw]


def _parse_frequency(raw: object) -> Frequency | None:
    """Parse a CKAN frequency field to a Frequency object."""
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, dict):
                raw = parsed
        except json.JSONDecodeError, ValueError:
            pass
    if isinstance(raw, dict):
        en_label = (raw.get("en") or "").strip()
    elif isinstance(raw, str):
        en_label = raw.strip()
    else:
        return None
    if not en_label:
        return None
    freq_id = _FREQ_LABEL_TO_ID.get(en_label.lower(), en_label.upper().replace(" ", "_"))
    # A plain string is the portal's frequency code (``as_needed``), not English text.
    label = _parse_istring(raw) if isinstance(raw, dict) else InternationalString.of({_NO_LANGUAGE: en_label})
    return Frequency(id=freq_id, label=label)


def _parse_subjects(raw: object) -> list[ConceptRef]:
    """Parse CKAN subject/topic_category field to a list of ConceptRef objects.

    CKAN tags don't carry vocabulary URIs, so a synthetic ``urn:ckan:tag:{slug}``
    URI is generated for each tag.  These are permissive (unvalidated) refs —
    they won't match any stored ConceptScheme unless one is explicitly ingested.
    """
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError, ValueError:
            raw = [raw]
    if not isinstance(raw, list):
        return []
    refs: list[ConceptRef] = []
    for item in raw:
        if isinstance(item, str) and item.strip():
            slug = item.strip().lower().replace(" ", "_")
            label = InternationalString.of({_NO_LANGUAGE: item.strip()})
            refs.append(ConceptRef(uri=f"urn:ckan:tag:{slug}", label=label))
    return refs


def _parse_spatial(raw: object) -> list[SpatialCoverage]:
    """Parse CKAN geographic_region field to SpatialCoverage objects."""
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError, ValueError:
            raw = [raw]
    if not isinstance(raw, list):
        return []
    regions: list[SpatialCoverage] = []
    for item in raw:
        if isinstance(item, str) and item.strip():
            regions.append(SpatialCoverage(label=InternationalString(en=item.strip())))
        elif isinstance(item, dict):
            label = _parse_istring(item) if item else None
            regions.append(SpatialCoverage(label=label))
    return regions


def _map_resource(res: dict[str, object], package_id: str) -> Distribution:
    """Map a CKAN resource dict to a Distribution."""
    name_raw = res.get("name_translated") or res.get("name") or ""
    title = _parse_istring(name_raw) or None
    url = _s(res, "url")
    url_type = _s(res, "url_type") or ""
    # download_url for uploaded files; access_url for links
    download_url = url if url_type in ("upload", "download") else None
    access_url = url if not download_url else None
    return Distribution(
        id=_s(res, "id") or f"{package_id}/{_s(res, 'name') or 'resource'}",
        title=title,
        access_url=access_url,
        download_url=download_url,
        media_type=_s(res, "mimetype"),
        format=_s(res, "format"),
        byte_size=_i(res.get("size")),
        issued=_parse_dt(res.get("created")),
        modified=_parse_dt(res.get("last_modified") or res.get("metadata_modified")),
    )


def _ckan_package_to_dataset(raw: dict[str, object]) -> OpenDataset:
    """Map a raw CKAN package dict to a statcatlib OpenDataset.

    This is a pure function: no network calls, no store access.

    Args:
        raw: One package dict as CKAN's ``package_search`` returned it.
            Values are `object`; every field is narrowed before use.

    Returns:
        The mapped `OpenDataset`.

    Raises:
        ValueError: The package carries no string ``id``. Every other field
            degrades to a default; the identifier cannot, since it is the
            catalog key.
    """
    pkg_id = _s(raw, "id")
    if pkg_id is None:
        msg = f"CKAN package has no string 'id': {sorted(raw)[:8]}"
        raise ValueError(msg)

    # Title and description — prefer bilingual translated fields
    title = _parse_istring(raw.get("title_translated") or raw.get("title"))
    description = _parse_istring(raw.get("notes_translated") or raw.get("notes"))

    # Publisher from CKAN organization
    org = _d(raw, "organization")
    publisher: Agent | None = None
    if org:
        org_name = _s(org, "name") or _s(org, "id")
        # An organization with no title is named by its slug, a code.
        pub_name = _parse_istring(org.get("title")) or InternationalString.of(
            {_NO_LANGUAGE: org_name} if org_name else {}
        )
        publisher = Agent(id=org_name, name=pub_name)

    # Temporal
    issued = _parse_dt(raw.get("metadata_created") or raw.get("date_published"))
    modified = _parse_dt(raw.get("metadata_modified") or raw.get("date_modified"))

    tc_start = _parse_dt(raw.get("time_period_coverage_start"))
    tc_end = _parse_dt(raw.get("time_period_coverage_end"))
    temporal_coverage = TemporalCoverage(start=tc_start, end=tc_end) if (tc_start or tc_end) else None

    # Frequency
    frequency = _parse_frequency(raw.get("frequency"))

    # Themes from subject
    themes = _parse_subjects(raw.get("subject") or raw.get("topic_category"))

    # Spatial
    spatial_coverage = _parse_spatial(raw.get("geographic_region"))

    # Keywords
    keywords = _parse_keywords(raw.get("keywords"))

    # Distributions from resources
    distributions = [_map_resource(res, pkg_id) for res in _seq(raw.get("resources")) if isinstance(res, dict)]

    # Licence
    licence: LicenceDocument | None = None
    lic_id = _s(raw, "license_id") or ""
    lic_title = _s(raw, "license_title") or lic_id
    lic_url = _s(raw, "license_url")
    if lic_id or lic_title:
        licence = LicenceDocument(
            id=lic_id or lic_title,
            label=_parse_istring(_s(raw, "license_title")) or None,
            uri=lic_url,
        )

    # Language
    lang_raw: object = raw.get("language") or []
    if isinstance(lang_raw, str):
        try:
            lang_raw = json.loads(lang_raw)
        except json.JSONDecodeError, ValueError:
            lang_raw = [lang_raw]
    language: list[str] = [str(lang) for lang in _seq(lang_raw) if lang]

    return OpenDataset(
        identifier=pkg_id,
        title=title,
        description=description,
        publisher=publisher,
        issued=issued,
        modified=modified,
        frequency=frequency,
        themes=themes,
        keywords=keywords,
        spatial_coverage=spatial_coverage,
        temporal_coverage=temporal_coverage,
        distributions=distributions,
        licence=licence,
        language=language,
        landing_page=_s(raw, "url"),
    )


# ---------------------------------------------------------------------------
# Public ingest function
# ---------------------------------------------------------------------------


def ingest_ckan(
    store: Catalog,
    base_url: str,
    *,
    organization: str | None = None,
    subject: str | None = None,
    limit: int = 100,
) -> list[OpenDataset]:
    """Ingest datasets from a CKAN portal into the catalog.

    Fetches up to ``limit`` packages from the CKAN Action API, maps each
    to a statcatlib ``OpenDataset``, stores them all, and returns the list.
    It also records the language pairs the portal's dataset schema declares
    (`CkanClient.alternate_languages`), with *base_url* as their source.

    Args:
        store: Target Catalog.
        base_url: CKAN portal base URL, e.g. ``"https://open.canada.ca/data"``.
        organization: Optional CKAN organization name slug filter,
            e.g. ``"statcan-statcan"``.
        subject: Optional free-text subject filter appended to the search query.
        limit: Maximum number of datasets to ingest.

    Returns:
        List of OpenDataset objects ingested.
    """
    with CkanClient(base_url) as client:
        raw_packages = client.package_search(
            organization=organization,
            subject=subject,
            rows=limit,
        )
        alternates = client.alternate_languages()
    datasets = [_ckan_package_to_dataset(pkg) for pkg in raw_packages]
    store.put(datasets)
    if alternates:
        store.add_language_alternates(alternates, source=base_url)
    return datasets
