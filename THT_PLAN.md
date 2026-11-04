# Plan: through-hole part assignment

Goal: give every through-hole part on a schematic that has no DigiKey PN a
chosen DigiKey part (MPN, manufacturer, DigiKey PN), found from its footprint
plus its Value, intent or Description. Through-hole parts are soldered by hand,
so they need no LCSC number; the JLCPCB BOM and CPL leave them out when the
SMD-only choice is taken.

This is a fourth search path on the Parts tab, beside capacitors, resistors and
inductors (`CAPACITOR_PLAN.md`) and ICs (`IC_PLAN.md`). It searches DigiKey
only.

## Decisions (2026-11-03)

| Topic | Decision |
|---|---|
| Scope | Parts whose footprint is through-hole and that have no DigiKey PN. |
| Through-hole test | The footprint's `(attr through_hole)`, from the board's `.kicad_pcb`, or from the library `.kicad_mod` when the board has no copy. Pad types are counted as a cross-check (step 1). |
| Supplier | DigiKey only. No LCSC number is needed or written. |
| JLCPCB export | The SMD-only choices stay as they are (the GUI's "SMD parts only?" question and the CLI's `--smd` flag; DigiKey's three-way filter). Revised 2026-11-03: the first plan made SMD-only fixed, which the user rejected as too strong. The exports do use the new per-part mounting (step 1). |
| Headers | One series per run. The first header group searched in a run uses a manufacturer-filtered search, and the part the user picks sets the series. Every later header group in that run searches that series first (step 5). The series is not stored between runs. |
| Pads with no part | Solder-wire pads, mounting holes, fiducials and bare test pads get a "no part" status instead of a search. Loop test points (BuckConverter `36-5001-ND`) are real parts. |

## Evidence from the boards

Survey of 15 board schematics (`scripts/tht_survey.py`, 2026-11-03):

| Tier | Parts | Contents |
|---|---|---|
| Have a DigiKey PN | 58 | — |
| Generic connectors | 10 | All 2.54 mm pin headers: 1×01, 1×04 (×2, BarrelJackAdapter J2, J4), 1×06 horizontal (MiDi J2), 1×07 (×3), 1×08, 2×12, 2×17 |
| Footprint names a part | 4 | JST `S3B-PH-K` (Breakout J101), CUI `PJ-002B` (Breakout J102), DIN-5 MIDI (MiDi J1), Rohm `BD33FC0WEFJ-E2` (Breakout U6; an SMD part whose footprint declares `through_hole`) |
| No part | 22 | 13 solder-wire pads (SX1509), 4 mounting holes (WiringHarness), 5 bare test pads |
| Passives with intent | 0 | The radial and film capacitors all have DigiKey PNs |

Not covered: BlackBox (Eagle import, 32 parts, no library footprint resolves)
and NeoPMOD (17 parts without a found footprint, 8 without any footprint).

## DigiKey API (tested 2026-11-03)

- `products/v4/search/keyword` with `FilterOptionsRequest`:
  - `CategoryFilter: [{"Id": "<leaf id>"}]` limits the search to one leaf
    category and returns that category's `ParametricFilters`, each with
    `ParameterId` and the `ValueId` of every value.
  - `ParameterFilterRequest: {"CategoryFilter": {"Id": …}, "ParameterFilters":
    [{"ParameterId": …, "FilterValues": [{"Id": …}]}]}` filters on those IDs.
  - `ManufacturerFilter`, `StatusFilter` (0 = Active) and
    `MinimumQuantityAvailable` work; `SortOptions {"Field": "Price"}` sorts.
  - One call takes about 2 s.
- Keywords must be short. "pin header 1x04 2.54mm through hole vertical" and
  "100uF electrolytic capacitor radial 8mm 2.5mm" return nothing; "header",
  "capacitor" and "terminal block" with filters return the expected parts.
- The product's `Category` is the root (`Connectors, Interconnects`, 20); the
  leaf is the last `ChildCategories` entry.
- Leaf categories and parameter IDs seen:

  | Leaf category | Id | Parameters used |
  |---|---|---|
  | Headers, Male Pins | 314 | Number of Positions 88, Number of Rows 90, Pitch – Mating 1790, Mounting Type 69, Contact Type 512, Connector Type 28, Contact Finish – Mating 1791 |
  | Aluminum Electrolytic Capacitors | 58 | Capacitance 2049, Voltage – Rated 2079, Lead Spacing 508, Size / Dimension 46, Height – Seated (Max) 1500, Mounting Type 69 |
  | Wire to Board (terminal blocks) | 371 | Positions Per Level 1075, Number of Levels 2168, Pitch 89, Mounting Type 69 |

- Results (in stock ≥ 100, active, sorted by price): 1×4 2.54 mm male
  through-hole header, 448 hits (213 with a seven-maker filter, led by Sullins
  `PRPC004SAAN-RC`); 100 µF, 2.50 mm spacing, 8 mm diameter, 8 hits; 2-position
  5.00 mm terminal block, 283 hits.
- Quirks: pitch and size values are text (`0.100" (2.54mm)`), matched on the
  mm figure. Some value IDs are small numbers that repeat across parameters
  (`"1"` is both "-" and "1 level"); one filter set that included Number of
  Levels returned nothing. DigiKey's `Series` field is "-" for Sullins headers,
  so it does not identify a series.

## Build steps

### Step 1 — mounting per part

A `mounting_for(record)` that reads the board's `attr`, falls back to the
library `.kicad_mod` (through `FootprintPads.footprint_file`), and counts
`thru_hole` and `smd` pads. When the `attr` and the pads disagree (Rohm U6),
the pads decide and the row shows a note. The JLCPCB export and the Parts tab
use the same function.

### Step 2 — dropped

Was: make the JLCPCB export SMD-only and remove the choice. The user decided
(2026-11-03) to keep the choices as they are. The exports switched to step 1's
mounting instead.

### Step 1 — as built (2026-11-03)

`src/kicadtools/mounting.py`: `decide(attr, pads, source)`, `BoardMounting`
(`mounting(ref, lib_id)`, `mounting_for(record)`, `for_schematic`),
`mounting_for_records` and `mounting_kinds`. `pcb.Pad` gained `kind` (the pad
type token) and `pcb.Placement` gained `attr`; `FootprintPads` gained
`placed()` and `library_footprint()` (`footprints.LibraryFootprint`,
`parse_kicad_mod`). Tests: `tests/test_mounting.py`.

Differences from the plan, from a survey of all boards:

- The pads decide only when the plated pads are all one type. Mixed pads
  (USB connectors on AtTinyDemo J4 and MotorDriver J1: `attr smd`, SMD signal
  pads, plated shield tabs) follow the `attr`; with no `attr` they are
  `unknown`. `np_thru_hole` and `connect` pads are not counted.
- KiCad's standard through-hole library footprints have no `attr`; only the
  board copy has one. Before layout the pads classify them.
- The board copy is used even when its library id differs from the
  schematic's Footprint field, with a note. 15 parts on Breakout and SX1509
  have KiCad 5 names in the schematic (`LEDs:LED_0603`) and the renamed
  footprint on the board. The pad-count lookup for ICs still skips such a
  board copy.
- The JLCPCB BOM, DigiKey BOM and CPL exports (CLI and GUI) read mounting
  through `mounting_kinds`. Across the 15 boards: 65 parts went from
  undetermined to SMD, 28 to through-hole, Breakout U6 from through-hole to
  SMD, none lost a mounting. 39 stay undetermined: footprint libraries not in
  the project, blank Footprint fields (ECG 7, NeoPMOD 8), and the Tag-Connect
  footprint (no plated pads, no `attr`).
- Without a `.kicad_pcb` the exports now read the library footprints; the
  CLI warning says so instead of "the SMD BOM will be empty".
- Not done in step 1: the Parts tab does not show mounting yet (step 9).

### Step 3 — footprint signatures (`thtfootprint.py`)

Parse KiCad footprint names into a signature:

- `Connector_PinHeader_*` / `Connector_PinSocket_*`: kind (header, socket),
  rows, positions, pitch, orientation (Vertical, Horizontal).
- `TerminalBlock_*`: positions, pitch.
- `Capacitor_THT` / `Resistor_THT`: diameter or L×W, lead spacing (this also
  settles `CAPACITOR_PLAN.md` TBD 2).
- A footprint that names a part (`JST_PH_S3B-PH-K…`, `CUI_PJ-002B`): the MPN
  token.
- No-part footprints: `SolderWirePad`, `MountingHole_*`, `Fiducial`,
  `TestPoint_THTPad`, `TestPoint_Pad`.

### Step 3 — as built (2026-11-03)

`src/kicadtools/thtfootprint.py`: `parse_signature(lib_id, pad_count=None)`
→ `FootprintSignature` (family, rows, per_row, `positions`, pitch, orientation,
smd variant, passive style, polarized, diameter/length/width/height, lead
spacings, maker, mpn, and `what` for no-part footprints). Tests:
`tests/test_thtfootprint.py`. Pure, no I/O.

- Families: header, socket, terminal block, capacitor, resistor, named part,
  test point, no part, other.
- No part, added beyond the plan's list: solder jumpers, net ties, KiCad's
  `Connector_Wire` `SolderWire-…` pads, `TestPoint_Plated_Hole`,
  `TestPoint_Bridge`. Loop test points (`TestPoint_Loop_…`) are family
  "test point", a real part with no number; Keystone test points are named
  parts.
- Named part: a maker token from a fixed list followed by a token with a digit
  or hyphen (`JST_PH_S3B-PH-K` → JST, `S3B-PH-K`), or a name of at most two
  tokens whose last token is a part number (`691137710002`,
  `SW_SLW-118562-4A-N-D`). `MountingHole:HARWIN_H9051-01` is a named part, not
  a hole. `Bourns_3386P` gives `3386P`, a series prefix; step 6's prefix
  match handles it.
- Terminal blocks whose name has no `1xNN` (`Terminal_Block_D1.3mm_P5mm` on the
  boards, Dinkle, old `bornier-N` names) take positions from `pad_count`.
- Checked on all 39 through-hole footprint names on the boards and on every
  footprint in KiCad's header, socket, terminal block, `Capacitor_THT`,
  `Resistor_THT`, test point, mounting hole, fiducial, wire, net tie and
  jumper libraries. Gaps: SIP resistor arrays give no pitch; `Connector_Pin`
  press pins, tactile switches, crystals and the unnamed barrel jack are
  "other".
- Not done: the exports still list solder jumpers and net ties as SMD parts.

### Step 4 — DigiKey filtered search (`suppliers.py`)

`DigiKeyClient.category_search(keyword, category_id, filters, *,
manufacturers=(), min_stock, limit)`: one call returns the hits and the
category's filter options. Value names are mapped to IDs from those options,
which are cached per category for the session.

### Step 5 — header search and the run's series

1. The first header group in a run searches category 314 (or the socket
   category) with the signature's filters and the maker list (setting
   `hdr.manufacturers`, built-in list: Sullins, Samtec, Amphenol, Harwin,
   Wurth, TE, Molex). The user picks one.
