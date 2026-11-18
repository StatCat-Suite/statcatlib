"""Label lookup types: the tag argument, gap lists and the label errors.

Every label call takes ``lang``, a tag or an ordered sequence of tags
(`Langs`), and returns `sdmxlib.LocalisedString`, which carries the tag that
answered. A single lookup raises `MissingTranslationError` when no requested
tag is held. A bulk or paged call returns ``None`` for each missing label and
lists it in the result's ``gaps``: `Labels` for a mapping, `RowList` for
``Row`` projections and `ValueList` for scalar projections.

This module is a leaf: it imports sdmxlib, attrs and the standard library, so
`statcatlib.query` and the store can both import it
(``docs/design/label-lookup.md``).
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator, Mapping, Sequence
from typing import TYPE_CHECKING, Self, override

from attrs import frozen
from sdmxlib import LocalisedString, MissingTagError, normalize_tag

if TYPE_CHECKING:
    from statcatlib.store.row import Row

__all__ = [
    "Gap",
    "Labels",
    "Langs",
    "MissingTranslationError",
    "RowList",
    "UnknownCodeError",
    "ValueList",
]

#: A language tag, or an ordered sequence of tags of which the first held one
#: answers. A bare ``str`` is one tag.
type Langs = str | Sequence[str]


def _as_langs(lang: Langs) -> tuple[str, ...]:
    """Normalise *lang* to a tuple of tags, duplicates dropped keeping the first.

    Args:
        lang: A tag, or a sequence of tags in order of preference.

    Returns:
        The normalised tags, in order, each once.

    A malformed tag raises `sdmxlib.InvalidTagError`, a ``ValueError``, from
    `sdmxlib.normalize_tag`.

    Raises:
        TypeError: *lang* or one of its items is not a ``str``.
        ValueError: *lang* is an empty sequence.
    """
    if isinstance(lang, str):
        return (normalize_tag(lang),)
    if not isinstance(lang, Sequence):
        msg = f"lang must be a tag or a sequence of tags, not {type(lang).__name__}"
        raise TypeError(msg)
    if not lang:
        msg = "lang must name at least one tag"
        raise ValueError(msg)
    out: list[str] = []
    for item in lang:
        if not isinstance(item, str):
            msg = f"a language tag is a str, not {type(item).__name__}"
            raise TypeError(msg)
        tag = normalize_tag(item)
        if tag not in out:
            out.append(tag)
    return tuple(out)


def _quoted(items: Iterable[str]) -> str:
    return ", ".join(map(repr, items)) or "nothing"


class MissingTranslationError(MissingTagError):
    """An entity's required label holds none of the requested tags.

    A `sdmxlib.MissingTagError`, and so a `KeyError`. Catch it by name:
    ``except KeyError`` also catches `UnknownCodeError`. Its ``langs`` and
    ``held``, from `sdmxlib.MissingTagError`, are the tags asked for and the
    tags the field holds.

    Attributes:
        entity: The entity looked up, such as a codelist or code URN.
        field: The label field, such as ``name``.
    """

    entity: str
    field: str

    def __init__(self, entity: str, field: str, langs: Sequence[str], held: Sequence[str]) -> None:
        """Record the entity, the field, the tags asked for and the tags held.

        Args:
            entity: The entity looked up.
            field: The label field.
            langs: The tags asked for.
            held: The tags the field holds.
        """
        super().__init__(langs, held)
        self.entity = entity
        self.field = field

    @override
    def __str__(self) -> str:
        return f"no text for {_quoted(self.langs)} in {self.field} of {self.entity}; held: {_quoted(self.held)}"

    @override
    def __reduce__(self) -> tuple[type[Self], tuple[str, str, tuple[str, ...], tuple[str, ...]]]:
        return (type(self), (self.entity, self.field, self.langs, self.held))


class UnknownCodeError(KeyError):
    """``batch_labels`` was asked for codes their codelists do not hold.

    A `KeyError`. Catch it by name: ``except KeyError`` also catches
    `MissingTranslationError`.

    Attributes:
        missing: Each (codelist URN, code ID) pair not found, in request order.
    """

    missing: tuple[tuple[str, str], ...]

    def __init__(self, missing: Sequence[tuple[str, str]]) -> None:
        """Record the pairs not found.

        Args:
            missing: Each (codelist URN, code ID) pair not found.
        """
        self.missing = tuple((urn, code) for urn, code in missing)
        super().__init__(self.missing)

    @override
    def __str__(self) -> str:
        return "; ".join(f"no code {code!r} in {urn}" for urn, code in self.missing)

    @override
    def __reduce__(self) -> tuple[type[Self], tuple[tuple[tuple[str, str], ...]]]:
        return (type(self), (self.missing,))


@frozen
class Gap:
    """A label a bulk or paged call could not answer in any requested tag.

    Attributes:
        key: The result key the gap belongs to: a code ID, a
            (codelist URN, code ID) pair, an agent ID, a dataset identifier.
        entity: The entity whose label is missing.
        field: The label field, such as ``name``, ``label`` or ``title``.
        langs: The tags asked for, normalised.
    """

    key: object
    entity: str
    field: str
    langs: tuple[str, ...]


class Labels[K](Mapping[K, LocalisedString | None]):
    """An immutable mapping of keys to labels, with the gaps it holds.

    A missing label maps to ``None`` and is listed in `gaps`. Iteration
    follows the order the call states (request order, or the key order of a
    whole-table map). ``json.dumps`` refuses it, as it refuses any
    `Mapping` that is not a ``dict``.

    Attributes:
        gaps: Every key whose label holds none of the requested tags.
    """

    __slots__ = ("_data", "gaps")

    _data: dict[K, LocalisedString | None]
    gaps: tuple[Gap, ...]

    def __init__(self, items: Iterable[tuple[K, LocalisedString | None]] = (), gaps: Iterable[Gap] = ()) -> None:
        """Hold *items* and *gaps*.

        Args:
            items: The (key, label) pairs; a later duplicate key replaces an
                earlier one's label in the earlier one's position.
            gaps: The gaps.
        """
        self._data = dict(items)
        self.gaps = tuple(gaps)

    @override
    def __getitem__(self, key: K) -> LocalisedString | None:
        return self._data[key]

    @override
    def __iter__(self) -> Iterator[K]:
        return iter(self._data)

    @override
    def __len__(self) -> int:
        return len(self._data)

    @override
    def __repr__(self) -> str:
        return f"Labels({self._data!r}, gaps={self.gaps!r})"


class RowList(list["Row"]):
    """The ``Row`` objects of a projection, with the gaps among them.

    `gaps` describes the rows as returned: it does not follow mutation,
    slicing, ``copy()`` or ``+``, which return a plain ``list``, and ``==``
    ignores it.

    Attributes:
        gaps: Every selected label that holds none of the requested tags.
    """

    gaps: tuple[Gap, ...]

    def __init__(self, rows: Iterable[Row] = (), gaps: Iterable[Gap] = ()) -> None:
        """Hold *rows* and *gaps*.

        Args:
            rows: The rows.
            gaps: The gaps.
        """
        super().__init__(rows)
        self.gaps = tuple(gaps)


class ValueList(list[object]):
    """The values of a scalar projection, with the gaps among them.

    `gaps` describes the values as returned, as `RowList.gaps` does.

    Attributes:
        gaps: Every selected label that holds none of the requested tags.
    """

    gaps: tuple[Gap, ...]

    def __init__(self, values: Iterable[object] = (), gaps: Iterable[Gap] = ()) -> None:
        """Hold *values* and *gaps*.

        Args:
            values: The values.
            gaps: The gaps.
        """
        super().__init__(values)
        self.gaps = tuple(gaps)
