"""A product's one unsealed edition, and its seal (Phase 2a).

`Product.working_copy` starts or resumes a `WorkingCopy`. Ingest writes into
it through the write methods and `WorkingCopy.registry`, `WorkingCopy.view`
reads it back, and `WorkingCopy.seal` turns it into an immutable edition and
records it in the Inventory, live when the catalog's embargo is off.

**The journal rule.** sdmxlib's `seal` writes a journal entry,
``<product>/journal/<edition_id>.json``, before the manifest, and statcatlib
clears it once the edition is recorded. An entry left behind is a seal that a
crash may have stopped before the record. Every writer settles the product's
entries against the newest Inventory version when it starts a working copy,
when it resumes one, at the start of a seal and after its record
(`settle_journal`):

| Journal entry | Action |
|---|---|
| The newest root names or tombstones the edition | Clear the entry |
| The edition has no ``manifest.json`` | Clear the entry; the edition is an unsealed working copy |
| It has a manifest and no root names it | Record it, then clear the entry |
| Recording raises ``duplicate`` with the same ``manifest_sha256`` | Treat as success; clear the entry |
| Recording raises any other `InventoryRuleError` | Clear the entry, log a WARNING, leave the edition |
| `InventoryContentionError` | Keep the entry, so the next working copy records it |
| The entry cannot be read | Raise `JournalError` naming the file and ``statcat doctor`` |

A product has one writer at a time: a laptop convention, stated in the docs.
"""

from __future__ import annotations

import contextlib
import hashlib
import importlib.util
import logging
import shutil
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Self

import sdmxlib
import sdmxlib.edition as _edition
from attrs import frozen
from sdmxlib.edition import MANIFEST_NAME, Finding, ManifestError
from sdmxlib.inventory import FullInventory, Leaf, TransitionError, head, record
from sdmxlib.storage.schema import SCHEMA_VERSION, meta_value

from statcatlib.bundle import STATCAT_SECTION, catalog_section
from statcatlib.store import schema as _catalog_schema
from statcatlib.store.inventory import (
    Change,
    InventoryContentionError,
    InventoryRuleError,
    InventoryWriter,
    NonFastForwardError,
    producer,
)
from statcatlib.store.store import EditionView, _Store

if TYPE_CHECKING:
    from collections.abc import Callable, Generator, Iterable, Mapping, Sequence
    from pathlib import Path

    from sdmxlib.edition import JournalEntry, Lifecycle

    from statcatlib.model.dataset import Dataset
    from statcatlib.model.skos import Concept, ConceptScheme
    from statcatlib.store.store import CodelistIndexPolicy, EmbeddingModel, ReferenceMetadataBlend

__all__ = [
    "Edition",
    "JournalError",
    "SealCheckError",
    "WorkingCopy",
    "WorkingCopyClosedError",
    "WorkingCopyExistsError",
    "settle_journal",
]

log = logging.getLogger(__name__)

#: The only product kind Phase 2a writes; structure products arrive in Phase 3.
_KIND = "data"


@frozen
class Edition:
    """A sealed edition, as the Inventory records it.

    Attributes:
        edition_id: The edition's id, a UUIDv7.
        product_id: Its product.
        previous: The edition it was built on, or ``None`` for the product's
            first.
        manifest_sha256: The SHA-256 of its ``manifest.json``.
    """

    edition_id: str
    product_id: str
    previous: str | None
    manifest_sha256: str


class WorkingCopyExistsError(Exception):
    """The product already has an unsealed edition; a product has one at a time."""


class WorkingCopyClosedError(RuntimeError):
    """The working copy was sealed or discarded, so neither it nor its views answer."""


class SealCheckError(Exception):
    """A seal check found something wrong, so the edition was not sealed.

    Attributes:
        findings: Everything the checks found.
    """

    findings: tuple[Finding, ...]

    def __init__(self, findings: Sequence[Finding]) -> None:
        """List the findings."""
        self.findings: tuple[Finding, ...] = tuple(findings)
        listed = "; ".join(str(f) for f in self.findings)
        super().__init__(
            f"the seal checks found {len(self.findings)} problem(s), so nothing was sealed and the working "
            f"copy stays open: {listed}"
        )


class JournalError(Exception):
    """A seal journal entry cannot be read, so the product's seals cannot be settled."""


