"""Every ``lang`` parameter: required, and refusing a malformed tag (``docs/design/label-lookup.md``).

The enumeration walks the public modules as sdmxlib's
``test_every_public_lang_parameter_refuses_a_malformed_tag`` does
(``tests/unit/test_language_tags.py`` at sdmxlib ``v0.89.0``): every public
function or method with a ``lang`` or ``langs`` parameter must be registered
in `_calls`, so a new one fails until it is.
"""

from __future__ import annotations

import ast
import importlib
import inspect
import string
from collections.abc import Callable, Iterator
from pathlib import Path

import msgspec
import pytest
import sdmxlib
from sdmxlib.model.codelist import Code, Codelist
from sdmxlib.model.dataflow import Dataflow
from sdmxlib.model.datastructure import DataStructure, Dimension
from sdmxlib.model.in_memory_registry import InMemoryRegistry

import statcatlib as sc
import statcatlib.query as q
from statcatlib.store.lance import LanceIndex
from tests.conftest import ScratchStore, concept_of, open_scratch

pytestmark = pytest.mark.unit

pytest.importorskip("lancedb", reason="the search callables need statcatlib[search]")

#: The modules the enumeration walks.
_MODULES = (
    "statcatlib",
    "statcatlib.query",
    "statcatlib.labels",
    "statcatlib.store.scopes",
    "statcatlib.store.store",
    "statcatlib.store.lance",
    "statcatlib.admin",
)
#: Private classes reachable from the public API, walked as if public.
_REACHABLE_PRIVATE = {"_FilteredCodelistsScope"}  # through DatasetScope.codelists
#: Callables whose ``lang`` is a data field, not a lookup argument.
_DATA = {
    "statcatlib.labels.Gap.__init__",
    "statcatlib.store.store.Hit.__init__",
    "statcatlib.store.store.SearchResult.__init__",
}
#: Callables whose ``lang`` defaults to None: needed only for a MAP column.
_OPTIONAL = {
    "statcatlib.store.store.QueryBuilder.select",
    "statcatlib.store.store.QueryBuilder.scalars",
    "statcatlib.store.store.QueryBuilder.sort_by",
}
_GEO = "urn:sdmx:org.sdmx.infomodel.codelist.Codelist=TEST:CL_GEO(1.0)"
_BAD = "fr_CA"


def _discover() -> dict[str, inspect.Parameter]:  # noqa: C901
    """Every public callable with a ``lang`` or ``langs`` parameter, by qualified name."""
    names = set(_MODULES)
    found: dict[str, inspect.Parameter] = {}

    def visit(qualname: str, fn: object) -> None:
        try:
            params = inspect.signature(fn).parameters  # pyrefly: ignore[bad-argument-type]
        except TypeError, ValueError:
            return
        param = params.get("lang") or params.get("langs")
        if param is not None and qualname not in _DATA:
            found[qualname] = param

    for module in map(importlib.import_module, _MODULES):
        for name, obj in vars(module).items():
            if (name.startswith("_") and name not in _REACHABLE_PRIVATE) or getattr(
                obj, "__module__", None
            ) not in names:
                continue
            if inspect.isfunction(obj):
                visit(f"{obj.__module__}.{obj.__qualname__}", obj)
            elif inspect.isclass(obj) and not issubclass(obj, BaseException):
                for member, value in vars(obj).items():
                    if member.startswith("_") and member != "__init__":
                        continue
                    fn = value.__func__ if isinstance(value, (staticmethod, classmethod)) else value
                    if callable(fn):
                        visit(f"{obj.__module__}.{obj.__qualname__}.{member}", fn)
    return found


@pytest.fixture(scope="module")
def store(tmp_path_factory: pytest.TempPathFactory) -> ScratchStore:
    geo = Codelist(
        id="CL_GEO",
        maintainer="TEST",
        name=sc.InternationalString(en="Geography"),
        codes=[Code(id="CA", name=sc.InternationalString(en="Canada"))],
    )
    dsd = DataStructure(
        id="DSD",
        maintainer="TEST",
        name=sc.InternationalString(en="D"),
        dimensions=[Dimension(id="GEO", concept=concept_of("GEO"), representation=geo)],
    )
    registry = InMemoryRegistry()
    for obj in (geo, dsd, Dataflow(id="DF", maintainer="TEST", name=sc.InternationalString(en="F"), structure=dsd)):
        registry.add(obj)
    s = open_scratch(tmp_path_factory.mktemp("tags") / "catalog")
    sc.ingest_sdmx(s, registry)
    s.build_index()
    return s


