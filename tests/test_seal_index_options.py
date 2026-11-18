"""The seal keeps the index options of a `build_index` call made in another process (GH-108).

`WorkingCopy.build_index` records its options in ``statcat._meta.index_options``
and a fingerprint of the edition's tables in ``statcat._meta.index_built_from``.
The seal keeps the index when the tables still have that fingerprint, and
otherwise rebuilds it with the recorded options. Each test builds the index in
a child Python process and seals in the test's own process, as
``statcat build-index`` followed by a seal does.
"""

from __future__ import annotations

import json
import subprocess
import sys
import textwrap
from typing import TYPE_CHECKING

import pytest

import statcatlib as sc
from statcatlib.store.store import _Store
from tests.conftest import search_table

if TYPE_CHECKING:
    from pathlib import Path

pytestmark = pytest.mark.integration

pytest.importorskip("lancedb", reason="the seal's index needs statcatlib[search]")

_PRODUCT = "p"

# Builds a working copy with codes and two datasets, indexes it without codes,
# and closes it unsealed: the first process of the CLI's build-index-then-seal.
_BUILD = textwrap.dedent(
    """
    import sys
    from pathlib import Path

    from sdmxlib.testing import Dim, Shape, synthetic_edition

    import statcatlib as sc
    from statcatlib.sources.sdmx import prepare_sdmx

    root = Path(sys.argv[1])
    shape = Shape(dimensions=(Dim(codes=6), Dim(codes=3)), observations=20, languages=("en",))
    with sc.Catalog.create(root) as cat:
        synthetic_edition(root / "products" / "p", shape, seed=1, seal=False).close()
        wc = cat.product("p").working_copy(resume=True)
        wc.put(prepare_sdmx(wc.registry))
        wc.build_index(include_codes=False, max_codes_per_codelist=4)
        wc.close()
    """
)


def _build_in_another_process(root: Path) -> None:
    # S603 asks for a literal argument list; the script is this module's own constant.
    subprocess.run([sys.executable, "-c", _BUILD, str(root)], check=True)  # noqa: S603


def _entity_types(view: sc.EditionView) -> set[str]:
    return {row["entity_type"] for row in search_table(view).to_arrow().to_pylist()}


def _dataset(identifier: str, title: str) -> sc.OpenDataset:
    text = sc.InternationalString(en=title)
    return sc.OpenDataset(identifier=identifier, title=text, description=text)


@pytest.fixture
def builds(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, object]]:
    """The keyword arguments of each `_Store.build_index` call made in this process."""
    calls: list[dict[str, object]] = []
    real = _Store.build_index

    def spy(self: _Store, *args: object, **kwargs: object) -> int:
        calls.append(kwargs)
        return real(self, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(_Store, "build_index", spy)
    return calls


def test_the_options_are_recorded_in_the_working_copy(tmp_path: Path) -> None:
    root = tmp_path / "catalog"
    _build_in_another_process(root)
    with sc.Catalog.create(root, exist_ok=True) as cat:
        wc = cat.product(_PRODUCT).working_copy(resume=True)
        cursor = wc._store._cursor()
        rows = dict(cursor.execute("SELECT key, value FROM _meta").fetchall())
        wc.close()
    options = json.loads(rows["index_options"])
    assert options["include_codes"] is False
    assert options["max_codes_per_codelist"] == 4
    assert len(rows["index_built_from"]) == 64


def test_a_seal_with_no_write_keeps_the_index_built_in_another_process(
    tmp_path: Path, builds: list[dict[str, object]]
) -> None:
    root = tmp_path / "catalog"
    _build_in_another_process(root)
    with sc.Catalog.create(root, exist_ok=True) as cat:
        cat.product(_PRODUCT).working_copy(resume=True).seal()
        cat.refresh()
        types = _entity_types(cat.product(_PRODUCT).view())
    assert builds == []
    assert "aggregate" in types
    assert "code" not in types


def test_a_write_makes_the_seal_rebuild_with_the_recorded_options(
    tmp_path: Path, builds: list[dict[str, object]]
) -> None:
    root = tmp_path / "catalog"
    _build_in_another_process(root)
    with sc.Catalog.create(root, exist_ok=True) as cat:
        wc = cat.product(_PRODUCT).working_copy(resume=True)
        wc.put(_dataset("added", "Added after the index"))
        wc.seal()
        cat.refresh()
        view = cat.product(_PRODUCT).view()
        types = _entity_types(view)
        ids = {row["id"] for row in search_table(view).to_arrow().to_pylist()}
    assert len(builds) == 1
    assert builds[0]["include_codes"] is False
    assert builds[0]["max_codes_per_codelist"] == 4
    assert "code" not in types
    assert "added" in ids


def test_a_seal_straight_after_build_index_does_not_rebuild(tmp_path: Path, builds: list[dict[str, object]]) -> None:
    with sc.Catalog.create(tmp_path / "catalog") as cat:
        wc = cat.product(_PRODUCT).working_copy()
        wc.put(_dataset("d1", "Labour force"))
        wc.build_index()
        wc.seal()
    assert len(builds) == 1


def test_a_write_after_build_index_rebuilds_in_the_same_process(
    tmp_path: Path, builds: list[dict[str, object]]
) -> None:
    with sc.Catalog.create(tmp_path / "catalog") as cat:
        wc = cat.product(_PRODUCT).working_copy()
        wc.put(_dataset("d1", "Labour force"))
        wc.build_index(max_codes_per_codelist=7)
        wc.put(_dataset("d2", "Consumer prices"))
        wc.seal()
    assert len(builds) == 2
    assert builds[1]["max_codes_per_codelist"] == 7


def test_the_next_edition_starts_without_the_recorded_options(tmp_path: Path, builds: list[dict[str, object]]) -> None:
    """A new working copy drops the keys its copied ``metadata.duckdb`` carries, so its seal builds an index."""
    with sc.Catalog.create(tmp_path / "catalog") as cat:
        wc = cat.product(_PRODUCT).working_copy()
        wc.put(_dataset("d1", "Labour force"))
        wc.build_index(max_codes_per_codelist=7)
        wc.seal()
        nxt = cat.product(_PRODUCT).working_copy()
        keys = {key for (key,) in nxt._store._cursor().execute("SELECT key FROM _meta").fetchall()}
        nxt.seal()
        cat.refresh()
        types = _entity_types(cat.product(_PRODUCT).view())
    assert "index_options" not in keys
    assert "index_built_from" not in keys
    assert len(builds) == 2
    assert "max_codes_per_codelist" not in builds[1]
    assert "open" in types
