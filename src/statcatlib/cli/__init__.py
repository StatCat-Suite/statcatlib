"""statcatlib command-line interface.

Top-level entry point installed as the ``statcat`` console script. It has
eight commands: `build_index`, which builds the search index of a product's
working copy; `verify_catalog`, which checks a catalog's integrity; `doctor`,
which reports on a catalog for support; `prune`, which retires old editions
and deletes the files no edition needs; `upload`, which copies a sealed
edition to Azure Blob Storage; `advance_current`, which moves ``CURRENT``
forward; `gc`, the GC job over a catalog in Azure Blob Storage; and `release`,
the release job over one. The
``admin`` group (`admin_app`) makes the operational changes: ``editions``,
``release``, ``cancel``, ``rollback`` and ``embargo``, over a catalog directory
or ``az://``, and ``create``, which creates a catalog at an ``az://`` URL.
The CLI is intentionally thin — the work lives
in `statcatlib.WorkingCopy.build_index`, `statcatlib.diagnostics`,
`statcatlib.CatalogAdmin`, `statcatlib.upload_edition`,
`statcatlib.gc.collect_garbage` and `statcatlib.release.ReleaseJob`.

`build_index` requires the ``[search]`` extra (typer + sentence-transformers +
lancedb), and `upload` the ``[blob]`` extra (typer + obstore + azure-identity).
Function-level imports with ``# noqa: PLC0415`` load the ``blob`` extra until
Python 3.15 is the minimum version.
"""

from __future__ import annotations

import contextlib
import enum
import json
import logging
import os
import sys
import time
import warnings
from datetime import datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, NoReturn, override

import typer
from rich.console import Console
from rich.markup import escape
from rich.progress import (
    BarColumn,
    MofNCompleteColumn,
    Progress,
    SpinnerColumn,
    TaskID,
    TaskProgressColumn,
    TextColumn,
    TimeElapsedColumn,
    TimeRemainingColumn,
)

from sdmxlib.edition import ManifestError
from sdmxlib.inventory import InventoryError

import statcatlib as sc
from statcatlib import diagnostics
from statcatlib.access import ARM_SCOPE, AzureReaderListing
from statcatlib.gc import GcReport, GcStoppedError, collect_garbage
from statcatlib.jobs import _EVENT_ATTRIBUTE, JobsConfigError, _emit, _stamp, load_jobs_config
from statcatlib.release import ReleaseJob, ReleaseReport
from statcatlib.store.blob import (
    AzureBlobRest,
    BlobInventoryStorage,
    BlobLocation,
    BlobStoreError,
    TokenIdentity,
    _load_obstore,
    _token_identity,
)
from statcatlib.store.store import ASYMMETRIC_MODEL_PREFIXES, IncompatibleCatalogError
from statcatlib.upload import UploadCheckError, upload_edition

if TYPE_CHECKING:
    from collections.abc import Callable, Generator

    from azure.identity import DefaultAzureCredential
    from sdmxlib.edition import Lifecycle

__all__ = ["admin_app", "app", "main"]


_DEFAULT_MODEL = "intfloat/multilingual-e5-base"

_ACCEPTED_CODELIST_MODES = frozenset({"fts_only"})

#: The `obstore` variables `statcat upload` refuses: every ``azure_`` spelling that object_store 0.14.2's
#: `AzureConfigKey::from_str` accepts for these keys, the crate obstore 0.12.1 bundles. Spec §10.1 allows the
#: publish job no account key or SAS. The credential provider's token wins beside the key, SAS and token variables
#: on obstore 0.12.1, and a later obstore could change that order. An endpoint, the emulator or the Fabric
#: endpoint sends the writes to another host than the digest read. Skipping the signature sends unsigned writes. A
#: customer-provided key encrypts blobs that the digest read sends no key for.
_REFUSED_VARIABLES = (
    "AZURE_STORAGE_ACCOUNT_KEY",
    "AZURE_STORAGE_ACCESS_KEY",
    "AZURE_STORAGE_MASTER_KEY",
    "AZURE_STORAGE_SAS_KEY",
    "AZURE_STORAGE_SAS_TOKEN",
    "AZURE_STORAGE_TOKEN",
    "AZURE_STORAGE_ENDPOINT",
    "AZURE_ENDPOINT",
    "AZURE_STORAGE_USE_EMULATOR",
    "AZURE_USE_FABRIC_ENDPOINT",
    "AZURE_SKIP_SIGNATURE",
    "AZURE_STORAGE_ENCRYPTION_KEY",
)

#: The refused names as `MicrosoftAzureBuilder::from_env` compares them: it reads each variable whose name starts
#: with ``AZURE_`` and parses the name lowercased.
_REFUSED_KEYS = frozenset(name.lower() for name in _REFUSED_VARIABLES)

#: How long one request may take: a 5,000 MiB Put at about 1.4 MB/s.
_UPLOAD_TIMEOUT = "1h"

#: How long one request of `advance-current`, `admin`, `gc` or `release` may take: each reads or writes one small
#: document, or one of an edition's catalog files, or lists one page.
_ADMIN_TIMEOUT = "5m"


def _parse_codelist_policy(raw: str) -> sc.CodelistIndexPolicy:
    """Parse a ``URN[,URN...]=mode`` string into a CodelistIndexPolicy."""
    urns_part, sep, mode = raw.rpartition("=")
    if not sep or not mode.strip() or not urns_part.strip():
        msg = f"Expected 'URN[,URN...]=mode', got {raw!r}"
        raise typer.BadParameter(msg)
    mode = mode.strip()
    urns = tuple(u.strip() for u in urns_part.split(",") if u.strip())
    if not urns:
        msg = f"No URNs in codelist policy {raw!r}"
        raise typer.BadParameter(msg)
    if mode not in _ACCEPTED_CODELIST_MODES:
        accepted = ", ".join(sorted(_ACCEPTED_CODELIST_MODES))
        msg = f"Unknown codelist policy mode {mode!r}; accepted: {accepted}"
        raise typer.BadParameter(msg)
    return sc.CodelistIndexPolicy(urns=urns, mode=mode)


def _parse_reference_metadata_blend(raw: str) -> sc.ReferenceMetadataBlend:
    """Parse an ``ATTRIBUTE_PATH[:max_chars]`` string into a blend.

    ``max_chars`` is optional and defaults to ``ReferenceMetadataBlend``'s
    own default. Examples: ``"FOOTNOTE_TEXT"`` or ``"FOOTNOTE_TEXT:240"``.
    """
    path, sep, max_chars_part = raw.partition(":")
    path = path.strip()
    if not path:
        msg = f"Expected 'ATTRIBUTE_PATH[:max_chars]', got {raw!r}"
        raise typer.BadParameter(msg)
    if not sep:
        return sc.ReferenceMetadataBlend(attribute_path=path)
    try:
        max_chars = int(max_chars_part.strip())
    except ValueError:
        msg = f"max_chars must be an integer in reference-metadata blend {raw!r}"
        raise typer.BadParameter(msg) from None
    if max_chars <= 0:
        msg = f"max_chars must be positive in reference-metadata blend {raw!r}"
        raise typer.BadParameter(msg)
    return sc.ReferenceMetadataBlend(attribute_path=path, max_chars=max_chars)


