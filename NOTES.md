# KicadTools — working notes

Scratch pad for cross-machine handoff: where things stand and what's next.
Not user docs (see `README.md` for those).

## JLCPCB fixes: LCSC ranking, BOM invariants, CPL generation (2026-07-14)

Three-part effort, spec-driven. An ultracode research→adversarial-verify workflow
first pinned the exact JLCPCB BOM/CPL formats + KiCad→JLCPCB rotation rules; the
full synthesized spec lives in **`JLCPCB_SPEC.md`** (read it before touching this
code). Suite **194 passing** (was 171). Nothing committed yet.

1. **LCSC lookup correctness** (`suppliers.py`). Root cause of the "J8 resolves to
   a part that doesn't exist" bug: for MPN `PH2-10-UA` JLCPCB returns the genuine
   ADAM TECH `C5708577` **and** a synthetic `JLCPCB Assembly` SKU `C9900016815`,
   both stock 0; the old `next(in-stock, else matches[0])` picked the placeholder.
   Replaced with `min(matches, key=_rank_match)`: **in-stock → genuine catalog
   part (a `C9900…` code lacking `lcscGoodsUrl` is deprioritized, NOT dropped — it
   can be orderable when in stock) → Basic lib → preferred → more stock → cheaper.**
   New helpers `_stock_count/_in_stock/_min_price/_is_placeholder/_rank_match`.
   Live-verified: J8 → `C5708577`. NB the research REFUTED my first instinct to
   hard-drop `C9900`; stock is the real orderability signal.
   **Cache poisoning (found when J8 still resolved wrong after the ranking fix):**
   the SQLite cache had already memoized the old bogus pick (`PH2-10-UA →
   C9900016815`, + 2 more `C9900*`), and `_lcsc_for` reads the cache before
   re-ranking, so the fix never ran for cached MPNs. Fixed at the source:
   `resolve.Cache` now stamps `mpn_to_lcsc` with `LCSC_SELECTION_VERSION` (now
   `"2"`) and wipes that table on a version mismatch — bump the constant whenever
   the pick logic changes and stale picks self-invalidate. `dk_to_mpn` (stable) is
   kept. The user's real `~/.kicadtools/lcsc_cache.sqlite` was already invalidated
   during verification; a fresh `lcsc` run re-resolves the wiped MPNs.
2. **BOM invariants** (`jlcpcb.py`). Header/format were already accepted (wokwi-
   proven `Comment,Designator,Footprint,LCSC Part #`, no UTF-8 BOM). Added the two
   missing guards: split any group with >200 designators across lines
   (`MAX_DESIGNATORS_PER_LINE = 200`), and `_valid_lcsc()` blanks any part-number
   cell that isn't `^C\d+$` (an MPN in that column is a documented reject cause),
   upper-casing valid codes. The user's BOM was likely rejected because of the bad
   `C9900` numbers from bug #1, not the format.
3. **CPL generation from the `.kicad_pcb`** (NEW — user chose "generate our own",
   CSV). Pure-sexpr, no `pcbnew` (not importable in the venv; matches the project's
   read-files-directly philosophy).
   - `pcb.py` grew `Placement`/`Pad` dataclasses + `parse_footprint_placements()`
     (per-ref origin `(at x y angle)`, side from the direct `(layer F.Cu|B.Cu)`,
     pads with local `(at)`+`(size)`) and `read_aux_origin()`.
   - `rotations.py` (NEW): regex-over-footprint-name family offset DB. Ships ONLY
     the offsets BOTH community tables agree on (SOIC/SSOP/TSSOP/DFN/LQFP/TQFP=270,
     CP_*=180, JST_*=180, TO-277=90, WS2812B-2020=90); the disputed SOT-23/QFN
     (−90 vs 180 / 270 vs 90) are deliberately OMITTED → 0°, nudge in the previewer.
   - `cpl.py` (NEW): `build_cpl()` = pad-bbox centroid, `- aux_origin`, **Y-invert**
     (KiCad Y-down → JLCPCB Y-up); rotation = footprint angle, `180−angle` for
     bottom (BEFORE offsets), `+ family + manual` offset, `% 360`. `CplLine`,
     `CPL_FIELDS=[Designator,Mid X,Mid Y,Layer,Rotation]`, `write_cpl_csv` (bare mm,
     4 decimals, `Top`/`Bottom`), `cpl_from_schematic`. `include=` restricts to the
     BOM's ref set so BOM/CPL designators match (JLCPCB requirement).
   - CLI `kicadtools cpl <sch> [-o CSV] [--smd]` + Cockpit **File ▸ Export JLCPCB
     CPL…** (read-only, dirty flag untouched, `<board>_cpl.csv` default, SMD-only
     Yes/No/Cancel). Warns on BOM parts with no placed footprint.
   - Verified live on real ECG (sch+pcb): 55 placements, 7 BOM parts w/o footprint
     reported, `G***` graphic excluded; symmetric parts' centroid == anchor; R8 =
     (130.34, −102.79) Top 270°; 4 bottom-side parts flipped. Headless GUI export OK.
   - ⚠️ Could NOT validate against KiCad's own `.pos` (no KiCad/kicad-cli on this
     box) or a real JLCPCB upload. The bottom-flip `(180−angle)` and family offsets
     are the highest-risk bits — do a real upload + previewer check before trusting.

