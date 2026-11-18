"""Build ``tests/fixtures/v3_catalog.tar.xz``: a catalog whose search indexes are format 3.

Run with statcatlib 0.20.0, the last release that writes format 3, from the
repository root:

    uv run --no-project --with "statcatlib[search]==0.20.0" python -I tools/make_v3_fixture.py

The catalog holds two products. ``sealed`` has one live edition, indexed with
a deterministic stub encoder under the model name ``stub-encoder``, so its
rows carry vectors and ``text_sha256``. ``open`` has a live edition and an
unsealed working copy indexed under 0.20. Every table has the format 3
columns: ``text``, and no concept columns. The tests that read it
(``tests/test_index_v3.py``) unpack it into a temporary directory. It is
checked in as an archive: the DuckDB files are mostly empty pages, about 8 MB
unpacked and under 50 kB compressed (``docs/design/label-lookup.md``,
LL-D15).
"""

from __future__ import annotations

import hashlib
import math
import shutil
import tarfile
import tempfile
from pathlib import Path

import statcatlib as sc

ARCHIVE = Path(__file__).resolve().parent.parent / "tests" / "fixtures" / "v3_catalog.tar.xz"
MODEL_NAME = "stub-encoder"


class StubEncoder:
    """Deterministic 8-dimension unit vectors from the SHA-256 of the text."""

    DIM = 8

    def encode(self, text: str | list[str]) -> list[float] | list[list[float]]:
        """Encode one text or a list of texts."""
        if isinstance(text, list):
            return [self._one(t) for t in text]
        return self._one(text)

    def _one(self, text: str) -> list[float]:
        digest = hashlib.sha256(text.encode()).digest()
        v = [b / 255.0 + 0.01 for b in digest[: self.DIM]]
        norm = math.sqrt(sum(x * x for x in v))
        return [x / norm for x in v]


def _dataset(identifier: str, word: str) -> sc.OpenDataset:
    title = sc.InternationalString.of(
        {"en": f"{word} estimates", "fr": f"Estimations {word}", "en-t-fr": f"{word} totals"}
    )
    return sc.OpenDataset(identifier=identifier, title=title, description=title)


def main() -> None:
    """Write the fixture archive, replacing any earlier one."""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp) / "v3_catalog"
        _build(root)
        ARCHIVE.parent.mkdir(parents=True, exist_ok=True)
        with tarfile.open(ARCHIVE, "w:xz") as tar:
            tar.add(root, arcname="v3_catalog")
        shutil.rmtree(root)


def _build(root: Path) -> None:
    model = StubEncoder()
    with sc.Catalog.create(root) as cat:
        wc = cat.product("sealed").working_copy(embedding_model=model)
        wc.put(_dataset("rain", "rainfall"))
        wc.build_index(embedding_model=model, model_name=MODEL_NAME)
        wc.seal()

        wc = cat.product("open").working_copy(embedding_model=model)
        wc.put(_dataset("snow", "snowfall"))
        wc.build_index(embedding_model=model, model_name=MODEL_NAME)
        wc.seal()

        wc = cat.product("open").working_copy(embedding_model=model)
        wc.put(_dataset("hail", "hailfall"))
        wc.build_index(embedding_model=model, model_name=MODEL_NAME)
        wc.close()


if __name__ == "__main__":
    main()
