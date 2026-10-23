"""Convenience URN builders for SDMX artefact references.

Instead of hand-typing long URN strings, use these helpers::

    import statcatlib as sc

    store.codelist(sc.urn.codelist("SDMX", "CL_GEO", "1.0"))
    store.codelist(sc.urn.codelist("ESTAT", "CL_GEO"))  # version defaults to "1.0"

    # Parse a URN back into components
    parsed = sc.urn.parse("urn:sdmx:org.sdmx.infomodel.codelist.Codelist=SDMX:CL_GEO(1.0)")
    parsed.agency  # "SDMX"
    parsed.id  # "CL_GEO"
    parsed.version  # "1.0"
"""

from __future__ import annotations

from sdmxlib.model.urn import SdmxUrn

__all__ = [
    "codelist",
    "concept_scheme",
    "data_structure",
    "dataflow",
    "parse",
]

# SDMX info model package/class mappings
_CODELIST = ("codelist", "Codelist")
_DATAFLOW = ("datastructure", "Dataflow")
_DATA_STRUCTURE = ("datastructure", "DataStructure")
_CONCEPT_SCHEME = ("conceptscheme", "ConceptScheme")


def _build(package: str, artefact_class: str, agency: str, artefact_id: str, version: str) -> str:
    return str(
        SdmxUrn(
            package=package,
            artefact_class=artefact_class,
            agency=agency,
            id=artefact_id,
            version=version,
        )
    )


def codelist(agency: str, id: str, version: str = "1.0") -> str:  # noqa: A002
    """Build a codelist URN string.

    Args:
        agency: The maintenance agency's id.
        id: The codelist's id.
        version: Its version.

    Returns:
        The URN, as SDMX spells it.

    Example:
        sc.urn.codelist("SDMX", "CL_GEO", "1.0")
        # → "urn:sdmx:org.sdmx.infomodel.codelist.Codelist=SDMX:CL_GEO(1.0)"
    """
    return _build(*_CODELIST, agency, id, version)


def dataflow(agency: str, id: str, version: str = "1.0") -> str:  # noqa: A002
    """Build a dataflow URN string.

    Args:
        agency: The maintenance agency's id.
        id: The dataflow's id.
        version: Its version.

    Returns:
        The URN, as SDMX spells it.

    Example:
        sc.urn.dataflow("ESTAT", "NAMA_10_GDP", "1.0")
        # → "urn:sdmx:org.sdmx.infomodel.datastructure.Dataflow=ESTAT:NAMA_10_GDP(1.0)"
    """
    return _build(*_DATAFLOW, agency, id, version)


def data_structure(agency: str, id: str, version: str = "1.0") -> str:  # noqa: A002
    """Build a data structure definition URN string.

    Args:
        agency: The maintenance agency's id.
        id: The data structure definition's id.
        version: Its version.

    Returns:
        The URN, as SDMX spells it.

    Example:
        sc.urn.data_structure("ESTAT", "DSD_NAMA_10_GDP", "1.0")
        # → "urn:sdmx:org.sdmx.infomodel.datastructure.DataStructure=ESTAT:DSD_NAMA_10_GDP(1.0)"
    """
    return _build(*_DATA_STRUCTURE, agency, id, version)


def concept_scheme(agency: str, id: str, version: str = "1.0") -> str:  # noqa: A002
    """Build a concept scheme URN string.

    Args:
        agency: The maintenance agency's id.
        id: The concept scheme's id.
        version: Its version.

    Returns:
        The URN, as SDMX spells it.

    Example:
        sc.urn.concept_scheme("SDMX", "CS_FREQ", "1.0")
        # → "urn:sdmx:org.sdmx.infomodel.conceptscheme.ConceptScheme=SDMX:CS_FREQ(1.0)"
    """
    return _build(*_CONCEPT_SCHEME, agency, id, version)


def parse(urn: str) -> SdmxUrn[object]:
    """Parse a URN string into its components.

    Args:
        urn: An SDMX URN string. One that is not valid raises `ValueError`.

    Returns:
        The parsed URN, with ``.agency``, ``.id``, ``.version``, ``.package``
        and ``.artefact_class``.

    Example:
        parsed = sc.urn.parse("urn:sdmx:org.sdmx.infomodel.codelist.Codelist=SDMX:CL_GEO(1.0)")
        parsed.agency  # "SDMX"
        parsed.id  # "CL_GEO"
        parsed.version  # "1.0"
        str(parsed)  # round-trips to original URN string
    """
    return SdmxUrn.parse(urn)