app = typer.Typer(
    name="statcat",
    help="Operational tools for statcatlib catalogs.",
    no_args_is_help=True,
    add_completion=False,
    # Stated, since the floor is typer 0.15: a printed frame must never show a token.
    pretty_exceptions_show_locals=False,
)

from statcatlib.store.store import EmbeddingModel

console = Console()
log = logging.getLogger("statcatlib.cli")


@app.callback()
def _main() -> None:
    """Force multi-command mode.

    Without an explicit callback, Typer treats a single-command app as
    that command and the command name becomes implicit — so
    ``statcat build-index <catalog> <product>`` would parse ``build-index``
    as the CATALOG argument. This empty callback keeps the subcommand seam
    in place whatever the number of commands.
    """


def _resolve_prefixes(
    model: str,
    document_prefix: str | None,
    query_prefix: str | None,
) -> tuple[str, str]:
    """Decide which document/query prefixes to use.

    Explicit CLI overrides win. When omitted, look up the model in
    `ASYMMETRIC_MODEL_PREFIXES`; an unrecognized model gets empty prefixes
    (correct for symmetric encoders, harmless for ones that don't care).

    The table lives in ``statcatlib.store.store``, so the Python API applies
    the same defaults.
    """
    default_doc, default_q = ASYMMETRIC_MODEL_PREFIXES.get(model, ("", ""))
    doc = default_doc if document_prefix is None else document_prefix
    qry = default_q if query_prefix is None else query_prefix
    return doc, qry


def _hush_encoder_logs() -> None:
    """Silence the noisy encoder/runtime libraries before they emit anything."""
    for noisy in ("httpx", "huggingface_hub", "transformers", "sentence_transformers"):
        logging.getLogger(noisy).setLevel(logging.CRITICAL)
    warnings.filterwarnings("ignore", message=".*unauthenticated.*")
    try:
        import transformers  # noqa: PLC0415

        transformers.logging.set_verbosity_error()
        transformers.logging.disable_progress_bar()
    except ImportError:
        return


def _load_encoder(model_name: str, device: str | None) -> EmbeddingModel:
    """Load a SentenceTransformer encoder, preferring offline files.

    Returns `EmbeddingModel` — the Protocol naming the one method statcatlib
    calls — rather than the concrete class. The import stays inside the
    function so the search extra is only required when indexing.

    Tries ``local_files_only=True`` first so cached models avoid hitting
    the network on every invocation; falls back to a normal load (which
    will download if needed). ``device`` may be ``None`` / ``"auto"`` to
    let SentenceTransformers pick.
    """
    try:
        from sentence_transformers import SentenceTransformer  # noqa: PLC0415
    except ImportError as exc:
        console.print(
            f"[red]Missing dependency:[/] {exc}.\n"
            f"Install the search extra: [bold]uv add 'statcatlib[search]'[/] "
            f"(or [bold]pip install 'statcatlib[search]'[/])"
        )
        raise typer.Exit(code=1) from exc

    resolved_device = None if device in (None, "auto") else device

    try:
        model = SentenceTransformer(model_name, device=resolved_device, local_files_only=True)
    except OSError:
        # Cache miss — fall back to a network-allowed load.
        model = SentenceTransformer(model_name, device=resolved_device)

    # No isinstance narrowing needed: `SentenceTransformer` satisfies
    # `EmbeddingModel` structurally, because the Protocol asks only for a
    # positional `encode` returning `object`. An earlier draft promised
    # `-> Iterable[float]` there and had to narrow here, which basedpyright
    # correctly called unsafe — a runtime_checkable Protocol tests method
    # presence, never signatures, so that check would have passed on anything
    # with an `encode` attribute.
    return model


class _Lifecycle(enum.StrEnum):
    """The ``--lifecycle`` choices: a product's lifecycle, as `Product.working_copy` takes it."""

    REVISABLE = "revisable"
    FINAL = "final"


def _require_catalog(catalog: Path) -> None:
    """Exit with code 1 unless *catalog* holds ``catalog.json``; nothing is created."""
    if not (catalog / "catalog.json").is_file():
        console.print(
            f"[red]Not a statcatlib catalog:[/] {catalog}\n"
            f"Expected to find [bold]catalog.json[/] at the top level. Create one with "
            f"[bold]Catalog.create(path)[/]."
        )
        raise typer.Exit(code=1)


def _resume(catalog: Path, product: str, lifecycle: Lifecycle) -> tuple[sc.Catalog, sc.WorkingCopy]:
    """Open *catalog* for writing and resume *product*'s working copy, or exit with code 1."""
    cat = sc.Catalog.create(catalog, exist_ok=True)
    try:
        return cat, cat.product(product).working_copy(resume=True, lifecycle=lifecycle)
    except sc.InventoryRuleError as exc:
        cat.close()
        console.print(f"[red]Cannot resume the working copy:[/] {exc}. Pass --lifecycle to match.")
        raise typer.Exit(code=1) from None
    except FileNotFoundError:
        cat.close()
        console.print(
            f"[red]No working copy to index:[/] {product} has no unsealed edition in {catalog}. "
            f"Start one with [bold]cat.product({product!r}).working_copy()[/] and ingest into it."
        )
        raise typer.Exit(code=1) from None


