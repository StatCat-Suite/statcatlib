"""SQL for picking a label from a multilingual MAP over a tuple of tags.

Every helper uses positional ``?`` parameters, and needs no macro, so it works
on a read-only connection. The first held tag in the requested order answers,
as `sdmxlib.InternationalString.pick` does; a held ``""`` answers.

A leaf module: it imports attrs and the standard library only, so
`statcatlib.query` and the store can both import it
(``docs/design/label-lookup.md``, "SQL helpers").
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping

from attrs import frozen


@frozen
class PickSql:
    """The SQL that picks one label from a MAP column, with its parameters.

    Attributes:
        text: ``COALESCE(col[?], …)``: the text of the first held tag.
        text_params: The tags, in order, for `text`.
        lang: ``CASE WHEN col[?] IS NOT NULL THEN ? … END``: that tag.
        lang_params: Each tag twice, in order, for `lang`.
    """

    text: str
    text_params: tuple[str, ...]
    lang: str
    lang_params: tuple[str, ...]


def pick_map(column: str, langs: tuple[str, ...]) -> PickSql:
    """Pick the first of *langs* that *column* holds.

    Args:
        column: A SQL expression of type ``MAP(VARCHAR, VARCHAR)``, such as
            ``d.title``.
        langs: Normalised tags, in order of preference, at least one.

    Returns:
        The text and tag expressions and their parameters.
    """
    text = "COALESCE(" + ", ".join(f"{column}[?]" for _ in langs) + ")"
    lang = "CASE " + " ".join(f"WHEN {column}[?] IS NOT NULL THEN ?" for _ in langs) + " END"
    return PickSql(
        text=text,
        text_params=langs,
        lang=lang,
        lang_params=tuple(t for tag in langs for t in (tag, tag)),
    )


def localized_map(field: str) -> tuple[str, tuple[str, ...]]:
    """A subquery giving one MAP per owner from ``sdmx.localized_text`` rows.

    Its columns are ``owner_urn`` and ``m``, the owner's texts for *field* as
    a ``MAP(VARCHAR, VARCHAR)``, so `pick_map` applies to it.

    Args:
        field: The ``field`` value, such as ``name``.

    Returns:
        The parenthesised subquery and its one parameter.
    """
    sql = (
        "(SELECT owner_urn, MAP_FROM_ENTRIES(LIST({'key': lang, 'value': text})) AS m"
        " FROM sdmx.localized_text WHERE field = ? GROUP BY owner_urn)"
    )
    return sql, (field,)


#: The prefix of a spatial facet key derived from a label map.
SPATIAL_KEY_PREFIX = "sha256:"


def spatial_key(label: Mapping[str, str | None] | None) -> str | None:
    """The spatial facet key of a coverage with no ``uri``: the reference for `spatial_key_sql`.

    The key bytes are the map's entries whose text is not ``None``, in
    ascending order of tag, each written ``<tag>=<n>:<text>`` with ``<n>`` the
    text's length in UTF-8 bytes, concatenated and encoded as UTF-8. The key
    is ``sha256:`` and the bytes' lowercase hex SHA-256
    (``docs/design/label-lookup.md``, LL-D5).

    Args:
        label: The coverage's label map.

    Returns:
        The key, or ``None`` for ``None``, an empty map or a map whose texts
        are all ``None``.
    """
    if not label:
        return None
    body = "".join(f"{tag}={len(text.encode())}:{text}" for tag, text in sorted(label.items()) if text is not None)
    if not body:
        return None
    return SPATIAL_KEY_PREFIX + hashlib.sha256(body.encode()).hexdigest()


def spatial_key_sql(alias: str) -> str:
    """The SQL of a spatial facet key over a ``dataset_spatial`` row aliased *alias*.

    The coverage's ``uri`` when it has one, else the key `spatial_key`
    computes from its ``label``, else ``NULL``. DuckDB's ``strlen`` counts
    UTF-8 bytes.

    Args:
        alias: The table alias, such as ``ds``.

    Returns:
        A SQL expression of type ``VARCHAR``.
    """
    entries = f"list_filter(list_sort(map_entries({alias}.label), 'ASC'), lambda e: e.value IS NOT NULL)"
    entry = "e.key || '=' || strlen(e.value)::VARCHAR || ':' || e.value"
    body = f"array_to_string(list_transform({entries}, lambda e: {entry}), '')"
    return f"COALESCE({alias}.uri, '{SPATIAL_KEY_PREFIX}' || sha256(NULLIF({body}, '')))"
