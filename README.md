# KicadTools

Read and analyze KiCad schematics (`.kicad_sch`) directly — no netlist export step.

The parser walks the schematic and all of its hierarchical subsheets, reading
each placed symbol's properties and per-instance reference designators.

## Install

```bash
cd Code/KicadTools
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"          # add ,gui  for the BOM Cockpit GUI:  ".[dev,gui]"
```

## Usage

List the components in a schematic (a BOM-style listing):

```bash
kicadtools bom path/to/board.kicad_sch
```

Power flags (`#…`), do-not-place parts, and parts excluded from the BOM are
filtered out. Each remaining component is classified and printed with a short
description. Every line is marked with its classification outcome:

| Mark | Meaning |
|------|---------|
| `✅` | Classified from an explicit `Intent:Type` property |
| `≈`  | Type **guessed** from the other properties (see below) |
| `❌` | Couldn't be classified |

### How a component's type is decided

An explicit `Intent:Type` property always wins. When it's absent, the type is
*guessed* from the other properties, trying these signals in order and taking
the first confident answer (see `src/kicadtools/classify.py`):

1. **Footprint library** — a footprint from a type-named library
   (`Resistor_SMD`, `Capacitor_SMD`, `Diode_SMD`, `LED_SMD`, `Connector_*`, …).
2. **Description / value** — an unambiguous type word in the `Description` (or
   `Value`) text.
3. **Reference designator** — the prefix letter(s): `R`, `C`, `D`, `L`, `J`, …

LEDs get special handling because they're diodes: a part on a plain diode
footprint is promoted to an LED when its text carries an LED signal — the word
"LED", a wavelength (`630nm`), or, in that diode-footprint context, a colour.

If none is confident, the part is left unclassified (`❌`) for now. A guessed
type whose subclass doesn't exist yet (e.g. battery) also stays `❌`, but the
reason line names what it looks like.

### Explaining the guesses

Add `-v` / `--verbose` to print, under each `≈`/`❌` line, how it was handled —
which signal produced a guess, or why a part couldn't be classified:

```bash
kicadtools bom -v path/to/board.kicad_sch
```

```
≈   C12  |  Capacitor_SMD:C_0603_1608Metric  |  C12: 100nF capacitor
        ↳ guessed from footprint library 'Capacitor_SMD'
❌   BT1  |  ECG_Footprints:MPD_BC2032-E2     |  No details
        ↳ looks like 'battery' (reference prefix 'BT'), but there's no classifier for it yet
```

### Normalizing part-number keys

Boards in this repo spell the same part-number field a dozen ways (`DigikeyPN`,
`Digi-Key_PN`, `Manufacturer_PN`, `MANUFACTURER`, …), often several — mostly
empty — on one symbol. `normalize` rewrites each placed symbol's part-number
keys to a single canonical spelling (`LCSC`, `MPN`, `DigiKeyPN`, `Manufacturer`),
moving the value and dropping the redundant variants:

```bash
kicadtools normalize -n path/to/board.kicad_sch   # -n / --dry-run: preview only
kicadtools normalize    path/to/board.kicad_sch   # apply, in place
```

The dry run prints, per symbol, exactly which keys would be renamed, set, or
removed. Applying is a surgical text edit (same clean-diff approach as the
`Intent:Type` write-back) and is idempotent. Fields whose only variants are
empty are left untouched, and `lib_symbols` template definitions aren't touched —
only placed instances.

### Supplier lookups

`lcsc` and `confirm` call DigiKey's API. They read the credentials from
`DIGIKEY_CLIENT_ID` and `DIGIKEY_CLIENT_SECRET` in the environment. Both take
`--delay` (seconds to wait after each live call, default 0.5) and `-v` for a
live status line.

#### Resolving LCSC part numbers

`lcsc` finds a JLCPCB/LCSC part number for every placed BOM part:

```bash
kicadtools lcsc path/to/board.kicad_sch
kicadtools lcsc --report parts.csv path/to/board.kicad_sch   # also write a per-part CSV
```

Each part is handled by the first rule that applies:

1. It already has an `LCSC` number: kept as is (`ok`).
2. It has an `MPN`: searched on JLCPCB (`resolved`).
3. It has a `DigiKeyPN`: DigiKey gives the MPN, then JLCPCB gives the LCSC (`resolved`).
4. None of these, or a lookup fails: `flagged` for manual work, with the reason.