@app.command("build-index")
def build_index(
    catalog: Path = typer.Argument(
        ...,
        help="Path to a statcatlib catalog (the directory holding catalog.json).",
        exists=True,
        file_okay=False,
        dir_okay=True,
    ),
    product: str = typer.Argument(
        ...,
        help="The product whose working copy to index.",
    ),
    lifecycle: _Lifecycle = typer.Option(
        _Lifecycle.REVISABLE,
        "--lifecycle",
        help="The product's lifecycle, as it was recorded.",
    ),
    model: str = typer.Option(
        _DEFAULT_MODEL,
        "--model",
        help="SentenceTransformers / Hugging Face model id.",
    ),
    document_prefix: str | None = typer.Option(
        None,
        "--document-prefix",
        help="Prefix to prepend to documents at index time. Auto-derived for known model families.",
    ),
    query_prefix: str | None = typer.Option(
        None,
        "--query-prefix",
        help="Prefix to prepend to queries at search time. Auto-derived for known model families.",
    ),
    batch_size: int = typer.Option(
        256,
        "--batch-size",
        help="Encoder batch size. Sets STATCAT_EMBED_BATCH_SIZE for this run.",
        min=1,
    ),
    device: str = typer.Option(
        "auto",
        "--device",
        help="Encoder device: auto | cpu | cuda | mps.",
    ),
    include_codes: bool = typer.Option(
        True,
        "--include-codes/--no-include-codes",
        help="Index SDMX code rows for BM25 search.",
    ),
    embed_codes: bool = typer.Option(
        False,
        "--embed-codes/--no-embed-codes",
        help="Also compute vector embeddings for code rows (slow; hours on large catalogs).",
    ),
    max_codes_per_codelist: int = typer.Option(
        500,
        "--max-codes-per-codelist",
        help="Skip codelists with more entries than this to avoid index bloat.",
        min=1,
    ),
    codelist_policy: list[str] = typer.Option(
        [],
        "--codelist-policy",
        help=(
            "Per-codelist override 'URN[,URN...]=mode' (repeatable). "
            "Bypasses --max-codes-per-codelist for the listed URNs. "
            "Accepted modes: fts_only."
        ),
    ),
    reference_metadata_blend: list[str] = typer.Option(
        [],
        "--reference-metadata-blend",
        help=(
            "Blend an SDMX MetadataSet attribute into each code's search "
            "text, 'ATTRIBUTE_PATH[:max_chars]' (repeatable). "
            "E.g. 'FOOTNOTE_TEXT:240'. Requires the edition to carry "
            "reference-metadata tables, else build fails fast."
        ),
    ),
    include_reference_metadata: bool = typer.Option(
        False,
        "--include-reference-metadata/--no-include-reference-metadata",
        help=(
            "Index SDMX reference-metadata sections (reference guides, "
            "methodology reports) as their own searchable 'reference_metadata' "
            "rows, chunked one unit per section. Complements "
            "--reference-metadata-blend: the blend boosts a code's recall, "
            "this makes the document itself retrievable."
        ),
    ),
    embed_reference_metadata: bool = typer.Option(
        True,
        "--embed-reference-metadata/--no-embed-reference-metadata",
        help="Compute vector embeddings for reference-metadata rows (default on).",
    ),
    reference_metadataflow: list[str] = typer.Option(
        [],
        "--reference-metadataflow",
        help=(
            "Restrict reference-metadata indexing to sets whose structure is "
            "one of these MSD/Metadataflow URNs (repeatable). Empty = all sets."
        ),
    ),
) -> None:
    """Build the search index of a product's working copy.

    It resumes the product's unsealed edition: one an ingest started and has
    not sealed. The edition's ``search/embeddings.json`` sidecar records the
    chosen model, prefixes, embedding dimensionality, and row counts, so
    that a query-side client can call
    `statcatlib.EditionView.check_index_compatible` to validate its encoder.
    The working copy stays unsealed. `statcatlib.WorkingCopy.seal` rebuilds
    the index with the arguments of the last ``build_index`` call made on
    that working copy object, so a seal in a later process rebuilds with the
    defaults: call ``wc.build_index(...)`` with these options before
    ``wc.seal()`` to seal this index.
    """
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-8s %(message)s",
        datefmt="%H:%M:%S",
    )

    _require_catalog(catalog)

    if embed_codes and not include_codes:
        console.print("[yellow]--embed-codes implies --include-codes; enabling.[/]")
        include_codes = True

    policies = [_parse_codelist_policy(s) for s in codelist_policy]
    blends = [_parse_reference_metadata_blend(s) for s in reference_metadata_blend]

    os.environ["STATCAT_EMBED_BATCH_SIZE"] = str(batch_size)
    _hush_encoder_logs()

    doc_pref, qry_pref = _resolve_prefixes(model, document_prefix, query_prefix)

    console.print(
        f"[dim]catalog:[/] {catalog}  [dim]product:[/] {product}\n"
        f"[dim]model:[/]  {model}  ([dim]device={device}, batch_size={batch_size}[/])\n"
        f"[dim]prefixes:[/] document={doc_pref!r}  query={qry_pref!r}"
    )
    if policies:
        for p in policies:
            console.print(f"[dim]codelist-policy:[/] {','.join(p.urns)} = {p.mode}")
    if blends:
        for b in blends:
            console.print(f"[dim]reference-metadata-blend:[/] {b.attribute_path} (max_chars={b.max_chars})")
    if include_reference_metadata:
        scope = ", ".join(reference_metadataflow) if reference_metadataflow else "all sets"
        console.print(f"[dim]reference-metadata index:[/] on (embed={embed_reference_metadata}, scope={scope})")

    with console.status(f"[dim]Loading encoder {model!r}…[/]"):
        encoder = _load_encoder(model, device)

    with console.status("[dim]Opening the working copy…[/]"):
        cat, store = _resume(catalog, product, "final" if lifecycle is _Lifecycle.FINAL else "revisable")

    progress = Progress(
        SpinnerColumn(),
        TextColumn("[bold cyan]Embedding[/]"),
        BarColumn(bar_width=40),
        MofNCompleteColumn(),
        TaskProgressColumn(),
        TimeElapsedColumn(),
        TimeRemainingColumn(),
        console=console,
    )

    task_id: TaskID | None = None

    def _on_progress(current: int, total: int) -> None:
        nonlocal task_id
        if task_id is None:
            task_id = progress.add_task("embed", total=total)
        else:
            progress.update(task_id, total=total)
        progress.update(task_id, completed=current)

    start = time.monotonic()
    with progress:
        n = store.build_index(
            embedding_model=encoder,
            progress=_on_progress,
            include_codes=include_codes,
            embed_codes=embed_codes,
            max_codes_per_codelist=max_codes_per_codelist,
            codelist_policies=policies,
            reference_metadata_blends=blends,
            include_reference_metadata=include_reference_metadata,
            embed_reference_metadata=embed_reference_metadata,
            reference_metadataflow_urns=reference_metadataflow or None,
            document_prefix=doc_pref,
            query_prefix=qry_pref,
            model_name=model,
        )

    elapsed = time.monotonic() - start
    meta = store.view().embedding_metadata
    dim = meta.embedding_dim if meta is not None else None
    dim_str = f"dim={dim}" if dim is not None else "dim=?"
    edition = store.edition_id
    store.close()
    cat.close()
    console.print(
        f"[bold green]Done[/] — [green]{n:,}[/] datasets indexed ([{dim_str}], {elapsed:.1f}s) "
        f"in the working copy {edition} of {product}"
    )


def _say(line: str, style: str | None = None) -> None:
    """Print *line* as plain text: no markup, no highlighting, never wrapped, so a path stays whole."""
    console.print(escape(line), style=style, soft_wrap=True, highlight=False)


@app.command("verify-catalog")
def verify_catalog(
    path: Path = typer.Argument(..., help="Path to a statcatlib catalog (the directory holding catalog.json)."),
) -> None:
    """Check a catalog's integrity: its Inventory and every edition it records.

    It reads the newest valid Inventory version and checks every leaf, every
    manifest's SHA-256, each entry's routing fields and artefact records, every
    file each manifest lists, the shared ``data/`` files included, each partial against its structure edition, and each
    structure edition's references (`statcatlib.diagnostics.verify_catalog`).
    It writes nothing. It exits 0 when everything checks, 1 on any finding,
    each printed with the file it names, and 2 when the catalog cannot be
    read. Each stale data edition is printed as a ``STALE`` line, with the
    fix; a stale edition is no finding, so it leaves the exit code alone.
    """
    try:
        report = diagnostics.verify_catalog(path)
    except diagnostics.CatalogUnreadableError as exc:
        _say(f"Cannot read the catalog: {exc}", style="red")
        raise typer.Exit(code=2) from None
    if report.seq is None:
        _say(f"{report.root} has no Inventory version yet; there is nothing to verify.")
        return
    for finding in report.findings:
        _say(f"FAIL {finding}", style="red")
    for stale in report.stale:
        _say(f"STALE {stale}", style="yellow")
    summary = (
        f"Inventory version {report.seq}: {len(report.editions)} edition(s) checked, "
        f"{len(report.findings)} problem(s) found."
    )
    if report.stale:
        summary += f" {len(report.stale)} stale data edition item(s); republish each product named."
    if report.findings:
        _say(summary, style="red")
        raise typer.Exit(code=1)
    _say(summary, style="green")