def manifest_sha256(edition_dir: Path) -> str:
    """The SHA-256 of *edition_dir*'s ``manifest.json``, lowercase hex."""
    return hashlib.sha256((edition_dir / MANIFEST_NAME).read_bytes()).hexdigest()


def _read_journal(product_dir: Path) -> list[JournalEntry]:
    try:
        return _edition.read_journal(product_dir)
    except ManifestError as exc:
        msg = f"{exc}. Run `statcat doctor` on the catalog to see the product's seals."
        raise JournalError(msg) from exc


def _named(full: FullInventory | None, product_id: str, edition_id: str) -> bool:
    """Whether the version *full* holds names *edition_id* in any state, or tombstones it."""
    if full is None:
        return False
    if any(t.product_id == product_id and t.edition_id == edition_id for t in full.retired):
        return True
    if product_id not in full.products():
        return False
    return any(e.edition_id == edition_id for e in full.entries(product_id))


def record_journalled(
    leaf: Leaf | None,
    entries: Sequence[tuple[JournalEntry, str, str]],
    *,
    product_id: str,
    lifecycle: Lifecycle,
    embargo: bool,
    retired: Iterable[str],
    now: datetime,
) -> tuple[Leaf | None, list[str], dict[str, TransitionError]]:
    """Record each journalled edition in *leaf*, by the journal rule.

    Args:
        leaf: The product's leaf in the head, or ``None``.
        entries: Each journal entry with its edition's ``manifest_sha256`` and
            ``schema_version``.
        product_id: The product.
        lifecycle: The product's lifecycle, for a first record.
        embargo: The head's embargo setting.
        retired: The edition ids the head tombstones for the product.
        now: The record time.

    Returns:
        The next leaf (``None`` when nothing changed), the edition ids that are
        settled (recorded now, or recorded already with the same
        ``manifest_sha256``), and each refused edition's refusal.
    """
    current = leaf
    changed = False
    settled: list[str] = []
    refused: dict[str, TransitionError] = {}
    gone = set(retired)
    for entry, sha, schema_version in entries:
        try:
            current = record(
                current,
                product_id=product_id,
                kind=_KIND,
                lifecycle=current.lifecycle if current is not None else lifecycle,
                edition_id=entry.edition_id,
                previous=entry.previous,
                manifest_sha256=sha,
                schema_version=schema_version,
                embargo=embargo,
                now=now,
                retired=gone,
            )
        except TransitionError as exc:
            held = next((e for e in (current.editions if current else ()) if e.edition_id == entry.edition_id), None)
            if exc.rule == "duplicate" and held is not None and held.manifest_sha256 == sha:
                settled.append(entry.edition_id)
            else:
                refused[entry.edition_id] = exc
            continue
        changed = True
        settled.append(entry.edition_id)
    return (current if changed else None), settled, refused


def settle_journal(writer: InventoryWriter, product_dir: Path, product_id: str, lifecycle: Lifecycle) -> None:
    """Apply the journal rule to the product's journal entries (see the module docstring).

    Every edition that needs recording goes into one Inventory commit.

    Args:
        writer: The catalog's Inventory writer.
        product_dir: The product's directory.
        product_id: The product.
        lifecycle: The lifecycle to record a product's first edition with.

    An entry that cannot be read raises `JournalError`.

    Raises:
        InventoryContentionError: The record lost too many races; the entries
            are kept, and the next working copy records them.
    """
    entries = _read_journal(product_dir)
    if not entries:
        return
    full = writer.full()
    pending: list[tuple[JournalEntry, str, str]] = []
    for entry in entries:
        edition_dir = product_dir / entry.edition_id
        if _named(full, product_id, entry.edition_id) or not (edition_dir / MANIFEST_NAME).is_file():
            _edition.clear_journal(product_dir, entry.edition_id)
            continue
        manifest = _edition.read_manifest(edition_dir)
        pending.append((entry, manifest_sha256(edition_dir), manifest.schema_version))
    if not pending:
        return
    refused: dict[str, TransitionError] = {}

    def change(head_now: FullInventory | None) -> Change:
        leaf = head_now.leaf(product_id) if head_now is not None and product_id in head_now.products() else None
        retired = [t.edition_id for t in head_now.retired if t.product_id == product_id] if head_now else []
        nxt, _, found = record_journalled(
            leaf,
            pending,
            product_id=product_id,
            lifecycle=lifecycle,
            embargo=head_now.embargo if head_now is not None else False,
            retired=retired,
            now=datetime.now(UTC),
        )
        refused.clear()
        refused.update(found)
        return Change(leaves={} if nxt is None else {product_id: nxt})

    if change(full).leaves:
        try:
            writer.commit(change, kind="seal", reason="record a seal its writer did not record (journal repair)")
        except InventoryContentionError as exc:
            msg = f"{exc}; the journalled edition is recorded at the next working_copy()"
            raise InventoryContentionError(msg) from exc
    for entry, _, _ in pending:
        if entry.edition_id in refused:
            log.warning(
                "%s: the journalled edition %s cannot be recorded (%s); it stays on disk unrecorded, and "
                "`statcat doctor` reports it",
                product_id,
                entry.edition_id,
                refused[entry.edition_id],
            )
        _edition.clear_journal(product_dir, entry.edition_id)


