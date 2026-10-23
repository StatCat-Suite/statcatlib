"""Sentinel types for selective relationship loading.

When a QueryBuilder uses .include() to request only specific relationships,
unloaded fields are set to an UNLOADED sentinel rather than None or [].
Accessing any attribute or iterating over an unloaded field raises
NotLoadedError with a message that points to the fix.
"""

from __future__ import annotations

from typing import Any, Never

# One cached sentinel instance per field name so identity checks work
# and we don't create unnecessary objects.
_cache: dict[str, _UnloadedType] = {}


class NotLoadedError(AttributeError):
    """Raised when accessing a relationship that was not loaded in this query.

    Example::

        ds = store.query(BaseDataset).include("publisher").first()
        ds.themes  # NotLoadedError: 'themes' was not loaded.
        #   Add 'themes' to .include() or call .full().
    """

    def __init__(self, field: str) -> None:
        super().__init__(f"'{field}' was not loaded. Add '{field}' to .include() or call .full() to load everything.")
        self.field = field


class _UnloadedType:
    """Per-field sentinel placed on relationship fields that were not loaded.

    Typed as ``Any`` at the usage site so it does not pollute model type
    annotations — the contract is runtime, not static (same approach as
    SQLAlchemy lazy-loading proxies).

    Do not instantiate directly; use ``make_unloaded(field)`` instead.
    """

    __slots__ = ("_field",)

    def __init__(self, field: str) -> None:
        object.__setattr__(self, "_field", field)

    # Attribute access — covers ds.publisher.name, ds.themes[0].label, etc.
    def __getattr__(self, name: str) -> Never:
        raise NotLoadedError(object.__getattribute__(self, "_field"))

    # Iteration — covers `for t in ds.themes`
    def __iter__(self) -> Never:
        raise NotLoadedError(object.__getattribute__(self, "_field"))

    # len() — covers `len(ds.themes)`
    def __len__(self) -> Never:
        raise NotLoadedError(object.__getattribute__(self, "_field"))

    # Indexing — covers ds.themes[0]
    def __getitem__(self, key: object) -> Never:
        raise NotLoadedError(object.__getattribute__(self, "_field"))

    # bool() — return False so `if ds.publisher:` doesn't raise;
    # the error fires on attribute access, iteration, len(), or indexing instead.
    def __bool__(self) -> bool:
        return False

    def __repr__(self) -> str:
        field = object.__getattribute__(self, "_field")
        return f"UNLOADED({field!r})"


def make_unloaded(field: str) -> Any:
    """Return a cached UNLOADED sentinel for *field*.

    One of the few places `Any` is the honest annotation rather than a hole.
    The sentinel is assigned into model fields of every declared type — a
    `Agent`, a `list[Distribution]`, an `InternationalString` — so the return
    genuinely needs the erased type back out, which is the litmus for `Any`
    over `object`. `object` here would push a narrowing onto every assignment
    it was designed to satisfy.

    The hole is one line wide: the value is opaque, and touching it raises
    `NotLoadedError` at runtime.

    (No `# noqa: ANN401` yet — ANN401 is still ignored project-wide until
    #109 step 3, and a suppression that suppresses nothing is the thing
    pyrefly's `unused-ignore` exists to catch. Add it with the
    rule.)
    """
    if field not in _cache:
        _cache[field] = _UnloadedType(field)
    return _cache[field]


def is_unloaded(value: object) -> bool:
    """Return True if *value* is an UNLOADED sentinel."""
    return isinstance(value, _UnloadedType)
