# Style review: catalog on blob storage

**Target:** `docs/design/catalog-on-blob-storage.md`, revision 2, at `e7eba47`.
**Standard:** "Writing style" in statcatlib's `CLAUDE.md`, lines 606-669.
**Reviewer:** `egm-style`. All findings are against the design.

## Summary

The document is largely clean. The prose is direct and specific, with real numbers and
`file:line` references. A search found no `?`, no em dashes and no line with "not ...
but". Counts: 0 Blockers, 4 Major, 9 Minor. Most are run-in headings, a few colon
setups, and short table cells or labels that allude to a fact.

## Major

### STY-1. Run-in and section titles written as half-question hook clauses

Lines 205, 358, 405, 544, 666, 683, 740. Rule: hook clauses; titles must be plain noun
phrases or imperatives.

- L205 "### What `obstore` does": "### `obstore` 0.12.1 behaviour"
- L358 "**How stage 2 fits (GH-125, H21, H23).**": "**Stage 2 placement (GH-125, H21, H23).**"
- L405 "**What the tier needs for the bearer path.**": "**OAuth Azurite container for the bearer path.**"
- L544 "**Where the files land:**": "**Cache layout:**"
- L666 "**Who builds the tombstones.**": "**Tombstone rule owner.**" Then lead with the answer:
  "sdmxlib adds the `correct` rule (S10), because `retire` refuses a live or embargoed
  entry (`_rules.py`, `rule="state"`)."
- L683 "**What `sdmxlib.inventory` accepts**": "**`sdmxlib.inventory` results for tombstones and empty leaves**"
- L740 "**What a correction changes for editions recorded after `from_seq`.**": "**Effect of a correction on editions recorded after `from_seq`.**"

### STY-2. Colon reveals and setups

Lines 735, 268, 827. Rule: colon reveals; drop the setup and write the sentence.

- L735 "How the rules see it: `incoming` compares `leaves` with the head". Delete the setup:
  "`incoming` compares `leaves` with the head, so ..."