@app.command("doctor")
def doctor(
    path: Path = typer.Argument(..., help="Path to a statcatlib catalog (the directory holding catalog.json)."),
) -> None:
    """Report on a catalog for support. It writes nothing.

    The report covers a synced-folder path, the layout, ``catalog.json``,
    ``CURRENT`` against the newest valid root, root and leaf validation,
    stale data editions, partials with no structure home, unsealed editions with their start
    times, seal journal entries, sealed
    editions no Inventory version names, and files a writer staged in
    ``inventory/`` and left behind (`statcatlib.diagnostics.doctor`). It exits
    0 when it reports no warning and 1 when it reports one.
    """
    report = diagnostics.doctor(path)
    for item in report.items:
        if item.level == "warning":
            _say(f"WARNING {item}", style="yellow")
        else:
            _say(f"ok {item}")
    if report.warnings:
        _say(f"{len(report.warnings)} warning(s).", style="yellow")
        raise typer.Exit(code=1)
    _say("No warnings.", style="green")


@app.command("prune")
def prune(
    path: Path = typer.Argument(..., help="Path to a statcatlib catalog (the directory holding catalog.json)."),
    older_than: int = typer.Option(
        ..., "--older-than", min=1, metavar="DAYS", help="The window, in days: prune what is older."
    ),
    reason: str = typer.Option(..., "--reason", help="Why; the retire version records it as event.reason."),
    apply: bool = typer.Option(
        False,
        "--apply",
        help="Retire and delete. Without it, prune only reports.",
    ),
) -> None:
    """Retire editions superseded or withdrawn before the window, and delete the files no edition needs.

    It runs `statcatlib.CatalogAdmin.prune` with ``older_than`` *DAYS* days:
    it retires those editions in one Inventory version, deletes the
    directories of editions tombstoned for longer than the window, and
    deletes ``data/`` files that no manifest lists and that are older than
    the window. It skips every product with a working copy or a seal journal
    entry, and deletes no Inventory root or leaf. Without ``--apply`` it
    prints what it would do and changes nothing. It exits 1 when the catalog
    cannot be opened or a deletion fails; the next run tries a failed
    deletion again.
    """
    _require_catalog(path)
    try:
        admin = sc.CatalogAdmin.open(path)
        report = admin.prune(older_than=timedelta(days=older_than), reason=reason, dry_run=not apply)
    except (
        IncompatibleCatalogError,
        sc.InventoryRuleError,
        sc.InventoryContentionError,
        sc.InventoryWriteRefusedError,
        ValueError,
    ) as exc:
        _say(f"Cannot prune the catalog: {exc}", style="red")
        raise typer.Exit(code=1) from None
    for product_id, why in report.skipped_products.items():
        _say(f"skip {product_id}: {why}", style="yellow")
    for edition in report.retired:
        _say(f"retire {edition}")
    for rel in (*report.deleted_dirs, *report.deleted_files):
        _say(f"delete {rel}")
    for rel, error in report.failed.items():
        _say(f"FAIL {rel}: {error}", style="red")
    n_retired, n_dirs, n_files = len(report.retired), len(report.deleted_dirs), len(report.deleted_files)
    if report.dry_run:
        _say(
            f"With --apply, prune retires {n_retired} edition(s), deletes {n_dirs} edition directories and "
            f"{n_files} data file(s), and skips {len(report.skipped_products)} product(s). "
            "This dry run changed nothing.",
            style="green",
        )
        return
    done = (
        f"Prune retired {n_retired} edition(s), deleted {n_dirs} edition directories and {n_files} data file(s), "
        f"and skipped {len(report.skipped_products)} product(s)."
    )
    if report.failed:
        _say(f"{done} {len(report.failed)} deletion(s) failed; the next run tries them again.", style="red")
        raise typer.Exit(code=1)
    _say(done, style="green")


def _usage(message: str) -> NoReturn:
    """Print *message*, and exit 2: the usage error's code, as Typer gives."""
    _say(message, style="red")
    raise typer.Exit(code=2) from None


def _release_time(raw: str | None) -> datetime | None:
    """``--release-at`` as an aware time; anything else exits 2."""
    if raw is None:
        return None
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        _usage(f"--release-at {raw!r} is no RFC 3339 time")
    if parsed.tzinfo is None:
        _usage(f"--release-at {raw!r} has no time zone: end it with Z or an offset")
    return parsed


def _catalog_uri(raw: str, *, command: str = "upload") -> str:
    """``AZ_URL`` as ``az://<container>[/<prefix>]/``; anything else exits 2, and ``s3://`` names the reason."""
    if raw.startswith("s3://"):
        _usage(f"{raw}: {command} supports Azure only in Phase 4 (W3), since obstore cannot set or read CRC32C")
    scheme, sep, rest = raw.partition("://")
    segments = rest.removesuffix("/").split("/")
    usable = all(part and part not in {".", ".."} for part in segments) and not set(rest) & set("?#")
    if scheme != "az" or not sep or not usable:
        _usage(f"{raw}: expected az://<container>[/<prefix>]")
    return f"az://{'/'.join(segments)}/"


def _refused_variables() -> list[str]:
    """The environment's names that `obstore` parses as a refused key, spelled as the environment spells them.

    `MicrosoftAzureBuilder::from_env` lowercases ASCII letters only, so a name with any other character never
    matches a key there, and is not refused here.
    """
    return sorted(
        name for name in os.environ if name.startswith("AZURE_") and name.isascii() and name.lower() in _REFUSED_KEYS
    )


def _azure_account(command: str = "upload") -> str:
    """The storage account from the environment; a refused variable, even empty, or no account, exits 2.

    *command* names the command in the messages.
    """
    if found := _refused_variables():
        _usage(
            f"{command} writes with its own credential, signed and with no customer-provided key, "
            f"to the account's URL; unset {', '.join(found)}"
        )
    account = os.environ.get("AZURE_STORAGE_ACCOUNT_NAME")
    if not account:
        _usage("set AZURE_STORAGE_ACCOUNT_NAME to the storage account's name")
    return account


def _azure_location(uri: str, account: str, *, command: str = "upload", timeout: str = _UPLOAD_TIMEOUT) -> BlobLocation:
    """The catalog at *uri*, reached with `DefaultAzureCredential`, each request allowed *timeout*.

    *command* names the command in the messages. A missing ``blob`` extra exits 2, and so does an environment
    value `obstore` cannot parse, such as ``AZURE_DISABLE_TAGGING=junk``: obstore 0.12.1 raises `GenericError`
    from the store build, before any request.
    """
    return _azure_clients(uri, account, command=command, timeout=timeout)[0]


def _azure_clients(
    uri: str, account: str, *, command: str, timeout: str
) -> tuple[BlobLocation, DefaultAzureCredential]:
    """`_azure_location`'s catalog, with the `DefaultAzureCredential` its store asks for tokens.

    ``statcat release`` asks the same credential for its Azure Resource Manager token, so one identity reads the
    store and lists the role assignments.
    """
    try:
        from azure.identity import DefaultAzureCredential  # noqa: PLC0415
        from obstore.auth.azure import AzureCredentialProvider  # noqa: PLC0415
        from obstore.exceptions import GenericError  # noqa: PLC0415
        from obstore.store import from_url  # noqa: PLC0415
    except ImportError:
        _usage(f"{command} needs the blob extra: uv add 'statcatlib[blob]' (or pip install 'statcatlib[blob]')")
    credential = DefaultAzureCredential()
    try:
        store = from_url(
            uri.removesuffix("/"),
            account_name=account,
            credential_provider=TokenIdentity(AzureCredentialProvider(credential)),
            client_options={"timeout": timeout},
        )
    except GenericError as exc:
        _usage(_store_build_message(exc))
    return BlobLocation(uri, store), credential