def _index(store: ScratchStore) -> LanceIndex:
    index = store._store._lance
    assert index is not None
    return index


_calls: dict[str, Callable[[ScratchStore], object]] = {
    "statcatlib.query.description_contains": lambda s: q.description_contains("x", lang=_BAD),
    "statcatlib.query.has_keyword": lambda s: q.has_keyword("x", lang=_BAD),
    "statcatlib.query.has_spatial": lambda s: q.has_spatial("x", lang=_BAD),
    "statcatlib.query.title_contains": lambda s: q.title_contains("x", lang=_BAD),
    "statcatlib.store.lance.LanceIndex.bm25_search": lambda s: _index(s).bm25_search("x", lang=_BAD),
    "statcatlib.store.lance.LanceIndex.bm25_search_rows": lambda s: _index(s).bm25_search_rows(
        "x", columns=("id",), lang=_BAD
    ),
    "statcatlib.store.lance.LanceIndex.search": lambda s: _index(s).search([0.0], lang=_BAD),
    "statcatlib.store.lance.LanceIndex.search_rows": lambda s: _index(s).search_rows([0.0], columns=("id",), lang=_BAD),
    "statcatlib.store.lance.LanceIndex.table_for": lambda s: _index(s).table_for(_BAD),
    "statcatlib.store.scopes.CodelistScope.batch_labels": lambda s: s.codelist(_GEO).batch_labels(["CA"], lang=_BAD),
    "statcatlib.store.scopes.CodelistScope.label": lambda s: s.codelist(_GEO).label(lang=_BAD),
    "statcatlib.store.scopes.CodelistScope.rows": lambda s: (
        s.codelist(_GEO).enrich(n=sc.each("datasets").count()).rows(lang=_BAD)
    ),
    "statcatlib.store.scopes.CodelistsScope.batch_labels": lambda s: s.codelists.batch_labels(
        [(_GEO, "CA")], lang=_BAD
    ),
    "statcatlib.store.scopes.CodelistsScope.filter": lambda s: s.codelists.filter(text_contains="x", lang=_BAD),
    "statcatlib.store.scopes.CodelistsScope.rows": lambda s: s.codelists.enrich(n=sc.each("datasets").count()).rows(
        lang=_BAD
    ),
    "statcatlib.store.scopes.SchemeScope.labels": lambda s: s.themes["urn:scheme"].labels(lang=_BAD),
    "statcatlib.store.scopes.SchemeScope.rows": lambda s: (
        s.themes["urn:scheme"].enrich(n=sc.each("datasets").count()).rows(lang=_BAD)
    ),
    "statcatlib.store.scopes._FilteredCodelistsScope.rows": lambda s: (
        s.dataset("TEST:DF(1.0)").codelists.enrich(n=sc.each("datasets").count()).rows(lang=_BAD)
    ),
    "statcatlib.store.store.CodesScope.find": lambda s: s.codes.find("x", lang=_BAD),
    "statcatlib.store.store.CodesScope.get": lambda s: s.codes.get("urn:x", lang=_BAD),
    "statcatlib.store.store.ConceptsScope.find": lambda s: s.concepts.find("x", lang=_BAD),
    "statcatlib.store.store.ConceptsScope.get": lambda s: s.concepts.get("urn:x", lang=_BAD),
    "statcatlib.store.store.DatasetsScope.find": lambda s: s.datasets.find("x", lang=_BAD),
    "statcatlib.store.store.ReferenceMetadataScope.find": lambda s: s.reference_metadata.find("x", lang=_BAD),
    "statcatlib.store.store.EditionView.agent_labels": lambda s: s.agent_labels(lang=_BAD),
    "statcatlib.store.store.EditionView.code_map": lambda s: s.code_map("TEST:DF(1.0)", lang=_BAD),
    "statcatlib.store.store.EditionView.concept_labels": lambda s: s.concept_labels(lang=_BAD),
    "statcatlib.store.store.EditionView.preference": lambda s: s.preference(lang=_BAD),
    "statcatlib.store.store.EditionView.search": lambda s: s.search("x", lang=_BAD),
    "statcatlib.store.store.EditionView.spatial_labels": lambda s: s.spatial_labels(lang=_BAD),
    "statcatlib.store.store.QueryBuilder.hybrid_search": lambda s: s.query(sc.OpenDataset).hybrid_search(
        "x", lang=_BAD
    ),
    "statcatlib.store.store.QueryBuilder.scalars": lambda s: s.query(sc.OpenDataset).scalars("title", lang=_BAD),
    "statcatlib.store.store.QueryBuilder.search": lambda s: s.query(sc.OpenDataset).search("x", lang=_BAD),
    "statcatlib.store.store.QueryBuilder.select": lambda s: s.query(sc.OpenDataset).select("title", lang=_BAD),
    "statcatlib.store.store.QueryBuilder.sort_by": lambda s: s.query(sc.OpenDataset).sort_by("title", lang=_BAD),
    # Their parameter is ``key``, a tag or tags, so the walk does not see them.
    "statcatlib.query.MapCondition": lambda s: q.MapCondition(key=_BAD, value="x"),
    "statcatlib.query.MapFilter": lambda s: q.MapFilter(column="title", key=_BAD, value="x"),
}
_BY_HAND = {"statcatlib.query.MapCondition", "statcatlib.query.MapFilter"}


