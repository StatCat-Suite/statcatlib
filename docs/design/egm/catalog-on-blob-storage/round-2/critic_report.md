# Design critic report, round 2: catalog on blob storage, revision 3

**Verdict:** the design can be implemented, and D1, D6 and D7 break no embargo or data guarantee that I could find. There are no Blockers. There is 1 Major: Part 3 has a caller for the `decode_manifest` export that W12 defers to Phase 5. There are 7 Minors.

**What I read:**
- `/Users/gabrielgellner/dev/statcatlib/docs/design/catalog-on-blob-storage.md`, all 1150 lines, and the round 1 report.
- The persona and the Target Standard.
- statcatlib: `store/inventory.py`, `store/cross_product.py`, `catalog.py`, `store/store.py:2786-2960`, `bundle.py:143-339`, `tests/test_inventory_conformance.py`, `store/EGM.md`.
- sdmxlib working tree: `edition/_check.py`, `edition/_remote.py`, `edition/_copy.py`, `edition/EGM.md`, `inventory/_reader.py`, parts of `inventory/conformance.py` and `inventory/_rules.py`, `storage/_manifest.py`, `local/registry.py:1455-1650`, `local/engine.py` (by grep), spec §4.6, §4.7, §10.1 and §11.2.
- The `obstore` stubs, for `AzureStore` only.

**Limits:**
- Nothing was run. Every finding comes from reading the code.
- I could not confirm that sdmxlib's working tree `src/` equals `11f18e3f`. It holds no `references=` parameter, which fits #371 being unmerged.
- The spec text on `02d3af2a` already contains the fold's reference check (§11.2 line 969).

## Major

### DES-1. Part 3 is a first caller of `decode_manifest`, which W12 and S6 defer to Phase 5

**Sites:** design:161, 287, 580, 1076, 840, and handoff rows 3 and 7. Code: `sdmxlib/edition/__init__.py:91,149` exports `read_manifest` only. `storage/_manifest.py:442-455` shows `read_manifest` takes a directory. `storage/_manifest.py:471` holds `decode_manifest(raw, path)`. `sdmxlib/storage/__init__.py` has no manifest export. `catalog.py:942-947` shows `Product.editions` needs `manifest.previous`, and `LiveEdition` (`_reader.py:87-115`) has no `previous`.

**Failure:**
- D6:580 says `Product.editions` reads the manifest "by a GET and a SHA-256 check, with no pull", and row 1076 tests that it pulls nothing. It has bytes and no directory on an uncached edition, and no public API decodes bytes.
- The Part 3 implementer must choose between importing the private `sdmxlib.storage._manifest`, writing a temporary directory for `read_manifest`, or calling `msgspec.json.decode(raw, type=Manifest)`. The last skips `decode_manifest`'s format-version refusal (`_manifest.py:479-498`).
- `pull_edition(..., manifest_bytes, dest)` has the same need, but can write the bytes into its staged directory first, so it has a workaround.
- W12's phrase "with its first caller" is contradicted by the code, which is why I report it despite W12 being settled. Handoff row 3 says the #371 release holds "S16 alone".

**Recommendation:** the owner picks one.
1. Add the `decode_manifest` export to the #371 release, which Parts 2 and 3 already wait on. S6 then closes early and adds no new gate, because the release order is unchanged.
2. Keep W12, and state in D6 that `Product.editions` writes the bytes to a temporary directory and calls `read_manifest`.

## Minor

### DES-2. The NotImplementedError works, but its site is unnamed and one reading breaks the design

**Sites:** design:367-368, 810, 1011; `inventory.py:310,596-598`; `cross_product.py:219-235,395-399`.

**It works.** `_refuse` runs before `try_commit` (`inventory.py:598,600`), so nothing is written first. `cross_product.incoming` already defines an incoming entry: an entry in `live` or `embargoed` whose state differs from the head's. `try_commit`, which conformance, the corpus and `record_uploaded` use, runs no rule.

**The gap.** The design never names `incoming` or the site.
- An implementer who raises when `planned.leaves` is non-empty refuses the no-incoming commits that the lost-race row relies on (design:956).
- An implementer who installs a raising `Artefacts` stub in `__init__` gets the right behaviour, and `refusals()` is covered too. But `_planned` reads `full.leaves()` first (`cross_product.py:398,214`), so the refusal arrives after up to 7,000 GETs, about 70 s at 10 ms each.

**Recommendation:** one sentence. The default blob `artefacts` raises, and `_refuse` checks `cross_product.incoming` first.

### DES-3. "Bytes equal to its own mean this writer won" also covers a second writer with identical bytes

**Sites:** design:357-359, 322; `_rules.py:557`; D4's one-second `Date`; `inventory.py:402-408`; `store/EGM.md:366`.

**Failure:**
- Two writers on one base, with the same identity, `reason`, `kind` and leaves, and the same `Date` second, compute identical `created_at` values (`max(now, base + 1 ms)`) and identical root bytes. Both return success.
- The catalog state is correct, so no data is lost. But "exactly one creates a root" no longer holds on blob.
- `write_catalog_json(catalog_id=x)` run twice returns success, against its `FileExistsError` contract, because it goes through the same `create`.