def _auth_error() -> type[Exception]:
    """azure-core's `ClientAuthenticationError`, which a `DefaultAzureCredential` with no credential raises."""
    from azure.core.exceptions import ClientAuthenticationError  # noqa: PLC0415

    return ClientAuthenticationError


def _store_build_message(exc: Exception) -> str:
    """The exit-2 text for a store build that failed on the environment.

    obstore 0.12.1's message names the value and omits the variable, so the text names
    ``AZURE_DISABLE_TAGGING`` when the environment sets it, in any case after ``AZURE_``.
    """
    reason = str(exc).splitlines()[0] if str(exc) else type(exc).__name__
    message = f"obstore cannot build the store from the environment ({reason})"
    tagging = sorted(
        name
        for name in os.environ
        if name.startswith("AZURE_") and name.isascii() and name.lower() == "azure_disable_tagging"
    )
    if tagging:
        message += f"; {', '.join(tagging)} takes true or false"
    return message


@app.command("upload")
def upload(
    edition_dir: Path = typer.Argument(
        ..., metavar="EDITION_DIR", help="The sealed edition's directory.", exists=True, file_okay=False, dir_okay=True
    ),
    az_url: str = typer.Argument(
        ..., metavar="AZ_URL", help="The catalog root: az://<container>, or az://<container>/<prefix>."
    ),
    release_at: str | None = typer.Option(
        None,
        "--release-at",
        metavar="TIME",
        help="When the edition may be released: RFC 3339 with Z or an offset. Omitted schedules no release.",
    ),
) -> None:
    """Copy a sealed edition to a catalog in Azure Blob Storage, and register it in ``pending/``.

    It runs `statcatlib.upload_edition`, with `DefaultAzureCredential` and the
    account in ``AZURE_STORAGE_ACCOUNT_NAME``. It sends only what the store lacks,
    checks every listed file against the digest the store keeps, and writes
    nothing under ``inventory/``, so the edition waits in ``pending/`` for the
    release job. It exits 0 when the edition is uploaded, or already registered
    with the same bytes; 1 on any finding, each printed as ``FAIL <file>: <detail>``,
    or on a store or credential failure, printed as ``ERROR <message>``; and 2 on
    a usage error, a missing ``blob`` extra, an environment value ``obstore``
    cannot parse, such as ``AZURE_DISABLE_TAGGING=junk``, or any of these
    variables set, even empty: ``AZURE_STORAGE_ACCOUNT_KEY``,
    ``AZURE_STORAGE_ACCESS_KEY``,
    ``AZURE_STORAGE_MASTER_KEY``, ``AZURE_STORAGE_SAS_KEY``,
    ``AZURE_STORAGE_SAS_TOKEN``, ``AZURE_STORAGE_TOKEN``,
    ``AZURE_STORAGE_ENDPOINT``, ``AZURE_ENDPOINT``,
    ``AZURE_STORAGE_USE_EMULATOR``, ``AZURE_USE_FABRIC_ENDPOINT``,
    ``AZURE_SKIP_SIGNATURE`` and ``AZURE_STORAGE_ENCRYPTION_KEY``. Any case
    after ``AZURE_`` counts, such as ``AZURE_storage_endpoint``, since
    ``obstore`` reads that spelling too.
    """
    when = _release_time(release_at)
    uri = _catalog_uri(az_url)
    location = _azure_location(uri, _azure_account())
    auth_error = _auth_error()

    identity = _token_identity(location.store)
    try:
        oid = identity.object_id() if identity is not None else None
        report = upload_edition(edition_dir, location, release_at=when)
    except UploadCheckError as exc:
        for finding in exc.findings:
            _say(f"FAIL {finding.subject}: {finding.detail}", style="red")
        raise typer.Exit(code=1) from None
    except (ManifestError, BlobStoreError, auth_error) as exc:
        _say(f"ERROR {exc}", style="red")
        raise typer.Exit(code=1) from None
    except ImportError as exc:
        _usage(str(exc))
    _say(f"identity {oid or 'unknown: the token has no oid claim'}")
    state = "registered in pending/" if report.registered else "already registered in pending/"
    _say(f"{report.product_id} {report.edition_id}: {len(report.sent)} sent, {len(report.kept)} kept, {state}.")


# -- advance-current and admin ------------------------------------------------

admin_app = typer.Typer(
    name="admin",
    help=(
        "Create a catalog in object storage, and make operational changes to a catalog's Inventory: release, "
        "cancel, roll back, embargo. "
        "On a server, run them under the release job's identity and name yourself in --reason."
    ),
    no_args_is_help=True,
    add_completion=False,
    pretty_exceptions_show_locals=False,
)
app.add_typer(admin_app, name="admin")

#: What an `advance-current` or `admin` command prints as ``ERROR <message>`` and exits 1 for.
_ADMIN_ERRORS: tuple[type[Exception], ...] = (
    FileNotFoundError,
    IncompatibleCatalogError,
    InventoryError,
    sc.InventoryRuleError,
    sc.InventoryConflictError,
    sc.InventoryContentionError,
    ManifestError,
    BlobStoreError,
    ValueError,
)


class _NoCredentialError(Exception):
    """Never raised: a catalog directory takes no credential, so it has no credential error to catch."""


_URL_HELP = "The catalog: a directory holding catalog.json, az://<container>, or az://<container>/<prefix>."


class _Switch(enum.StrEnum):
    """``statcat admin embargo``'s setting."""

    ON = "on"
    OFF = "off"


def _admin_call[T](url: str, command: str, action: Callable[[sc.CatalogAdmin], T]) -> T:
    """Open the catalog at *url* and run *action* on it; a refusal or a store fault prints ``ERROR`` and exits 1.

    *url* with a scheme is `statcat upload`'s ``AZ_URL``, read with upload's environment rules and a five-minute
    request timeout; anything else is a catalog directory. A usage error exits 2.
    """
    auth_error: type[Exception] = _NoCredentialError
    if "://" in url:
        uri = _catalog_uri(url, command=command)
        location = _azure_location(uri, _azure_account(command), command=command, timeout=_ADMIN_TIMEOUT)
        auth_error = _auth_error()
        target: Path | BlobLocation = location
    else:
        target = Path(url)
    try:
        return action(sc.CatalogAdmin.open(target))
    except _ADMIN_ERRORS as exc:
        _say(f"ERROR {exc}", style="red")
        raise typer.Exit(code=1) from None
    except auth_error as exc:
        _say(f"ERROR {exc}", style="red")
        raise typer.Exit(code=1) from None


def _reason(reason: str) -> str:
    """*reason*, or exit 2 when it is blank: every change records one as ``event.reason``."""
    if not reason.strip():
        _usage("--reason is blank; name yourself and why, as the version's event.reason records it")
    return reason