Also fixed a **pre-existing red baseline**: commit `1b1b8be "add descript to lcsc
page"` was inconsistent — the model put `Description` at column 5 but the tests
expect it at 2 (`Accept·Ref·Description·LCSC·DigiKey·MPN·Status`). Reordered
`_LCSC_COLUMNS` + the `data()` DisplayRole tuple to match; that took the suite from
6-failing to green before this work.

### Still open
- `kicadtools lcsc --write` CLI still not wired (unchanged from before).
- CPL rotation offsets are best-effort/unvalidated; expose a GUI per-part manual
  rotation override (mechanism is already in `build_cpl(manual_rotation=…)`).
- Consider stripping the `Library:` prefix from the BOM Footprint column (JLCPCB's
  sample shows bare package names); not a known reject cause, left as-is for now.

## SMD-only option for the JLCPCB BOM — mounting from the PCB (2026-07-14)

The JLCPCB export can now emit an **SMD-only** BOM. Mounting type is read from
the **authoritative** source — the board's `.kicad_pcb`, which stamps every placed
footprint with `(attr smd)` / `(attr through_hole)` — not guessed from the
footprint name. (We explored a name-heuristic workflow first; abandoned it once
we confirmed the PCB carries the real attr, incl. for custom-library parts that
no name rule could resolve.)

- **`pcb.py`** (new, pure) — `parse_footprint_mounting(text)` walks the
  `.kicad_pcb` s-expr and maps each footprint's Reference → `smd`/`tht`/`unknown`
  from its `(attr …)` tokens (compound flags like `smd exclude_from_bom dnp`
  still read as smd; `board_only` / no-attr → unknown). `sibling_pcb(sch)` finds
  `<name>.kicad_pcb` beside the schematic; `read_mounting_for_schematic(sch)`
  returns `({refdes: mounting}, pcb_path)` or `({}, None)` when there's no PCB.
  sexpdata parses the 2.3 MB MotorDriver PCB in ~0.4 s — fine for an interactive
  export.
- **`jlcpcb.py`** — `build_jlcpcb_bom(records, lcsc_override=None, mounting=None,
  smd_only=False)`: with `smd_only`, keeps only parts whose `mounting` is `smd`
  (a group shares one footprint → one mounting; `JlcBomLine` gained a `mounting`
  field). `undetermined_references(records, mounting)` lists the excluded-but-
  *unknown* refs (confirmed THT is correctly excluded and **not** listed) — used
  for the "exclude + warn" behavior. Join is by refdes; a schematic part absent
  from the PCB (or no PCB at all) → unknown → excluded.
- **CLI** — `kicadtools jlcpcb <sch> [-o CSV] [--smd]`. `--smd` reads the sibling
  PCB, filters to SMD, and warns on stderr about undetermined-mounting exclusions
  (and specially when no `.kicad_pcb` is found → empty SMD BOM). Missing-LCSC
  warning + exit-1 gating still apply, to the filtered lines.
- **GUI** — File ▸ Export JLCPCB BOM… now asks **"Include surface-mount (SMD)
  parts only?"** (Yes / No / Cancel) before the save dialog (getSaveFileName has
  no custom-widget slot, so a pre-dialog question is the simplest headless-
  testable option). Yes reads the sibling PCB for mounting; status bar reports the
  undetermined-excluded count. Still read-only (dirty flag untouched).
