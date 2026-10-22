"""Row — typed dict returned by .select() projections."""

from __future__ import annotations


class Row(dict[str, object]):
    """A dict subclass returned by ``.select()`` projections.

    Supports both dict-style and attribute-style field access::

        row = store.query(BaseDataset).select("identifier", "status").first()
        row["identifier"]  # dict-style
        row.identifier  # attribute-style

    ``Row`` is a plain ``dict`` subclass — ``isinstance(row, dict)`` is
    ``True``, JSON serialisation works as normal, and all dict methods
    (``keys()``, ``items()``, ``values()``, etc.) are inherited.
    """

    __slots__ = ()

    def __getattr__(self, name: str) -> object:
        try:
            return self[name]
        except KeyError:
            msg = f"Row has no field {name!r}"
            raise AttributeError(msg) from None

    def __repr__(self) -> str:
        return f"Row({dict.__repr__(self)})"