def _wrote(seq: int, what: str) -> None:
    _say(f"Inventory version {seq} written: {what}.", style="green")


@app.command("advance-current")
def advance_current(url: str = typer.Argument(..., metavar="URL", help=_URL_HELP)) -> None:
    """Move ``CURRENT`` forward to the newest Inventory version, never back.

    It runs `statcatlib.CatalogAdmin.advance_current`: it reads the newest root by
    the read protocol and moves ``CURRENT`` to it when ``CURRENT`` names an older
    version or another restore epoch. It writes ``CURRENT`` only. It exits 0 when
    ``CURRENT`` moved or named the newest version already, 1 when the catalog
    cannot be read or ``CURRENT`` cannot be written, printed as ``ERROR <message>``,
    and 2 on a usage error, as ``statcat upload`` does for ``az://``.
    """
    seq = _admin_call(url, "advance-current", lambda admin: admin.advance_current())
    if seq is None:
        _say("CURRENT names the newest Inventory version already; nothing was written.", style="green")
        return
    _say(f"CURRENT moved to Inventory version {seq}.", style="green")


@admin_app.command("create")
def admin_create(
    az_url: str = typer.Argument(
        ..., metavar="AZ_URL", help="The new catalog's root: az://<container>, or az://<container>/<prefix>."
    ),
) -> None:
    """Create a catalog in Azure Blob Storage (`statcatlib.CatalogAdmin.create`).

    It writes ``catalog.json`` with a new ``catalog_id`` and ``restore_epoch``
    0, by one create-only Put, and nothing else: ``statcat upload`` and
    ``statcat release`` then run against the prefix, and the first release run
    that folds a registration writes Inventory version 1 with the embargo off.
    Standard output holds the ``catalog_id`` alone, and standard error the
    human line. Run it once, under the release job's identity, which holds
    write on ``catalog.json``. A local catalog is created by
    `statcatlib.Catalog.create`, so a directory exits 2.

    It exits 0 when the catalog is created; 1 when the prefix holds a
    ``catalog.json`` already, naming its ``catalog_id``, when it holds objects
    under ``inventory/``, ``products/`` or ``pending/`` and no ``catalog.json``,
    or on a store or credential failure, each printed as ``ERROR <message>``
    on standard error with nothing written; and 2 on a usage error, as
    ``statcat upload`` does, or a store other than Azure Blob Storage.
    """
    if "://" not in az_url:
        _usage(
            f"{az_url}: admin create creates a catalog in object storage, at az://<container>[/<prefix>]; "
            "Catalog.create(path) creates a local catalog"
        )
    uri = _catalog_uri(az_url, command="admin create")
    location = _azure_location(uri, _azure_account("admin create"), command="admin create", timeout=_ADMIN_TIMEOUT)
    _require_azure(location, "admin create")
    auth_error = _auth_error()
    try:
        catalog_id = sc.CatalogAdmin.create(location).catalog_id
    except (FileExistsError, IncompatibleCatalogError, InventoryError, BlobStoreError, auth_error) as exc:
        _tell(f"ERROR {exc}", style="red")
        raise typer.Exit(code=1) from None
    _say(catalog_id)
    _tell(f"Created the catalog {uri} with catalog_id {catalog_id}.", style="green")


@admin_app.command("editions")
def admin_editions(
    url: str = typer.Argument(..., metavar="URL", help=_URL_HELP),
    product: str = typer.Argument(..., metavar="PRODUCT", help="The product."),
) -> None:
    """List a product's editions in every state, as the newest Inventory version records them.

    Each line is ``<edition_id> <state> previous=<id or -> schema=<version>
    since=<state_changed_at> release_at=<time or ->``. It writes nothing. It exits
    1 when the catalog records no such product.
    """
    entries = _admin_call(url, "admin editions", lambda admin: admin.editions(product))
    if not entries:
        _say(f"ERROR the catalog records no product {product}", style="red")
        raise typer.Exit(code=1)
    for entry in entries:
        release_at = "-" if entry.release_at is None else entry.release_at.isoformat()
        _say(
            f"{entry.edition_id} {entry.state} previous={entry.previous or '-'} schema={entry.schema_version} "
            f"since={entry.state_changed_at.isoformat()} release_at={release_at}"
        )


@admin_app.command("release")
def admin_release(
    url: str = typer.Argument(..., metavar="URL", help=_URL_HELP),
    product: str = typer.Argument(..., metavar="PRODUCT", help="The product."),
    edition: str = typer.Argument(..., metavar="EDITION", help="The embargoed edition."),
    reason: str = typer.Option(..., "--reason", help="Who and why; the version records it as event.reason."),
) -> None:
    """Make an embargoed edition live now (`statcatlib.CatalogAdmin.release`).

    It exits 1 when the release is refused, printed as ``ERROR <message>``: the
    edition is not embargoed, it is built on an edition that is no longer live, or
    a cross-product rule refuses it.
    """
    why = _reason(reason)
    seq = _admin_call(url, "admin release", lambda admin: admin.release(product, edition, reason=why))
    _wrote(seq, f"released {product}/{edition}")


@admin_app.command("cancel")
def admin_cancel(
    url: str = typer.Argument(..., metavar="URL", help=_URL_HELP),
    product: str = typer.Argument(..., metavar="PRODUCT", help="The product."),
    edition: str = typer.Argument(..., metavar="EDITION", help="The embargoed edition."),
    reason: str = typer.Option(..., "--reason", help="Who and why; the version records it as event.reason."),
) -> None:
    """Withdraw an embargoed edition before its release (`statcatlib.CatalogAdmin.cancel`).

    It exits 1 when the edition is not recorded or not embargoed.
    """
    why = _reason(reason)
    seq = _admin_call(url, "admin cancel", lambda admin: admin.cancel(product, edition, reason=why))
    _wrote(seq, f"withdrew {product}/{edition}")


@admin_app.command("rollback")
def admin_rollback(
    url: str = typer.Argument(..., metavar="URL", help=_URL_HELP),
    product: str = typer.Argument(..., metavar="PRODUCT", help="The product."),
    to: str | None = typer.Option(
        None, "--to", metavar="EDITION", help="The superseded edition to make live; the newest one by default."
    ),
    if_live: str | None = typer.Option(
        None, "--if-live", metavar="EDITION", help="The edition expected live; the live one when the call starts."
    ),
    reason: str = typer.Option(..., "--reason", help="Who and why; the version records it as event.reason."),
) -> None:
    """Withdraw the live edition and make a superseded one live again (`statcatlib.CatalogAdmin.rollback`).

    It exits 1 when the rollback is refused: the live edition is no longer
    ``--if-live``, ``--to`` is not superseded, or a cross-product rule refuses it.
    """
    why = _reason(reason)
    seq = _admin_call(url, "admin rollback", lambda admin: admin.rollback(product, to=to, if_live=if_live, reason=why))
    _wrote(seq, f"rolled back {product}")


