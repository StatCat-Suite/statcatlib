# Comprehension review: catalog on blob storage

**Target:** `docs/design/catalog-on-blob-storage.md`, revision 2, at `e7eba47`.
**Standard:** the EGM feature-spec standard.
**Reviewer:** `egm-comprehension`.

**Verdict as reported:** no Blockers, 13 Major, 11 Minor. All findings are against the
design. The Alternatives Rule and Risks and Limits sections are met. The Core Principle
(understandable by a competent engineer unfamiliar with the codebase) is not met yet,
mainly because of CMP-1 to CMP-3. Line numbers are design lines unless a file is named.

## The reviewer's understanding of the central claims

1. `InventoryWriter` gains an injected storage protocol, so one set of commit logic runs on local disk and blob. Blob commits use create for roots, a conditional forward-only write for `CURRENT`, and unconditional Puts for changed leaves.
2. `obstore` cannot do digests, `Date` or leases. A small `httpx` client reads `Content-MD5` now and carries stage 2's lease work later. An anonymous `HEAD` supplies the clock.
3. `statcat upload` copies a sealed edition and registers it in `pending/`, on Azure only. It never touches the Inventory.
4. `Catalog.open(BlobLocation)` serves live editions, pulling catalog files to a local cache and reading data through DuckDB.
5. `correct` builds a new version from an old one under an `if_seq` check. It keeps later tombstones, tombstones what it drops, and keeps later embargoed entries (pending O4). `reinstate` turns a tombstone into `superseded`.
6. A documents-only 7,000-leaf measurement lets the owner decide on the "baked summary".
7. Until Phase 5, no production path records an upload. Blob reads and blob corrections run only over editions placed by a test helper. Local `correct` and `reinstate` appear usable by real operators today, but the design never says so. The reviewer inferred it from L195 and L966, and GH-98 (`98-live.md:83`) names Phase 9 as their first real use.

A reader of only the Overview and CUJs can state what Phase 4 delivers only partly (CMP-1).

## Major

### CMP-1. The Overview reads as an upload-and-serve pipeline

L37-50, L159-165, L185-186, L195-196, L198-201, L99-100, L827.

The Overview lists upload, then `Catalog.open`, as if they form a pipeline. CUJ 1 ends at "`pending/...` appears, and nothing else changes", with no statement that the edition stays unreadable. The Overview has no caveat. The caveat appears only at L185 and L195, and "a catalog in this state" at L185 is vague. The outcome sits in Non-Goals (L99-100). CUJ 4 has no caveat and omits Phase 9. Local `correct` and `reinstate` are never named as the part usable now. L827 keeps `record_uploaded` out of the operator guide, while L924 and L944 document upload and remote reads there, so a guide reader could run upload and wait for an edition that never appears.

**Misreading:** a reader would plan a production upload-and-serve rollout after Phase 4.

**Fix:** add a short paragraph to the Overview on what a real user gets after Phase 4: local `correct` and `reinstate` work for real; upload only fills the inbox, and nothing reads it until Phase 5; blob reads and corrections run only over `record_uploaded` catalogs, in tests and drills; first real use is Phases 5, 6 and 9. Add one sentence to CUJ 1 and one to CUJ 4, and require the operator guide to state the upload limit.

### CMP-2. No glossary and no explanation of the Inventory

The reader cannot follow L37-61 or D7 without the spec open. "Inventory" is never defined, though it is the central noun (L40). The same holds for these terms, with first use:

- leaf and carried-forward leaf (L21-23); root (L67); `CURRENT` (L69);
- seal and sealed edition (L44); working copy (L110);
- tombstone (L78); retire, retired, retention window (L194, L661; the length is never given);
- embargo as an entry state against the embargo setting (L79, L748);
- fold, release job, `release_due` (L99); publish job (L159);
- stage 1 and stage 2 (L58, L87), used in the Overview and never defined;
- 2.x (L355, L364), never tied to stage 2;
- required and lazy tiers (L148, L557); check profiles publish, receive and open (L148, L560);
- receive stamp (L548); probe (L317, L330); the cross-product rules (L73);
- baked summary (L94, never explained; `sharded-bundle-catalog-plan.md:191,255` is the only clue); readiness budget (L1024);
- GC (L111), expunge (L619), drill (L196), corpus (L106, L485), journal repair and `settle_journal` (L693), `doctor` (L984), opt-in (L102), "live-only export" (L609), the `Any` ratchet (L381), `EmbargoReader` (L101), DFS (L835).