Results go to a SQLite cache (`~/.kicadtools/lcsc_cache.sqlite` by default;
`--cache PATH` to move it, `--no-cache` to turn it off) so reruns don't repeat
the network calls. The command only reports; it doesn't edit the schematic. It
exits non-zero if any part still has no LCSC number.

#### Confirming parts against DigiKey

`confirm` fetches the exact DigiKey product for each part that names a supplier
part number and compares DigiKey's parameters with the intent fields on the
symbol. Each field comes out as confirmed (they agree), a conflict (they
disagree), or a fill (the symbol left it blank and DigiKey supplies it).
Resistors only for now.

```bash
kicadtools confirm path/to/board.kicad_sch           # report only
kicadtools confirm --write path/to/board.kicad_sch   # also write the fills into the schematic
```

### Exporting for assembly and ordering

The three export commands write CSV to stdout, or to a file with `-o PATH`.
Warnings go to stderr, so `… > bom.csv` gives a clean file. The mounting filters read the `.kicad_pcb` that sits next to the schematic.

#### JLCPCB BOM

```bash
kicadtools jlcpcb -o bom_jlcpcb.csv path/to/board.kicad_sch
kicadtools jlcpcb --smd -o bom_jlcpcb.csv path/to/board.kicad_sch   # surface-mount parts only
```