@admin_app.command("embargo")
def admin_embargo(
    url: str = typer.Argument(..., metavar="URL", help=_URL_HELP),
    setting: _Switch = typer.Argument(..., metavar="on|off", help="on holds every automatic release."),
    reason: str = typer.Option(..., "--reason", help="Who and why; the version records it as event.reason."),
) -> None:
    """Turn the catalog's embargo on or off (`statcatlib.CatalogAdmin.set_embargo`).

    With the embargo on, a seal records its edition ``embargoed`` and no
    automatic release runs; an explicit ``statcat admin release`` still releases.
    It exits 1 when the catalog already has that setting.
    """
    why = _reason(reason)
    on = setting is _Switch.ON
    seq = _admin_call(url, "admin embargo", lambda admin: admin.set_embargo(on=on, reason=why))
    _wrote(seq, f"embargo {setting.value}")


# -- gc -----------------------------------------------------------------------

#: The human lines of a job command, on standard error; standard output holds its events only.
err_console = Console(stderr=True)

#: The logger `statcatlib.gc` logs its events on.
_GC_LOGGER = "statcatlib.gc"


def _tell(line: str, style: str | None = None) -> None:
    """Print *line* on standard error, plain, as `_say` prints on standard output."""
    err_console.print(escape(line), style=style, soft_wrap=True, highlight=False)


class _EventLines(logging.Handler):
    """Prints each job event (`statcatlib.jobs._emit`) as one compact JSON line on standard output."""

    @override
    def emit(self, record: logging.LogRecord) -> None:
        event = getattr(record, _EVENT_ATTRIBUTE, None)
        if isinstance(event, dict):
            sys.stdout.write(json.dumps(event, separators=(",", ":"), ensure_ascii=False) + "\n")
            sys.stdout.flush()


@contextlib.contextmanager
def _events(name: str) -> Generator[logging.Logger]:
    """While the block runs, print the events of the logger *name* on standard output, and nothing else of it."""
    logger = logging.getLogger(name)
    handler = _EventLines()
    level, propagate = logger.level, logger.propagate
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    logger.propagate = False
    try:
        yield logger
    finally:
        logger.removeHandler(handler)
        logger.setLevel(level)
        logger.propagate = propagate


def _forecast(path: Path) -> frozenset[str]:
    """The paths of the ``gc.planned`` lines in an earlier dry run's output; an unreadable file exits 2."""
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        _usage(f"--forecast {path}: cannot be read: {exc}")
    planned: set[str] = set()
    for number, line in enumerate(text.splitlines(), start=1):
        if not line.strip():
            continue
        try:
            event = json.loads(line)
        except ValueError:
            _usage(f"--forecast {path}: line {number} is no JSON line of statcat gc --dry-run")
        if isinstance(event, dict) and event.get("event") == "gc.planned" and isinstance(event.get("path"), str):
            planned.add(event["path"])
    return frozenset(planned)


def _require_azure(location: BlobLocation, command: str) -> None:
    """Exit 2 unless *location*'s store is an `AzureStore`: the server jobs run on Azure only (Phase 4 W3)."""
    obstore, _ = _load_obstore()
    if not isinstance(location.store, obstore.store.AzureStore):
        _usage(
            f"{command} supports AzureStore only, as upload does (Phase 4 W3); "
            f"this store is a {type(location.store).__name__}"
        )


def _event(name: str, severity: str, at: datetime | None, catalog: str, detail: dict[str, object]) -> dict[str, object]:
    return {
        "event": name,
        "severity": severity,
        "at": None if at is None else _stamp(at),
        "catalog": catalog,
        "detail": detail,
    }


def _gc_summary(report: GcReport) -> None:
    """Print *report*'s findings and its summary on standard error."""
    for product_id, why in report.failed_closed.items():
        _tell(f"FAILED-CLOSED {product_id}: {why}", style="yellow")
    for edition in report.unregistered:
        _tell(f"UNREGISTERED {edition}: rerun its upload, or discard it with --discard", style="yellow")
    for path, side in report.deviations.items():
        _tell(f"DEVIATION {path}: in the {side} only, not deleted", style="yellow")
    for path, error in report.failed.items():
        _tell(f"FAIL {path}: {error}", style="red")
    summary = (
        f"GC planned {len(report.planned)} deletion(s) and deleted {len(report.deleted)}; {len(report.failed)} "
        f"failed, {len(report.failed_closed)} product(s) failed closed, {len(report.unregistered)} unregistered "
        f"edition(s), {len(report.deviations)} deviation(s) from the forecast."
    )
    if report.dry_run:
        summary += " This dry run deleted nothing."
    _tell(summary, style="red" if report.failed else "green")