- L268 "duckdb/duckdb-azure#171, open on 2026-10-19, is why it comes up: sdmxlib's
  `StoreSecret` docstring warns that `credential_chain` fails on Azure with a managed
  identity." Rewrite: "sdmxlib's `StoreSecret` docstring warns that `credential_chain`
  fails on Azure with a managed identity (duckdb/duckdb-azure#171, open on 2026-10-19),
  which raises the question." Wording only; W5 stands.
- L827 "It adds no power: the identity already needs write on `inventory/`, and
  `CatalogAdmin` could do as much." Rewrite: "The identity already needs write on
  `inventory/`, and `CatalogAdmin` could make the same commit."

### STY-3. "Nothing here is built now" contradicts the table under it

Line 358. Rule: definite, specific language.

L362 says `try_commit` wraps its body in `with storage.exclusive()` "from Part 1". L365
says "Already `write_leaves`". L366 says Part 2 adds `AzureBlobRest`. L887 (Part 1) splits
out `_commit_locked`. The table corrects the sentence at once, so this is no Blocker.

Rewrite: "Phase 4 builds `exclusive()` as a no-op, `_commit_locked`, `write_leaves` and
`AzureBlobRest`. It builds none of stage 2's locking, stale-base refusal, 2.x decoding,
conditional deletes or opt-in check."

### STY-4. Numbered "Question" references with no referent

Lines 273-281, 724, 977. Rule: the actual number, name or mechanism instead of an
allusion.

The Design decisions table has a "Question" column holding 1 to 7. L724 is titled
"**Question 7: the cross-product rules and the fast-forward check on a correction.**" and
L977 cites "Question 7". "Open Questions" (L115) holds only O4 and S1 to S11, and the
design has no list numbered 1 to 7. The numbers are GH-98's, and the design does not say
so.

Rewrite: rename the column "GH-98 question", retitle L724 "**GH-98 question 7: the
cross-product rules and the fast-forward check on a correction.**", and change L977 to
"GH-98 question 7".

## Minor

### STY-5. A "needs" hook, and a stale name for a mechanism

Lines 65, 860. Rule: framing by what is missing; definite, specific language.

- L65 "The spec's blob protocol has five needs the local writer does not meet:" has the
  shape of the standard's "the evidence Y lacks". Rewrite: "The spec's blob protocol
  requires:". The fifth bullet (L73-75) is a cost, so split it out: "The cross-product
  rules also read each manifest ..."
- L860 "The clock, the `oid` and the signing are each small and replaceable." "Small" is
  unmeasured, and "signing" names the wrong path: L390-393 make the bearer token the
  production path and signing the plain Azurite path. Rewrite: "The clock (`now()`), the
  `oid` (`TokenIdentity`) and the bearer `HEAD` (`AzureBlobRest`) each sit in one named
  place."

### STY-6. Short table cells and labels that allude where a mechanism belongs

Rule: definite, concrete language; L132 also breaks punchline structure.

- L132 "The simplest rule, and no gap.": "One refusal condition, and no embargoed entry can lose its `release_at`."
- L132 "It blocks recovery at the worst moment, when the Inventory is already wrong.": "It blocks `correct` while the Inventory is wrong."
- L345 "Two `commit` loops to keep equal, and the retry rules are where bugs would diverge.": "Two `commit` loops, including their retry rules, to keep equal."
- L382 "No import guard, simpler typing.": "No import guard, and no lazy imports under the `Any` ratchet."
- L479 "The clean API.": "`Date` from `obstore`, with no second HTTP client."
- L503 "small code": delete; the "one callable" clause already gives the size.
- L507 "Fast.": "No manifest GETs on `release`."
- L731 "Fast.": "No manifest reads on `correct`."
- L574 "Reads naturally.": "The URI stays a string, as for a local path."
- L575 "Plain separation.": "Remote and local open have separate signatures."
- L715 "both are rare operations": "both run only for `correct` and `reinstate`"
- L732 "Correction works.": "`correct` can move a live edition backward."
- L733 "Uniform rules.": "`correct` passes the same check as other commits."

### STY-7. Undefined or positional references

Rule: the actual name instead of an allusion.

- L16-17 "overrides the plan where they differ". "The plan" is never named. Name the document.
- L77 "`correct` as the RFC states it" and L671 "RFC decision 8". The Inputs list has no RFC. Add it, with its name or link.
- L80 "Phase 2a deferred it here (ZM2, P6)." "P6" is undefined and "it" is loose. Rewrite: "Phase 2a deferred `correct` to Phase 4 (ZM2)."
- L94, L1023, L1043 "baked summary", "baked-summary choice", "summary decision". The design never says what the summary is. Add a one-clause definition or a spec section at L94.
- L568-569 "`pull_bytes` gives only the required size.": "`pull_bytes` reports the required tier only, so it understates a pull of both tiers."
- L692 "`head` raises `KeyError`, `products()` omits it". "It" has no antecedent in the row. Rewrite: "For an empty-leaf product, `LiveInventory.head` raises `KeyError` and `products()` omits the product; `FullInventory.products()` lists it."
- L699 "for the last row": "for the `rollback` row".
- L1023 "or a subagent the hub session delegates to". "Hub session" is undefined workflow vocabulary, and L314 says "hub design" where L12 and L25 say "suite design". Rewrite: "through an sdmxlib PR." Use "suite design" throughout.

### STY-8. The overview alludes to the seam and the REST client

Lines 42-43, 52, 58-59. Rule: name, not allusion; punchline fragment at L59.

- L42-43 "a storage seam in `InventoryWriter`, with a blob implementation over `obstore`, so one set of rules commits versions on both". "Both" has no stated pair. Rewrite: "an injected `InventoryStorage` in `InventoryWriter`, with a local implementation and a blob one over `obstore`, so one set of rules commits versions on both".
- L52 "Three findings from checking `obstore` 0.12.1 shape the design:". Finding 3 (L60) is about sdmxlib's `check_edition`. Rewrite: "Three findings shape the design:"
- L58-59 "need a small REST client beside it. The seam leaves room for one." L366 and L919 show the client is `AzureBlobRest`, and Part 2 adds it. Rewrite: "need a REST client beside it. `AzureBlobRest`, which Part 2 adds for the digest lookup (D2), is that client."

### STY-9. Sentences that end on a turn or wordplay

Lines 252-253, 794. Rule: punchline structure.

- L252-253 "SeaweedFS's ETag was an MD5 in the run; that proves nothing about AWS.": "SeaweedFS's ETag was an MD5 in the run, and AWS was not run."
- L794 "`AlreadyExistsError` sets `registered=False` and is no error.": "On `AlreadyExistsError`, the upload sets `registered=False` and returns normally."

### STY-10. Contrast with an option the design does not propose

Lines 464-465, 443-445. Rule: "contrasts with a claim nobody made".

- L464-465 "A release cannot go out minutes off, which a 5-minute bound would allow." No 5-minute skew bound is on offer in this revision, and L469's 5-minute limit is a different check, so a reader can confuse the two. "Minutes off" also reads loosely against L466, where a clock behind storage passes. Rewrite: "A writer clock more than 30 s ahead of storage refuses the commit." Wording only; W4 stands.
- L443-445 "Telling verified from declared values apart in the audit record would need a format change, which this design does not propose." Rewrite: "The audit record cannot tell a verified actor from a declared one; that would need a format change."

### STY-11. Needless words and repeats

Lines 117-118, 122, 135-137 with 755-757, 141, 992. Rule: omit needless words; delayed point.

- L117-118 "What stays open is one owner decision, O4, and each contract question, with the repository that answers it.": "O4 and contract questions S1 to S11 remain open."
- L122 "**Pending the owner's decision.**" repeats L120's "(pending)" and L118. Delete.
- L135-137 and L755-757 both explain that the §11.2 fold skips these entries. Keep L755-757 and replace the L135-137 clause with "§11.2 skips all three outcomes (D7, The fold)."
- L141 "None of these block Part 1 unless stated." Name the blockers: "S6 blocks Part 1's manifest reader and S10 blocks Part 4; S4 must land before Phase 6." The owner should confirm this against the table.
- L992 "This corpus holds Inventory documents only, and the data corpus is out of this design." repeats L106-108, L264 and L840. Rewrite: "This corpus holds Inventory documents only (W1)."

### STY-12. Unspecific triggers and a clumsy subject

Lines 130, 383, 402, 747, 764-766, 801. Rule: definite, specific language; subject-verb-object.

- L130 "so the operator must look at what remains": "so the operator must check each kept entry and `cancel` any wrong one".
- L383 "Worth reconsidering if stage 2's REST client grows." and L402 "Stage 2 is the point to look again if `AzureBlobRest` grows." "Grows" has no threshold. Name one, or write "Revisit at stage 2."
- L747 "A release after `from_seq` still returns to `embargoed`". An edition returns. Compare L986. Rewrite: "An edition released after `from_seq` returns to `embargoed`, because its entry in `from_seq` is embargoed, and must be released again."
- L764-766 "the file checks are what prove it, as §6.4 states the precondition": "the file checks prove it (§6.4's precondition)."
- L801 "Two alternatives wait for a real need": "Two alternatives remain for later".

### STY-13. Journey titles and an identifier collision

Lines 159, 167, 188, 198, 147. Rule: titles as noun phrases or imperatives; plain language.

- L159, L167, L188, L198 are full clauses, such as "### CUJ 1: A publish job uploads an embargoed edition". The ID-colon use is acceptable. If the rule is applied strictly, use imperatives: "CUJ 1: upload an embargoed edition", "CUJ 2: read a blob catalog", "CUJ 3: correct a wrong Inventory version", "CUJ 4: restore a retired or dropped edition". This is the lowest-priority finding here.
- L147 uses "S3" as a contract-question ID where "S3" otherwise means the AWS service (L109, L266, L433, L435). Prefix the series if it is renamed for another reason. This alone does not justify renumbering.

## Checked and not flagged

- Refusals and limits stated as negatives, where the negative is the claim: L39, L88, L185, L240-256, L429, L466, L609, L752, L766, L807, L850-851, L1025.
- L1 "Phase 4: the catalog on blob storage". The colon separates the scope from a noun phrase.
- Template section names (Overview, The Problem, Goals, Non-Goals).
- Colons that give a reason: L136, L376, L410, L477, L1019.
- Terse "Gain" and "Cost" cells that are already concrete, such as L505 and L581.

## Outside the standard, not ranked

- L238: F17 sits after F22, out of order.
- L741-743: a leftover hard wrap leaves "An" alone at the end of the line.
