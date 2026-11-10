"""Azurite in a container, for the `stores` tier (GH-98), and what the tier's tests record with.

Azurite runs from an image pinned by digest, the one sdmxlib's tier pins, on a
free local port, for the session. Phase 4 needs one emulator: the leaf rule
gives every store one code path, and an S3 catalog can hold no real edition in
Phase 4.

`RecordingOps` records every store call and refuses the Inventory's paths.
`ReadingOps` records a reader's calls and fails any write. `KeyedRest` is
`AzureBlobRest` with Azurite's account key, the one path no deployment uses.
`record_uploaded` places an uploaded edition in a blob Inventory, since Phase 4
has no fold.

Every store and DuckDB secret here names Azurite's loopback address and its
well-known key, so no test can reach a real account. `location` refuses any other
host, and nothing here builds a credential.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import http.client
import shutil
import socket
import subprocess
import threading
import time
from collections.abc import Callable, Generator, Sequence
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from email.utils import format_datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import TYPE_CHECKING, Any, override
from unittest import mock
from urllib.parse import urlsplit

import attrs
import httpx
import obstore
import pytest
import sdmxlib as sl
from attrs import field, frozen
from obstore.store import AzureStore
from sdmxlib.edition import MANIFEST_NAME, Lifecycle, read_manifest
from sdmxlib.inventory import CATALOG_NAME, CURRENT_NAME, Root, record, routing_fields, sha256_hex

from statcatlib.store import blob as blob_module
from statcatlib.store.blob import AzureBlobRest, BlobLocation, _ObjectOps, pull_edition
from statcatlib.store.inventory import InventoryWriter
from statcatlib.store.working_copy import Edition, edition_kind, fold_lifecycle, recorded_routing
from statcatlib.upload import _upload

if TYPE_CHECKING:
    from obstore import ObjectMeta
    from obstore.store import ObjectStore

#: Azurite's image, at the digest sdmxlib's `tests/stores/_stores.py` pins.
IMAGE = (
    "mcr.microsoft.com/azure-storage/azurite@sha256:830430c1da1a2d537e08f3e6764dd1f5ae00cf0346bcaf625b968ec3f0971fd5"
)

#: Azurite's well-known development account, the same in every install.
ACCOUNT = "devstoreaccount1"
KEY = "Eby8vdM02xNOcqFlqUwJPLlmEtlCDXJ1OUzFT50uSRZ6IFsuFq2UVErCz4I6tq/K1SZFPTOtr/KBHBeksoGMGw=="

#: The container every test writes under, each test at its own prefix.
CONTAINER = "catalogs"


def _docker(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    """Run the docker CLI, found on PATH, with *args*: the tier's one subprocess."""
    docker = shutil.which("docker")
    if docker is None:
        msg = "no docker CLI on PATH"
        raise FileNotFoundError(msg)
    # The docker CLI from shutil.which; arguments are the module's pinned image and docker's own output.
    return subprocess.run([docker, *args], capture_output=True, text=True, check=check)  # noqa: S603


def runtime_missing() -> str | None:
    """Why no container can run here, or None when one can."""
    if shutil.which("docker") is None:
        return "no docker CLI on PATH; install Docker, or colima and the docker CLI"
    if _docker("info", check=False).returncode != 0:
        return "docker is installed and its daemon is not running; start it, for example with `colima start`"
    return None


@frozen
class Running:
    """Azurite, started for the session."""

    container: str
    host_port: int

    @property
    def base(self) -> str:
        return f"http://127.0.0.1:{self.host_port}"

    @property
    def endpoint(self) -> str:
        return f"{self.base}/{ACCOUNT}"


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def start() -> Running:
    """Run Azurite on a free local port, wait until it answers HTTP, and create `CONTAINER`."""
    port = _free_port()
    # The image's own command serves on every interface inside the container;
    # the port is published on 127.0.0.1 alone.
    container = _docker("run", "-d", "--rm", "-p", f"127.0.0.1:{port}:10000", IMAGE)
    running = Running(container.stdout.strip(), port)
    deadline = time.monotonic() + 120
    while True:
        try:
            httpx.get(running.base, timeout=2)
        except httpx.TransportError:
            if time.monotonic() > deadline:
                stop(running)
                raise
            time.sleep(0.5)
        else:
            break
    _create_container(running)
    return running


def stop(running: Running) -> None:
    """Remove the emulator's container."""
    _docker("rm", "-f", running.container, check=False)


