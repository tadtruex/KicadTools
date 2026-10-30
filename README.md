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
footprint is promoted to an LED when its text contains an LED signal — the word
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

A PySide6 desktop app with three tabs. **Intents** reviews the intent-type
guesses and writes the accepted ones back into the schematic (so the next read
has fewer guesses). **Parts** chooses a manufacturer part for each group of
capacitors and records it in a local parts database. **LCSC** resolves, checks
stock for, and substitutes JLCPCB part numbers (see below). See `BOMCOCKPIT.md`
for the full vision.

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

### Parts tab

The table has one row per capacitor requirement group: capacitors whose
`Value`, `Footprint`, `Voltage:Min`, `Class:Preferred`, `Tolerance:Preferred`,
`Package:Preferred` and existing `MPN`, `DigiKeyPN` and `LCSC` numbers are
identical on the schematic, and (for symbols with no part number) whose
Descriptions state the same values for the fields those properties leave
open, share a row and one part choice, so
symbols with different existing parts are separate rows. Columns: Value ·
Package · Voltage · Dielectric · Tolerance · Qty · References · Current part ·
Status · Chosen MPN. A requirement cell whose value came from a default, an
edit, the existing part or the Description is marked `(default)` / `(edited)`
/ `(part)` / `(description)`; a dash means no value. A library-style Value is
shown with its capacitance (`C_4p7U (4.7uF)`). Current part is the `MPN`
field, else the `DigiKeyPN` field, as written. Status is `queued` once a part
has been accepted this session, `conflict` when two sources disagree (see
below), `assigned` when every member already has `MPN`, `Manufacturer`,
`DigiKeyPN`, `LCSC` and a `Footprint`, `not searchable` for a footprint with
no chip size (radial and axial electrolytics), else `unassigned`.

Each requirement field is taken from the first source that has a value:

| Field | 1 | 2 | 3 | 4 | 5 |
|-------|---|---|---|---|---|
| Capacitance | `Value` | existing part | Description | — | — |
| Package | the `Footprint`'s chip size | `Package:Preferred` | the Value's size suffix (`C_10U_805`) | existing part, then Description | `cap.package` (`0603`) |
| Voltage:Min | `Voltage:Min` | existing part's rating | Description (the lowest of a range: `25V - 50V` is 25 V) | `cap.voltage_min` (no built-in value) | — |
| Dielectric | `Class:Preferred` | existing part | Description | `cap.dielectric.small` ≤ 1 nF (`C0G, NP0`), `cap.dielectric.mid` (`X7R`), `cap.dielectric.bulk` ≥ 10 µF (`X7R, X5R`) | — |
| Tolerance | the loosest `Tolerance:Preferred` | existing part | `cap.tolerance.class1` for C0G/NP0 only (`5%`), else `cap.tolerance.class2` (`10%`) | — | — |

`Value` is read in plain form (`100nF`, `0.1uF`, `4.7n`, `1u`) and in
library form: an optional `C_` prefix, the value with `p` as the decimal
point before a unit letter (`C_4p7U` is 4.7 µF, `C_P1U` is 0.1 µF), and an
optional size suffix (`C_22U_1210`). The Description is read for its first
capacitance, its voltages, every dielectric code and a standalone chip size
(`10uF, 25V, X5R, 805 package`); a generic Description such as `Unpolarized
capacitor, small symbol` provides none. A dielectric from the existing part or
the Description admits its upgrades as well: X5R, X6S and X7S also accept
X7R, and C0G and NP0 accept each other.

The Description is the last source before a default: it fills only the fields
that no schematic property and no existing part states, and a symbol described
completely by those gives a Description that is not used at all. A
Description capacitance or package that disagrees with the value used is a
warning (`Description says 0603; Footprint says 0402 (Description not used)`),
shown in amber in the dialog and in the row's tooltip; it does not block a
search. Library boilerplate and Descriptions copied from another symbol are
common.

The existing part is read when the Assign Part dialog opens (see below). Its
evidence then stays on the row for the session, so the cells show `(part)`
values after the dialog closes.

