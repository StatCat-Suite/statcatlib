"""statcatlib command-line interface.

Top-level entry point installed as the ``statcat`` console script. It has
five commands: `build_index`, which builds the search index of a product's
working copy; `verify_catalog`, which checks a catalog's integrity; `doctor`,
which reports on a catalog for support; `prune`, which retires old editions
and deletes the files no edition needs; and `upload`, which copies a sealed
edition to Azure Blob Storage. The CLI is intentionally thin — the work lives
in `statcatlib.WorkingCopy.build_index`, `statcatlib.diagnostics`,
`statcatlib.CatalogAdmin.prune` and `statcatlib.upload_edition`.

`build_index` requires the ``[search]`` extra (typer + sentence-transformers +
lancedb), and `upload` the ``[blob]`` extra (typer + obstore + azure-identity).
Function-level imports with ``# noqa: PLC0415`` load the ``blob`` extra until
Python 3.15 is the minimum version.
"""

from __future__ import annotations

import enum
import logging
import os
import sys
import time
import warnings
from datetime import datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, NoReturn

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

import statcatlib as sc
from statcatlib import diagnostics
from statcatlib.store.blob import BlobLocation, BlobStoreError, TokenIdentity, _token_identity
from statcatlib.store.store import ASYMMETRIC_MODEL_PREFIXES, IncompatibleCatalogError
from statcatlib.upload import UploadCheckError, upload_edition

if TYPE_CHECKING:
    from sdmxlib.edition import Lifecycle

__all__ = ["app", "main"]


_DEFAULT_MODEL = "intfloat/multilingual-e5-base"

_ACCEPTED_CODELIST_MODES = frozenset({"fts_only"})

#: The `obstore` variables `statcat upload` refuses, as `_store/_azure.pyi` spells them. Keys and SAS break spec
#: §10.1. A token writes under another identity than the one the upload records. An endpoint sends the writes to
#: another host than the digest read.
_REFUSED_VARIABLES = (
    "AZURE_STORAGE_ACCOUNT_KEY",
    "AZURE_STORAGE_ACCESS_KEY",
    "AZURE_STORAGE_MASTER_KEY",
    "AZURE_STORAGE_SAS_KEY",
    "AZURE_STORAGE_SAS_TOKEN",
    "AZURE_STORAGE_TOKEN",
    "AZURE_STORAGE_ENDPOINT",
    "AZURE_ENDPOINT",
)

#: How long one request may take: a 5,000 MiB Put at about 1.4 MB/s.
_UPLOAD_TIMEOUT = "1h"


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


def _catalog_uri(raw: str) -> str:
    """``AZ_URL`` as ``az://<container>[/<prefix>]/``; anything else exits 2, and ``s3://`` names the reason."""
    if raw.startswith("s3://"):
        _usage(f"{raw}: upload supports Azure only in Phase 4 (W3), since obstore cannot set or read CRC32C")
    scheme, sep, rest = raw.partition("://")
    segments = rest.removesuffix("/").split("/")
    usable = all(part and part not in {".", ".."} for part in segments) and not set(rest) & set("?#")
    if scheme != "az" or not sep or not usable:
        _usage(f"{raw}: expected az://<container>[/<prefix>]")
    return f"az://{'/'.join(segments)}/"


def _azure_account() -> str:
    """The storage account from the environment; a refused variable, even empty, or no account, exits 2."""
    if found := [name for name in _REFUSED_VARIABLES if name in os.environ]:
        _usage(f"upload acts under one identity at the account's URL; unset {', '.join(found)}")
    account = os.environ.get("AZURE_STORAGE_ACCOUNT_NAME")
    if not account:
        _usage("set AZURE_STORAGE_ACCOUNT_NAME to the storage account's name")
    return account


def _azure_location(uri: str, account: str) -> BlobLocation:
    """The catalog at *uri*, reached with `DefaultAzureCredential`; a missing ``blob`` extra exits 2."""
    try:
        from azure.identity import DefaultAzureCredential  # noqa: PLC0415
        from obstore.auth.azure import AzureCredentialProvider  # noqa: PLC0415
        from obstore.store import from_url  # noqa: PLC0415
    except ImportError:
        _usage("upload needs the blob extra: uv add 'statcatlib[blob]' (or pip install 'statcatlib[blob]')")
    store = from_url(
        uri.removesuffix("/"),
        account_name=account,
        credential_provider=TokenIdentity(AzureCredentialProvider(DefaultAzureCredential())),
        client_options={"timeout": _UPLOAD_TIMEOUT},
    )
    return BlobLocation(uri, store)


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
    a usage error, a missing ``blob`` extra, or any of these variables set, even
    empty: ``AZURE_STORAGE_ACCOUNT_KEY``, ``AZURE_STORAGE_ACCESS_KEY``,
    ``AZURE_STORAGE_MASTER_KEY``, ``AZURE_STORAGE_SAS_KEY``,
    ``AZURE_STORAGE_SAS_TOKEN``, ``AZURE_STORAGE_TOKEN``,
    ``AZURE_STORAGE_ENDPOINT`` and ``AZURE_ENDPOINT``.
    """
    when = _release_time(release_at)
    uri = _catalog_uri(az_url)
    location = _azure_location(uri, _azure_account())
    from azure.core.exceptions import ClientAuthenticationError  # noqa: PLC0415

    identity = _token_identity(location.store)
    try:
        oid = identity.object_id() if identity is not None else None
        report = upload_edition(edition_dir, location, release_at=when)
    except UploadCheckError as exc:
        for finding in exc.findings:
            _say(f"FAIL {finding.subject}: {finding.detail}", style="red")
        raise typer.Exit(code=1) from None
    except (ManifestError, BlobStoreError, ClientAuthenticationError) as exc:
        _say(f"ERROR {exc}", style="red")
        raise typer.Exit(code=1) from None
    except ImportError as exc:
        _usage(str(exc))
    _say(f"identity {oid or 'unknown: the token has no oid claim'}")
    state = "registered in pending/" if report.registered else "already registered in pending/"
    _say(f"{report.product_id} {report.edition_id}: {len(report.sent)} sent, {len(report.kept)} kept, {state}.")


def main() -> None:
    """Console-script entry point for ``statcat``."""
    app()


if __name__ == "__main__":
    sys.exit(app() or 0)
