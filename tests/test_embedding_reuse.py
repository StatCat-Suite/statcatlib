"""A seal reuses a carried vector whose text hash matches (RFC D21, design item 26).

Each search row records `text_sha256`, the hash of the model name, the
document prefix and the text. A `build_index` given ``model_name``, and so the
seal's rebuild of it, reads the stored vectors before it replaces the tables,
and encodes only the texts whose hash has no vector. Without ``model_name``
it encodes every text, since the encoder may be a new model.
"""

from __future__ import annotations

import logging
import math
from pathlib import Path

import pytest

import statcatlib as sc
from statcatlib.store.lance import text_sha256
from tests.conftest import require_index, require_table

pytestmark = pytest.mark.unit

pytest.importorskip("lancedb", reason="embedding reuse needs statcatlib[search]")


class _CountingModel:
    """A deterministic encoder that records every text it is asked to encode."""

    DIM = 8

    def __init__(self, salt: str = "") -> None:
        self.salt = salt
        self.encoded: list[str] = []

    def encode(self, sentences: str | list[str], /) -> list[float] | list[list[float]]:
        if isinstance(sentences, str):
            self.encoded.append(sentences)
            return self._one(sentences)
        self.encoded.extend(sentences)
        return [self._one(t) for t in sentences]

    def _one(self, text: str) -> list[float]:
        codes = [ord(c) for c in self.salt + text] or [1]
        v = [float(1 + sum(codes[i :: self.DIM])) for i in range(self.DIM)]
        norm = math.sqrt(sum(x * x for x in v))
        return [x / norm for x in v]


def _dataset(identifier: str, title: str) -> sc.OpenDataset:
    return sc.OpenDataset(
        identifier=identifier, title=sc.InternationalString(en=title), description=sc.InternationalString(en=title)
    )


def _vectors(cat: sc.Catalog, product: str = "p") -> dict[str, tuple[str | None, list[float]]]:
    """Each row's id with its `text_sha256` and vector, from the product's live edition."""
    cat.refresh()
    table = require_table(require_index(cat.product(product).view()), "en").to_arrow().to_pylist()
    return {row["id"]: (row["text_sha256"], list(row["embedding"])) for row in table}


@pytest.fixture
def root(tmp_path: Path) -> Path:
    return tmp_path / "catalog"


@pytest.fixture
def first(root: Path) -> _CountingModel:
    """Product ``p`` sealed once with model ``m``: datasets ``d1`` and ``d2``."""
    model = _CountingModel()
    with sc.Catalog.create(root) as cat:
        wc = cat.product("p").working_copy(embedding_model=model)
        wc.put([_dataset("d1", "Labour force"), _dataset("d2", "Consumer prices")])
        wc.build_index(model_name="m")
        wc.seal()
    return model


class TestReuse:
    def test_the_seal_reuses_the_vectors_its_own_build_index_wrote(self, root: Path) -> None:
        model = _CountingModel()
        with sc.Catalog.create(root) as cat:
            wc = cat.product("p").working_copy(embedding_model=model)
            wc.put([_dataset("d1", "Labour force"), _dataset("d2", "Consumer prices")])
            wc.build_index(model_name="m")
            built = len(model.encoded)
            wc.seal()
        assert built == 2
        assert len(model.encoded) == built

    def test_unchanged_text_causes_zero_encode_calls(self, root: Path, first: _CountingModel) -> None:
        """RFC D21: a second seal with unchanged text calls the model zero times."""
        with sc.Catalog.create(root, exist_ok=True) as cat:
            before = _vectors(cat)
            model = _CountingModel()
            wc = cat.product("p").working_copy(embedding_model=model)
            wc.build_index(model_name="m")
            wc.seal()
            assert model.encoded == []
            assert _vectors(cat) == before

    def test_only_a_new_text_is_encoded(self, root: Path, first: _CountingModel) -> None:
        with sc.Catalog.create(root, exist_ok=True) as cat:
            model = _CountingModel()
            wc = cat.product("p").working_copy(embedding_model=model)
            wc.put([_dataset("d2", "Consumer price index"), _dataset("d3", "Population")])
            wc.build_index(model_name="m")
            wc.seal()
            assert len(model.encoded) == 2
            assert all(("Consumer price index" in t) or ("Population" in t) for t in model.encoded)
            hashes = {i: h for i, (h, _) in _vectors(cat).items()}
        assert set(hashes) == {"d1", "d2", "d3"}
        assert all(h is not None for h in hashes.values())

    def test_each_row_records_the_hash_of_model_prefix_and_text(self, root: Path, first: _CountingModel) -> None:
        with sc.Catalog.open(root) as cat:
            view = cat.product("p").view()
            rows = require_table(require_index(view), "en").to_arrow().to_pylist()
        for row in rows:
            assert row["text_sha256"] == text_sha256("m", "", row["text"])


class TestReencode:
    def test_after_a_model_change_every_text_is_reencoded(self, root: Path, first: _CountingModel) -> None:
        with sc.Catalog.create(root, exist_ok=True) as cat:
            model = _CountingModel(salt="v2")
            wc = cat.product("p").working_copy(embedding_model=model)
            wc.build_index(model_name="m2")
            assert len(model.encoded) == 2
            wc.seal()
            assert len(model.encoded) == 2
            after = _vectors(cat)
            meta = cat.product("p").view().embedding_metadata
        assert meta is not None
        assert meta.model == "m2"
        assert {h for h, _ in after.values()} == {text_sha256("m2", "", t) for t in first.encoded}

    def test_a_model_without_model_name_reencodes_every_text(
        self, root: Path, first: _CountingModel, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A new model object given without ``model_name`` reuses nothing, so two models never share an index."""
        with sc.Catalog.create(root, exist_ok=True) as cat:
            model = _CountingModel(salt="new")
            wc = cat.product("p").working_copy(embedding_model=model)
            with caplog.at_level(logging.INFO, logger="statcatlib.store.store"):
                wc.seal()
            assert sorted(model.encoded) == sorted(first.encoded)
            after = _vectors(cat)
            rows = require_table(require_index(cat.product("p").view()), "en").to_arrow().to_pylist()
        for row in rows:
            assert list(row["embedding"]) == pytest.approx(model._one(row["text"]), abs=1e-6)
        assert "not reused because no model_name was given" in caplog.text
        # The rows still carry the hash under the recorded name, for a later explicit build.
        assert {h for h, _ in after.values()} == {text_sha256("m", "", t) for t in first.encoded}

    def test_a_prefix_change_reencodes_every_text(self, root: Path, first: _CountingModel) -> None:
        with sc.Catalog.create(root, exist_ok=True) as cat:
            model = _CountingModel()
            wc = cat.product("p").working_copy(embedding_model=model)
            wc.build_index(document_prefix="passage: ", model_name="m")
            wc.seal()
        assert len(model.encoded) == 2
        assert all(t.startswith("passage: ") for t in model.encoded)

    def test_without_a_model_name_nothing_is_reused_or_hashed(self, root: Path) -> None:
        model = _CountingModel()
        with sc.Catalog.create(root) as cat:
            wc = cat.product("p").working_copy(embedding_model=model)
            wc.put([_dataset("d1", "Labour force")])
            wc.build_index()
            wc.seal()
            assert len(model.encoded) == 2
            assert [h for h, _ in _vectors(cat).values()] == [None]
