"""CatalogAdmin: rollback, release, cancel, embargo and the description (Phase 2a S3).

RFC rows D4, D7, D13, D14, D16, D17 and D20, and CUJs 2 and 6 (design
`docs/design/phase-2a-catalog-editions.md`, item 33). A race is staged by
running a competing change inside the first `InventoryWriter.try_commit` of
the change under test, so that version lands first and the commit re-reads
the head.
"""

from __future__ import annotations

import annotationlib
import json
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import get_args

import pytest
import sdmxlib.edition
from sdmxlib.inventory import (
    EventKind,
    FullInventory,
    Leaf,
    Root,
    State,
    Tombstone,
    decode_root,
    local_fetch,
    root_path,
)

import statcatlib as sc
from statcatlib.admin import CatalogAdmin, EditionEntry
from statcatlib.bundle import description_from_json, description_to_json
from statcatlib.store.inventory import InventoryRuleError, InventoryWriter, NonFastForwardError
from statcatlib.store.store import IncompatibleCatalogError

pytestmark = pytest.mark.integration


def _dataset(identifier: str) -> sc.OpenDataset:
    return sc.OpenDataset(
        identifier=identifier,
        title=sc.InternationalString(en=identifier),
        description=sc.InternationalString(en=identifier),
    )


def _seal(root: Path, identifier: str, product: str = "p") -> sc.Edition:
    with sc.Catalog.create(root, exist_ok=True) as cat:
        wc = cat.product(product).working_copy()
        wc.put([_dataset(identifier)])
        return wc.seal()


def _states(admin: CatalogAdmin, product: str = "p") -> dict[str, State]:
    return {e.edition_id: e.state for e in admin.editions(product)}


def _root(path: Path, seq: int) -> Root:
    raw = local_fetch(path)(root_path(seq))
    assert raw is not None
    return decode_root(raw)


def _newest(path: Path) -> Root:
    return FullInventory(local_fetch(path)).refresh().root


def _race_once(monkeypatch: pytest.MonkeyPatch, writer: InventoryWriter, competing: Callable[[], object]) -> None:
    """Run *competing* inside *writer*'s first `try_commit`, before it writes, so it wins the race."""
    original = writer.try_commit
    calls: list[int] = []

    def raced(
        base: Root | None,
        leaves: Mapping[str, Leaf],
        *,
        retired: Sequence[Tombstone] | None = None,
        embargo: bool | None = None,
        extensions: Mapping[str, Mapping[str, object]] | None = None,
        built_from: int | None = None,
        kind: EventKind,
        reason: str,
    ) -> Root:
        if not calls:
            calls.append(1)
            competing()
        return original(
            base,
            leaves,
            retired=retired,
            embargo=embargo,
            extensions=extensions,
            built_from=built_from,
            kind=kind,
            reason=reason,
        )

    monkeypatch.setattr(writer, "try_commit", raced)


def _description(identifier: str, title: str) -> sc.CatalogDescription:
    return sc.CatalogDescription(
        identifier=identifier,
        title=sc.InternationalString(en=title, fr=f"{title} (fr)"),
        description=sc.InternationalString(en="Every table"),
        publisher=sc.Agent(name=sc.InternationalString(en="Stats"), id="stats", homepage="https://stats.example"),
        homepage="https://catalog.example",
        language=["en", "fr"],
        licence=sc.LicenceDocument(id="ogl", label=sc.InternationalString(en="Open Licence"), uri="https://ogl"),
        theme_taxonomy=[sc.ConceptSchemeRef(uri="https://themes", label=sc.InternationalString(en="Themes"))],
        issued=datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC),
        modified=datetime(2026, 11, 2, tzinfo=UTC),
    )


@pytest.fixture
def root(tmp_path: Path) -> Path:
    return tmp_path / "cat"


@pytest.fixture
def embargoed(root: Path) -> tuple[CatalogAdmin, sc.Edition, sc.Edition]:
    """A live edition A of product p, and B built on it and sealed under embargo."""
    a = _seal(root, "a")
    admin = CatalogAdmin.open(root)
    admin.set_embargo(on=True, reason="embargo the next release")
    b = _seal(root, "b")
    return admin, a, b