2. The pick sets the run's series: its manufacturer, its non-size parameters
   (Contact Finish – Mating, Connector Type, Contact Length – Mating and
   – Post) and an MPN pattern with the position digits replaced
   (`PRPC004SAAN-RC` → `PRPC0??SAAN-RC`).
3. Each later header group searches with its own signature plus the series'
   manufacturer and parameters, ranks hits whose MPN fits the pattern first,
   and shows the top one as the proposed part. The user can still pick
   another, which does not change the series.

### Step 6 — part-named footprints

Search the MPN token from the footprint (or the Value) with the IC search
(`icsearch.py`): same prefix match, same pad-count check.

### Step 7 — through-hole passives

Value and intent from the existing requirement, with the footprint signature
as the package: lead spacing must match, diameter or body size must not
exceed the footprint's. DigiKey only, category 58 for aluminum electrolytics,
film and ceramic categories to be found.

### Step 8 — parts database

Accepted through-hole parts are stored with their footprint signature, so a
later board with the same signature finds the stored part first.

### Step 9 — Cockpit

Through-hole rows on the Parts tab: class "THT", status "no part" for pads,
"needs part" otherwise, an assign dialog for the header and passive paths.
The header dialog shows the run's series once it is set.

### Step 10 — live check

BarrelJackAdapter J2/J4, SX1509 J5/J7, MiDi J1/J2, Breakout J101/J102/J5.

## TBD

1. Female sockets, IDC and shrouded headers: their categories and parameters
   are not checked yet.
2. Mating parts (JST housings and crimp contacts) are not assigned.
3. BlackBox and NeoPMOD footprints are not resolved (see "Evidence").
4. Whether a series picked in one run is offered as the default for the next.
