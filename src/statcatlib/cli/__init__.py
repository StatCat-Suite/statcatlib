"""statcatlib command-line interface.

Top-level entry point installed as the ``statcatlib`` console script. Today's
single command is `build_index`, which rebuilds a bundle's
``search/`` LanceDB index. The CLI is intentionally thin — all real work
lives in `statcatlib.Catalog.build_index`.

Requires the ``[search]`` extra (typer + sentence-transformers + lancedb).
"""

from __future__ import annotations

import logging
import os
import sys
import time
import warnings
from pathlib import Path

import typer
from rich.console import Console
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

import statcatlib as sc
from statcatlib.store.store import ASYMMETRIC_MODEL_PREFIXES

__all__ = ["app", "main"]


_DEFAULT_MODEL = "intfloat/multilingual-e5-base"

_ACCEPTED_CODELIST_MODES = frozenset({"fts_only"})


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
    help="Operational tools for statcatlib Catalog bundles.",
    no_args_is_help=True,
    add_completion=False,
)
bundle_app = typer.Typer(
    name="bundle",
    help="Manage statcatlib bundles (inspect and rebind data paths, emit ATTACH commands).",
    no_args_is_help=True,
    add_completion=False,
)
app.add_typer(bundle_app, name="bundle")

from statcatlib.store.store import EmbeddingModel

console = Console()
log = logging.getLogger("statcatlib.cli")


def _require_bundle(bundle: Path) -> Path:
    """Validate that ``bundle`` looks like a statcatlib Catalog bundle."""
    if not (bundle / "meta.duckdb").is_file():
        console.print(
            f"[red]Not a statcatlib Catalog bundle:[/] {bundle}\n"
            f"Expected to find [bold]meta.duckdb[/] at the top level."
        )
        raise typer.Exit(code=1)
    if not (bundle / "lake.ducklake").is_file():
        console.print(
            f"[red]Not a statcatlib Catalog bundle:[/] {bundle}\n"
            f"Expected to find [bold]lake.ducklake[/] at the top level."
        )
        raise typer.Exit(code=1)
    return bundle


@bundle_app.command("rebind")
def bundle_rebind(
    bundle: Path = typer.Argument(
        ...,
        help="Path to a statcatlib Catalog bundle (the folder containing meta.duckdb).",
        exists=True,
        file_okay=False,
        dir_okay=True,
    ),
    to: str | None = typer.Option(
        None,
        "--to",
        help=(
            "Where the observation parquet lives. A local directory or a remote URI "
            "(abfss://, s3://, gs://). Defaults to <bundle>/parquet/."
        ),
    ),
    scratch_dir: Path | None = typer.Option(
        None,
        "--scratch-dir",
        help=(
            "Fallback directory when the lake file is on a read-only mount. "
            "The lake file is byte-copied there and rewritten in place."
        ),
    ),
    force: bool = typer.Option(
        False,
        "--force",
        help="Permit rewriting a remote data_path to a local one, discarding the only record of where the data lives.",
    ),
) -> None:
    """Point a bundle at where its observation parquet actually lives.

    A deliberate, once-per-deploy act — the init-container step for a
    bundle synced from object storage, whose stamp nobody set at publish
    time. Opening a bundle does not do this; ``statcat bundle explain``
    shows what an open would decide without changing anything.

    Idempotent and read-first: a bundle already pointing at the target
    completes with no writes, no lock contention, and exits 0.
    """
    from statcatlib.bundle import BundleDataPathError, normalize_lake_data_path  # noqa: PLC0415

    bundle = _require_bundle(bundle.resolve())
    target = to if to is not None else f"{bundle / 'parquet'}/"

    try:
        result = normalize_lake_data_path(
            bundle / "lake.ducklake",
            target,
            scratch_dir=scratch_dir,
            force=force,
        )
    except BundleDataPathError as exc:
        console.print(f"[red]refused[/] — {exc}")
        raise typer.Exit(code=1) from exc

    if result == bundle / "lake.ducklake":
        console.print(f"[green]ok[/] — data_path = [bold]{target}[/]")
    else:
        console.print(f"[yellow]copied[/] read-only lake to [bold]{result}[/] with data_path = [bold]{target}[/]")