- Decisions (user): source = the board `.kicad_pcb`; undetermined parts under
  `--smd` are **excluded + warned**. Mounting is computed (not a stored/overridable
  property) — an explicit `Intent:Mounting` override could follow later.
- Tests: `test_pcb.py` (attr classification incl. compound/board_only/no-attr,
  sibling lookup, no-PCB) + `test_jlcpcb.py` SMD-filter/undetermined/mounting-field
  cases and two CLI `--smd` tests (synthetic sibling PCB on the flat ECG board;
  MotorDriver is hierarchical so can't be copied standalone). Suite **165 passing**.
  Verified live on the real MotorDriver (sch + real PCB): full 82 parts → SMD 75
  (the 7 through-hole dropped), **0 undetermined**; GUI Yes/No/Cancel all correct.

## JLCPCB BOM export — `kicadtools jlcpcb` + Cockpit menu (2026-07-14)

Export a BOM in the CSV format JLCPCB's SMT-assembly uploader auto-detects.
Format was pinned by a research→adversarial-verify→design workflow: two
independent web verifiers both landed high-confidence on the header row
**`Comment, Designator, Footprint, LCSC Part #`** (only the first three are
strictly required; LCSC recommended). Quantity is intentionally omitted (JLCPCB
derives it from the Designator cell).

- **`jlcpcb.py`** (new, GUI-free/pure) — `JlcBomLine` frozen dataclass
  (`.designator` = comma-joined refs, `.quantity` = len), `JLC_FIELDS`,
  `build_jlcpcb_bom(records, lcsc_override=None)` (pure), `write_jlcpcb_csv(lines,
  fileobj)`, `missing_lcsc(lines)`, and the I/O wrapper
  `jlcpcb_bom_from_schematic(path)`. Grouping key is **(Value, Footprint, LCSC)** —
  LCSC is in the key on purpose (JLCPCB wants one LCSC per line, so differing/
  one-blank LCSC values don't collapse). Refs within a line and the line order are
  `natural_key`-sorted (deterministic regardless of traversal order).
- **`naturalsort.py`** (new) — `natural_key` moved here out of `gui/model.py`
  (which imports PySide6) so the pure module + CLI can share it; `gui/model.py`
  now re-imports it (existing `from …gui.model import natural_key` still works).
- **CLI** — `kicadtools jlcpcb <sch> [-o CSV]`: writes to `-o` or stdout (pure CSV
  on stdout, all human text on stderr). Parts with no LCSC are exported with a
  **blank cell** (never dropped), warned on stderr with their designators, and the
  command **exits 1** when any line lacks an LCSC (mirrors `lcsc`'s gating stance).
- **GUI** — File ▸ Export JLCPCB BOM… (save dialog, `.csv`). Read-only: never
  touches the schematic or the dirty flag. Prefers session-resolved LCSC numbers
  from the LCSC tab via a `{ref: lcsc}` override into `build_jlcpcb_bom`, so
  just-resolved-but-unsaved numbers land in the export.
- ⚠️ **Alias gap found while building:** the design's codemap claimed
  `extract_part_numbers` tolerates a property literally keyed `"JLCPCB Part #"` — it
  does **not** (that normalizes to `jlcpcbpart`, not in the `lcsc` alias list
  `lcsc/lcscpn/lcscpartnumber/lcscpart/jlcpcb/jlc`). Real boards store canonical
  `LCSC`, so export is correct; but if we ever want to read a `"JLCPCB Part #"`
  property we'd need to add that alias in `partnum.py` (also affects `normalize`).
- Tests: `test_jlcpcb.py` (grouping, natural sort, LCSC-splits-lines, blank-not-
  dropped, override, deterministic order, CSV header/quoting, real board, 3 CLI
  tests) + a GUI data-contract test in `test_gui_lcsc.py`. Suite **153 passing**.
  Verified live: CLI on MotorDriver (36 lines, 13 blank-LCSC, exit 1) and headless
  GUI export (file written, dirty flag untouched).

## Clickable LCSC / DigiKey links in the LCSC tab (2026-07-14)

The LCSC and DigiKey cells are now clickable links to the supplier pages.

- **`suppliers.py`** — pure URL builders `lcsc_url(code)` →
  `lcsc.com/search?q=…` and `digikey_url(pn)` →
  `digikey.com/en/products/result?keywords=…` (both `quote(…, safe="")`-encoded,
  `None` when the value is empty). Search URLs, not product-detail slugs, so just
  the code/PN resolves.
- **`gui/lcsc.py`** — new `LINK_ROLE = Qt.UserRole + 1` returns the cell's URL
  (LCSC col 2 / DigiKey col 3 with a value, else `None`). Link cells also render
  blue (palette link colour, theme-aware; fixed fallback headless) + underlined
  via `ForegroundRole`/`FontRole`, and their `ToolTipRole` shows the URL.
- **`gui/main_window.py`** — `_build_lcsc_tab` turns on mouse tracking and wires
  `clicked → _open_link` (opens `LINK_ROLE`'s URL via `QDesktopServices.openUrl`,
  no-op for non-link cells) and `entered → _hover_link` (pointing-hand cursor
  over link cells). `LINK_ROLE` forwards through the `NaturalSortProxyModel`
  (it only overrides `lessThan`), so no source-index mapping is needed.
- Tests: URL builders in `test_suppliers.py`; link roles + styling in
  `test_gui_lcsc.py`. Suite **138 passing**. Smoke-tested headless on MotorDriver:
  clicking an LCSC/DigiKey cell opens the right URL, a Ref cell opens nothing.

## Dirty tracking + quit guard (2026-07-14)

The Cockpit now tracks unsaved edits and guards the quit path
(`gui/main_window.py`).

- **Dirty state** rides on Qt's `setWindowModified` / `isWindowModified` (title
  carries the `[*]` placeholder, so a `*`/platform dirty-marker shows only while
  modified). `_mark_dirty()` sets it in all four accept handlers
  (`_accept_checked/all`, `_lcsc_accept_checked/all`); `_open_path` clears it on
  every fresh load (including the reload after a save), so accept → dirty, save
  or open → clean.
- **Quit guard** — new `closeEvent` fires for both File ▸ Quit (calls `close`)
  and the window close button. Clean → quits. Dirty → Save / Discard / Cancel:
  Cancel & Discard behave as expected; Save writes in place and only quits if the
  write succeeds (a failed write keeps the window open). Dirty = accepted-but-
  unsaved edits (`_merged_edits`); merely *checking* a row isn't dirty.
- **Refactor** — `_save` split into `_write_in_place(edits) -> bool` (write +
  reload + status, no dialog) and `_save() -> bool` (confirm then delegate). The
  quit-Save path calls `_write_in_place` directly to avoid a double confirmation.
- Verified headless (offscreen Qt), all six branches: fresh load clean; accept →
  modified; clean quit passes through with no prompt; dirty quit → Cancel keeps
  the window, Discard quits without writing, Save writes + quits (single confirm).
  Full suite still 132 passing.

## Save dialog rework + Save As (2026-07-14)

Reworked the Cockpit's save flow in `gui/main_window.py`.

- **Save** — normal confirmation now: title "Save", body "Save N changes?"
  (`Save`/`Cancel`, Save default). Dropped the old "git is your backstop" line
  and the files-and-counts blurb. Success is a status-bar message, not a modal
  "Done" box. Still writes every edited file in place across the hierarchy.
- **Save As…** (new, `QKeySequence.SaveAs`) — writes accepted edits to a
  schematic under a new name via `QFileDialog.getSaveFileName`, using
  `writer.write_properties(src, props, dest=lambda _p: dest)`. Forces a
  `.kicad_sch` suffix, then `_open_path(dest)` so the window follows the new
  file (accepted rows re-read as ✅).
- **Hierarchy is blocked in Save As** — a hierarchical design's subsheet links
  would still point at the originals and we don't relink yet, so Save As refuses
  (warning steers to Save) when `has_instantiated_sheets(self._board)` is true.
  New helper `sexpr_parser.has_instantiated_sheets(root)` — cheap root-only parse,
  true if the root has any `(sheet …)` with a Sheetfile. Save As therefore only
  ever touches one file (a flat schematic → exactly one edit key).
- Shared helpers extracted: `_pending_edits()` (merged edits or a nudge) and
  `_change_count()`.
- Tests: `test_sexpr_parser.py` covers `has_instantiated_sheets` on MotorDriver
  (hierarchical) vs resistor_test/ECG (flat). Suite **132 passing**. Also smoke-
  tested headless (offscreen Qt): flat Save As writes + follows the new file;
  hierarchical Save As is blocked before the file dialog opens; in-place Save
  shows "Save 1 change?" and writes.

Still deferred: full hierarchical Save As (copy/relink the whole tree).

## LCSC write-back — Cockpit LCSC tab now Accept + Save (2026-07-13)

The planned next step from the LCSC-tab section below is done: the LCSC tab got
the Intents tab's check/accept + Save flow, so a resolved LCSC (and any MPN
discovered via DigiKey) can be written surgically back into the schematic.

- **`writer.py`** — new `set_or_insert_property` (update a present-but-empty
  property in place, else insert — handles the "symbol already carries an empty
  `LCSC`" wrinkle `insert_property` couldn't) plus generic `add_properties` /
  `write_properties` over a `{uuid: {key: value}}` plan. `write_back` /
  `add_intent_types` kept for the existing Intent path + their tests.
- **`gui/lcsc.py`** — `LcscTableModel` grew an **Accept** column at index 0
  (columns are now `Accept · Ref · LCSC · DigiKey · MPN · Status`, so any code
  indexing by column shifted +1). Rows carry write-back provenance
  (`source_file`, `symbol_uuid`, `had_mpn`) and a `pending` `{canonical_key:
  value}` dict. `apply_resolution` fills `pending` only for `Status.RESOLVED`:
  the resolved `LCSC` always, and the DigiKey-discovered `MPN` only when the
  symbol lacked one. Checkable == has pending & not yet accepted; mirrors the
  Intent model's `_checked`/`_accepted`, `accept_checked`/`accept_all`,
  `accepted_edits()` → `{file: {uuid: {key: value}}}`. New status `✔ accepted`.
- **`gui/main_window.py`** — LCSC tab gets Accept Checked / Accept All buttons.
  `_write` now merges **both** tabs via `_merged_edits()` into one
  `{file: {uuid: {key: value}}}` plan (Intent:Type + LCSC/MPN; a symbol touched
  by both gets both) and writes once with `writer.write_properties`, then
  reloads so accepted rows re-read as ✅.
- Tests: writer primitives + accept-flow model tests added. Suite **130
  passing**. Also verified headless end-to-end on a temp copy of MotorDriver:
  resolve→accept→Save writes `(property "LCSC" …)` into the symbol, the file
  reparses, and the reloaded row reads ✅.

Still deliberately NOT done: a CLI `kicadtools lcsc --write` (the writer plumbing
— `write_properties` + assembling edits from `Resolution`s — is all there; just
not wired to the CLI yet). Field-spelling on write targets the canonical `LCSC`
key; a symbol carrying an *empty* variant spelling (e.g. `lcsc`) would get a new
`LCSC` alongside it rather than filling the variant — run `kicadtools normalize`
first if that matters.

## Part-number key normalization — `kicadtools normalize` (2026-07-10)

Closed the long-standing "DigiKeyPN vs DigikeyPN" mismatch, both on read and on
write.

- **Read fix** — `SpecificPartIntent._from_dict_impl` now reads through
  `partnum.extract_part_numbers` instead of hard-coding `data.get("DigiKeyPN")` /
  `data.get("mpn")`, so any spelling (`DigikeyPN`, `Digi-Key_PN`,
  `Manufacturer_PN`, …) classifies. ECG's lowercase-k parts were the motivation.
- **Write** — new `normalize` subcommand rewrites each *placed* symbol's
  part-number keys to one canonical spelling: `CANONICAL_KEYS` in `partnum.py`
  (`LCSC` / `MPN` / `DigiKeyPN` / `Manufacturer`). `partnum.normalization_edits`
  plans the edits (pure, no I/O): for each field with a non-empty value it moves
  the value onto the canonical key (rename in place, or set-existing-canonical),
  and drops every other variant (incl. empty duplicates). Fields with only empty
  variants are left alone — no churn.
- **Writer primitives** — `writer.py` gained `set_property_value`,
  `rename_property_key`, `remove_property` (+ shared `_symbol_bounds`), and
  `apply_key_edits` to run a `PropEdit` plan on one symbol. Same surgical,
  clean-diff, additions/edits-in-place approach as the Intent:Type insert.
- **`normalize.py`** — `plan_normalization` (walk tree → per-symbol edits),
  `compute_file_changes` (group by file, apply in memory → before/after),
  `write_file_changes`. `lib_symbols` template defs aren't touched (only
  top-level placed symbols). CLI `main(argv=None)` now takes an argv list.
- **CLI** — `kicadtools normalize <sch>` applies in place; `-n` / `--dry-run`
  prints per-symbol what would change and writes nothing.
- Verified on ECG (69 edits, 64 symbols) and MotorDriver (429 edits across root
  + subsheet): re-parses valid, idempotent, and lossless — the only multiset
  change is exact-duplicate values (same PN under two keys) collapsing to one.
  Tests: `test_normalize.py` + additions to `test_partnum.py`, `test_writer.py`,
  `test_intent.py`. Full suite **123 passing**.

Not done (by choice): normalizing `lib_symbols` template definitions; a GUI
surface for this (it's CLI-only for now).

## BOM Cockpit — LCSC tab (2026-07-09)

Wired the LCSC resolution into the Cockpit GUI as a **second tab**. The window
is now a `QTabWidget`: "Intents" (the original table) + "LCSC".

- `gui/lcsc.py` — `LcscTableModel` (columns Ref · LCSC · DigiKey · MPN · Status,
  all sortable via the shared `NaturalSortProxyModel`), populated instantly from
  the schematic's existing part-number fields (no network on open). `ResolveWorker`
  (`QObject` on a `QThread`) drives `resolve.Resolver`; emits `progress` →
  status bar and `row_resolved(row, Resolution)` → `apply_resolution`, so rows
  fill in as they land and the UI never blocks.
- `gui/main_window.py` — split into `_build_intent_tab` / `_build_lcsc_tab`;
  `_make_table` factory (sortable + drag-resize). "Resolve LCSC" button starts
  the worker; warns if DigiKey creds are missing; uses the shared cache at
  `~/.kicadtools/lcsc_cache.sqlite` (opened inside the worker thread).
- Tests: `test_gui_lcsc.py` (offline model tests). Full suite **97 passing**
  (GUI tests now run — PySide6 6.11 installed into `.venv` via the `gui` extra).

Deliberately NOT done yet: writing the resolved LCSC back into the schematic
(`writer.insert_property` supports it — same surgical path as Intent:Type); the
tab is read-only display for now, matching the CLI's report-only stance.

**Next step — give the LCSC tab the Intents tab's check/accept + Save flow.**
Mirror the Intents tab: an *Accept* checkbox on each row whose resolution
discovered something not already in the schematic, plus *Accept Checked* /
*Accept All* buttons that flip an accepted row to a checkmark. Then the existing
**File ▸ Save** should, in one action, surgically insert every accepted new
property across *both* tabs.

Concretely:
- Checkable rows are the ones where resolution produced a value the symbol
  doesn't already carry — primarily a newly resolved `LCSC` (status `➕`), and
  optionally the `MPN` discovered via DigiKey when the symbol had only a DigiKey
  PN. Rows that already had an LCSC (`✅`) or that flagged (`⚑`) aren't checkable.
- `LcscTableModel` grows the same shape as `IntentTableModel`: `_checked` /
  `_accepted` sets, `flags()` marking the Accept column `ItemIsUserCheckable`,
  `setData` for the checkbox, `accept_checked()` / `accept_all()`, and an
  `accepted_edits()` returning `{source_file: {symbol_uuid: {key: value}}}`.
  Provenance is already on each `PartRecord` (`source_file`, `symbol_uuid`), so
  keep those on the row when building it.
- `writer.insert_property` is already generic (key/value) and additions-only, so
  writing an `LCSC` (and/or `MPN`) property reuses it directly. One wrinkle to
  handle: a symbol may already carry an *empty* `LCSC` property — `insert_property`
  raises if the key exists. Add a set-or-insert helper (update the value in place
  when the property is present-but-empty, insert when absent) rather than only
  inserting. Watch the field-spelling variants too (`partnum` normalizes on
  read, but on *write* we must pick one canonical key, e.g. `LCSC`, and target
  the existing property if the symbol already has one under a variant spelling).
- `main_window._write` currently gathers `self.model.accepted_edits()` only.
  Extend it to merge edits from both models — `{file: {uuid: {key: value}}}` —
  and write once, then reload the board so accepted rows re-read as `✅`.
  Consider promoting write-back to the CLI in parallel (`kicadtools lcsc --write`).

## LCSC resolution — `kicadtools lcsc` (2026-07-09)

Goal: make sure every placed BOM part has an **LCSC** (JLCPCB) number. Pipeline
per part: (1) has LCSC → trusted; (2) has MPN → JLCPCB search → LCSC; (3) has a
DigiKey PN → DigiKey → MPN → JLCPCB; (4) none → flagged. Report-only for now
(no schematic write-back yet — that lands when this is wired into the Cockpit).

Modules (all `src/kicadtools/`):
- **`partnum.py`** — normalizes the ~dozen part-number field spellings across the
  boards (`DigiKey PN`, `Digikey`, `DigikeyPartNumber`, `Digi-Key_PN`, …) onto
  canonical `lcsc/mpn/digikey_pn/manufacturer` by matching alphanumerics-only
  keys, always preferring a non-empty value. Fixes the old `DigiKeyPN`-only bug.
- **`suppliers.py`** — `DigiKeyClient` (encapsulated OAuth token, `mpn_for()`) +
  `jlcpcb_lcsc_for()`. Lifted/cleaned from `Code/Digikey/main.py`.
- **`resolve.py`** — the state machine (`Resolver`/`Resolution`/`Status`),
  `iter_bom_part_records()` (raw-props BOM iterator matching `bom_components`),
  and a SQLite `Cache` (dk→mpn, mpn→lcsc; positive results only).
- **`cli.py`** — `kicadtools lcsc <sch> [--report csv] [--cache path]
  [--no-cache] [--delay s]`. Prints ✅ had-LCSC / ➕ resolved / ⚑ flagged +
  summary; exits non-zero if anything is still flagged. Cache defaults to
  `~/.kicadtools/lcsc_cache.sqlite`.
- Tests: `test_partnum.py`, `test_resolve.py` (four states, mocked clients),
  `test_suppliers.py` (monkeypatched requests). **85 passing, 1 skipped (GUI).**

⚠️ **JLCPCB endpoint changed — important.** The old GET
`jlcpcb.com/parts/componentSearch` now serves an HTML SPA shell, not JSON. The
old code's regex fallback then scraped the *first* `C\d+` out of the page
(`C9900027346`) and returned it for **every** query — silent false positives.
Now using the JSON API
`POST …/overseas-pcb-order/v1/shoppingCart/smtGood/selectSmtComponentList`, and
crucially **verifying `componentModelEn` matches the queried MPN** (normalized)
before accepting a hit — an unrelated result can't be returned. Unofficial &
unauthenticated, so it can still break/rate-limit → part just gets flagged.
`Code/Digikey/main.py` still has the dead endpoint + regex fallback (untouched).

Live-verified on `tests/data/MotorDriver.kicad_sch`: 82/82 parts get an LCSC
(62 already had one, 20 resolved), each via a verified MPN match.

Next: wire `resolve.py` into the BOM Cockpit (LCSC column + resolve button), and
reuse `writer.insert_property` to surgically write the `LCSC` property back into
the schematic (the M5-ish write-back, deferred here by choice).

## BOM Cockpit — Milestone 1 done (2026-07-09)

PySide6 GUI (`kicadtools-cockpit`, `src/kicadtools/gui/`) implementing
`BOMCOCKPIT.md` items 1–6: open a schematic, table of components mirroring
`bom`, Accept-Checked / Accept-All on guessed rows, then write accepted
`Intent:Type` back into the schematic.

- **Write path** — `src/kicadtools/writer.py`. `sexpdata.dumps` collapses the
  whole file to one line (verified on the 634 KB MotorDriver), so writing is a
  **surgical text insertion** keyed by each symbol's `uuid`: `insert_property`
  adds one hidden `(property "Intent:Type" …)` after the Reference property,
  additions-only (clean git diff). Writes **in place** across the hierarchy
  (grouped by `source_file`); `sibling_dest()` exists for `*.intents.kicad_sch`.
- **Provenance** — `ElectricalIntent` now carries `source_file` + `symbol_uuid`
  (set in `get_components`; uuid via `sexpr_parser.symbol_uuid`).
- **Shared filter** — `components.bom_components()` (power/`#`, dnp, not-in-bom)
  used by both the CLI and GUI so they show the same set.
- Tests: 50 passing (`test_writer.py`, `test_gui_model.py` guarded by
  `importorskip("PySide6")`). Plan file:
  `~/.claude/plans/fancy-tumbling-heron.md`.

Follow-on milestones (from the spec): **M2** placement view from `.kicad_pcb`;
**M3** part-number assignment consulting `/Users/d66317d/Code/Boards/Inventory.db`
(SQLite: Parts/OnHand/PartType/Footprints) first, then DigiKey — reuse
`Code/Digikey/main.py` (`get_mpn_from_digikey`, `obtain_access_token`; refactor
out its global token state + fix the merge-conflict marker in its `bom.csv`);
**M4** JLCPCB/LCSC via `search_lcsc_by_mpn` (Inventory.db has no LCSC column
yet); **M5** inventory write-back.

## Where we are (2026-07-08)

Fresh project based on `../kicad_tools` (`/Users/tad/Code/kicad_tools`), started
clean. Reads `.kicad_sch` directly (no netlist export), walking hierarchical
subsheets. 10 tests passing.

Latest feature: **intent guessing** — when a symbol has no explicit
`Intent:Type` property, the type is inferred in `classify.py`, trying signals in
priority order and taking the first confident answer:

1. footprint library prefix (`Resistor_SMD`, `Capacitor_SMD`, `LED_SMD`, …)
2. unambiguous `Description` keyword
3. reference-designator prefix (`R`, `C`, `D`, `BT`, `J`, …)

Explicit `Intent:Type` always wins over a guess. CLI marks lines `✅` explicit /
`≈` guessed / `❌` unclassified; `kicadtools bom -v <sch>` explains each `≈`/`❌`.
On the ECG board: 34 guessed, 28 unclassified.

## Resume checklist (new machine)

```bash
git pull
git submodule update --init --recursive
cd Code/KicadTools
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
pytest                                    # expect 10 passed
kicadtools bom -v ../../ECG/ECG/ECG.kicad_sch
```

## Next steps / open decisions

- **Check JLCPCB inventory for matched parts and suggest in-stock alternatives
  for any shortfalls.** (i.e. once a part is matched to an LCSC number, verify it
  has enough stock; where it falls short, surface orderable in-stock substitutes.)
- Add intent subclasses for types that are guessable but have no class yet:
  `battery`, `ic`, `switch`. Until a subclass exists they're guessed but stay
  `❌` (the `-v` reason names what they look like).
  Done:
  - `connector` — `ConnectorIntent` parses pin count + pitch out of the
    footprint name (`…_1x04_…P1.00mm…`); all fields optional.
  - `led` — `LedIntent` parses colour + wavelength from the Value/Description.
    LEDs are diodes, so guessing also disambiguates: a diode footprint is
    promoted to `led` when the text carries an LED signal (the word "LED", a
    wavelength, or — only in that diode-footprint context — a colour). LED text
    in the *Value* is honoured too (e.g. `1515 rgb led` on a `U`-refdes custom
    footprint → led, not ic).
  - `transistor` — `TransistorIntent` parses family (MOSFET/JFET/IGBT/BJT/array)
    and polarity (N/P-channel, NPN/PNP) from the Value/Description. Transistor
    footprints (TO/SOT packages) are shared with diodes/regulators so there's
    *no* footprint rule — guessing leans on the description keyword list (FET,
    MOSFET, NMOS/PMOS, BJT, NPN/PNP, bipolar, darlington, n/p-channel, …) with
    the `Q` refdes as last resort.
    NB: the MotorDriver `Q3/Q4/Q5` carry explicit `Intent:Type: specificpart`
    (+DigiKeyPN) so they stay specific-parts; `Q1` has a typo'd `specifiedpart`
    that lands it `❌`. No board part actually *guesses* transistor yet.
- Description matching only fires when exactly one type word is present
  (ambiguous → no guess). Revisit if that's too conservative.
- ~~Latent mismatch: `SpecificPartIntent._from_dict_impl` reads `DigiKeyPN`, but
  the ECG schematics store the property as `DigikeyPN` (lowercase k).~~ **Done
  (2026-07-10)** — reads through `partnum.extract_part_numbers` now, and the new
  `kicadtools normalize` command rewrites variant keys to canonical on disk. See
  the top section.
