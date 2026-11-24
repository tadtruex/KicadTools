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

Power flags (`#…`), do-not-place parts, parts excluded from the BOM, and
solder jumpers and net ties are filtered out. A solder jumper or net tie is
copper, not a part, so it is left out of every BOM, the CPL and the Cockpit's
lists whatever its BOM flag says; it is recognized by its footprint name
(`SolderJumper…`, `NetTie…`, in any library) or, without a footprint, by its
Value. Each remaining component is classified and printed with a short
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
Warnings go to stderr, so `… > bom.csv` gives a clean file. The mounting filters
read each part's footprint from the `.kicad_pcb` that sits next to the schematic,
or from the footprint library when the part is not on the board. The footprint's
pads decide: all through-hole pads is through-hole, all SMD pads is SMD (even
when the footprint's `attr` says otherwise), and a mix of both (USB connectors
with plated shield tabs) follows the `attr`.

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
capacitors, resistors, inductors or ICs and records it in a local parts database. **LCSC** resolves, checks
stock for, and substitutes JLCPCB part numbers (see below). See `BOMCOCKPIT.md`
for the full vision.

Every table, on the tabs and in the dialogs, sorts on a header click. A cell
that starts with a number sorts by its value (`1,234` after `987`, `$0.12`
before `$0.5`, `100nF` before `10µF`); other text sorts in natural order (`C2`
before `C10`). A check-box column (Accept) sorts checked rows first, then
unchecked, then rows without a box. A table keeps its fill order until a header is clicked, and a
dialog's new search results keep the column picked.

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

The table lists every BOM part. It has one row per capacitor, resistor or
inductor requirement group, one per plain-LED, IC and through-hole group (see
below), and one per group of parts with no assignment path (see **Parts with
no search** below). Capacitors, resistors and
inductors: parts of one type whose `Value`, `Footprint`, `Voltage:Min`,
`Power:Min`, `Current:Min`, `Class:Preferred`, `Shielding:Preferred`,
`Tolerance:Preferred`, `Package:Preferred` and existing
`MPN`, `DigiKeyPN` and `LCSC` numbers are identical on the schematic, and (for symbols with no part number) whose
Descriptions state the same values for the fields those properties leave
open, share a row and one part choice, so
symbols with different existing parts are separate rows. The stated ratings,
tolerance and class are compared by meaning, not by text: `25V` and `25 V`
are the same, a tolerance is its loosest percentage, and a class is the set
of classes it allows, so `X5R` and `X5R, X7R` (both X5R or better) share a
row. Columns: Value ·
Package · Footprint · Rating · Class · Tolerance · Qty · References ·
Current part · Status · Chosen MPN. Footprint is the footprint name without
its library (the full lib id is in the tooltip). Rating is the voltage floor
of a capacitor, the power floor of a resistor and the saturation-current
floor of an inductor. Class is the dielectric of a capacitor and the
shielding of an inductor, and is empty for a resistor. A requirement cell whose value came from a default, an
edit, the existing part or the Description is marked `(default)` / `(edited)`
/ `(part)` / `(description)`; a dash means no value. A library-style Value is
shown with its value (`C_4p7U (4.7uF)`, `R_49p9K (49.9k)`). Current part is the `MPN`
field, else the `DigiKeyPN` field, as written. Status is `queued` once a part
has been accepted this session, `conflict` when two sources disagree (see
below), `assigned` when every member already has `MPN`, `Manufacturer`,
`DigiKeyPN`, `LCSC` and a `Footprint`, `not searchable` for a footprint with
no chip size (radial and axial electrolytics; an inductor footprint is the
exception, see below), else `unassigned`.

Each requirement field is taken from the first source that has a value.
Capacitors:

| Field | 1 | 2 | 3 | 4 | 5 |
|-------|---|---|---|---|---|
| Capacitance | `Value` | existing part | Description | — | — |
| Package | the `Footprint`'s chip size | `Package:Preferred` | the Value's size suffix (`C_10U_805`) | existing part, then Description | `cap.package` (`0603`) |
| Voltage:Min | `Voltage:Min` | existing part's rating | Description (the lowest of a range: `25V - 50V` is 25 V) | `cap.voltage_min` (no built-in value) | — |
| Dielectric | `Class:Preferred` | existing part | Description | `cap.dielectric.small` ≤ 1 nF (`C0G, NP0`), `cap.dielectric.mid` (`X7R`), `cap.dielectric.bulk` ≥ 10 µF (`X7R, X5R`) | — |
| Tolerance | the loosest `Tolerance:Preferred` | existing part | `cap.tolerance.class1` for C0G/NP0 only (`5%`), else `cap.tolerance.class2` (`10%`) | — | — |