**Conformance is not affected.** The stale-commit, lost-race and rollback checks use differing leaves (`conformance.py:222-225,285-287,574-576`).

**Recommendation:** state that twin roots count as one commit, and have `write_catalog_json` read first or refuse equal bytes on blob.

### DES-4. "Transport error" has no definition at the `_ObjectOps` layer

**Sites:** design:356-361, 753, 803-806.

**Failure:**
- `_ObjectOps` wraps every `BaseError` other than the handled cases into `BlobStoreError`. `create` therefore cannot tell a timeout from a definite 403.
- A writer lacking write permission reads the name as absent, retries, and gets `InventoryCommitUnknownError` saying the outcome is unknown. In fact nothing was written.
- The following are also unstated: a transport fault on the reconciling read itself, and an `AlreadyExistsError` with other bytes on the retry. The retry case should be a conflict, not "unknown", but the words "if that fails too" cover it.

**Recommendation:** name which `obstore` classes count as unknown, and have the retry re-enter the same read-and-compare.

### DES-5. The #371 rule leaves these undefined, and its "later full check" has no Phase 4 caller

**Sites:** design:591-593, 287, 1074; `_check.py:247-270,293,305-350,362-368,755-759`; spec:283; `edition/EGM.md:35-37`. #371's text was not available, so it may answer some of these.

**Trace of the rule as stated:**

| Starting state | Call | Result |
|---|---|---|
| No stamp, no bindings | `references=False` | `receive` without references, writes a deferred stamp |
| Deferred stamp | `references=False` | `open` |
| Full stamp | `references=False` | `open` |
| Deferred stamp, all references internal | Full check | `receive`, clean, stamp upgraded |
| Deferred stamp, external references | Full check | Always a finding, because `check_on_open` has no `structures=` (W12) and `_references` with `None` reports each one. The stamp stays deferred (`_check.py:348`), so the next deferring call passes |
| Bindings | Default | Full stamp, then `open` |

**The rule has no caller in statcatlib.** `view` derives the flag from a manifest fixed by its SHA, so one edition always gets one value. Upload calls `check_edition` directly and writes no stamp. So "reruns `receive`" never fires from statcatlib. Row 1074 tests only deferred to deferred. Any future caller using the default on a valid structure edition is refused, so B1 is closed only for callers that pass the flag.

**Undefined:**
1. How a stamp records deferral (a new key, or `"references"` missing from `checked`), and how a pre-#371 stamp reads. A `checked` test gives the right answer for both.
2. Whether `references=False` on a manifest with bindings is honoured or refused. If honoured, nothing later checks a data edition, since the fold owns no-bindings editions only.
3. `references=False` together with `structures=`.
4. `references=False` at `publish`, and at `open`. The module's convention is that an argument a profile does not use raises `ValueError`, so `check_on_open` must not forward it at `_check.py:346`.
5. An older sdmxlib reading a deferred stamp treats it as full, since `_stamped` compares only the digest.

**The spec also disagrees.** Spec §4.7 line 283 says a matching stamp runs `open`, and the §4.6 table has no deferral. Handoff row 1 lists the spec changes and neither of these is among them.

**Recommendation:** add §4.6 and §4.7 to a handoff row, and put the five answers in #371 with a row for each.

### DES-6. `pull_edition` states no fsync, while the stamp is durable

**Sites:** design:570-573; `edition/_copy.py:92-97`; `_check.py:371-379`; `edition/EGM.md:129`.

**Failure:**
- After a node crash or power loss (not a process crash), the rename and the durable stamp may survive while file bytes do not. Later views run `open`, which checks sizes only (`_check.py:399-405`), so a torn file can pass.
- `copy_edition` fsyncs for exactly this reason (GH-344).
- I rate this Minor because Phase 4 has no pods, but Phase 6 inherits the code.
- A process crash mid-pull is clean, as the D6 traces below show.

**Recommendation:** fsync each file and directory before the rename, using `sdmxlib.storage` helpers if they are public, or the same steps.

### DES-7. D4's endpoint and D6's `remote=` are not specified for the production form

**Sites:** design:204, 467, 574, 578; `_azure.pyi:155` (`endpoint` is an optional override); `registry.py:1540-1542`.

**Failure:**
- CUJ 2's `AzureStore("catalogs", prefix="stc", account_name="acct", ...)` holds no endpoint, going by the stub types. An implementer must derive `https://<account>.blob.core.windows.net` for `now()` and for `AzureBlobRest`, including the fabric option and any sovereign cloud. `S3Store` needs a similar derivation.
- D6 does not say that `remote=` is the edition directory, `<uri>products/<p>/<e>/`, which is what `LocalRegistry.edition` needs.

**Recommendation:** add one line for each.

### DES-8. M7 is not fully closed: the `record` argument table omits `leaf` and `now`

**Sites:** design:779-791; `_rules.py:178-190`.

