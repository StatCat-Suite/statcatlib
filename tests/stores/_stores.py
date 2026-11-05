"""Azurite in a container, for the `stores` tier (GH-98).

Azurite runs from an image pinned by digest, the one sdmxlib's tier pins, on a
free local port, for the session. Part 1 of Phase 4 needs one emulator: the
leaf rule gives every store one code path, and an S3 catalog can hold no real
edition in Phase 4.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import http.client
import shutil
import socket
import threading
import time
from collections.abc import Callable, Generator
from contextlib import contextmanager
from datetime import UTC, datetime
from email.utils import format_datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import httpx
import pytest
from attrs import field, frozen
from obstore.store import AzureStore
from sdmxlib.inventory import Root

from statcatlib.store.blob import BlobLocation
from statcatlib.store.inventory import InventoryWriter

#: Azurite's image, at the digest sdmxlib's `tests/stores/_stores.py` pins.
IMAGE = (
    "mcr.microsoft.com/azure-storage/azurite@sha256:830430c1da1a2d537e08f3e6764dd1f5ae00cf0346bcaf625b968ec3f0971fd5"
)

#: Azurite's well-known development account, the same in every install.
ACCOUNT = "devstoreaccount1"
KEY = "Eby8vdM02xNOcqFlqUwJPLlmEtlCDXJ1OUzFT50uSRZ6IFsuFq2UVErCz4I6tq/K1SZFPTOtr/KBHBeksoGMGw=="

#: The container every test writes under, each test at its own prefix.
CONTAINER = "catalogs"


@frozen
class Docker:
    """What one run of the docker CLI returned."""

    returncode: int
    stdout: str
    stderr: str


def _docker(*args: str, check: bool = True) -> Docker:
    """Run the docker CLI, found on PATH, with *args*: the tier's one subprocess."""
    docker = shutil.which("docker")
    if docker is None:
        msg = "no docker CLI on PATH"
        raise FileNotFoundError(msg)

    async def run() -> Docker:
        process = await asyncio.create_subprocess_exec(
            docker, *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
        out, err = await process.communicate()
        return Docker(process.returncode or 0, out.decode(), err.decode())

    result = asyncio.run(run())
    if check and result.returncode != 0:
        msg = f"docker {' '.join(args)} exited {result.returncode}: {result.stderr}"
        raise RuntimeError(msg)
    return result


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
    """A catalog at *prefix* in `CONTAINER`, with Azurite's key, reached through *endpoint* when given."""
    store = AzureStore(
        CONTAINER,
        prefix=prefix,
        account_name=ACCOUNT,
        account_key=KEY,
        endpoint=endpoint or running.endpoint,
        client_options={"allow_http": True},
    )
    return BlobLocation(f"az://{CONTAINER}/{prefix}/", store)


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
