# KicadTools — working notes

Scratch pad for cross-machine handoff: where things stand and what's next.
Not user docs (see `README.md` for those).

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
