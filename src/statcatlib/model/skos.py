"""SKOS domain types for statcatlib.

ConceptScheme and Concept are first-class artefacts, on a par with Dataset
and Catalog. They model DCAT's use of SKOS for themes, types, subjects, and
other controlled vocabulary references.

Identity model
--------------
- ``ConceptScheme`` is globally identified by URI.
- ``Concept`` is identified by ``notation`` within its scheme (the "code").
  It does not carry a ``uri`` field — the URI is implicit from the scheme and
  reconstructed at serialization or recovered from the storage PK at load time.
  This mirrors how sdmxlib models Code (local ``id``) vs. Codelist (global URN).

Ref types
---------
Cross-boundary citations use typed ref objects rather than bare strings:

- ``ConceptSchemeRef`` — carried on ``Catalog.theme_taxonomy``
- ``ConceptRef`` — carried on ``Dataset.themes``, ``.subject``, ``.dataset_type``

Non-identity fields on refs (``label``, ``notation``, ``scheme_uri``) are
``eq=False, hash=False``: two refs with the same URI compare equal regardless
of what display metadata they carry. This ensures refs parsed from different
sources (DCAT-AP XML, JSON-LD, store read-back) match cleanly.

XKOS extensibility
------------------
The ``broader``/``narrower``/``related`` relation names in storage use a
VARCHAR column rather than an enum, so future XKOS types (``xkos:generalizes``,
``xkos:isPartOf``, etc.) can be added without schema changes.
"""

from __future__ import annotations

import attrs
from sdmxlib import InternationalString

__all__ = [
    "Concept",
    "ConceptRef",
    "ConceptScheme",
    "ConceptSchemeRef",
    "concept_uri",
    "is_concept_in_scheme",
]


@attrs.frozen
class ConceptSchemeRef:
    """Typed reference to a ConceptScheme.

    Carried on ``Catalog.theme_taxonomy`` (DCAT's ``dcat:themeTaxonomy``).
    URI is identity; label is denormalized display convenience only.
    """

    uri: str
    label: InternationalString = attrs.field(factory=InternationalString, eq=False, hash=False)


@attrs.frozen
class ConceptRef:
    """Typed reference to a Concept.

    Carried on ``Dataset.themes``, ``.subject``, and ``.dataset_type``
    (DCAT's ``dcat:theme``, ``dcterms:subject``, ``dcterms:type``).

    URI is identity. ``scheme_uri``, ``notation``, and ``label`` are hints
    that speed up display and within-scheme lookups but do not affect equality.

    ``label`` is populated at read time via a LEFT JOIN with the concept table;
    it is an empty dict when the concept is not materialized in the store
    (permissive refs — datasets may cite concepts from external vocabularies
    that are never locally ingested).
    """

    uri: str
    scheme_uri: str | None = attrs.field(default=None, eq=False, hash=False)
    notation: str | None = attrs.field(default=None, eq=False, hash=False)
    label: InternationalString = attrs.field(factory=InternationalString, eq=False, hash=False)


@attrs.frozen
class Concept:
    """A SKOS Concept.

    Identity is ``notation`` within its parent scheme — the "code" (e.g. "PUMF",
    "HEAL", "ON"). ``broader``, ``narrower``, and ``related`` are tuples of
    notations in the same scheme, not URIs. Cross-scheme relations use
    ``ConceptRef`` or (in future) an XKOS Correspondence artefact.
    """

    notation: str
    label: InternationalString = attrs.field(factory=InternationalString)
    alt_label: InternationalString = attrs.field(factory=InternationalString)
    definition: InternationalString | None = None
    broader: tuple[str, ...] = ()  # notations within same scheme
    narrower: tuple[str, ...] = ()
    related: tuple[str, ...] = ()


@attrs.frozen
class ConceptScheme:
    """A SKOS ConceptScheme — a controlled vocabulary.

    Globally identified by URI. Owns its Concepts: a Concept has no identity
    outside its scheme (``skos:inScheme`` is a required property).

    ``concepts`` maps notation → Concept for Pythonic within-scheme access::

        scheme.concepts["PUMF"]  # direct lookup by notation

    ``top_concepts`` lists notations of the top-level hierarchy entries
    (``skos:hasTopConcept``).

    Construction with embedded concepts enables cascade persistence::

        store.put_concept_scheme(ConceptScheme(uri=..., label=..., concepts={...}))

    ``put_concept_scheme()`` writes the scheme row and cascades to write each
    Concept row. This is the correct container convenience: the ownership
    relation is genuine.
    """

    uri: str
    label: InternationalString
    definition: InternationalString | None = None
    concepts: dict[str, Concept] = attrs.field(factory=dict)  # notation → Concept
    top_concepts: tuple[str, ...] = ()  # notations


def concept_uri(scheme_uri: str, notation: str) -> str:
    """Return the URI of the concept *notation* in the scheme *scheme_uri*.

    The catalog stores a concept under ``{scheme_uri}/{notation}``. Every
    reference to a stored concept builds its URI here, so a reference made
    at ingest joins the ``concept`` row the store wrote.

    Args:
        scheme_uri: The concept scheme's URI.
        notation: The concept's notation within the scheme.

    Returns:
        The concept's URI.
    """
    return f"{scheme_uri}/{notation}"


def is_concept_in_scheme(scheme: ConceptScheme, notation: str) -> bool:
    """Return True if *notation* is a known concept in *scheme*.

    Pure-Python — no DuckDB or any other storage involved.
    Useful for validation pipelines that operate on in-memory domain objects.

    Args:
        scheme: The concept scheme.
        notation: The concept's notation.

    Returns:
        Whether *scheme* lists *notation* among its concepts.

    Example:
        valid = is_concept_in_scheme(product_type_scheme, dataset.dataset_type.notation)
    """
    return notation in scheme.concepts
