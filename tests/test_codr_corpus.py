"""The CODR corpus generator and the Phase 4 bench script (GH-124).

`codr_corpus` seals groups of one-table and multi-table data products, some
under embargo, and grows a corpus a smaller call built. ``bench/corpus.py``
builds a corpus and records what the cross-product rules cost; a run at two
products checks that every measurement it makes is recorded.
"""

from __future__ import annotations

import importlib
import json
import sys
from pathlib import Path

import pytest

import statcatlib as sc
from statcatlib.testing import (
    STRUCTURE_PRODUCTS,
    CodrShape,
    CorpusGroup,
    codr_corpus,
    corpus_embargoed,
    corpus_product_id,
    temporary_catalog,
)

pytestmark = pytest.mark.integration

SHAPE = CodrShape(geography_codes=200, geography_depth=3, classification_codes=(40, 20, 10), observations=200)

#: At this share, `corpus_embargoed` picks ``codr-1t-00001`` and ``codr-1t-00003`` of the ids below.
EMBARGOED = 0.3

REPOSITORY = Path(__file__).resolve().parents[1]


def _states(root: Path) -> dict[str, set[str]]:
    admin = sc.CatalogAdmin.open(root)
    return {product_id: {e.state for e in admin.editions(product_id)} for product_id in admin.products()}


def test_a_corpus_group_refuses_a_negative_count_and_a_product_of_no_table() -> None:
    with pytest.raises(ValueError, match="zero or more products"):
        CorpusGroup(-1)
    with pytest.raises(ValueError, match="one or more tables"):
        CorpusGroup(2, tables=0)
    assert corpus_product_id(CorpusGroup(3, tables=12), 7) == "codr-12t-00007"


def test_the_embargo_draw_takes_none_at_zero_and_all_at_one() -> None:
    ids = [corpus_product_id(CorpusGroup(50), n) for n in range(50)]

    assert not any(corpus_embargoed(p, embargoed=0) for p in ids)
    assert all(corpus_embargoed(p, embargoed=1) for p in ids)
    assert [p for p in ids[:5] if corpus_embargoed(p, embargoed=EMBARGOED)] == ["codr-1t-00001", "codr-1t-00003"]


def test_a_share_outside_zero_to_one_is_refused_before_anything_is_written() -> None:
    with temporary_catalog() as cat, pytest.raises(ValueError, match="share from 0 to 1"):
        codr_corpus(cat, [CorpusGroup(1)], embargoed=1.5)


def test_a_mixed_corpus_holds_live_and_embargoed_products_and_grows_on_a_second_call(tmp_path: Path) -> None:
    root = tmp_path / "corpus"
    seen: list[str] = []
    with sc.Catalog.create(root) as cat:
        first = codr_corpus(
            cat,
            [CorpusGroup(3), CorpusGroup(1, tables=2)],
            shape=SHAPE,
            embargoed=EMBARGOED,
            on_sealed=lambda product_id, _edition: seen.append(product_id),
        )
        second = codr_corpus(cat, [CorpusGroup(4), CorpusGroup(1, tables=2)], shape=SHAPE, embargoed=EMBARGOED)
        cat.refresh()
        pair = cat.product("codr-2t-00000").view().query(sc.AggregateDataset).count()

    assert first == seen == ["codr-1t-00000", "codr-1t-00001", "codr-1t-00002", "codr-2t-00000"]
    assert second == ["codr-1t-00003"], "the second call seals only the product the first did not record"
    assert pair == 2, "the two-table product holds one dataset per table"
    states = _states(root)
    assert all(states[p] == {"live"} for p in STRUCTURE_PRODUCTS)
    assert {p: s for p, s in states.items() if p not in STRUCTURE_PRODUCTS} == {
        "codr-1t-00000": {"live"},
        "codr-1t-00001": {"embargoed"},
        "codr-1t-00002": {"live"},
        "codr-1t-00003": {"embargoed"},
        "codr-2t-00000": {"live"},
    }
    assert not sc.CatalogAdmin.open(root).embargo, "the embargo is off when the call returns"


def test_the_bench_records_warm_and_cold_seals_a_batch_and_a_release(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # The cold measurements run in spawned processes, which import the
    # script as bench.corpus from the repository root.
    monkeypatch.syspath_prepend(str(REPOSITORY))
    monkeypatch.delitem(sys.modules, "bench.corpus", raising=False)
    corpus = importlib.import_module("bench.corpus")
    root = tmp_path / "corpus"
    flags = ["--root", str(root), "--shape", "small", "--embargoed", str(EMBARGOED)]

    corpus.main(["build", *flags, "--n", "2", "--samples", "2", "--repeats", "1", "--batch", "1"])
    capsys.readouterr()
    corpus.main(["report", *flags])

    records = [json.loads(line) for line in (root / "bench.jsonl").read_text().splitlines()]
    kinds = [r["kind"] for r in records]
    assert kinds == ["start", "build", "warm", "cold", "batch-warm", "batch-cold", "release-cold", "end"]
    by_kind = {r["kind"]: r for r in records}
    assert by_kind["build"]["live"] == 1, "codr-1t-00001 seals under embargo"
    assert by_kind["build"]["embargoed"] == 1
    assert by_kind["warm"]["rules_calls"] >= 2, "the pre-check and the Inventory commit both run the rules"
    assert by_kind["cold"]["reads"] == 1, "a new process reads the incoming manifest alone: every entry is filled"
    assert by_kind["cold"]["read_bytes"] > 0
    assert by_kind["release-cold"]["reads"] > 0
    assert "| Data products |" in capsys.readouterr().out