Resistors:

| Field | 1 | 2 | 3 | 4 | 5 |
|-------|---|---|---|---|---|
| Resistance | `Value` | existing part | Description | — | — |
| Package | the `Footprint`'s chip size | `Package:Preferred` | the Value's size suffix (`R_10K_805`) | existing part, then Description | `res.package` (`0603`) |
| Power:Min | `Power:Min` | existing part's rating | Description | `res.power_min` (no built-in value) | `any`: no limit |
| Tolerance | the loosest `Tolerance:Preferred` | existing part | Description | `res.tolerance` (`1%`) | — |

A resistor with no power floor from any source is searched with none, and on
Accept the chosen part's own rating is written to `Power:Min` (for example
`100mW`). A floor typed in the dialog becomes the remembered `res.power_min`.

Inductors:

| Field | 1 | 2 | 3 | 4 | 5 |
|-------|---|---|---|---|---|
| Inductance | `Value` | existing part | Description | — | — |
| Package | the `Footprint`'s chip size | `Package:Preferred` | the Value's size suffix (`L_10U_0805`) | existing part, then Description | `ind.package` (no built-in value; never for a symbol with a footprint) |
| Current:Min | `Current:Min` | existing part's saturation current | Description (`Inductor 10 uH, 1A`) | `ind.current_min` (no built-in value) | `any`: no limit |
| Shielding | `Shielding:Preferred` | existing part | Description | `ind.shielding` (`Shielded`) | — |
| Tolerance | the loosest `Tolerance:Preferred` | existing part | Description | `ind.tolerance` (`20%`) | — |

`Current:Min` is compared with the saturation current (Isat). A part whose
supplier lists no saturation current (most chip inductors show `-` at JLCPCB
and DigiKey) is compared on its rated current instead. As with a resistor's
power floor, a group with no current floor is searched with none, and the
chosen part's own current is written.

Shielding is `Shielded`, `Semi-Shielded` or `Unshielded`, read from DigiKey's
`Shielding` and JLCPCB's `Type` (`Magnetic Shielded Inductor`). A stated class
accepts the better-shielded ones: `Semi-Shielded` also accepts `Shielded`,
`Unshielded` accepts all three. JLCPCB lists no shielding for most chip
inductors; such a hit is not rejected for it, and the DigiKey check decides.

