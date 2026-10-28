# src

## Overview

`src/` uses the src layout and holds one package, `statcatlib`. The
`pyproject.toml` build packages it from here, and `just lint`, `just typecheck`
and the `tools/` gate scripts read it as `src/` or `src/statcatlib/`.

## Subdirectories & Boundaries

- `statcatlib`: [`./statcatlib/EGM.md`](./statcatlib/EGM.md) — the library:
  public API re-exports, bundles, query filters, traversal expressions, URN
  builders and synthetic test bundles, with the `model`, `store`, `sources` and
  `cli` subpackages.