def _create_container(running: Running) -> None:
    """Create `CONTAINER` with Azure's SharedKey signature, written out, since `obstore` cannot create one."""
    headers = {"x-ms-date": format_datetime(datetime.now(UTC), usegmt=True), "x-ms-version": "2021-08-06"}
    canonical = "".join(f"{k}:{v}\n" for k, v in sorted(headers.items()))
    resource = f"/{ACCOUNT}/{ACCOUNT}/{CONTAINER}\nrestype:container"
    to_sign = "PUT\n" + "\n" * 11 + canonical + resource
    signature = base64.b64encode(hmac.new(base64.b64decode(KEY), to_sign.encode(), hashlib.sha256).digest()).decode()
    response = httpx.put(
        f"{running.endpoint}/{CONTAINER}",
        params={"restype": "container"},
        headers={**headers, "Authorization": f"SharedKey {ACCOUNT}:{signature}"},
    )
    response.raise_for_status()


def location(running: Running, prefix: str, *, endpoint: str | None = None) -> BlobLocation:
    """A catalog at *prefix* in `CONTAINER`, with Azurite's key, reached through *endpoint* when given.

    Any endpoint off the loopback address raises, so no test store can reach a real account.
    """
    reached = endpoint or running.endpoint
    if urlsplit(reached).hostname != "127.0.0.1":
        msg = f"the stores tier reaches Azurite on 127.0.0.1 only, and was given {reached}"
        raise ValueError(msg)
    store = AzureStore(
        CONTAINER,
        prefix=prefix,
        account_name=ACCOUNT,
        account_key=KEY,
        endpoint=reached,
        client_options={"allow_http": True},
    )
    return BlobLocation(f"az://{CONTAINER}/{prefix}/", store)


def secret(running: Running) -> sl.StoreSecret:
    """The DuckDB secret reading Azurite, the only store DuckDB is given in this tier."""
    connection = (
        f"DefaultEndpointsProtocol=http;AccountName={ACCOUNT};AccountKey={KEY};BlobEndpoint={running.endpoint};"
    )
    return sl.StoreSecret("azure", options={"connection_string": connection})


@frozen
class Recorded:
    """One request a `RecordingProxy` forwarded."""

    method: str
    path: str
    headers: dict[str, str]
    size: int


@frozen
class RecordingProxy:
    """What `recording_proxy` yields: the endpoint to use, and the requests sent to it."""

    endpoint: str
    requests: list[Recorded] = field(factory=list)

    def puts(self, name: str) -> list[Recorded]:
        """The PUTs to any path holding *name*, block uploads included."""
        return [r for r in self.requests if r.method == "PUT" and name in r.path]


@contextmanager
def recording_proxy(running: Running) -> Generator[RecordingProxy]:
    """An HTTP proxy in front of Azurite, showing the headers and request counts `_ObjectOps` cannot."""
    recorded: list[Recorded] = []

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def __getattr__(self, name: str) -> Callable[[], None]:
            # BaseHTTPRequestHandler dispatches each method to `do_<METHOD>`.
            if name.startswith("do_"):
                return self._forward
            raise AttributeError(name)

        def _forward(self) -> None:
            length = int(self.headers.get("content-length") or 0)
            body = self.rfile.read(length) if length else None
            headers = {k.lower(): v for k, v in self.headers.items()}
            recorded.append(Recorded(self.command, self.path, headers, length))
            upstream = http.client.HTTPConnection("127.0.0.1", running.host_port, timeout=120)
            try:
                upstream.request(self.command, self.path, body=body, headers=headers)
                answer = upstream.getresponse()
                data = answer.read()
                self.send_response_only(answer.status, answer.reason)
                for key, value in answer.getheaders():
                    if key.lower() not in {"transfer-encoding", "connection", "content-length"}:
                        self.send_header(key, value)
                length_sent = answer.getheader("content-length") if self.command == "HEAD" else str(len(data))
                self.send_header("Content-Length", length_sent or "0")
                self.end_headers()
                if self.command != "HEAD":
                    self.wfile.write(data)
            finally:
                upstream.close()

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield RecordingProxy(f"http://127.0.0.1:{server.server_address[1]}/{ACCOUNT}", recorded)
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def race_once(monkeypatch: pytest.MonkeyPatch, writer: InventoryWriter, competing: Callable[[], object]) -> None:
    """Run *competing* inside *writer*'s first `try_commit`, before it writes, so it wins the race."""
    original = writer.try_commit
    calls: list[int] = []

    def raced(*args: Any, **kwargs: Any) -> Root:
        if not calls:
            calls.append(1)
            competing()
        return original(*args, **kwargs)

    monkeypatch.setattr(writer, "try_commit", raced)