@frozen
class _Parts:
    """What `WorkingCopy._start` hands its constructor."""

    store: _Store
    registry: sdmxlib.LocalRegistry
    writer: InventoryWriter
    product_id: str
    previous: str | None
    lifecycle: Lifecycle


def _live_head(writer: InventoryWriter, product_id: str) -> str | None:
    """The product's live edition at this sdmxlib's store schema, or ``None``."""
    full = writer.full()
    if full is None or product_id not in full.products():
        return None
    entry = head(full.leaf(product_id), SCHEMA_VERSION)
    return None if entry is None else entry.edition_id


class WorkingCopy:
    """A product's one unsealed edition, open for writing.

    `Product.working_copy` returns one. Write datasets with `put` and the
    other write methods, and SDMX structures and observations through
    `registry`; read them back through `view`; then `seal` it, or `discard` it.
    Once sealed or discarded, every method on it, and on its views, raises
    `WorkingCopyClosedError`.
    """

    #: The relationship values `add_lineage` accepts (``prov:wasDerivedFrom`` family).
    LINEAGE_RELATIONSHIPS: tuple[str, ...] = _Store.LINEAGE_RELATIONSHIPS
    #: The confidence values `add_lineage` accepts.
    LINEAGE_CONFIDENCE: tuple[str, ...] = _Store.LINEAGE_CONFIDENCE

    _store: _Store
    _registry: sdmxlib.LocalRegistry
    _writer: InventoryWriter
    _product_dir: Path
    _product_id: str
    _edition_id: str
    _previous: str | None
    _lifecycle: Lifecycle
    _closed: str | None
    _rebuild_index: Callable[[], int] | None

    def __init__(self, *args: object, _parts: _Parts | None = None, **kwargs: object) -> None:
        """Refuse construction from outside, with a TypeError: `Product.working_copy` returns working copies."""
        if _parts is None or args or kwargs:
            msg = "start a working copy with Catalog.create(path).product(id).working_copy()"
            raise TypeError(msg)
        self._store = _parts.store
        self._registry = _parts.registry
        self._writer = _parts.writer
        self._product_dir = _parts.registry.edition_dir.parent
        self._product_id = _parts.product_id
        self._edition_id = _parts.registry.edition_dir.name
        self._previous = _parts.previous
        self._lifecycle = _parts.lifecycle
        self._closed = None
        self._rebuild_index = None

    @classmethod
    def _start(
        cls,
        writer: InventoryWriter,
        product_id: str,
        *,
        lifecycle: Lifecycle,
        resume: bool,
        embedding_model: EmbeddingModel | None,
        document_prefix: str,
        query_prefix: str,
    ) -> Self:
        """Start or resume the product's working copy: repair, find the base, build, set up the schema.

        `_build` raises `WorkingCopyExistsError` for a product with an
        unsealed edition when *resume* is false, and `FileNotFoundError` for
        one without when it is true.

        Args:
            writer: The catalog's Inventory writer.
            product_id: The product.
            lifecycle: The product's lifecycle.
            resume: Reopen the unsealed edition in place of starting one.
            embedding_model: The encoder the seal embeds with.
            document_prefix: Prepended to every document before embedding.
            query_prefix: Prepended to every query before embedding.

        Returns:
            The working copy.

        Raises:
            InventoryRuleError: The product is recorded with another lifecycle
                (``rule="lifecycle"``).
        """
        product_dir = writer.root / "products" / product_id
        settle_journal(writer, product_dir, product_id, lifecycle)
        full = writer.full()
        leaf = full.leaf(product_id) if full is not None and product_id in full.products() else None
        if leaf is not None and leaf.lifecycle != lifecycle:
            msg = (
                f"{product_id} is recorded as a {leaf.lifecycle} product, and working_copy() asked for "
                f"{lifecycle}; changing a product's lifecycle is not supported before Phase 4"
            )
            raise InventoryRuleError(msg, rule="lifecycle", product=product_id, edition=None)
        live = head(leaf, SCHEMA_VERSION)
        registry = _build(
            writer, product_dir, product_id, lifecycle, base=None if live is None else live.edition_id, resume=resume
        )
        edition_dir = registry.edition_dir
        opened = False
        try:
            previous = _previous_of(registry)
            if previous is None:
                # A product's first edition: the statcat tables are created here.
                # An edition built on another arrives with them, in the copied
                # metadata.duckdb. Every statement is IF NOT EXISTS, so a resumed
                # first edition is left as it is.
                _catalog_schema.create_schema(registry.connection, registry.catalog_name).close()
            store = _Store._over(
                registry.connection.cursor,
                catalog_name=registry.catalog_name,
                search_dir=edition_dir / "search",
                writable=True,
                registry=registry,
                embedding_model=embedding_model,
                document_prefix=document_prefix,
                query_prefix=query_prefix,
            )
            opened = True
        finally:
            if not opened:
                registry.close()
                if not resume:
                    # The edition this call started holds nothing yet; leave none behind.
                    _edition.discard(edition_dir)
        return cls(
            _parts=_Parts(
                store=store,
                registry=registry,
                writer=writer,
                product_id=product_id,
                previous=previous,
                lifecycle=lifecycle,
            )
        )

    # -- state -----------------------------------------------------------

    def _live(self) -> _Store:
        """The store, or `WorkingCopyClosedError` once the working copy is sealed or discarded."""
        if self._closed is not None:
            raise WorkingCopyClosedError(self._closed)
        return self._store

    def _close(self, why: str) -> None:
        """Close the store and the build registry; every later call raises `WorkingCopyClosedError`."""
        self._closed = why
        self._store._close(lambda: WorkingCopyClosedError(self._closed or why))
        self._registry.close()

    @property
    def product_id(self) -> str:
        """The product this working copy belongs to."""
        return self._product_id

    @property
    def edition_id(self) -> str:
        """The id of the edition it seals into, a UUIDv7."""
        return self._edition_id

    @property
    def previous(self) -> str | None:
        """The edition it was built on, or ``None`` for the product's first."""
        return self._previous

    @property
    def registry(self) -> sdmxlib.LocalRegistry:
        """The writable sdmxlib registry over the edition: SDMX structures and observations.

        ``wc.registry.ingest(source)`` adds structures, and
        ``wc.registry.write_observations(...)`` writes observations into the
        edition's lake.
        """
        self._live()
        return self._registry

    def view(self) -> EditionView:
        """A read-only view of this working copy, which sees its writes as they are made."""
        return EditionView._over(self._live())

    # -- writes ----------------------------------------------------------

    def put(self, obj: Dataset | ConceptScheme | Sequence[Dataset | ConceptScheme]) -> None:
        """Add or update a dataset, a concept scheme, or a sequence of either.

        Args:
            obj: What to write.
        """
        self._live().put(obj)

    def put_concept(self, scheme_uri: str, concept: Concept) -> None:
        """Add or update one concept of a scheme.

        Args:
            scheme_uri: The scheme's URI.
            concept: The concept.
        """
        self._live().put_concept(scheme_uri, concept)

    def add_lineage(
        self,
        source_id: str,
        target_id: str,
        relationship: str,
        *,
        activity_label: str | None = None,
        activity_type: str | None = None,
        agent_id: str | None = None,
        timestamp: datetime | None = None,
        confidence: str = "asserted",
        notes: str | None = None,
    ) -> None:
        """Record a PROV-O-aligned lineage edge between two datasets.

        Args:
            source_id: The source dataset.
            target_id: The target dataset.
            relationship: One of `LINEAGE_RELATIONSHIPS`.
            activity_label: The activity's label.
            activity_type: The activity's type.
            agent_id: The agent responsible.
            timestamp: When.
            confidence: One of `LINEAGE_CONFIDENCE`.
            notes: Free text.
        """
        self._live().add_lineage(
            source_id,
            target_id,
            relationship,
            activity_label=activity_label,
            activity_type=activity_type,
            agent_id=agent_id,
            timestamp=timestamp,
            confidence=confidence,
            notes=notes,
        )

    def add_language_alternates(self, pairs: Mapping[str, Sequence[str]], *, source: str) -> None:
        """Record language tags a source declares as alternates of a primary tag.

        Args:
            pairs: Each primary tag mapped to its alternate tags.
            source: Where the pairs come from.
        """
        self._live().add_language_alternates(pairs, source=source)

    @contextlib.contextmanager
    def bulk_load(self) -> Generator[Self]:
        """Hold DuckDB's write-ahead log until the block ends, then checkpoint once."""
        with self._live().bulk_load():
            yield self

    @contextlib.contextmanager
    def transaction(self) -> Generator[Self]:
        """Wrap the writes in the block in one DuckDB transaction."""
        with self._live().transaction():
            yield self

    def build_index(
        self,
        embedding_model: EmbeddingModel | None = None,
        progress: Callable[[int, int], None] | None = None,
        *,
        include_codes: bool = True,
        embed_codes: bool = False,
        max_codes_per_codelist: int = 500,
        codelist_urns: Sequence[str] | None = None,
        codelist_policies: Sequence[CodelistIndexPolicy] = (),
        include_alt_labels: bool = True,
        carry_hierarchy_fields: bool = True,
        reference_metadata_blends: Sequence[ReferenceMetadataBlend] = (),
        context_annotation_types: Sequence[str] | None = None,
        exclude_annotation_types: Sequence[str] | None = None,
        include_concepts: bool = True,
        embed_concepts: bool = True,
        concept_scheme_urns: Sequence[str] | None = None,
        include_reference_metadata: bool = False,
        embed_reference_metadata: bool = True,
        reference_metadataflow_urns: Sequence[str] | None = None,
        document_prefix: str | None = None,
        query_prefix: str | None = None,
        model_name: str | None = None,
    ) -> int:
        """Build the search index now, so `view` can search before the seal.

        `seal` rebuilds the index from the edition's rows with the arguments
        of the last call here (the defaults when there was none), so the
        sealed index always holds the sealed rows.

        Args:
            embedding_model: The encoder; the working copy's when ``None``.
            progress: Called with ``(current, total)`` while texts are encoded.
            include_codes: Index SDMX code rows.
            embed_codes: Embed code rows too (slow on large catalogs).
            max_codes_per_codelist: Leave out codelists larger than this.
            codelist_urns: Index only these codelists' codes.
            codelist_policies: Per-codelist overrides (`CodelistIndexPolicy`).
            include_alt_labels: Fold ``skos:altLabel`` annotations into code text.
            carry_hierarchy_fields: Record each code's parent and depth.
            reference_metadata_blends: Reference-metadata attributes folded into code text.
            context_annotation_types: Annotation types folded into code text;
                ``statcat:searchContext`` when ``None``.
            exclude_annotation_types: Annotation types that keep a code out;
                ``statcat:searchExclude`` when ``None``.
            include_concepts: Index SDMX concept rows.
            embed_concepts: Embed concept rows.
            concept_scheme_urns: Index only these schemes' concepts.
            include_reference_metadata: Index reference-metadata sections as rows.
            embed_reference_metadata: Embed reference-metadata rows.
            reference_metadataflow_urns: Index only sets of these structures.
            document_prefix: Prepended to every document before embedding.
            query_prefix: Prepended to every query before embedding.
            model_name: The model id recorded in ``embeddings.json``.

        Returns:
            The number of datasets indexed.
        """
        store = self._live()
        extra: dict[str, Sequence[str]] = {}
        if context_annotation_types is not None:
            extra["context_annotation_types"] = context_annotation_types
        if exclude_annotation_types is not None:
            extra["exclude_annotation_types"] = exclude_annotation_types

        def rebuild() -> int:
            return store.build_index(
                embedding_model,
                progress,
                include_codes=include_codes,
                embed_codes=embed_codes,
                max_codes_per_codelist=max_codes_per_codelist,
                codelist_urns=codelist_urns,
                codelist_policies=codelist_policies,
                include_alt_labels=include_alt_labels,
                carry_hierarchy_fields=carry_hierarchy_fields,
                reference_metadata_blends=reference_metadata_blends,
                include_concepts=include_concepts,
                embed_concepts=embed_concepts,
                concept_scheme_urns=concept_scheme_urns,
                include_reference_metadata=include_reference_metadata,
                embed_reference_metadata=embed_reference_metadata,
                reference_metadataflow_urns=reference_metadataflow_urns,
                document_prefix=document_prefix,
                query_prefix=query_prefix,
                model_name=model_name,
                **extra,
            )

        n = rebuild()
        self._rebuild_index = rebuild
        return n

    # -- ending ----------------------------------------------------------

    def close(self) -> None:
        """Close the working copy and leave its edition unsealed, for ``working_copy(resume=True)``.

        A no-op once it is sealed, discarded or closed.
        """
        if self._closed is None:
            self._close(
                f"the working copy {self._edition_id} of {self._product_id} is closed; reopen it with "
                "working_copy(resume=True)"
            )

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *args: object) -> None:
        self.close()

    def discard(self) -> None:
        """Delete the unsealed edition (`sdmxlib.edition.discard`) and close the working copy."""
        self._live()
        edition_dir = self._registry.edition_dir
        self._close(f"the working copy {self._edition_id} of {self._product_id} was discarded")
        _edition.discard(edition_dir)

    def _search(self, store: _Store) -> None:
        """Seal step 3: rebuild the BM25 tables from this edition's rows, and drop the carried ones."""
        if importlib.util.find_spec("lancedb") is None:
            # Without lancedb nothing can rebuild the index, so a carried one
            # would describe the previous edition's rows: it is removed.
            search_dir = store._search_dir
            if search_dir is not None and search_dir.is_dir():
                shutil.rmtree(search_dir)
            return
        carried = store.embedding_metadata
        model = store._embedding_model
        if carried is not None and model is not None and self._rebuild_index is None:
            log.info("%s: sealing re-encodes every text with the working copy's model", self._product_id)
        if self._rebuild_index is not None:
            self._rebuild_index()
        else:
            store.build_index()
        if store._lance is not None:
            store._lance.drop_carried()

    def seal(self, checks: Iterable[Callable[[EditionView], Iterable[Finding]]] = ()) -> Edition:
        """Seal the working copy into an edition and record it in the Inventory.

        The steps: settle the journal; run *checks*; refuse when the base is
        no longer live; rebuild the search index; compute the ``statcat``
        section; close; seal with sdmxlib (journal entry, then manifest);
        record the edition, ``live`` when the catalog's embargo is off; settle
        the journal entry.

        Args:
            checks: Seal checks (`statcatlib.checks.SealCheck`), run against
                `view` before anything is written. Any finding refuses the
                seal and leaves the working copy open.

        Returns:
            The edition.

        Raises:
            SealCheckError: A check found something.
            NonFastForwardError: The edition's base is no longer the live
                edition. Raised before the manifest is written, or after it
                when a rollback lands during the seal; then the sealed edition
                stays on disk unrecorded.
            ManifestError: sdmxlib refused the edition, such as for a coded
                value outside its codelist, before writing the journal entry.
                The working copy is closed; ``working_copy(resume=True)``
                reopens it.
        """
        store = self._live()
        settle_journal(self._writer, self._product_dir, self._product_id, self._lifecycle)
        findings = [finding for check in checks for finding in check(self.view())]
        if findings:
            raise SealCheckError(findings)
        live = _live_head(self._writer, self._product_id)
        if live != self._previous:
            msg = (
                f"{self._product_id}: this working copy is built on {self._previous}, and the live edition is "
                f"{live}. Discard it with discard(), then start one from the live edition with working_copy()."
            )
            raise NonFastForwardError(msg, rule="fast-forward", product=self._product_id, edition=self._edition_id)
        self._search(store)
        section = catalog_section(store)
        edition_dir = self._registry.edition_dir
        self._close(f"the working copy {self._edition_id} of {self._product_id} is sealed")
        try:
            manifest = _edition.seal(
                edition_dir,
                product_id=self._product_id,
                producer=producer(),
                lifecycle=self._lifecycle,
                extensions={STATCAT_SECTION: section},
            )
        except ManifestError as exc:
            self._closed = (
                f"the seal of {self._edition_id} of {self._product_id} was refused ({exc}); reopen it with "
                "working_copy(resume=True)"
            )
            raise
        sha = manifest_sha256(edition_dir)
        self._record(manifest.schema_version, sha)
        return Edition(
            edition_id=manifest.edition_id, product_id=self._product_id, previous=manifest.previous, manifest_sha256=sha
        )

    def _record(self, schema_version: str, sha: str) -> None:
        """Seal step 6: commit the record, then settle the journal entry by the journal rule."""
        product_id, edition_id = self._product_id, self._edition_id

        def change(full: FullInventory | None) -> Change:
            leaf = full.leaf(product_id) if full is not None and product_id in full.products() else None
            retired = {t.edition_id for t in full.retired if t.product_id == product_id} if full else set()
            try:
                nxt = record(
                    leaf,
                    product_id=product_id,
                    kind=_KIND,
                    lifecycle=self._lifecycle,
                    edition_id=edition_id,
                    previous=self._previous,
                    manifest_sha256=sha,
                    schema_version=schema_version,
                    embargo=full.embargo if full is not None else False,
                    now=datetime.now(UTC),
                    retired=retired,
                )
            except TransitionError as exc:
                raise InventoryRuleError.wrapping(exc, product=product_id, edition=edition_id) from exc
            return Change(leaves={product_id: nxt})

        try:
            self._writer.commit(change, kind="seal", reason=f"seal {edition_id}")
        except InventoryRuleError as exc:
            # Refused after the manifest: the edition stays sealed and
            # unrecorded, and its journal entry is cleared so no repair ever
            # records it (the journal rule).
            _edition.clear_journal(self._product_dir, edition_id)
            log.warning(
                "%s: the sealed edition %s was not recorded (%s); it stays on disk unrecorded, and "
                "`statcat doctor` reports it",
                product_id,
                edition_id,
                exc,
            )
            raise
        except InventoryContentionError as exc:
            msg = f"{exc}; the edition {edition_id} is recorded at the next working_copy()"
            raise InventoryContentionError(msg) from exc
        _edition.clear_journal(self._product_dir, edition_id)