class TestOpen:
    def test_it_is_not_constructed_directly(self) -> None:
        with pytest.raises(TypeError, match=r"CatalogAdmin\.open"):
            CatalogAdmin()

    def test_a_path_with_no_catalog_is_refused(self, tmp_path: Path) -> None:
        with pytest.raises(FileNotFoundError, match="not a catalog"):
            CatalogAdmin.open(tmp_path)

    def test_a_legacy_layout_is_refused_and_left_unchanged(self, tmp_path: Path) -> None:
        (tmp_path / "meta.duckdb").write_bytes(b"0.11")
        with pytest.raises(IncompatibleCatalogError, match=r"0\.11"):
            CatalogAdmin.open(tmp_path)
        assert sorted(p.name for p in tmp_path.iterdir()) == ["meta.duckdb"]

    def test_opening_writes_nothing(self, root: Path) -> None:
        sc.Catalog.create(root).close()
        before = sorted(p.name for p in root.iterdir())
        admin = CatalogAdmin.open(root)
        assert admin.products() == []
        assert admin.editions("p") == []
        assert admin.embargo is False
        assert sorted(p.name for p in root.iterdir()) == before
        assert repr(admin).startswith("<CatalogAdmin ")

    def test_every_change_needs_a_reason(self, root: Path) -> None:
        _seal(root, "a")
        admin = CatalogAdmin.open(root)
        with pytest.raises(ValueError, match="needs a reason"):
            admin.set_embargo(on=True, reason=" ")
        with pytest.raises(ValueError, match="needs a reason"):
            admin.rollback("p", reason="")
        assert _newest(root).seq == 1

    def test_an_unknown_product_is_refused(self, root: Path) -> None:
        _seal(root, "a")
        admin = CatalogAdmin.open(root)
        with pytest.raises(InventoryRuleError, match="no product q") as caught:
            admin.release("q", "e", reason="r")
        assert caught.value.rule == "unknown"
        with pytest.raises(InventoryRuleError, match="no product q"):
            admin.rollback("q", reason="r")


class TestPublicCatalogNeverSeesEmbargoed:
    """RFC D4, D13, D16."""

    def test_the_public_catalog_returns_no_embargoed_entry(
        self, root: Path, embargoed: tuple[CatalogAdmin, sc.Edition, sc.Edition]
    ) -> None:
        admin, a, b = embargoed
        # A second product whose only edition is embargoed.
        _seal(root, "x", product="q")
        assert _states(admin, "q") == {admin.editions("q")[0].edition_id: "embargoed"}
        with sc.Catalog.open(root) as cat:
            assert cat.products() == ["p"]
            assert [e.edition_id for e in cat.product("p").editions()] == [a.edition_id]
            view = cat.product("p").view()
            assert view.get(sc.OpenDataset, "a") is not None
            assert view.get(sc.OpenDataset, "b") is None
            assert cat.product("q").editions() == []
            with pytest.raises(sc.NoLiveEditionError, match=r"CatalogAdmin\.editions"):
                cat.product("q").view()
        assert _states(admin)[b.edition_id] == "embargoed"

    def test_no_public_attribute_returns_a_catalog_admin(
        self, root: Path, embargoed: tuple[CatalogAdmin, sc.Edition, sc.Edition]
    ) -> None:
        del embargoed
        with sc.Catalog.open(root) as cat:
            product = cat.product("p")
            view = product.view()
            for owner in (cat, product, view):
                for name in dir(owner):
                    if name.startswith("_"):
                        continue
                    try:
                        value = getattr(owner, name)
                    except AttributeError, NotImplementedError, RuntimeError:
                        continue
                    where = f"{type(owner).__name__}.{name}"
                    assert not isinstance(value, CatalogAdmin), where
                    assert not isinstance(value, EditionEntry), where
                    if callable(value) and hasattr(value, "__annotations__"):
                        returns = annotationlib.get_annotations(value, format=annotationlib.Format.STRING)
                        assert "CatalogAdmin" not in returns.get("return", ""), where


