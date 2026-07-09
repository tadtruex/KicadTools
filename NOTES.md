# KicadTools — working notes

Scratch pad for cross-machine handoff: where things stand and what's next.
Not user docs (see `README.md` for those).

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
- Latent mismatch: `SpecificPartIntent._from_dict_impl` reads `DigiKeyPN`, but
  the ECG schematics store the property as `DigikeyPN` (lowercase k). Every ECG
  part has a `DigikeyPN` — worth normalizing the field lookup.