**Failure:**
- `record`'s first argument is the product's current leaf, and `now` is required ("the storage server's time").
- The sketch passes `head`, a `Root`, which holds no leaf. The helper must also read `full().leaf(product)`, and D1 exposes no `writer.now()`.
- This only affects a test helper, and the rest of M7 is closed (`lifecycle`, `embargo`, `retired`).

**Recommendation:** add both rows, with `product_id`.

## Noted without a separate finding

D3:443 says the refusal comes "before anything is written", but `_actor()` is evaluated at `inventory.py:540`, after the leaf loop at `:532-535`. The implementer must move it up. Orphan leaves are harmless.

## Areas traced clean

**D1:**
- `commit`, `full`, `has_version` and `FullInventory(read)`: a malformed `CURRENT` falls back (`_reader.py:183-192`), then `advance_current` replaces it.
- `current_is_older` equals `inventory.py:388`.
- A failed `CURRENT` update after a root is safe, because readers probe (`_reader.py:174,194-203`). With `CURRENT` absent, the fallback reaches `catalog.json` (`:218`).
- `_stored_root`, `_base_leaves` and `check_writable` all map onto `read`.
- `InventoryCommitUnknownError` passes through `commit`'s `except` at `:610`.
- A timed-out create landing late loses or matches on create.
- Conformance drives `try_commit` only (`test_inventory_conformance.py:133`), so `BlobBackend` needs no `artefacts`.
- The delegates satisfy the patches at `tests/test_seal_crash.py:86,91`.

**D6:**
- **Crash mid-pull:** no name exists, so it pulls again.
- **Crash after the rename, before the stamp:** cache hit, SHA passes, no stamp, `receive` runs.
- **Cached manifest mismatch:** raises before any check. After a delete and re-pull, a new digest misses the stamp and runs `receive`.
- **Directory deleted with the stamp left and the same digest:** `open` runs, and pull has already hashed the edition's files.
- **Rollback to a cached edition:** same SHA, stamp valid.
- **Tiers:** data files are always tier `remote` (`_write.py:157-158`). Edition files default to `required` or `lazy`, so `_files` at `receive` finds nothing missing. A seal `tiers=` override could change this (`_write.py:159-161`), but the design pulls per the manifest, and I did not check whether statcatlib uses overrides.
- **`_Store._over`:** no writer dependency, takes `writable=False`.
- **`_Opening` and `Catalog`:** every `InventoryWriter(self._root)` site (`catalog.py:618,636,645,665`) is covered by D6.
- **Engine holder counts:** a check before a view detaches and reattaches safely (`engine.py:133-141,166-174`).
- **`refresh`:** clean, though `has_version` adds a read beyond the "3 GETs" in design:617. That count mismatch is cosmetic.

**Embargo and data:**
- `pending/` is written last, after all three verify steps. Any finding raises first, and create is put-if-absent.
- The verify order covers `data/` (`_check.py:202-205`), edition files (`verify_remote`, every tier) and the manifest (a GET).
- Upload reads nothing under `catalog.json`, `inventory/` or `CURRENT`.
- `view` only ever GETs an edition taken from `LiveInventory`, so an embargoed edition is never pulled. Leaves stay in memory (`_reader.py:268`).
- Step 5's `check_edition` reads local data for the lake and the bindings, as D7 says.

## Round 1 closure

| Item | Status |
|---|---|
| B1 | Closed for Upload, `view` and `record_uploaded`, given #371 (rows 1037, 1074). See DES-5 for default callers |
| M3 | Closed |
| M5 as S12 | Closed: S12, handoff row 7, risk row |
| M6 | Closed by W10 |
| M7 | Not fully closed: DES-8 |
| M11 | Closed: D1 builds a new reader, `inventory.py:360` |
| M12 | Closed |
| M24 | Closed: D4's table weighs the bearer `HEAD`, and the risk row records the rest |

## Per-area verdict

| Area | Verdict |
|---|---|
| D1 | Sound. Three Minors (DES-2, DES-3, DES-4) |
| D6 | Sound as traced. One Major (DES-1), three Minors (DES-5, DES-6, DES-7) |
| Embargo and data guarantees across D1, D6, D7 | No break found |
| Round 1 closure | All closed but M7, which has a small remainder (DES-8) |

## Could not verify

- #371's actual text, so DES-5 reviews the rule only as design:591-593 states it.
- That sdmxlib's working tree `src/` equals `11f18e3f`.
- That `AzureStore.credential_provider` returns the same Python object, so `isinstance(..., TokenIdentity)` holds. The stub shows only the property.
- What `store.config` actually holds at runtime for `from_url` or a bare `account_name`. DES-7 rests on the stub's types.
- Real Azure behaviour, and DuckDB `az://` reads against Azurite for Part 3's tests. Both rest on sdmxlib's existing tier, and I read only part of `tests/stores/_stores.py`.
- Whether any statcatlib seal passes `tiers=` overrides that would place an edition file in the `remote` tier.
- Commit hashes, which come from the brief, since I had no git.