Columns: `Comment`, `Designator`, `Footprint`, `LCSC Part #`. Parts with the
same value, footprint and LCSC number are combined into one line, split into
lines of at most 200 designators (JLCPCB's limit). With `--smd`, parts whose
mounting can't be determined are left out and listed. The command exits
non-zero if any line has no LCSC number, but the CSV is still written in full.

#### DigiKey BOM

```bash
kicadtools digikey -o bom_digikey.csv path/to/board.kicad_sch
kicadtools digikey --mounting non-smd -o bom_th.csv path/to/board.kicad_sch
```

Columns: `Quantity`, `Digi-Key Part Number`, `Reference Designator`,
`Manufacturer Part Number`, ready for DigiKey's cart/BOM uploader. `--mounting`
picks the parts: `smd` (surface-mount only; undetermined parts are left out and
listed), `non-smd` (through-hole and undetermined), or `both` (the default;
doesn't need the PCB file). Lines with no DigiKey or manufacturer part number
are still exported, but listed, and the command exits non-zero.

#### JLCPCB CPL (pick-and-place)

```bash
kicadtools cpl -o cpl.csv path/to/board.kicad_sch
kicadtools cpl --smd -o cpl.csv path/to/board.kicad_sch   # match a --smd BOM
```

Columns: `Designator`, `Mid X`, `Mid Y`, `Layer`, `Rotation`, computed from the
footprints in the `.kicad_pcb`. The position is the center of the pad bounding
box relative to the board's aux origin, with Y flipped. The rotation corrects
for bottom-side mirroring and adds the per-footprint-family offsets and any
per-LCSC rotation overrides stored in the SQLite cache (`--cache PATH` to use a
different one). `JLCPCB_SPEC.md` §3 has the derivation.

## BOM Cockpit (GUI)

A PySide6 desktop app with two tabs. **Intents** reviews the intent-type
guesses and writes the accepted ones back into the schematic (so the next read
has fewer guesses). **LCSC** resolves, checks stock for, and substitutes JLCPCB
part numbers (see below). See `BOMCOCKPIT.md` for the full vision.

```bash
pip install -e ".[dev,gui]"
kicadtools-cockpit path/to/board.kicad_sch     # path is optional; File → Open too
```

Intents tab:

- The table mirrors the `bom` listing (Ref · Status · Type · Footprint ·
  Description); each guessed (`≈`) row has an **Accept** checkbox, and hovering
  it shows why the type was guessed.
- **Accept Checked** / **Accept All** mark guesses valid (status → ✔).
- **File → Save** (⌘S) inserts an `Intent:Type` property onto each accepted
  symbol, **in place**, as a minimal edit (only new lines added — every other
  byte is untouched, so the git diff is clean). It edits each source file in the
  hierarchy and reloads; the accepted parts then read as explicit `✅`.

The write is a surgical text insertion, not a re-serialization: round-tripping
the whole schematic through the s-expression library would collapse it onto one
line. Rely on git as the backstop.

### LCSC tab

- The table lists each BOM part's LCSC, DigiKey and MPN numbers. **Resolve
  LCSC** looks up an LCSC number for parts that lack one (see `kicadtools
  lcsc`). A row where the lookup found something new gets an **Accept**
  checkbox; **Accept Checked** / **Accept All** queue those values for
  **File → Save**, which writes them the same way as the Intents tab.
- **Check Stock** reads JLCPCB's stock for every LCSC number on the board into
  the **Stock** column. Rows are tinted red when the part has no stock or JLCPCB
  won't sell it (for example a discontinued `C9900…` placeholder), and amber
  when the stock is less than the build needs: places per board × **Boards**
  (default 25, editable beside the buttons). Results are cached for 12 hours in
  `~/.kicadtools/lcsc_cache.sqlite`.
- Right-click a resistor or capacitor row and choose **Find Parametrically
  Equivalent Substitute…** to search JLCPCB for in-stock parts that match it.
  The dialog shows the requirements it searched with and where each came from.
  The current part's attributes come first (from JLCPCB, or from DigiKey when
  JLCPCB has none), then the symbol's intent fields fill any gaps, and the
  package always comes from the KiCad footprint. Value must match; voltage and
  power may be higher, tolerance tighter, and dielectric the same or better
  (X5R/X6S/X7S → X7R). Matches are listed with enough stock first, then Basic
  before Extended, then by price.
- Select one row in the dialog and press **Use Selected** (or double-click
  the row). This replaces the LCSC number on every row placing the same part
  and appends the substitute's DigiKey part number (cut tape) to the symbol's
  `Alternates` property, a comma-separated list. `MPN`, `DigiKeyPN` and
  `Manufacturer` keep the original part. The row is accepted immediately (no
  checkbox) and written on **File → Save**.

## Running the tests

With the dev dependencies installed (`pip install -e ".[dev]"`) and the venv
active, run pytest from the project root:

```bash
cd Code/KicadTools
source .venv/bin/activate
pytest
```

Handy variations:

```bash
pytest -q                                   # quiet, one line of dots
pytest -v                                   # verbose, one line per test
pytest tests/test_components.py             # a single file
pytest tests/test_components.py::test_motordriver_component_counts   # a single test
pytest -k motordriver                       # every test matching a name substring
```

Test schematics live in `tests/data/`.

## Layout

- `src/kicadtools/sexpr_parser.py` — s-expression schematic reader (files, symbols, properties, instances)
- `src/kicadtools/intent.py` — component model: `ElectricalIntent` + typed subclasses
- `src/kicadtools/classify.py` — guess a component's intent type from its properties
- `src/kicadtools/components.py` — build the component list from a schematic tree
- `src/kicadtools/partnum.py` — canonical part-number extraction + key-normalization planning
- `src/kicadtools/writer.py` — surgical property edits (insert/rename/set/remove) in the schematic text
- `src/kicadtools/normalize.py` — plan + apply part-number key normalization across the tree
- `src/kicadtools/suppliers.py` — DigiKey (part number → MPN) and JLCPCB/LCSC (MPN → LCSC) API clients
- `src/kicadtools/resolve.py` — LCSC resolution pipeline and its SQLite cache (`lcsc`)
- `src/kicadtools/confirm.py` — compare intent fields with a DigiKey product and propose fills (`confirm`)
- `src/kicadtools/changes.py` — the set of property changes a resolution proposes for a symbol
- `src/kicadtools/jlcpcb.py` — JLCPCB BOM grouping and CSV writer (`jlcpcb`)
- `src/kicadtools/digikey_bom.py` — DigiKey BOM grouping and CSV writer (`digikey`)
- `src/kicadtools/pcb.py` — read SMD/THT mounting and footprint placements from the `.kicad_pcb`
- `src/kicadtools/cpl.py` — JLCPCB CPL centroid and rotation math (`cpl`)
- `src/kicadtools/rotations.py` — per-footprint-family CPL rotation offsets
- `src/kicadtools/partrotation.py` — per-LCSC CPL rotation overrides stored in SQLite
- `src/kicadtools/naturalsort.py` — natural-sort key (R2 before R10)
- `src/kicadtools/substitute.py` — parametric substitute search: spec, matching rules, ranking, `Alternates` merge
- `src/kicadtools/cli.py` — command-line entry point (`kicadtools`)
- `src/kicadtools/gui/` — the BOM Cockpit GUI (`kicadtools-cockpit`):
  - `model.py` — `IntentTableModel` + `NaturalSortProxyModel`
  - `main_window.py` — the main window (menu, table, Accept buttons)
  - `app.py` — `QApplication` entry point
  - `lcsc.py` — LCSC tab: part-number table and a background worker that runs the resolver
  - `param_editor.py` — the "Edit Electrical Parameters" dialog
  - `substitute_dialog.py` — the "Find Parametrically Equivalent Substitute" dialog