class TestFastForward:
    """RFC D7."""

    def test_a_release_that_is_not_a_fast_forward_is_refused_and_names_both_editions(
        self, root: Path, embargoed: tuple[CatalogAdmin, sc.Edition, sc.Edition]
    ) -> None:
        admin, a, b = embargoed
        # C is built on A too, since B is embargoed and A stays live.
        c = _seal(root, "c")
        assert c.previous == a.edition_id
        admin.release("p", b.edition_id, reason="release B")
        seq = _newest(root).seq
        with pytest.raises(NonFastForwardError) as caught:
            admin.release("p", c.edition_id, reason="release C")
        message = str(caught.value)
        assert c.edition_id in message
        assert b.edition_id in message
        assert "cancel()" in message
        assert caught.value.rule == "fast-forward"
        assert caught.value.edition == c.edition_id
        assert _newest(root).seq == seq
        assert _states(admin)[c.edition_id] == "embargoed"

    def test_a_conditional_rollback_retried_after_a_competing_release_is_refused(
        self, root: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        a = _seal(root, "a")
        b = _seal(root, "b")
        admin = CatalogAdmin.open(root)
        admin.set_embargo(on=True, reason="embargo")
        c = _seal(root, "c")
        competitor = CatalogAdmin.open(root)
        _race_once(monkeypatch, admin._writer, lambda: competitor.release("p", c.edition_id, reason="release C"))
        with pytest.raises(InventoryRuleError, match=f"expects {b.edition_id} live") as caught:
            admin.rollback("p", reason="roll back B")
        assert caught.value.rule == "if-live"
        assert _states(admin) == {a.edition_id: "superseded", b.edition_id: "superseded", c.edition_id: "live"}
        assert _newest(root).event.kind == "release"

    def test_an_explicit_if_live_that_is_not_live_is_refused(self, root: Path) -> None:
        a = _seal(root, "a")
        _seal(root, "b")
        admin = CatalogAdmin.open(root)
        with pytest.raises(InventoryRuleError) as caught:
            admin.rollback("p", if_live=a.edition_id, reason="r")
        assert caught.value.rule == "if-live"

    def test_a_rollback_to_an_edition_that_is_not_superseded_is_refused(
        self, root: Path, embargoed: tuple[CatalogAdmin, sc.Edition, sc.Edition]
    ) -> None:
        admin, _, b = embargoed
        with pytest.raises(InventoryRuleError) as caught:
            admin.rollback("p", to=b.edition_id, reason="r")
        assert caught.value.rule == "state"

    def test_a_product_with_no_live_edition_has_nothing_to_roll_back(self, root: Path) -> None:
        sc.Catalog.create(root).close()
        admin = CatalogAdmin.open(root)
        admin.set_embargo(on=True, reason="embargo")
        _seal(root, "a")
        with pytest.raises(InventoryRuleError, match="no live edition") as caught:
            admin.rollback("p", reason="r")
        assert caught.value.rule == "if-live"


class TestManifestOfTheReleasedEdition:
    """Phase 5 Goal 2: no edition goes live with a manifest the Inventory does not record, its entry filled or not."""

    @pytest.mark.parametrize("damage", ["altered", "removed"])
    def test_a_release_is_refused_with_manifest_error_and_writes_nothing(
        self, root: Path, embargoed: tuple[CatalogAdmin, sc.Edition, sc.Edition], damage: str
    ) -> None:
        admin, _, b = embargoed
        full = FullInventory(local_fetch(root)).refresh()
        (entry,) = [e for e in full.entries("p") if e.edition_id == b.edition_id]
        assert entry.artefacts is not None, "the entry is filled, and the release still reads the manifest"
        manifest = root / "products" / "p" / b.edition_id / sdmxlib.edition.MANIFEST_NAME
        if damage == "altered":
            manifest.write_bytes(manifest.read_bytes() + b" ")
        else:
            manifest.unlink()
        seq = _newest(root).seq

        with pytest.raises(sdmxlib.edition.ManifestError, match=b.edition_id):
            admin.release("p", b.edition_id, reason="release B")

        assert _newest(root).seq == seq
        assert _states(admin)[b.edition_id] == "embargoed"


class TestWithdrawnStaysWithdrawn:
    """RFC D14."""

    def test_the_state_enum_has_four_values(self) -> None:
        assert len(get_args(State.__value__)) == 4

    def test_release_does_not_re_release_a_cancelled_edition(
        self, embargoed: tuple[CatalogAdmin, sc.Edition, sc.Edition]
    ) -> None:
        admin, a, b = embargoed
        version = admin.cancel("p", b.edition_id, reason="bad figures")
        assert _states(admin) == {a.edition_id: "live", b.edition_id: "withdrawn"}
        with pytest.raises(InventoryRuleError, match="withdrawn") as caught:
            admin.release("p", b.edition_id, reason="release")
        assert caught.value.rule == "state"
        assert _states(admin)[b.edition_id] == "withdrawn"
        assert version == _newest(admin._writer.root).seq

    def test_cancel_refuses_an_edition_that_is_not_embargoed(
        self, embargoed: tuple[CatalogAdmin, sc.Edition, sc.Edition]
    ) -> None:
        admin, a, _ = embargoed
        with pytest.raises(InventoryRuleError) as caught:
            admin.cancel("p", a.edition_id, reason="r")
        assert caught.value.rule == "state"

    def test_the_journal_repair_does_not_re_release_a_rolled_back_edition(
        self, root: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        a = _seal(root, "a")
        with monkeypatch.context() as patch:
            # B's writer stops before it clears its journal entry, so the entry stays.
            patch.setattr(sdmxlib.edition, "clear_journal", lambda *_args: None)
            b = _seal(root, "b")
        journal = root / "products" / "p" / "journal"
        assert [p.stem for p in journal.iterdir()] == [b.edition_id]
        admin = CatalogAdmin.open(root)
        admin.rollback("p", reason="B is wrong")
        with sc.Catalog.create(root, exist_ok=True) as cat:
            wc = cat.product("p").working_copy()
            assert wc.previous == a.edition_id
            wc.discard()
        assert list(journal.iterdir()) == []
        assert _states(admin) == {a.edition_id: "live", b.edition_id: "withdrawn"}


class TestEmbargoSetting:
    """RFC D17."""

    def test_changing_the_embargo_writes_one_version(self, root: Path) -> None:
        _seal(root, "a")
        admin = CatalogAdmin.open(root)
        assert admin.set_embargo(on=True, reason="embargo on") == 2
        newest = _newest(root)
        assert newest.embargo is True
        assert newest.event.kind == "embargo-change"
        assert newest.event.reason == "embargo on"
        assert admin.embargo is True
        assert admin.set_embargo(on=False, reason="embargo off") == 3
        assert CatalogAdmin.open(root).embargo is False

    @pytest.mark.parametrize("state", ["on", "off"])
    def test_setting_the_embargo_it_already_has_is_refused(self, root: Path, state: str) -> None:
        on = state == "on"
        _seal(root, "a")
        admin = CatalogAdmin.open(root)
        if on:
            admin.set_embargo(on=True, reason="embargo on")
        seq = _newest(root).seq
        with pytest.raises(ValueError, match=f"already {state}"):
            admin.set_embargo(on=on, reason="again")
        assert _newest(root).seq == seq
        assert CatalogAdmin.open(root).embargo is on

    def test_setting_the_embargo_before_the_first_version_off_is_refused(self, root: Path) -> None:
        sc.Catalog.create(root).close()
        admin = CatalogAdmin.open(root)
        with pytest.raises(ValueError, match="already off"):
            admin.set_embargo(on=False, reason="off")
        assert admin.products() == []
        assert not (root / "CURRENT").exists()

    def test_a_stale_automatic_release_after_embargo_is_turned_on_is_refused(
        self, root: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        a = _seal(root, "a")
        admin = CatalogAdmin.open(root)
        with sc.Catalog.create(root, exist_ok=True) as cat:
            wc = cat.product("p").working_copy()
            wc.put([_dataset("b")])
            _race_once(monkeypatch, wc._writer, lambda: admin.set_embargo(on=True, reason="embargo"))
            b = wc.seal()
        # The seal's first record, live, lost to the embargo; its retry recorded B embargoed.
        assert _states(admin) == {a.edition_id: "live", b.edition_id: "embargoed"}
        assert [_root(root, seq).event.kind for seq in (2, 3)] == ["embargo-change", "seal"]
        with sc.Catalog.open(root) as cat:
            assert cat.product("p").view().get(sc.OpenDataset, "b") is None


class TestDescription:
    """RFC D20."""

    def test_the_description_round_trips_through_json(self) -> None:
        described = _description("stats", "Statistics")
        data = json.loads(json.dumps(description_to_json(described)))
        assert description_from_json(data) == described

    def test_unknown_keys_are_ignored(self) -> None:
        data = description_to_json(_description("stats", "Statistics"))
        data["search"] = {"later": True}
        assert description_from_json(data).identifier == "stats"

    def test_a_malformed_description_is_refused(self) -> None:
        data = description_to_json(_description("stats", "Statistics"))
        del data["identifier"]
        with pytest.raises(TypeError, match="identifier"):
            description_from_json(data)
        with pytest.raises(TypeError, match="title"):
            description_from_json({**description_to_json(_description("s", "S")), "title": "plain"})
        with pytest.raises(TypeError, match="language"):
            description_from_json({**description_to_json(_description("s", "S")), "language": "en"})
        with pytest.raises(TypeError, match="theme_taxonomy"):
            description_from_json({**description_to_json(_description("s", "S")), "theme_taxonomy": {}})
        with pytest.raises(TypeError, match="homepage"):
            description_from_json({**description_to_json(_description("s", "S")), "homepage": 3})

    def test_the_optional_fields_may_be_empty(self) -> None:
        bare = sc.CatalogDescription(
            identifier="s",
            title=sc.InternationalString(en="S"),
            description=sc.InternationalString(),
            publisher=sc.Agent(name=sc.InternationalString(en="P")),
        )
        assert description_from_json(json.loads(json.dumps(description_to_json(bare)))) == bare

    def test_describe_changes_the_description_in_one_commit(self, root: Path) -> None:
        _seal(root, "a")
        admin = CatalogAdmin.open(root)
        with sc.Catalog.open(root) as cat:
            assert cat.description is None
        first, second = _description("stats", "Statistics"), _description("stats", "Official statistics")
        assert admin.describe(first, reason="describe the catalog") == 2
        assert admin.describe(second, reason="rename the catalog") == 3
        v2, v3 = _root(root, 2), _root(root, 3)
        assert (v2.event.kind, v3.event.kind) == ("describe", "describe")
        assert v2.products == v3.products == _root(root, 1).products
        assert v2.extensions["statcat"]["format_version"] == "3"
        assert v2.extensions["statcat"]["description"] != v3.extensions["statcat"]["description"]
        with sc.Catalog.open(root) as cat:
            assert cat.description == second

    def test_later_versions_carry_the_description_and_other_keys(self, root: Path) -> None:
        sc.Catalog.create(root).close()
        admin = CatalogAdmin.open(root)
        described = _description("stats", "Statistics")
        # A catalog's first version can be a description.
        assert admin.describe(described, reason="describe") == 1
        with sc.Catalog.open(root) as cat:
            assert cat.products() == []
            assert cat.description == described
        _seal(root, "a")
        admin.set_embargo(on=True, reason="embargo")
        assert _newest(root).seq == 3
        assert _newest(root).extensions == _root(root, 1).extensions
        with sc.Catalog.open(root) as cat:
            assert cat.description == described

    def test_a_catalog_with_no_version_has_no_description(self, root: Path) -> None:
        with sc.Catalog.create(root) as cat:
            assert cat.description is None


class TestJourneys:
    def test_cuj2_a_republish_followed_by_a_rollback(self, root: Path) -> None:
        """CUJ 2: B is withdrawn, A is live again, and a working copy built on B fails its seal."""
        a = _seal(root, "a")
        b = _seal(root, "b")
        with sc.Catalog.create(root, exist_ok=True) as cat:
            on_b = cat.product("p").working_copy()
            assert on_b.previous == b.edition_id
            on_b.put([_dataset("c")])
            version = CatalogAdmin.open(root).rollback("p", reason="B has an error")
            newest = _newest(root)
            assert newest.seq == version == 3
            assert newest.event.kind == "rollback"
            assert newest.event.reason == "B has an error"
            assert _states(CatalogAdmin.open(root)) == {a.edition_id: "live", b.edition_id: "withdrawn"}
            with pytest.raises(NonFastForwardError) as caught:
                on_b.seal()
            message = str(caught.value)
            assert b.edition_id in message
            assert a.edition_id in message
            assert "discard()" in message
            on_b.discard()
        with sc.Catalog.open(root) as cat:
            view = cat.product("p").view()
            assert view.get(sc.OpenDataset, "a") is not None
            assert view.get(sc.OpenDataset, "b") is None

    def test_cuj6_an_embargoed_seal_and_an_explicit_release(
        self, root: Path, embargoed: tuple[CatalogAdmin, sc.Edition, sc.Edition]
    ) -> None:
        """CUJ 6: B is visible only to CatalogAdmin until its release; then the view shows B."""
        admin, a, b = embargoed
        with sc.Catalog.open(root) as cat:
            before = cat.product("p").view()
            assert before.get(sc.OpenDataset, "a") is not None
            assert before.get(sc.OpenDataset, "b") is None
            entries = {e.edition_id: e for e in admin.editions("p")}
            assert entries[b.edition_id].state == "embargoed"
            assert entries[b.edition_id].previous == a.edition_id
            assert entries[b.edition_id].product_id == "p"
            version = CatalogAdmin.open(root).release("p", b.edition_id, reason="release B")
            assert _newest(root).event.kind == "release"
            assert _newest(root).seq == version
            cat.refresh()
            after = cat.product("p").view()
            assert after.get(sc.OpenDataset, "b") is not None
            assert before.get(sc.OpenDataset, "b") is None
        assert _states(admin) == {a.edition_id: "superseded", b.edition_id: "live"}