def _build(
    writer: InventoryWriter, product_dir: Path, product_id: str, lifecycle: Lifecycle, *, base: str | None, resume: bool
) -> sdmxlib.LocalRegistry:
    """`WorkingCopy._start`'s third step: the build registry of a new edition on *base*, or of the resumed one.

    Args:
        writer: The catalog's Inventory writer.
        product_dir: The product's directory.
        product_id: The product.
        lifecycle: The product's lifecycle, for the journal rule.
        base: The live edition to build on, or ``None``.
        resume: Reopen the unsealed edition in place of starting one.

    Returns:
        The open build registry.

    Raises:
        WorkingCopyExistsError: The product has an unsealed edition and
            *resume* is false.
        FileNotFoundError: *resume* is true and the product has no unsealed
            edition.
    """
    unsealed = [info for info in _edition.list_editions(product_dir) if not info.sealed]
    if unsealed and not resume:
        info = unsealed[0]
        msg = (
            f"{product_id} has an unsealed edition, {info.edition_id}, started "
            f"{info.created_at.isoformat(timespec='seconds')}; a product has one working copy at a time. "
            "Resume it with working_copy(resume=True), or throw it away with working_copy(resume=True).discard()."
        )
        raise WorkingCopyExistsError(msg)
    if resume:
        if not unsealed:
            msg = f"{product_id} has no unsealed edition to resume; call working_copy() to start one"
            raise FileNotFoundError(msg)
        registry = sdmxlib.LocalRegistry.build(product_dir, resume=unsealed[0].edition_id)
        settle_journal(writer, product_dir, product_id, lifecycle)
        return registry
    product_dir.mkdir(parents=True, exist_ok=True)
    registry = None
    try:
        registry = sdmxlib.LocalRegistry.build(product_dir, previous=base)
    finally:
        if registry is None:
            # sdmxlib makes the edition's directory before it opens the
            # store, which can refuse it (another store schema); the
            # directory holds nothing to keep.
            for found in _edition.list_editions(product_dir):
                if not found.sealed:
                    _edition.discard(product_dir / found.edition_id)
    return registry


def _previous_of(registry: sdmxlib.LocalRegistry) -> str | None:
    """The edition a build registry extends, from its ``sdmx._meta``."""
    return meta_value(registry.connection, "previous")