**Several existing parts.** Part-number fields that resolve to different
parts (an `MPN` field and a `DigiKeyPN` field that name different MPNs at
DigiKey, or an `LCSC` number whose MPN differs from DigiKey's) give one
current-part option each. An `LCSC` part is also looked up at DigiKey by its
MPN, and a DigiKey part without an `LCSC` field is searched at JLCPCB by its
MPN, so each option can be checked at both suppliers. An option fits when its
capacitance and package agree with the `Value`, `Footprint`,
`Package:Preferred` and the Value's size suffix, and with the first fitting
option. Every fitting option is a `current` candidate. The requirement's part
fields come from all fitting options, loosest wins: the lowest voltage
rating, the widest tolerance and every dielectric. An option that does not fit
is not used and adds a warning (`Footprint says 0402; current part
CGA3E2X7R1E104K080AA (DigiKey) says 0603 (current part CGA3E2X7R1E104K080AA
(DigiKey) not used)`); when none fits, the search runs from the schematic values alone.

**Conflicts.** When two schematic sources (`Value`, `Footprint`,
`Package:Preferred`, the Value's size suffix) state a capacitance that differs
by more than 0.1 %, or different packages, the row's Status is `conflict` and
the group cannot be searched or assigned. The dialog lists each conflict with
both sources and both values, for example `Footprint says 1206; Value says
0805`. Conflicts are fixed on the schematic.

The voltage floor is never derived from net names: a group with no
`Voltage:Min`, no existing part or Description stating a voltage, and no
remembered `cap.voltage_min` can't be searched until one is entered.

- Double-click an `unassigned` or `queued` row to open the **Assign Part**
  dialog. The header shows the references, the quantity per board and the units
  the build needs (quantity × **Boards** on the LCSC tab). Below it are the
  Package, Voltage:Min, Dielectric and Tolerance fields, each labelled with its
  source. A field stated on the schematic is read-only (change it on the
  schematic, or with **Edit Electrical Parameters** on the Intents tab), and
  Package is fixed when the symbol has a footprint. Editing a field reruns the
  search about 400 ms after the last keystroke.
- The header's Current part line lists what the existing-part lookup found.
  Each distinct `MPN` and `DigiKeyPN` value is looked up at DigiKey by exact
  match, which accepts either kind of number, so swapped fields resolve. The
  LCSC number is the `LCSC` field, else the JLCPCB part whose MPN equals the
  resolved MPN (orderable, then Basic, then more stock). The part's DigiKey
  parameters (else its JLCPCB attributes) become the `(part)` evidence in the
  table above, and the dialog's fields are rebuilt from it. A failed lookup is
  a line in the header, and the requirement is then built without that
  evidence.
- When the existing part is Active at DigiKey, orderable at JLCPCB, meets the
  requirement and the stock floor, it is the first candidate (source
  `current`), it is selected, and the supplier search does not run:
  a part that meets the requirement is kept. Otherwise the reason it was
  passed over is listed among the rejections (hover over the status line) and
  the search proceeds with the requirement its evidence provided.
- The other candidates come from the parts database: accepted parts that
  meet the requirement, with JLCPCB stock and price and DigiKey stock fetched
  fresh. When neither the existing part nor the database gives a candidate,
  or on **Search suppliers**, JLCPCB is searched once, the way its parts page
  filters: the capacitance as the keyword (`4.7uF`), with the MLCC category,
  the package and in-stock as filters, every result page. (A longer keyword
  such as `4.7uF 0603 X7R` is ranked by loose text relevance, and most hits
  are then other values.) Hits from a manufacturer not in the
  `cap.manufacturers` setting are dropped; the built-in list is Murata,
  Samsung, Yageo, TDK, Taiyo Yuden, KEMET, Walsin, Samwha, Vishay, Wurth,
  Kyocera, AVX, Holy Stone and Knowles, each matched as text within the
  supplier's manufacturer name. Each remaining hit's attributes are checked
  against the requirement, and the best hits are confirmed at DigiKey: same
  MPN, an active product, and parameters that also meet the requirement. The supplier search
  needs `DIGIKEY_CLIENT_ID` and `DIGIKEY_CLIENT_SECRET`; without them the
  dialog lists the current part and the database matches only and says why.
  Candidates are ranked with the current part first, then by recorded uses,
  then JLCPCB Basic, then JLCPCB preferred, then stock at each supplier
  covering the build, then price. The status line counts the candidates from
  each source, the search hits checked, the hits rejected, and the hits past
  the DigiKey lookup limit (10).
- **Stock floor.** Every candidate (current part, database part or search hit)
  needs a known stock of at least 1000, and at least the units the build
  needs, at both JLCPCB and DigiKey (`capsearch.MIN_STOCK`). A part below that
  is rejected with the reason, for example `JLCPCB stock 33, need at least
  1000` or `DigiKey stock unknown`.
- When no part meets a tolerance limit that came from a default, the search
  runs again without it. An amber line under the status line names the limit
  that was dropped, and on Accept the chosen part's own tolerance is written to
  `Tolerance:Preferred`; the remembered `cap.tolerance.*` default is left as it
  was. A tolerance stated on the schematic or edited in the dialog is always
  applied.
- **Clearing a field.** Empty the Voltage:Min, Dielectric or Tolerance field
  (or type `any`) to search without a limit on it; its note reads `any`. A
  field that had no value must be given one or `any`, so a voltage floor is
  never left out by accident. On Accept the chosen part's own value is written
  to the symbols for a cleared field, and the remembered default is left as it
  was.
- **Values popup.** After a supplier search, a popup lists, for each cleared
  field, the values the JLCPCB parts have and how many have each, counting
  only parts of the right value and package, in stock above the floor, from a
  listed manufacturer, and meeting the other fields (DigiKey is not checked).
  It opens by itself when a field is cleared, or for every field when the
  search found nothing; **Values…** opens it for every field at any time.
  Double-click a value to put it in its field.
- Right-click a candidate and choose **Reject** to record it as rejected in the
  parts database; it is excluded from every later search.
- Select a candidate and press **Accept**. The part is recorded as accepted in
  the parts database, with the group's references as its usage on this board,
  and each defaulted or edited field is stored as the new default for its
  setting key. A value from the existing part or the Description is written
  to the symbols but never stored as a default. **File → Save** then writes,
  on every symbol in the group:
  `MPN`, `Manufacturer`, `DigiKeyPN` and `LCSC` under those canonical keys
  (other spellings of the four keys are removed); `Voltage:Min`,
  `Class:Preferred` and `Tolerance:Preferred` when the value came from a
  default, an edit, the existing part or the Description (in the form `16V`,
  `X7R, X5R`, `10%`); `Value` when the symbol has none and the capacitance
  came from the existing part or the Description; `Package:Preferred` when
  the package was edited and the symbol has no footprint; `Footprint`
  (`Capacitor_SMD:C_0603_1608Metric` and the other KiCad standard chip sizes
  0402–1210) when the symbol has none; and `Intent:Type` = `capacitor` when the
  type was guessed. A value stated on the schematic is not overwritten. After
  the write the group reads as `assigned`, and the stated values give the same
  requirement on the next open. Accepting the current part writes its
  canonical numbers under the canonical keys, which also corrects swapped
  `MPN` / `DigiKeyPN` fields and fills an empty `LCSC`.

#### Parts database

The parts database is a SQLite file at `~/Code/Boards/parts.sqlite` (the same
path relative to the home directory on every machine); set
`KICADTOOLS_PARTS_DB` to use another file. It holds each accepted or rejected
part with its capacitance, voltage rating, dielectric, tolerance, package,
DigiKey and LCSC part numbers, and the board references that place it. Stock
and price are not stored. The file is listed in `Boards/.gitignore`, so each
machine has its own copy.

The `settings` table holds the remembered defaults under the `cap.*` keys in
the table above. A key absent from the table takes the built-in value shown in
parentheses. Accepting a part overwrites the keys of the fields that came from
a default or an edit, so the next group starts from the values last used.

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
- `src/kicadtools/units.py` — parse and format resistance, capacitance, voltage, tolerance and power; package and dielectric codes
- `src/kicadtools/partsdb.py` — the SQLite parts database: accepted/rejected parts, supplier PNs, usage, settings
- `src/kicadtools/requirement.py` — capacitor requirements (value + source per field), defaults, grouping
- `src/kicadtools/capsearch.py` — capacitor candidates: database first, then JLCPCB search confirmed at DigiKey
- `src/kicadtools/assign.py` — the property write plan that assigns a chosen capacitor to a group
- `src/kicadtools/cli.py` — command-line entry point (`kicadtools`)
- `src/kicadtools/gui/` — the BOM Cockpit GUI (`kicadtools-cockpit`):
  - `model.py` — `IntentTableModel` + `NaturalSortProxyModel`
  - `main_window.py` — the main window (menu, table, Accept buttons)
  - `app.py` — `QApplication` entry point
  - `lcsc.py` — LCSC tab: part-number table and a background worker that runs the resolver
  - `param_editor.py` — the "Edit Electrical Parameters" dialog
  - `substitute_dialog.py` — the "Find Parametrically Equivalent Substitute" dialog
  - `parts.py` — Parts tab: requirement-group table model and the background candidate-search worker
  - `assign_dialog.py` — the "Assign Part" dialog