def test_every_lang_parameter_is_registered() -> None:
    assert set(_discover()) == set(_calls) - _BY_HAND


@pytest.mark.parametrize("name", sorted(_calls))
def test_every_lang_parameter_refuses_a_malformed_tag(store: ScratchStore, name: str) -> None:
    with pytest.raises(sdmxlib.InvalidTagError):
        _calls[name](store)


def test_no_lang_parameter_has_a_default() -> None:
    defaults = {name: p.default for name, p in _discover().items() if p.default is not inspect.Parameter.empty}
    assert defaults == dict.fromkeys(_OPTIONAL)


def _string_defaults(tree: ast.AST) -> Iterator[tuple[int, str]]:
    """Each ``lang``, ``langs`` or ``*_lang`` parameter or class field whose default is a string."""

    def tagged(name: str) -> bool:
        return name.lstrip("_") in {"lang", "langs"} or name.endswith("_lang")

    def is_str(node: ast.expr | None) -> bool:
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            return True
        if isinstance(node, ast.Call):
            return any(k.arg == "default" and is_str(k.value) for k in node.keywords)
        return False

    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            args = node.args
            positional = [*args.posonlyargs, *args.args]
            pairs = list(zip(positional[len(positional) - len(args.defaults) :], args.defaults, strict=True))
            pairs += [(a, d) for a, d in zip(args.kwonlyargs, args.kw_defaults, strict=True) if d is not None]
            for arg, default in pairs:
                if tagged(arg.arg) and is_str(default):
                    yield node.lineno, arg.arg
        elif (
            isinstance(node, ast.AnnAssign)
            and isinstance(node.target, ast.Name)
            and tagged(node.target.id)
            and is_str(node.value)
        ):
            yield node.lineno, node.target.id


def test_no_lang_has_a_string_default_anywhere_in_src() -> None:
    src = Path(sc.__file__).parent
    found = {
        f"{path.relative_to(src)}:{line} {name}"
        for path in sorted(src.rglob("*.py"))
        for line, name in _string_defaults(ast.parse(path.read_text()))
    }
    assert found == set()


#: sdmxlib's well-formed tags (``tests/unit/test_language_tags.py`` at ``v0.89.0``).
_WELL_FORMED = (
    "en",
    "EN",
    "fr-CA",
    "zh-Hant-TW",
    "es-419",
    "en-t-fr",
    "und",
    "i-klingon",
    "zh-min-nan",
    "x-private",
)


def test_tag_alias_keeps_the_old_slug_body() -> None:
    """Search table names and ``index.json`` keys stay what ``_language_slug`` wrote."""
    pairs = (a + b for a in string.ascii_letters for b in string.ascii_letters)
    for tag in (*_WELL_FORMED, *pairs):
        assert sdmxlib.tag_alias(tag) == tag.strip().lower().replace("-", "_"), tag


def test_a_label_encodes_text_then_lang(store: ScratchStore) -> None:
    label = store.codelist(_GEO).label(lang="EN")
    assert msgspec.json.encode(label) == b'{"text":"Geography","lang":"en"}'


def test_index_grouping_refuses_an_empty_lang() -> None:
    row: dict[str, object] = {"id": "x", "entity_type": "open", "lang": "", "document": "x"}
    with pytest.raises(sdmxlib.InvalidTagError):
        LanceIndex._by_language([row], {})  # pyrefly: ignore[bad-argument-type]