An inductor's package is a chip size (`0805`) or, for a power inductor, a
body size in millimetres (`4x4mm`, longer side first). A power inductor's
footprint (`Inductor_SMD:L_Coilcraft_XxL4040`, `IND_XGL3520_COC`) does not give
the body size: the digits in such names are length and height, and the KiCad
fab outline is drawn larger than the body. So a footprint with no chip size
leaves the package to `Package:Preferred`, the existing part (DigiKey's
`Size / Dimension`, JLCPCB's `SMD,4x4mm`) or the Description, and the row is
`unassigned`, not `not searchable`: the dialog's Package field accepts a body
size. Two body sizes within 0.25 mm per side are the same size, since the
suppliers round one part differently. A candidate of the same body size need
not fit the footprint's pads; check the pad layout before accepting a part
from another series.

An inductor `Value` is read in plain form (`22uH`, `1u`, `4R7` is 4.7 µH) and
in library form (`L_10U_1A`, `L_4p7U_13p5A` with `p` as the decimal point, an
optional size suffix `L_10U_0805`). A current in the Value (`_1A`) is not
read; the Description's current is (`Inductor 4.7 uH, 13.5A`).

A symbol drawn with an `Inductor_SMD` footprint is a ferrite bead, not an
inductor, when its reference starts with `FB` or its Value or Description
mentions a ferrite or a bead.

`Value` is read in plain form (`100nF`, `0.1uF`, `4.7n`, `1u`) and in
library form: an optional `C_` prefix, the value with `p` as the decimal
point before a unit letter (`C_4p7U` is 4.7 µF, `C_P1U` is 0.1 µF), and an
optional size suffix (`C_22U_1210`). The Description is read for its first
capacitance, its voltages, every dielectric code and a standalone chip size
(`10uF, 25V, X5R, 805 package`); a generic Description such as `Unpolarized
capacitor, small symbol` provides none. A dielectric means that dielectric or
a better one, from any source (stated on the schematic, the default, the
existing part or the Description): X5R, X6S and X7S also accept X7R and X8R,
X7R accepts X8R, and C0G and NP0 accept each other. X6S and X7S are not
upgrades of X5R, since their change over temperature is looser (±22 %
against ±15 %). The upgrades apply when the parts database and the
suppliers are searched; the field shows, and Save writes, the codes as read.
An inductor's shielding works the same way: Semi-Shielded accepts
Shielded.

A resistor `Value` is read in plain form (`10K`, `4k7`, `4R7`, `1`, `10M` is
10 MΩ) and in library form (`R_10K`, `R_49p9K` with `p` as the decimal point,
an optional size suffix `R_10K_805`). Its Description is read for a
resistance, a chip size, a tolerance and a wattage (`10K, 603, 1%`,
`10 kOhms 1/4W ±5% 0805`).

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
value and package agree with the `Value`, `Footprint`,
`Package:Preferred` and the Value's size suffix, and with the first fitting
option. Every fitting option is a `current` candidate. The requirement's part
fields come from all fitting options, loosest wins: the lowest voltage or
power rating, the widest tolerance and every dielectric. An option that does not fit
is not used and adds a warning (`Footprint says 0402; current part
CGA3E2X7R1E104K080AA (DigiKey) says 0603 (current part CGA3E2X7R1E104K080AA
(DigiKey) not used)`); when none fits, the search runs from the schematic values alone.

**Conflicts.** When two schematic sources (`Value`, `Footprint`,
`Package:Preferred`, the Value's size suffix) state a value that differs
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
  Package, Voltage:Min (Power:Min for a resistor, Current:Min for an
  inductor), Dielectric (Shielding for an inductor; neither for a resistor)
  and Tolerance fields, each labelled with its source. A field stated on the schematic is read-only (change it on the
  schematic, or with **Edit Electrical Parameters** on the Intents tab), and
  Package is fixed when the symbol has a footprint with a chip size. Editing a field reruns the
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
  or on **Search suppliers**, JLCPCB is searched the way its parts page
  filters: the value as the keyword (`4.7uF`, `10kΩ`, `10uH`), with the part
  type's category (MLCC, chip resistor), the package and in-stock as filters,
  every result page. A chip inductor is searched in both JLCPCB inductor
  categories (`Inductors (SMD)` and `Power Inductors`); a body size is
  searched in `Power Inductors` without a package filter, and each hit's
  package is compared after the search. (A longer keyword such as `4.7uF 0603 X7R` is ranked by loose
  text relevance, and most hits are then other values.) Hits from a
  manufacturer not in the `cap.manufacturers` / `res.manufacturers` /
  `ind.manufacturers` setting are dropped, each entry matched as text within
  the supplier's manufacturer name. The built-in capacitor list is Murata,
  Samsung, Yageo, TDK, Taiyo Yuden, KEMET, Vishay, Wurth, Kyocera and AVX; the
  resistor list is Yageo, Vishay, Panasonic, Stackpole, Rohm, TE Connectivity
  and Bourns; the inductor list is Coilcraft, Murata, TDK, Taiyo Yuden,
  Bourns, Wurth, Vishay, Sunlord, Samsung, Eaton, Pulse and Abracon. Each maker
  is stocked at both suppliers; UNI-ROYAL, which supplies most JLCPCB Basic
  resistors, is not sold by DigiKey. Each remaining hit's attributes are checked
  against the requirement, and the best hits are confirmed at DigiKey: same
  MPN, an active product, and parameters that also meet the requirement. The supplier search
  needs `DIGIKEY_CLIENT_ID` and `DIGIKEY_CLIENT_SECRET`; without them the
  dialog lists the current part and the database matches only and says why.
  Candidates are ranked with the current part first, then by recorded uses,
  then JLCPCB Basic, then JLCPCB preferred, then stock at each supplier
  covering the build, then price. The status line counts the candidates from
  each source, the search hits checked, the hits rejected, and the hits past
  the DigiKey lookup limit (10).
- **Stock floor.** A search hit needs a known stock of at least 1000, and at
  least the units the build needs, at both JLCPCB and DigiKey
  (`partsearch.MIN_STOCK`); below that it is rejected with the reason, for
  example `JLCPCB stock 33, need at least 1000` or `DigiKey stock unknown`.
  The current part and stored parts below the floor stay selectable with
  that reason as a warning: their stock cells are amber, and selecting one
  shows the warning above the buttons. When no listed part meets the floor
  the suppliers are searched too. A part JLCPCB marks unbuyable, or DigiKey
  lists as other than Active, is rejected.
- **Units needed** is the group's symbol count times **Boards:** on the LCSC
  tab (25 when the Cockpit opens, `substitute.DEFAULT_BUILD_QTY`).
- When no part meets a tolerance limit that came from a default, the search
  runs again without it. An amber line under the status line names the limit
  that was dropped, and on Accept the chosen part's own tolerance is written to
  `Tolerance:Preferred`; the remembered `cap.tolerance.*` / `res.tolerance` /
  `ind.tolerance` default is left as it was. A tolerance stated on the schematic or edited in the dialog is always
  applied.
- **Clearing a field.** Empty the Voltage:Min / Power:Min / Current:Min,
  Dielectric / Shielding or Tolerance field (or type `any`) to search without a limit on it; its note
  reads `any`. A capacitor field that had no value must be given one or
  `any`, so a voltage floor is never left out by accident. On Accept the chosen part's own value is written
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
  `Power:Min` or `Current:Min`, `Class:Preferred` (capacitors) or
  `Shielding:Preferred` (inductors), and `Tolerance:Preferred` when
  the value came from a default, an edit, a cleared field, the existing part
  or the Description (in the form `16V`, `100mW`, `1.5A`, `X7R, X5R`,
  `Shielded`, `10%`); `Value`
  when the symbol has none and the value came from the existing part or the
  Description; `Package:Preferred` when the package was edited and the symbol
  has no footprint, and for an inductor body size not stated on the
  schematic; `Footprint` (`Capacitor_SMD:C_0603_1608Metric`,
  `Resistor_SMD:R_0603_1608Metric`, `Inductor_SMD:L_0603_1608Metric` and the
  other KiCad standard chip sizes 0402–1210) when the symbol has none and the
  package is a chip size; and `Intent:Type` = `capacitor` / `resistor` /
  `inductor` when the type was guessed. No footprint is written for a body
  size: an inductor with none stays `unassigned`, and its tooltip says to add
  one in KiCad. A value stated on the schematic is not overwritten. After
  the write the group reads as `assigned`, and the stated values give the same
  requirement on the next open. Accepting the current part writes its
  canonical numbers under the canonical keys, which also corrects swapped
  `MPN` / `DigiKeyPN` fields and fills an empty `LCSC`.

#### Autopopulate

**Autopopulate…** above the Parts table offers the parts already in the
parts database for every `unassigned` row, without asking a supplier (stock
and price are not checked). Capacitors, resistors and inductors are looked
up with their requirement (rating at least, class or better, tolerance at
most), ICs by their MPN field and Value tokens, through-hole parts by their
footprint signature. The dialog has an **Accept** check box in its first
column, coloured by the selected match, then the Parts tab's columns, then a
**Match** drop-down of the stored parts (most uses first):

- green (exact): the part the symbol already names, an IC named exactly, or
  a part whose rating, class and tolerance equal the stated ones;
- orange (near): it meets the stated requirement but exceeds a field (25V
  for 16V, X7R for X5R, 10% for 20%), or a part-named through-hole part
  stored for the same footprint and Value;
- red (less sure): the match rests on a default or unset field (rating,
  class or tolerance not stated on the schematic, by the part or the
  Description), or a header, socket or terminal block found by footprint
  alone.

Click the Accept header to sort by tier (exact first), the Match header to
sort by the selected MPN. **Check green** checks the exact matches, **Check all** every row, **Clear**
none. **Accept checked** queues each checked row as the assign dialog's
Accept does (write plan and usage record) and **File → Save** writes them.

#### ICs

A part is an IC when its intent type is `ic`: `Intent:Type = ic`, or guessed
from a `U` reference. Modules (an Arduino Nano, a radio module) are ICs here
too. An IC is found from the part number its symbol already carries, not from
values, so nothing in its dialog is editable.

- **Identifiers.** The `MPN`, `DigiKeyPN` and `LCSC` fields (any spelling),
  and MPN-shaped tokens in the Value: the Value is split on spaces, `_` and
  `,`, and a token of at least 5 characters with a letter and a digit is kept
  (`MotorControl_STM32L552CCT6_V2` gives `STM32L552CCT6`). Symbols with the
  same identifiers and footprint share a row.
- **Row.** Value is the `MPN` field, else the Value. Package is the chosen
  part's DigiKey package, else the package read from the footprint name,
  marked `(footprint)`. Rating, Class and Tolerance are empty. Status is
  `not searchable` without an identifier, `conflict` after a
  search found identifiers that name different parts, `assigned` when every
  symbol has `MPN`, `Manufacturer`, `DigiKeyPN` and a `Footprint` (an `LCSC`
  number is not required), and otherwise as for capacitors. The Footprint
  cell is red when the footprint does not match the part, amber when it
  could not be checked, from the chosen part or the part the last search
  found.
- **Search.** Double-click the row to open the **Assign IC** dialog; an
  `assigned` row opens too, to show its footprint check. The dialog looks up
  each identifier at DigiKey and JLCPCB by exact part number, then accepted
  ICs in the parts database with the same MPN. When nothing is found, each
  partial MPN (the `MPN` field, then the Value tokens, longest first) is
  searched at DigiKey by keyword, keeping only parts whose MPN starts with
  it. A part with no JLCPCB listing is searched at JLCPCB by its exact MPN.
  **Search again** reruns the search. The search needs
  `DIGIKEY_CLIENT_ID` and `DIGIKEY_CLIENT_SECRET`: without DigiKey no IC
  passes.
- **Checks.** A candidate must be listed at DigiKey with status `Active`, a
  DigiKey stock of at least 10 × the units the build needs, and not be
  rejected in the parts database. Stock below the floor (the units needed +
  1000) is accepted with a warning: the stock cell is amber, the warning is
  shown above Accept, and the part ranks after parts that meet the floor.
  JLCPCB is optional: a part JLCPCB does not list, cannot sell, or stocks
  below 10 × the units needed is still a candidate, shown as `not at JLCPCB`
  with the reason, and its LCSC number is not written; JLCPCB stock between
  that and the floor gets the same warning. When no part passes, the status
  line says why in one line, for example `no part passed: C165154
  DRV8313RHHR DigiKey stock 164, need at least 500`.
- **Conflicts.** Identifiers that name different parts (a Value of
  `LTC6992CS6-2` beside a DigiKey PN for `LTC6992HS6-2`), or one that names
  no part beside one that does, are listed in amber. Each part is still a
  candidate; pick one.
- **Footprint check.** The footprint is compared with the part's DigiKey
  package: the package, pin count, body size and exposed pad read from the
  footprint name, and the footprint's pad count (from the board, else from
  the footprint library through the board's `fp-lib-table` or
  `Libraries/kicad-footprints`). An exposed pad counts as a pad. The result
  is a match; a mismatch (red), with what disagrees, for example `20 pads vs
  20 pins + exposed pad`; or cannot verify (amber), when there is nothing to
  compare or the footprint states a pitch DigiKey does not. DigiKey states no
  pitch, so a QFN, DFN, TSSOP, MSOP or SSOP footprint that names its pitch is
  always "cannot verify". The message for the selected candidate is shown
  above **Accept**. The check ranks candidates; it never removes one, and the
  footprint is never changed. A symbol with no footprint is searched too
  (LabBoard's `L7805`): every candidate reads cannot verify, `the symbol has
  no footprint`, and the row reads as `assigned` once a footprint is added
  in KiCad.
- **Columns.** MPN · Manufacturer · Source · DigiKey PN · DigiKey stock ·
  LCSC · JLC library · JLC stock · Package · Footprint · Unit price. Source
  is `database`, `current part` (an identifier names it exactly, with the
  fields that name it) or `partial MPN`. Candidates are ranked database
  first, then the current part, then the footprint check (match, cannot
  verify, mismatch), then at JLCPCB, then JLCPCB Basic, then DigiKey stock,
  then price.
- Right-click a candidate and choose **Reject** to record it as rejected.
  **Accept** records the part in the parts database and **File → Save**
  writes, on every symbol in the group: `MPN`, `Manufacturer`, `DigiKeyPN`;
  `LCSC` when the part is at JLCPCB, otherwise an existing `LCSC` is
  removed; and `Intent:Type = ic` when the type was guessed. Other spellings
  of the four part-number fields are removed. `Value`, `Footprint`,
  `Datasheet` and `Reference` are never written, and no settings are stored.

#### Through-hole parts

Through-hole parts are soldered by hand, so they are searched at DigiKey only
and get no LCSC number. A part gets a through-hole row when it is not a
capacitor, resistor, inductor or IC (those have their own rows) and its
footprint is through-hole (see the export section for how mounting is read).
Parts with the same footprint, Value and part numbers share a row. The
design is in `THT_PLAN.md`.

- **Connectors with no footprint.** A connector symbol with no footprint
  gets a through-hole row searched as the KiCad footprint its text implies
  (`impliedfootprint.py`). The Footprint cell shows it, marked `(from value)`
  or `(default)`:
  - KiCad's generic connector symbols (Description `Generic connector,
    single row, 01x04`): a 2.54 mm vertical pin header with those rows and
    positions (`PinHeader_1x04_P2.54mm_Vertical`) `(default)`;
  - a JST series and position count in the Value (`TMP235 (JST PH 3)`):
    the top-entry part, `JST_PH_B3B-PH-K_1x03_P2.00mm_Vertical`; JST XH the
    same `(from value)`. JST SH, and Stemma QT / Qwiic (JST SH, 4
    positions), imply the side-entry SMD part, which is a surface-mount
    connector row (see below), not a through-hole one;
  - a barrel jack (`DC Barrel Jack`, `DC in`, a `BarrelJack*` footprint
    filter): CUI `PJ-102AH` (2.1 mm) `(default)`.

  On Accept + Save, the implied footprint is written to symbols with no
  footprint when the chosen part fits it: any part the header or socket
  search found, or a part whose MPN starts with the footprint's part number
  (`B3B-PH-K-S` on `JST_PH_B3B-PH-K`). Otherwise the row keeps its part
  numbers and needs a footprint added in KiCad. A through-hole row reads as
  `assigned` only with a footprint.
- **Row.** Value is the Value. Package is the footprint signature read from
  the footprint name: `header 1x04, 2.54 mm, vertical`, `named part JST
  S3B-PH-K`, `no part (solder wire pad)`. Class is `through-hole`; Rating and
  Tolerance are empty. Status is `no part` for a footprint with nothing to
  buy (solder-wire pads, mounting holes, fiducials, bare test pads),
  `not searchable` for a footprint family not searched yet (terminal blocks,
  loop test points, through-hole passives without an intent, footprints that
  name no part) unless the symbol has a `DigiKeyPN` or `MPN`, `assigned` when
  every symbol has `MPN`, `Manufacturer`, `DigiKeyPN` and a `Footprint`,
  else `unassigned`.
- **Search.** Double-click the row to open the **Assign through-hole part**
  dialog. A part stored in the parts database for the same footprint
  signature (and, except for headers, sockets and terminal blocks, the same
  Value) is checked at DigiKey and offered without a DigiKey search; **Search
  DigiKey** searches anyway. The symbol's `DigiKeyPN`, else its `MPN`, is
  looked up first and shown as the current part. Then:
  - pin headers and sockets are searched in DigiKey's header categories by
    positions, rows, pitch, orientation and contact type, with the header
    maker list (setting `hdr.manufacturers`, comma-separated; default
    Sullins, Samtec, Amphenol, Harwin, Wurth, TE, Molex). The first header or
    socket accepted sets the series for the rest of the run (until the
    schematic is opened again): later headers search that manufacturer, its
    contact finish and connector type, and rank the MPN the series predicts
    first;
  - a footprint that names a part (`JST_PH_S3B-PH-K…`, `CUI_PJ-002B`) is
    searched by that part number, or by a Value token that fits it.
- **Checks.** DigiKey status `Active` and stock of at least 10 × the units
  needed; below the units needed + 1000 with a warning. The footprint's pad
  count is compared with the part's positions: more pads than positions
  (mounting or shield pads) is shown as cannot verify, fewer as a mismatch.
  The check ranks candidates and never removes one.
- **Columns.** MPN · Manufacturer · Source · DigiKey PN · DigiKey stock ·
  Pads · Unit price · Description. Source is `database`, `current part`,
  `series`, `maker list`, or the text that found the part.
- Right-click a candidate to reject it. **Accept** records the part in the
  parts database under its footprint signature and **File → Save** writes
  `MPN`, `Manufacturer` and `DigiKeyPN` on every symbol in the group, and
  removes `LCSC` and other spellings of the part-number fields. `Value` and
  `Footprint` are never written.
- The status bar shows the DigiKey call count for the session and the calls
  left today (DigiKey allows 1000 a day).

#### Surface-mount connectors

A connector whose footprint is surface mount and names a part
(`Connector_JST:JST_SH_SM04B-SRSS-TB_…`), or that has an `MPN`, `DigiKeyPN`
or `LCSC` field, is searched as an IC is: by part number at DigiKey and
JLCPCB, and the chosen part gets an `LCSC` number when JLCPCB stocks it
(`smdconnector.py`). A connector with no footprint whose implied footprint
is surface mount (Stemma QT, JST SH) takes the same path, and Save writes
that footprint when the part's MPN starts with its part number. The
identifiers are the part-number fields and the footprint's part number; the
Value is not read for part numbers, since on a connector it names what
plugs in (`Stemma QT (LSM303AGR)` names a sensor). The row and dialog are
the IC ones. DigiKey states no package for most connectors, so the
footprint check reads cannot verify. LabBoard J3 finds `SM04B-SRSS-TB`,
LCSC C160404.

A JLCPCB listing whose MPN adds bracketed groups to the part's
(`SM04B-SRSS-TB(LF)(SN)`, JST's plating codes) is the same part, for every
search: an orderable listing comes first, then an exact MPN. The exact
`SM04B-SRSS-TB` listings at JLCPCB are zero-stock placeholders.

#### Parts with no search

Every BOM part no other row takes is listed, so the gaps on a board are
visible (`othergroup.py`). Parts with the same intent type, Value, Footprint
and part numbers share a row. Package shows the intent type (`diode`,
`transistor`, `switch`, `unknown`), and the tooltip gives the reason and the
Description. Status:

- `no part`: nothing to buy, read from the footprint (mounting hole,
  fiducial, bare pad) or, with no footprint, from the symbol's footprint
  filters (`SolderJumper*`, `MountingHole*`);
- `no footprint`: a connector whose text implies no footprint;
- `not searchable`: a connector whose footprint is neither on the board nor
  in the libraries, so its mounting is unknown;
- `type not supported yet`: a type with no assignment path (diodes,
  transistors, switches, potentiometers, jumpers, test points, unknown
  types), or a connector on a surface-mount footprint that names no part
  (an SMD pin header);
- `assigned` when the symbols already have `MPN`, `Manufacturer` and
  `DigiKeyPN`.

Double-clicking such a row shows the reason in the status bar.

#### Parts database

The parts database is a SQLite file, `parts.sqlite` at the root of the
Boards checkout, wherever that checkout is. KicadTools finds the root by
walking up from its own location to the folder holding `.gitmodules` and
`Libraries/`; set `KICADTOOLS_REPO_ROOT` to name another root, or
`KICADTOOLS_PARTS_DB` to use another file. The same root supplies the
standard footprint libraries (`Libraries/kicad-footprints`) for a board
copied outside the repo, and board paths in the usage table are recorded
relative to it. It can instead be the MariaDB
database on a server, which every machine reads and writes (see **Parts
database on a server** below). It holds each accepted or rejected
part with its type, its parameters (capacitors: capacitance, voltage rating,
dielectric, tolerance; resistors: resistance, power rating, tolerance;
inductors: inductance, saturation current, shielding, tolerance; ICs:
DigiKey's Package / Case and pin count; through-hole parts: the footprint
signature it was accepted for, and the Value for families other than headers,
sockets and terminal blocks), its package (a chip size, or an
inductor's body size such as `4x4mm`), its DigiKey and LCSC part numbers, and the board references that
place it. Stock
and price are not stored. The file is listed in `Boards/.gitignore`, so each
machine has its own copy. The schema is version 2; a version-1 file is
updated in place when it is opened (the `ic` table is added). The
`inductor` and `tht` tables were added within version 2 and are created when
a file without them is opened.

The `settings` table holds the remembered defaults under the `cap.*` and
`res.*` keys in the tables above. A key absent from the table takes the built-in value shown in
parentheses. Accepting a part overwrites the keys of the fields that came from
a default or an edit, so the next group starts from the values last used.

#### Parts database on a server

The design is in `DB_PLAN.md`. With `~/.kicadtools/config.toml` naming a
server, the Cockpit and `kicadtools cpl` use the parts database there, and
every change is written to it when it is made:

```toml
[parts_db]
backend = "mariadb"
database = "tadtruex_inventory"
user = "tadtruex_inventory"

[parts_db.ssh]
host = "10.0.10.13"   # broadpeak
user = "tad"
# local_port = 3307   # a tunnel you run yourself; omit to let the Cockpit open one
```

- **Tunnel.** MariaDB on the server listens on `127.0.0.1` only, so the
  Cockpit opens an SSH tunnel (`ssh -N -L`) on a free local port when
  it starts and closes it on quit. With an SSH key on the server there is no
  prompt; without one, ssh asks for the SSH password in a dialog.
- **Password.** `~/.kicadtools/db_password` (one line, mode `0600`, in no
  repository); `KICADTOOLS_DB_PASS` overrides it. When the file is missing,
  or the server refuses it, the Cockpit asks and writes the file.
- **Offline.** When the server cannot be reached (or the password prompt is
  cancelled), the newest backup in `~/.kicadtools/backups/` is opened
  read-only. A red bar across the window says `OFFLINE`, with the copy's date
  and the reason, and holds **Retry connection**; the title ends
  `[OFFLINE]`; Accept, Reject, Autopopulate and rotation changes are off.
  A connection lost during a session switches to the same state. With no
  backup, stored parts cannot be searched.
- **Backups.** Each start on the server writes
  `~/.kicadtools/backups/parts-<date>-<time>.sqlite` (the newest 10 are
  kept); the newest is the offline copy. `kicadtools db pull [PATH]` writes
  one on demand.
- **Status bar.** The right-hand label names the database (`parts DB:
  tadtruex_inventory (server)`).
- **Rotations.** The CPL rotation overrides are in the server's
  `part_rotation` table; the LCSC tab and `kicadtools cpl` read and write
  them there. `kicadtools cpl` fails, rather than use an old copy, when the
  server cannot be reached.
- **First use.** `kicadtools db import --dry-run`, then `kicadtools db
  import`, copies the local `parts.sqlite` and the cached rotation overrides
  into the empty server once, keeping part ids, compares every table, and
  renames the local file `parts.sqlite.imported`.
- **Moving to another server.** `kicadtools db pull FILE` on a machine that
  reaches the old server, then `kicadtools db import --from FILE` against the
  new, empty one. The rotation overrides in the file are imported, and a
  file named with `--from` is not renamed. A SQL dump of the old database,
  loaded with the `mariadb` client, works as well; `DB_PLAN.md` ("Move to the
  self-hosted server") has the steps.

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
- `src/kicadtools/mounting.py` — SMD/THT mounting per part, from the board's footprint or the library `.kicad_mod`
- `src/kicadtools/thtfootprint.py` — footprint signatures from through-hole footprint names (`THT_PLAN.md`)
- `src/kicadtools/dkfilters.py` — DigiKey category filter options, value names to filter IDs
- `src/kicadtools/headersearch.py` — pin header and socket search at DigiKey, with one series per run
- `src/kicadtools/namedsearch.py` — DigiKey search for through-hole parts whose footprint names the part
- `src/kicadtools/thtdb.py` — through-hole parts in the parts database, found by footprint signature
- `src/kicadtools/thtassign.py` — through-hole groups for the Parts tab, their search and write plan
- `src/kicadtools/autopopulate.py` — parts-database matches for the open Parts-tab rows, graded exact, near or less sure
- `src/kicadtools/cpl.py` — JLCPCB CPL centroid and rotation math (`cpl`)
- `src/kicadtools/rotations.py` — per-footprint-family CPL rotation offsets
- `src/kicadtools/partrotation.py` — per-LCSC CPL rotation overrides stored in SQLite
- `src/kicadtools/naturalsort.py` — natural-sort key (R2 before R10) and the table cell sort key (numbers by value)
- `src/kicadtools/substitute.py` — parametric substitute search: spec, matching rules, ranking, `Alternates` merge
- `src/kicadtools/units.py` — parse and format resistance, capacitance, voltage, tolerance and power; package and dielectric codes
- `src/kicadtools/partsdb.py` — the SQLite parts database: accepted/rejected parts, supplier PNs, usage, settings
- `src/kicadtools/parttypes.py` — what differs between capacitors, resistors and inductors: value and rating formats, property names, the class field (dielectric, shielding), packages (chip and body sizes), supplier parameter names, JLCPCB categories, footprints
- `src/kicadtools/requirement.py` — capacitor, resistor and inductor requirements (value + source per field), defaults, grouping
- `src/kicadtools/partsearch.py` — candidates: existing part, database, then filtered JLCPCB search confirmed at DigiKey
- `src/kicadtools/assign.py` — the property write plan that assigns a chosen part to a group
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
  - `tht_assign_dialog.py` — the "Assign through-hole part" dialog
  - `autopopulate_dialog.py` — the Autopopulate dialog
  - `sorting.py` — sortable dialog tables: sort items and the view-row to data-row map