@app.command("gc")
def gc(
    az_url: str = typer.Argument(
        ..., metavar="AZ_URL", help="The catalog root: az://<container>, or az://<container>/<prefix>."
    ),
    config: Path = typer.Option(..., "--config", metavar="FILE", help="The jobs configuration, a TOML file."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Plan and print the plan; delete nothing."),
    forecast: Path | None = typer.Option(
        None,
        "--forecast",
        metavar="FILE",
        help="An earlier --dry-run's output: delete only the paths both plans hold.",
    ),
    discard: list[str] | None = typer.Option(
        None,
        "--discard",
        metavar="PRODUCT/EDITION",
        help="Delete an edition the Inventory does not record: its registration, manifest and directory. Repeatable.",
    ),
) -> None:
    """Delete what no edition needs from a catalog in Azure Blob Storage: the GC job (Phase 5 D10).

    It runs `statcatlib.gc.collect_garbage` with the jobs configuration's
    ``[retention]``, under `DefaultAzureCredential` and the account in
    ``AZURE_STORAGE_ACCOUNT_NAME``, with `statcat upload`'s environment rules. It
    reads the Inventory back through the GC window and the newest ``keep_roots``
    roots, the registrations and every kept and registered manifest, lists
    ``products/`` and ``pending/``, and deletes retired edition directories,
    unreferenced ``data/`` files older than ``upload_grace``, folded registrations
    older than the window and registrations older than the tombstone window. It
    writes nothing and touches nothing under ``inventory/`` or ``history/``.

    Standard output holds one JSON line per event and nothing else: ``jobs.config``
    with the file's SHA-256, then the deletion log (``gc.planned``, ``gc.deleted``,
    ``gc.delete_failed``, each with ``path``, ``reason`` and ``edition``) and the
    alerts (``gc.failed_closed``, ``gc.unregistered``, ``gc.forecast_deviation``,
    ``step.failed``). Standard error holds the human lines and the summary. It
    exits 0 when the plan ran, with failed-closed products and unregistered editions
    reported; 1 when a deletion failed, the configuration is refused, or the run
    stopped, printed as ``ERROR <message>``; and 2 on a usage error, as ``statcat
    upload``'s, an unreadable ``--forecast``, a ``--discard`` that is no
    ``<product>/<edition>`` or names an edition the Inventory records, or a store
    other than `AzureStore`.
    """
    uri = _catalog_uri(az_url, command="gc")
    planned = None if forecast is None else _forecast(forecast)
    try:
        jobs = load_jobs_config(config)
    except JobsConfigError as exc:
        _tell(f"ERROR {exc}", style="red")
        raise typer.Exit(code=1) from None
    location = _azure_location(uri, _azure_account("gc"), command="gc", timeout=_ADMIN_TIMEOUT)
    _require_azure(location, "gc")
    auth_error = _auth_error()
    storage = BlobInventoryStorage(location)
    at: datetime | None = None
    with _events(_GC_LOGGER) as events:
        try:
            at = storage.now()
            detail: dict[str, object] = {"path": str(config), "sha256": jobs.sha256}
            _emit(events, logging.INFO, _event("jobs.config", "info", at, uri, detail), f"jobs.config {jobs.sha256}")
            report = collect_garbage(
                location, jobs.retention, storage=storage, dry_run=dry_run, forecast=planned, discard=discard or ()
            )
        except (GcStoppedError, InventoryError, BlobStoreError, auth_error) as exc:
            failure = _event("step.failed", "error", at, uri, {"step": "gc", "error": str(exc)})
            _emit(events, logging.ERROR, failure, f"step.failed {exc}")
            _tell(f"ERROR {exc}", style="red")
            raise typer.Exit(code=1) from None
        except ValueError as exc:
            _usage(str(exc))
    _gc_summary(report)
    if report.failed:
        raise typer.Exit(code=1)


# -- release ------------------------------------------------------------------

#: The logger `statcatlib.release` logs its events on.
_RELEASE_LOGGER = "statcatlib.release"


def _release_summary(report: ReleaseReport, names: dict[str, str]) -> None:
    """Print *report*'s refusals, skipped and failed steps and its summary on standard error.

    *names* gives each allowlisted object id its name from the jobs configuration.
    """
    for refusal in report.refused:
        _tell(
            f"REFUSED {refusal.product_id}/{refusal.edition_id} {refusal.rule or '-'}: {refusal.reason}",
            style="yellow",
        )
    for grant in report.access.outsiders:
        _tell(
            f"OUTSIDER {grant.principal_id} ({grant.principal_type}) holds {grant.role} at {grant.scope}; "
            "remove the assignment, or add the principal to [access] allowlist after a review",
            style="red",
        )
    for sentinel in report.access.missing_sentinels:
        _tell(f"UNCHECKED the listing lacks the sentinel assignment {sentinel}", style="red")
    for error in report.access.errors:
        _tell(f"UNCHECKED {error}", style="red")
    for object_id in report.access.unused:
        _tell(f"UNUSED {object_id} ({names.get(object_id, '?')}) is on the allowlist and holds no blob-read role")
    for step, why in report.skipped.items():
        _tell(f"SKIPPED {step}: {why}", style="yellow")
    for step, error in report.failed.items():
        _tell(f"ERROR {step}: {error}", style="red")
    versions = ", ".join(str(seq) for seq in report.versions) or "none"
    verb = "would fold" if report.dry_run else "folded"
    summary = (
        f"Release {verb} {len(report.folded)} registration(s), released {len(report.released)} edition(s) and "
        f"retired {len(report.retired)}; {len(report.refused)} refused, {len(report.skipped)} step(s) skipped, "
        f"{len(report.failed)} failed. Inventory version(s) written: {versions}."
    )
    if report.dry_run:
        summary += " This dry run wrote nothing."
    _tell(summary, style="red" if report.failed else "green")


@app.command("release")
def release(
    az_url: str = typer.Argument(
        ..., metavar="AZ_URL", help="The catalog root: az://<container>, or az://<container>/<prefix>."
    ),
    config: Path = typer.Option(..., "--config", metavar="FILE", help="The jobs configuration, a TOML file."),
    dry_run: bool = typer.Option(
        False, "--dry-run", help="Run the access check and plan the steps; move no CURRENT and write nothing."
    ),
) -> None:
    """Fold ``pending/``, release what is due and retire old entries: the release job (Phase 5 D1).

    It runs `statcatlib.release.ReleaseJob` with the jobs configuration, under
    `DefaultAzureCredential` and the account in ``AZURE_STORAGE_ACCOUNT_NAME``,
    with `statcat upload`'s environment rules. One run moves ``CURRENT`` to the
    newest root; lists who holds a role that reads the catalog's blobs and
    compares them with ``[access] allowlist``; records each passing ``pending/``
    registration as an ``embargoed`` entry; writes the refusal record and, when
    the access check passed and the catalog's embargo is off, makes every due
    entry ``live``; and tombstones the superseded and withdrawn entries older
    than ``retire_after``. Each step writes at most one Inventory version, and a
    run against an unchanged catalog writes nothing. Run it at least twice for
    each release time: before, so pre-materialisation can open the folded
    editions, and just after.

    Standard output holds one JSON line per event and nothing else:
    ``jobs.config`` with the file's SHA-256, then ``current.behind``,
    ``access.mismatch``, ``access.unchecked``, ``fold.refused``,
    ``fold.release_at_past``, ``fold.restored``, ``release.refused``,
    ``release.held``, ``release.unprepared``, ``release.overdue`` and
    ``step.failed``. Standard error holds the human lines (``REFUSED
    <product>/<edition> <rule or ->: <reason>``, ``SKIPPED <step>: <reason>``,
    ``ERROR <message>``) and the summary. It exits 0 when every step ran, with
    refusals, a held release and a skipped release reported; 1 when a step
    failed or the run stopped, each printed as ``ERROR <message>``: a refused
    configuration, one with no ``[access]``, a catalog that has opted in to
    expiry, a clock more than 30 s ahead of the storage clock, or a store or
    credential failure; and 2 on a usage error, as ``statcat upload``'s, or a
    store other than `AzureStore`.
    """
    uri = _catalog_uri(az_url, command="release")
    try:
        jobs = load_jobs_config(config)
    except JobsConfigError as exc:
        _tell(f"ERROR {exc}", style="red")
        raise typer.Exit(code=1) from None
    access = jobs.access
    if access is None:
        _tell(f"ERROR {config}: [access] is missing; the release job requires it for the access precondition", "red")
        raise typer.Exit(code=1)
    location, credential = _azure_clients(uri, _azure_account("release"), command="release", timeout=_ADMIN_TIMEOUT)
    _require_azure(location, "release")
    auth_error = _auth_error()
    storage = BlobInventoryStorage(location)
    at: datetime | None = None
    with _events(_RELEASE_LOGGER) as events, AzureBlobRest(location) as rest:
        try:
            listing = AzureReaderListing(
                access.storage_account,
                access.container,
                arm_token=lambda: credential.get_token(ARM_SCOPE).token,
                scopes=access.scopes,
            )
            job = ReleaseJob(location, jobs, rest=rest, listing=listing, storage=storage)
        except ValueError as exc:
            _tell(f"ERROR {config}: {exc}", style="red")
            raise typer.Exit(code=1) from None
        try:
            at = storage.now()
            detail: dict[str, object] = {"path": str(config), "sha256": jobs.sha256}
            _emit(events, logging.INFO, _event("jobs.config", "info", at, uri, detail), f"jobs.config {jobs.sha256}")
            report = job.run(dry_run=dry_run)
        except (FileNotFoundError, InventoryError, BlobStoreError, auth_error) as exc:
            failure = _event("step.failed", "error", at, uri, {"step": "start", "error": str(exc)})
            _emit(events, logging.ERROR, failure, f"step.failed {exc}")
            _tell(f"ERROR {exc}", style="red")
            raise typer.Exit(code=1) from None
    _release_summary(report, {principal.object_id: principal.name for principal in access.allowlist})
    if report.failed:
        raise typer.Exit(code=1)


def main() -> None:
    """Console-script entry point for ``statcat``."""
    app()


if __name__ == "__main__":
    sys.exit(app() or 0)
