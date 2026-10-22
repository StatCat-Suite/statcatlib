"""Generic CKAN source connector for statcatlib.

Ingests datasets from any CKAN portal (open.canada.ca, data.gov, etc.)
into the catalog as OpenDataset objects.

The open.canada.ca portal stores many metadata fields as JSON-encoded
strings rather than native JSON objects. The helpers here handle both
representations defensively.
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


def _parse_istring(
    raw: object,
    *,
    en_key: str = "en",
    fr_key: str = "fr",
    fallback_en: str = "",
) -> InternationalString:
    """Parse a bilingual field that may be a dict, a JSON-encoded string, or a plain string."""
    if isinstance(raw, dict):
        kw: dict[str, str] = {"en": raw.get(en_key) or fallback_en}
        if fr_val := raw.get(fr_key):
            kw["fr"] = fr_val
        return InternationalString(**kw)
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, dict):
                kw2: dict[str, str] = {"en": parsed.get(en_key) or fallback_en}
                if fr_val2 := parsed.get(fr_key):
                    kw2["fr"] = fr_val2
                return InternationalString(**kw2)
        except (json.JSONDecodeError, ValueError):
            pass
        return InternationalString(en=raw or fallback_en)
    return InternationalString(en=fallback_en)


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
    """Parse CKAN keywords field: {'en': [...], 'fr': [...]} or JSON-encoded."""
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            return []
    if not isinstance(raw, dict):
        return []
    en_words: list[str] = raw.get("en") or []
    fr_words: list[str] = raw.get("fr") or []
    # Zip together paired en/fr keywords; extras get en-only entries
    result: list[InternationalString] = []
    for i, en_kw in enumerate(en_words):
        kw_args: dict[str, str] = {"en": en_kw}
        if i < len(fr_words):
            kw_args["fr"] = fr_words[i]
        result.append(InternationalString(**kw_args))
    result.extend(InternationalString(fr=fr_kw) for fr_kw in fr_words[len(en_words) :])
    return result


def _parse_frequency(raw: object) -> Frequency | None:
    """Parse a CKAN frequency field to a Frequency object."""
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, dict):
                raw = parsed
        except (json.JSONDecodeError, ValueError):
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
    freq_kw: dict[str, str] = {"en": en_label}
    if isinstance(raw, dict) and (label_fr := raw.get("fr")):
        freq_kw["fr"] = label_fr
    return Frequency(
        id=freq_id,
        label=InternationalString(**freq_kw),
    )


def _parse_subjects(raw: object) -> list[ConceptRef]:
    """Parse CKAN subject/topic_category field to a list of ConceptRef objects.

    CKAN tags don't carry vocabulary URIs, so a synthetic ``urn:ckan:tag:{slug}``
    URI is generated for each tag.  These are permissive (unvalidated) refs —
    they won't match any stored ConceptScheme unless one is explicitly ingested.
    """
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            raw = [raw]
    if not isinstance(raw, list):
        return []
    refs: list[ConceptRef] = []
    for item in raw:
        if isinstance(item, str) and item.strip():
            slug = item.strip().lower().replace(" ", "_")
            refs.append(ConceptRef(uri=f"urn:ckan:tag:{slug}", label=InternationalString(en=item.strip())))
    return refs


def _parse_spatial(raw: object) -> list[SpatialCoverage]:
    """Parse CKAN geographic_region field to SpatialCoverage objects."""
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except (json.JSONDecodeError, ValueError):
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
    title = _parse_istring(name_raw) if name_raw else None
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
    title = _parse_istring(
        raw.get("title_translated") or raw.get("title") or pkg_id,
        fallback_en=pkg_id,
    )
    description = _parse_istring(
        raw.get("notes_translated") or raw.get("notes") or "",
    )

    # Publisher from CKAN organization
    org = _d(raw, "organization")
    publisher: Agent | None = None
    if org:
        org_name = _s(org, "name") or _s(org, "id") or ""
        org_title_raw = org.get("title") or org.get("name") or org_name
        org_desc_raw = org.get("description") or None
        pub_name = _parse_istring(org_title_raw, fallback_en=org_name)
        # Some CKAN portals store org title/description as bilingual JSON strings
        if org_desc_raw:
            desc_parsed = _parse_istring(org_desc_raw)
            if fr_name := desc_parsed.get("fr"):
                pub_name = InternationalString(
                    en=pub_name.get("en") or org_name,
                    fr=fr_name,
                )
        publisher = Agent(id=org_name or None, name=pub_name)

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
            label=InternationalString(en=lic_title) if lic_title else None,
            uri=lic_url,
        )

    # Language
    lang_raw: object = raw.get("language") or []
    if isinstance(lang_raw, str):
        try:
            lang_raw = json.loads(lang_raw)
        except (json.JSONDecodeError, ValueError):
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
    datasets = [_ckan_package_to_dataset(pkg) for pkg in raw_packages]
    store.put(datasets)
    return datasets