CUJ is expanded at L157, so it is fine. SeaweedFS and Azurite are labelled emulators only in passing (L210-213), and SeaweedFS is never identified as S3-compatible.

**Fix:** add a glossary of about 25 one-line entries with spec section links after the Inputs, plus a layout block quoting spec §4.1 (`sharded-bundle-catalog.md:107-126`). Define stage 1 and stage 2 there.

### CMP-3. One word carries several meanings

- root: Inventory root (L67), manifest field `root: data` (L783), directory (L300, L352), catalog prefix (L523).
- head: newest version (L190), `LiveInventory.head(product)` (L553), `obstore` `head` (L783), `AzureBlobRest.head` (L386), HTTP `HEAD` (L449).
- restore: "restore drill" (L198), "a restore calls directly" (L362, the hub's Phase 9 restore tool, never explained here), "restored leaf" (L636), "A restore that breaks item 4" (L977). A reader would take L362's restore to mean `correct` or `reinstate`, and call `_commit_locked` from them, skipping the lock logic.
- rules: the writer's commit logic (L84, L275), cross-product rules (L73), sdmxlib state-transition rules (S10), the owner's leaf rule (L21).
- Part 4: this design's Part 4 against "GH-125 part 4" (L104, L503, L505). L503 can read as this design's Part 4.
- probe: reader probe forward (L330), "a probe object" (L478), "Probing `meta.duckdb`" (L600).
- cold read: manifest read (L30, L74, L496) against leaf read (L848).
- owner: the person (L117) against the repository column (L141-143).

**Fix:** fix the qualifying term for each sense, for example "Inventory root", "history restore (Phase 9)", "GH-125's part 4", "cold manifest read" and "cold leaf read".

### CMP-4. CUJ 3 overstates what a correction removes

L193-195 against L645-646, L741, L747-753, L130.

CUJ 3 says flatly "An edition sealed after 39 is tombstoned". D7 keeps entries embargoed at the head with no tombstone, pending O4, and a wrong one can still auto-publish unless an operator runs `cancel` (L130). CUJ 3 also omits that an edition released after 39 goes back to embargoed (L747), so a correction can pull a public product. D6 notes the same case at L613. The W2 row, L122 and L265 disclose the O4 path honestly. CUJ 3 is the misleading text.

**Misreading:** an operator following CUJ 3 would believe the wrong version's effects are gone, skip `cancel`, and be surprised when a wrongly recorded edition releases or a released one vanishes.

**Fix:** reword to "Each non-embargoed edition recorded after 39 is tombstoned", and add two sentences covering kept embargoed entries (pending O4, check with `cancel`) and re-embargoed releases.

### CMP-5. "Drop" has two senses

L641-644, L974 against L647, L680, L741-745, L980.

Sense (a) is an entry in the restored leaf that the head tombstones (L641), which refuses when live (L642). Sense (b) is an entry recorded after `from_seq` that is absent from the restored leaf (L647), which is tombstoned and does not refuse, even when live at the head (L744 "a dropped live edition"). Test row L974, "a dropped live entry refuses", contradicts L744 unless the reader decodes the senses.

**Misreading:** an implementer could raise on case (b), which breaks ordinary corrections and W2.

**Fix:** use two terms, such as "removed because tombstoned" and "dropped". Reword L974 to "a `from_seq` live entry the head tombstones refuses".

### CMP-6. `rollback` after `reinstate` is stated without its limit

L201, L743-744 against L695, L699-701, L153.

CUJ 4 and L743-744 say a `rollback` makes a reinstated edition live, unqualified. L695 says `rollback` refuses when the product has no live edition, also with `if_live=None`. That is the normal case for a product new since `from_seq`.

**Misreading:** an operator in a drill would hit `rule="if-live"` with no guidance in the journey. The workaround at L700 is a second `correct` or a new seal, and it "restores that whole state", which can itself restore unwanted content.

**Fix:** state the limit in CUJ 4 and at L743, pointing to L699-701 and S9.

### CMP-7. The cold-release estimate leaves out leaf reads

L74-75, L496-497, L503-504, L846 against L333 and statcatlib `src/statcatlib/store/cross_product.py:214` (`_planned` calls `full.leaves()`).

The design gives a cold release as "63 s" sequentially and "about 1 s" with prefetch, "about 60 times faster". The rules also need every leaf, and L333 says a fresh reader's `leaves()` costs 7,000 GETs. D5 never adds them. L504's "63 s on each cold release" is only the manifest share. A cold release is about 7,000 + 6,300 GETs, or about 133 s sequentially. If leaf reads stay sequential, prefetch brings it only to about 71 s, roughly 2 times faster. L846's Cost line ("about 9 reads") also omits rules reads for commits that run rules. L319 says only that they are "extra".

**Misreading:** a reader would budget 1 s for a pod's cold release.

**Fix:** state a cold-release total in D5 and the NFR, split into leaves and manifests, and resolve CMP-9.

### CMP-8. Part 5 does not measure what D5 says it records

L497-498 against L108, L992, L996-998, L1007-1013.

L497 says "Part 5's run records the real ratio on Azurite" for the 6,300 manifest GETs. Part 5's corpus has placeholder hashes, "no manifest exists", and its table measures leaves only.

**Misreading:** a reader would believe the manifest prefetch's 60-times claim gets measured. Nothing in the plan measures it.

**Fix:** either say the ratio is unmeasured in Phase 4, or add a manifest case over the 14 KB objects F17 used.

### CMP-9. A reader cannot tell whether leaf reads are concurrent in shipped code

L587, L848-849, L885, L1010 against L492-494.

D5 defines a prefetch for manifests only. `read_many` is listed in Part 1 (L885) and defined nowhere. The sdmxlib inventory package has no thread pool (grep of `src/sdmxlib/inventory`). L1010 benches a "64-thread leaf prefetch", and L848 quotes 1.1 s.

**Misreading:** a reader would assume `Catalog.products()` takes about 1 s, while L587 can be read as 70 s. An implementer does not know whether to build it, or where.

**Fix:** define `read_many`. State whether `Catalog.products()` and the writer's `leaves()` use it in Phase 4, or whether it exists only in the bench.

### CMP-10. "Nothing here is built now" heads a table of things built now

L358 against L362, L366, L887, L919; L58-59 against L385-395.

The table's rows say `with storage.exclusive()` is in place "from Part 1", `_commit_locked` is split out, `write_leaves` is "Already" there, and Part 2 adds `AzureBlobRest`. L59 says the seam "leaves room" for a REST client, while D2 ships one in Phase 4. `storage.stage_2` (L364) and "when its storage says to" (L334) name protocol members that the protocol at L286-297 lacks.

**Misreading:** a reader could drop `_commit_locked` and `AzureBlobRest` from Parts 1 and 2 as stage 2 scope, and cannot tell whether Part 1 adds the members.

**Fix:** add a "Built in Phase 4 / stage 2" column to the table, reword L358 and L59, and either add the members to the protocol or mark them as later.

### CMP-11. `record_uploaded` contradicts D5's list and the fold's behaviour

L483-484 against L818-820; L110; L819 and L812 against spec §11.2 step 2 (`sharded-bundle-catalog.md:970`).

L483 says that on blob the rules read manifests "only in" `release`, `rollback` and `correct`, and "No seal runs there". `record_uploaded` runs the rules on blob and commits `event.kind: seal`. L819 says it applies `record` "as the fold will", but the default `embargoed=False` records live (L935), while §11.2 says the fold records `embargoed` entries.

**Misreading:** a reader would miss a D5 caller, could wrongly assert a read count in a test, and would believe tests over live editions mirror the fold.

**Fix:** add `record_uploaded` to L483, explain that the "seal" kind is deliberate, and state that the live default is a test shortcut the fold never takes. Define what `embargoed=True` with `release_at=None` means (L812).

### CMP-12. The production journey for `statcat upload` cannot be configured from the text

L161-162, L376-377, L390, L395, L775.

CUJ 1 runs `statcat upload` "with its workload identity". The CLI "builds the location from the environment" (L775), with no variables named and no rule for choosing a credential. The bearer path needs a `TokenIdentity` around a provider, and `azure-identity` "stays the caller's dependency" (L376), but the CLI has no caller code. As written, `pip install statcatlib[blob]` plus `statcat upload` may refuse (L395) or fail on import. Part 2's tests (L926-936) do not cover the CLI's identity path.

**Fix:** specify the environment variables, how the CLI gets a provider, and whether `blob` or the job image supplies `azure-identity`. Add a test row.

### CMP-13. Later machinery is written in the present tense

L99, L111-112, L194, L265, L313, L483, L662, L866 against the plan (`sharded-bundle-catalog-plan.md:59`, GC in Phase 5).

"GC keeps its files for the window" and "its files stay for the retention window" imply a working retention enforcer. The design never says GC is unbuilt, or when it arrives. On blob in Phase 4 nothing deletes, since `prune` refuses (L111), so L866 "the files go with the tombstone's window" describes no current behaviour on blob. "Which expiry keeps" (L313) cites a stage 2 feature as the safety reason for a Phase 4 write rule. Non-goal "the release job" (L99) sits beside blob `CatalogAdmin.release`, which is in use (L315, L483) and absent from the Overview's list, so a reader may conclude no blob release exists at all.

**Fix:** add a table of each capability on local and on blob, with the phase in which it arrives: seal, record, release, rollback, correct, reinstate, prune/GC, expiry, fold, upload, read. This also answers CMP-1 and CMP-20.

### CMP-14. Undefined or unresolvable references

L8, L16-17, L20, L27-29, L77-80, L275-281, L671, L724, L1022-1023.

- "the plan" (L17) is unlinked, and Phases 2a, 4, 5, 6 and 9 are never mapped.
- GH numbers carry no repository, and sdmxlib issue numbers also appear in this project.
- "the RFC" and "RFC decision 8" (L77, L671) are unlinked.
- P6 and ZM2 (L80) are given no meaning. ZM2 is in `intermediate_zoom_out_report.md:105`, N13 in `comprehension_report.md:282`, DES-M6 in `critic_report.md:118`, and P6 is mentioned in `goldfish_review_report.md:45`. L29's bare directory path does not say which report holds which.
- H10, H21 and H23 are used without gloss. H11 and H24 are listed as inputs (L27) and never used again.
- The "Question" column (L273-281) and "Question 7" (L724, L977) mean GH-98's numbered list (`98-live.md:32-38`), which is never said.
- §11.5 is listed as an input (L20) and never cited. §4.7, §7.1, §11.3 and §11.4 are cited and not listed. §6.5 "items 4, 7, 8" (L507) and "item 4" (L977) need the spec open, and only item 6 gets a gloss (L737).
- `file:line` paths mix repositories with no prefix (`store/inventory.py` is statcatlib, `edition/_check.py` is sdmxlib).
- L1023, "a subagent the hub session delegates to", is process text no reader can act on.

**Fix:** add a references block with links and a phase map. Gloss each item on first use, and restate GH-98's seven questions. Replace L1022-1023 with who owes the spec PR.

## Minor

### CMP-15. "A restored leaf keeps its bytes" is false in two cases

L720 against L641, L645-648. It fails when the rule removes a tombstoned entry or keeps an embargoed one. An implementer could assert hash equality. **Fix:** limit the sentence to leaves the rule did not alter, and state what happens to the others.

### CMP-16. Cost numbers reconcile but the document does not explain them

- 6,300 against 7,000: 6,300 is live products, with 10% embargoed (`bench/corpus-results.md:107`), used for manifests; 7,000 is all leaves. L30 and L74 say "at 7,000 products" without "live".
- "about 1 s" (L497) against "about 1.1 s" (L849): different workloads, ceil(6300/64) and ceil(7000/64) x 10 ms (L1016).
- 63 s against 70 s (L75, L848): same cause.
- "9 reads": the list at L317-319 has nine items, and 3 + 9 at 10 ms gives 120 ms. "Its root" and "the base root" are the same object read twice, likewise "the product's leaf" and "the base leaf" (`store/inventory.py:530`, `:550`, `:596`). They read as different objects. L846's "about 9" softens an exact count.
- 60x estimate (L503) against a measured 3x (F17: 1.35 s to 0.43 s), explained at L238 only as Azurite being CPU-bound.

**Fix:** one sentence on each, with a small numbers table.

### CMP-17. F17 sits after F22

L238. A reader scanning for F17 near F16 thinks it is missing, and it backs `MANIFEST_READERS = 64` (L494). Other cross-references resolve. **Fix:** move the row between F16 and F18 without renumbering.

### CMP-18. The real-Azure pass is misdescribed

L860, L1049-1051 against L242-256, L385-395. L860 lists "the signing" as a small replaceable real-Azure risk, but signing is now the emulator path only. "Part 1's PR" holds the checklist, while the bearer `Content-MD5` check at L1051 needs `AzureBlobRest` from Part 2. L1050-1051 lists four items as "answering the unconfirmed list", which has six. **Fix:** name the bearer path, move or split the checklist, and mark which items stay open.

### CMP-19. The status line undercounts what is open

L4-6, L117-118, L141, L150, L154. The status line says one pending decision. Eleven contract questions also remain, including S6 and S10, which block work. L141 says none block Part 1 "unless stated", yet S6 blocks D5's reader in Part 1 (L889), with a fallback that makes the blocking unclear. **Fix:** status says "O4 and S1 to S11 open; S10 blocks Part 4; S6 has a fallback". State whether Part 1 proceeds on the fallback.

### CMP-20. S3 scope is scattered

L86-87, L109, L266, L433, L451, L902 against L781, L817. The Goals name Azurite only. W3 says the writer "still runs on S3". SeaweedFS conformance runs. Upload refuses S3, and `record_uploaded` calls upload, so it fails too. A reader cannot tell whether `Catalog.open` over S3 is supported, or how an S3 catalog could ever hold an edition. **Fix:** one sentence stating what S3 gets in Phase 4: the writer passes conformance only, with no populated catalog.

### CMP-21. CUJ 2's example needs notes

L171-173, L182-183, L554-556. `sl`, `credential`, `connection` and `AggregateDataset` are not introduced. A workload identity sits next to a `connection_string` secret with no comment, so a reader may copy an account-wide key into production. "An embargoed product ... raises `NoLiveEditionError`" treats embargo as a product state; a product with an older live edition and a new embargoed one returns the old one. L554 does not say what happens on a cached-manifest mismatch, because the raise reads as belonging to the "Otherwise" sentence. **Fix:** note each, and say "a product whose only editions are embargoed".

### CMP-22. Sentences that need the code or spec open

- L335: `_ObjectOps` is placed under the unrelated "long-lived reader" bullet.
- L394: "two lines with no signer".
- L454: private `_commit_product`, with no gloss that it is `admin.py`'s single-product path.
- L568: "`pull_bytes` gives only the required size" has no definition and no consequence stated (`sdmxlib inventory/_routing.py:37-54`). It presumably warns that disk budgets from it under-count.
- L563: `_Store._over`.
- L693: journal and `_named`.
- `verify_remote` (L61, L790) and `RemoteFile` (L386) have no home in the Part file lists (L917-923).

**Fix:** gloss each, and add them to the file lists.

### CMP-23. "Naming the withdrawn base"

L753, L130, L978. The phrase suggests the `previous` entry is `withdrawn`, but L741 says a dropped one is a tombstone, and L130 says only "no longer live". A reader could write a wrong message assertion. **Fix:** use "a base no longer live".

### CMP-24. Two time bounds, and unnamed test files

L463-470, L1035-1042. Two bounds (30 s host skew, 5-minute base-ahead) sit close together, and "A release cannot go out minutes off" does not say which applies. Since the writer records storage time, a reader cannot see at first why a host clock ahead is refused and a host clock behind passes (L466). Separately, the acceptance map names `test_upload.py`, `test_remote_catalog.py`, `test_correct.py` and `test_reinstate.py`, while the Part 2 to 4 tables (L926-988) give no file names. **Fix:** label the two bounds, and add file names.

## Checked and found sound

- The Overview's "three findings" and The Problem's "five needs" match their counts.
- Copying to `products/` is right, since `data/` sits under `products/<id>/` (spec §4.1).
- 200 leaf Puts, 14 roots and 7,000 leaf Puts, and 3.6 MB and 17 MB are consistent.
- W2 against D7's O4 path is disclosed correctly at L122, L134 and L265. Only CUJ 3 misleads (CMP-4).
- Goals and Non-goals do not contradict each other directly. The trouble is the present-tense wording in CMP-13.
- GH-98 states upload's limit at `98-live.md:15`, and the design carries it only in Non-Goals.

## Not verified

- The design pins the leaf rule to sdmxlib `docs/phase-4-restated` at `cf11dd1a` (L12), and the supplied checkout is at `011312c3`. The relationship between them was not checked, because no git tool was available.
- W4's claim that spec §6.4 states the 30 s bound.
- `obstore` stub details cited in the F table, taken as stated.