class RecordingOps(_ObjectOps):
    """Records every store call, and refuses any call naming ``catalog.json``, ``inventory/`` or ``CURRENT``.

    A publish job has no access to those paths (spec §10.1), so a read there
    fails the test as a write does.
    """

    def __init__(self, store: ObjectStore) -> None:
        super().__init__(store)
        self.calls: list[tuple[str, str]] = []
        self.writes: list[str] = []
        self.before_put: Callable[[str], None] = lambda _path: None

    def _note(self, call: str, path: str) -> None:
        if path in {CATALOG_NAME, CURRENT_NAME} or path.startswith("inventory/"):
            pytest.fail(f"an upload called {call} on {path}")
        self.calls.append((call, path))

    def called(self, call: str) -> list[str]:
        """The paths of each call named *call*."""
        return [path for name, path in self.calls if name == call]

    @override
    def read(self, path: str) -> tuple[bytes, str | None] | None:
        self._note("read", path)
        return super().read(path)

    @override
    def head(self, path: str) -> ObjectMeta | None:
        self._note("head", path)
        return super().head(path)

    @override
    def put(self, path: str, data: bytes, **kwargs) -> str | None:
        self._note("put", path)
        self.before_put(path)
        written = super().put(path, data, **kwargs)
        self.writes.append(path)
        return written


def recording_location(running: Running, prefix: str, *, endpoint: str | None = None) -> BlobLocation:
    """`location`, with a `RecordingOps` as its route to the store."""
    plain = location(running, prefix, endpoint=endpoint)
    return BlobLocation(plain.uri, plain.store, ops=RecordingOps(plain.store))


class ReadingOps(_ObjectOps):
    """Records every read of a store, and fails the test on any write: a reader writes nothing to its catalog."""

    def __init__(self, store: ObjectStore) -> None:
        super().__init__(store)
        self.reads: list[str] = []

    def under(self, prefix: str) -> list[str]:
        """The paths read that start with *prefix*."""
        return [path for path in self.reads if path.startswith(prefix)]

    @override
    def read(self, path: str) -> tuple[bytes, str | None] | None:
        self.reads.append(path)
        return super().read(path)

    @override
    def head(self, path: str) -> ObjectMeta | None:
        self.reads.append(path)
        return super().head(path)

    @override
    def put(self, path: str, data: bytes, **kwargs) -> str | None:
        pytest.fail(f"a reader wrote {path}")

    @override
    def delete(self, path: str) -> None:
        pytest.fail(f"a reader deleted {path}")


def reader(written: BlobLocation, running: Running, cache_dir: Path) -> BlobLocation:
    """The location a reader opens: the catalog of *written*, with Azurite's secret, *cache_dir* and a `ReadingOps`."""
    store = written.store
    return attrs.evolve(written, secrets=(secret(running),), cache_dir=cache_dir, ops=ReadingOps(store))


def pull_at_the_rename(
    running: Running,
    prefix: str,
    ids: Sequence[str],
    raw: bytes,
    dest: Path,
    barrier: Any,
) -> None:
    """Pull an edition in a process of its own, waiting at *barrier* until every other pull is ready to rename."""
    original = blob_module._publish

    def publish(*args: Any) -> None:
        barrier.wait(120)
        original(*args)

    product_id, edition_id = ids
    with mock.patch.object(blob_module, "_publish", publish):
        pull_edition(location(running, prefix), product_id, edition_id, raw, dest)


class KeyedRest(AzureBlobRest):
    """`AzureBlobRest` over Azurite's account key: a ``HEAD`` to a signed GET URL (F8)."""

    @override
    def _send(self, path: str) -> httpx.Response:
        return self._client.head(obstore.sign(self._store, "GET", path, timedelta(hours=1)))


def record_uploaded(
    writer: InventoryWriter,
    location: BlobLocation,
    edition_dir: Path,
    *,
    lifecycle: Lifecycle | None = None,
    release_at: datetime | None = None,
) -> Edition:
    """Upload an edition and record it in the blob Inventory, with no cross-product rule, as the fold will (D8).

    With no *lifecycle*, the record takes the one `fold_lifecycle` resolves, as the fold does (Phase 5 D3).
    """
    with KeyedRest(location) as rest:
        _upload(edition_dir, location, release_at=release_at, rest=rest)
    manifest = read_manifest(edition_dir)
    product = manifest.product_id
    kind = edition_kind(manifest)
    full = writer.full()
    head = None if full is None else full.root
    sha = sha256_hex((edition_dir / MANIFEST_NAME).read_bytes())
    dataflows, pull_bytes, artefacts = recorded_routing(routing_fields(manifest))
    held = full.leaf(product) if full is not None and product in full.products() else None
    leaf = record(
        held,
        product_id=product,
        now=writer.storage.now(),
        kind=kind,
        previous=manifest.previous,
        schema_version=manifest.schema_version,
        edition_id=manifest.edition_id,
        lifecycle=fold_lifecycle(held, manifest) if lifecycle is None else lifecycle,
        embargo=full.embargo if full is not None else False,
        release_at=release_at,
        retired={t.edition_id for t in full.retired if t.product_id == product} if full is not None else (),
        manifest_sha256=sha,
        dataflows=dataflows,
        pull_bytes=pull_bytes,
        artefacts=artefacts,
    )
    writer.try_commit(head, {product: leaf}, kind="seal", reason="record_uploaded")
    return Edition(manifest.edition_id, product, manifest.previous, sha)