@bundle_app.command("explain")
def bundle_explain(
    bundle: Path = typer.Argument(
        ...,
        help="Path to a statcatlib Catalog bundle (the folder containing meta.duckdb).",
        exists=True,
        file_okay=False,
        dir_okay=True,
    ),
) -> None:
    """Report where a bundle says its data is, and what an open would do.

    Read-only — takes no write lock and changes nothing. Run it in an
    init container before serving: a stale data_path does not fail at
    ATTACH, it fails on the first query that reads observations, so this
    is what turns a latent runtime failure into a startup one.

    Exits 1 when the layout is ambiguous, so it composes with ``&&``.
    """
    from statcatlib.bundle import inspect_bundle  # noqa: PLC0415

    bundle = _require_bundle(bundle.resolve())
    info = inspect_bundle(bundle)

    console.print(f"bundle      [bold]{info.bundle}[/]")
    console.print(f"data_path   [bold]{info.stamp}[/]")
    console.print(f"remote      {info.is_remote}")
    console.print(f"resolves    {'not checked (remote)' if info.stamp_resolves is None else info.stamp_resolves}")
    console.print(f"parquet/    {info.colocated_exists}")
    console.print(f"data files  {info.data_files}")
    attach = "honour the embedded data_path" if info.target is None else info.target
    console.print(f"would open  {attach}")
    console.print(f"would write {info.will_write}")

    ambiguous = info.target is None and info.will_write is False and info.verdict.startswith("ambiguous")
    colour = "red" if ambiguous else "green"
    console.print(f"[{colour}]{info.verdict}[/]")
    if ambiguous:
        raise typer.Exit(code=1)


@bundle_app.command("attach-cmd")
def bundle_attach_cmd(
    bundle: Path = typer.Argument(
        ...,
        help="Path to a statcatlib Catalog bundle (the folder containing meta.duckdb).",
        exists=True,
        file_okay=False,
        dir_okay=True,
    ),
    data_path: str | None = typer.Option(
        None,
        "--data-path",
        help="Override DATA_PATH in the printed ATTACH (defaults to <bundle>/parquet/).",
    ),
    read_only: bool = typer.Option(
        False,
        "--read-only",
        help="Include READ_ONLY in the ATTACH options.",
    ),
) -> None:
    """Print the ``ATTACH`` statement for ad-hoc ``duckdb`` CLI use."""
    bundle = _require_bundle(bundle.resolve())
    target = data_path if data_path is not None else f"{bundle / 'parquet'}/"
    options = f"DATA_PATH '{target}'"
    if read_only:
        options += ", READ_ONLY"
    # Plain stdout — rich.Console wraps lines, but this is meant to be
    # piped into the duckdb CLI or copy-pasted verbatim.
    typer.echo(f"ATTACH 'ducklake:{bundle / 'lake.ducklake'}' AS lake ({options});")


@app.callback()
def _main() -> None:
    """Force multi-command mode.

    Without an explicit callback, Typer treats a single-command app as
    that command and the command name becomes implicit — so
    ``statcat build-index <bundle>`` would parse ``build-index`` as the
    BUNDLE argument. This empty callback keeps the subcommand seam in
    place even while ``build-index`` is the only command.
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

    The table used to live here, which left the Python API — the interface
    #81 was reported against — with no such default at all.
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
            f"Install the search extra: [bold]uv pip install 'statcatlib[search]'[/]"
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


@app.command("build-index")
def build_index(
    bundle: Path = typer.Argument(
        ...,
        help="Path to a statcatlib Catalog bundle (the folder containing meta.duckdb).",
        exists=True,
        file_okay=False,
        dir_okay=True,
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
            "E.g. 'FOOTNOTE_TEXT:240'. Requires the bundle to carry "
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
    """Build or rebuild the LanceDB vector index for a Catalog bundle.

    The bundle's ``search/embeddings.json`` sidecar is updated with the
    chosen model, prefixes, embedding dimensionality, and row counts so
    that a query-side client can call
    `statcatlib.Catalog.check_index_compatible` to validate its encoder.
    """
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-8s %(message)s",
        datefmt="%H:%M:%S",
    )

    if not (bundle / "meta.duckdb").is_file():
        console.print(
            f"[red]Not a statcatlib Catalog bundle:[/] {bundle}\n"
            f"Expected to find [bold]meta.duckdb[/] at the top level."
        )
        raise typer.Exit(code=1)

    if embed_codes and not include_codes:
        console.print("[yellow]--embed-codes implies --include-codes; enabling.[/]")
        include_codes = True

    policies = [_parse_codelist_policy(s) for s in codelist_policy]
    blends = [_parse_reference_metadata_blend(s) for s in reference_metadata_blend]

    os.environ["STATCAT_EMBED_BATCH_SIZE"] = str(batch_size)
    _hush_encoder_logs()

    doc_pref, qry_pref = _resolve_prefixes(model, document_prefix, query_prefix)

    console.print(
        f"[dim]bundle:[/] {bundle}\n"
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

    with console.status("[dim]Opening catalog…[/]"):
        store = sc.Catalog(str(bundle)).open()

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
    meta = store.embedding_metadata
    dim = meta.embedding_dim if meta is not None else None
    dim_str = f"dim={dim}" if dim is not None else "dim=?"
    console.print(
        f"[bold green]Done[/] — [green]{n:,}[/] datasets indexed ([{dim_str}], {elapsed:.1f}s) → {bundle / 'search'}"
    )


def main() -> None:
    """Console-script entry point for ``statcatlib``."""
    app()


if __name__ == "__main__":
    sys.exit(app() or 0)
