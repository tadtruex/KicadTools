# Plan: IC part assignment

Goal: give every IC on a schematic one chosen part (MPN, manufacturer,
DigiKey PN and, when JLCPCB has it, an LCSC number), found from the part
number the symbol already carries. The footprint is checked against the
chosen part's package and flagged when it does not match or cannot be
verified; it is never changed.

This adds a third type to the Parts tab beside capacitors and resistors
(`CAPACITOR_PLAN.md`). The search is different: capacitors and resistors are
found from electrical values; an IC is found from its part number.

## Decisions (2026-10-30)

| Topic | Decision |
|---|---|
| Scope | Parts whose intent type is `ic`: `Intent:Type = ic`, or guessed as `ic` (`classify.py` already guesses it from the `U` prefix). Modules (an Arduino Nano, a radio module) are ICs for this purpose and follow the same rules; most are DNP. Transistors, diodes and LEDs are not on this path: they will be searched by value and rating, like resistors and capacitors. |
| Identifier required | A symbol needs at least one of: `MPN`, `DigiKeyPN`, `LCSC`, or an MPN-shaped token in the Value (step 2). Without one it shows "not searchable". |
| Footprint required | Yes, but as a guide. It ranks candidates and is checked against the chosen part; it never rejects a candidate and is never written. |
| Suppliers | DigiKey required. JLCPCB optional: a part JLCPCB does not stock is accepted and shown as "not at JLCPCB". |
| Stock floor | Build quantity + 1000 at DigiKey; the same at JLCPCB when the part is there. A part below the floor is accepted with a warning down to 10 × the build quantity, and ranks after parts that meet the floor; below that it is rejected at DigiKey and not used at JLCPCB (decided 2026-11-01). (Capacitors and resistors use the larger of 1000 and the build quantity.) |
| Manufacturer list | None. The part number fixes the manufacturer. |
| Footprint check | Footprint name parsing plus the footprint's pad count compared with DigiKey's pin count. |

## Evidence from the boards

| Board | Ref | Value | Footprint | Part numbers |
|---|---|---|---|---|
| ECG | U2 | `LMC7101BYM5` | `SOT-23-5` | none |
| ECG | U1 | `AD8232ACPZ-R7` | `ECG_Footprints:CP_20_10_ADI_NOTH` | DigiKey |
| ECG | U3 | `ABX00027` (Arduino Nano) | `ECG_Footprints:DIP30-ArduinoNano` | none |
| MotorDriver | U1, U2 | `DRV8313RHHR` | `TexasInstruments:RHH0036C` | DigiKey, LCSC |
| MotorDriver | U9 | `MotorControl_STM32L552CCT6_V2` | `LQFP-48_7x7mm_P0.5mm` | DigiKey |
| MotorDriver | U4 | `LDL212PV33R` | `STMicro:LDL212_2x2mm_Pitch0.5mm` | DigiKey `497-19548-2-ND` |
| BarrelJackAdapter | U2 | `LDL212PV33R` | `DFN-6-1EP_2x2mm_P0.65mm_EP1x1.6mm` | DigiKey `497-19548-2-ND` |
| BuckConverter | U1 | `LTC6992CS6-2` | `TSOT-23-6` | DigiKey `161-LTC6992HS6-2#…` |

- Most ICs have no `MPN` field; the MPN comes from the DigiKey PN or the
  Value.
- Several footprints are custom, so the package cannot be read from the name;
  the pad count is the check that still works.
- The two LDL212 symbols name the same DigiKey part with footprints of
  different pitch (0.5 mm and 0.65 mm). At least one is wrong.
- BuckConverter U1: the Value says C grade, the DigiKey PN says H grade.
- Not classified as `ic` today: MotorDriver U5–U8 (guessed `led`) and
  BuckConverter U3, an LTC4442 MOSFET driver (guessed `transistor`). They stay
  out of scope unless the symbol gets `Intent:Type = ic`.

## Data on the schematic

Read: `Value` and `Footprint` (KiCad's own fields, never written), the
part-number fields through `partnum.extract_part_numbers` (every alias
spelling).

Written on accept, canonical keys: `MPN`, `Manufacturer`, `DigiKeyPN`, `LCSC`
(only when JLCPCB has the part), and `Intent:Type = ic` when the type was
guessed. Empty alias spellings of the part-number fields are removed, as for
capacitors.

## Build steps

### Step 1 — `IcIntent`

`@intent_subclass("ic")` in `intent.py`: base fields only plus the part
numbers read through `extract_part_numbers`. Construction never fails for a
missing part number (that is reported by the requirement, not by the intent).

Tests: explicit and guessed `ic`; alias spellings; U parts on the fixtures.

### Step 2 — identifiers and requirement (`icrequirement.py`)

- `ic_identifiers(props) -> Identifiers`: `mpn`, `digikey_pn`, `lcsc`, and
  `value_mpns` — MPN-shaped tokens taken from the Value, longest first. The
  Value is split on spaces, `_` and `,`; a token is MPN-shaped when it has at
  least 5 characters and contains both a letter and a digit.
  `MotorControl_STM32L552CCT6_V2` gives `STM32L552CCT6` (`MotorControl` has
  no digit, `V2` is too short); `LMC7101BYM5` and `ABX00027` are kept whole;
  `1515 rgb led` gives nothing. Each token is tried as a partial MPN until
  one finds a part.
- `IcRequirement`: the identifiers, the footprint, the parsed footprint
  package (step 3), the pad count (step 4), and `problem` ("no part number",
  "no footprint").
- Grouping: by normalized identifiers + footprint, so the two LDL212 symbols
  on different boards and footprints are separate groups.

### Step 3 — footprint name parsing (`package.py`)

`parse_footprint(name) -> PackageInfo(family, pins, body_mm, pitch_mm, ep)`,
each field optional:

- family: SOT-23, TSOT-23, SOT-563, SC-70, SOIC, MSOP, TSSOP, QFN, DFN, LQFP,
  TQFP, DIP …, with KiCad's spellings (`SOT-23-5`, `QFN-24-1EP_4x4mm_P0.5mm`,
  `LQFP-48_7x7mm_P0.5mm`);
- pins from the family suffix, body from `NxMmm`, pitch from `P0.5mm` /
  `Pitch0.5mm`, exposed pad from `-1EP`.

`parse_digikey_package(params) -> PackageInfo` from `Package / Case`
(`24-VFQFN Exposed Pad`), `Supplier Device Package` (`24-QFN (4x4)`) and
`Number of Pins` when present.

Tests: every footprint and DigiKey package string in the table above.

### Step 4 — pad count from the footprint itself

Pad count = distinct non-empty pad numbers (an exposed pad counted once;
unnumbered mechanical and paste pads not counted). Sources, in order:

1. the board's `.kicad_pcb`, by reference — `pcb.parse_footprint_placements`,
   with `Pad` gaining a `number` field;
2. the footprint library: the board's `fp-lib-table` (`$KIPRJMOD` expanded),
   else `Libraries/kicad-footprints/<lib>.pretty` for KiCad's standard
   libraries; read `<name>.kicad_mod`.

No source found → pad count unknown.

Tests: fixture `mini.kicad_pcb`; a `.kicad_mod` with an exposed pad and
paste pads; `fp-lib-table` resolution with `$KIPRJMOD`.

### Step 5 — candidate search (`icsearch.py`)

`find_ic_candidates(req, *, db, digikey, jlc_search, jlc_part, needed)`, every
network call injected, like `partsearch.find_candidates`.

1. **Database:** accepted `ic` parts with the same normalized MPN.
2. **Exact lookups:** `digikey.product_for` on the DigiKey PN and the full
   MPN; `jlc_part` on the LCSC number, then `digikey.product_for` on its MPN.
3. **Partial MPN** (when the exact lookups give nothing): DigiKey keyword
   search, keeping only products whose MPN starts with the partial MPN
   (keyword results are fuzzy — see `NOTES.md`); then a JLCPCB keyword search
   on each kept MPN, exact-MPN match only.
4. **Conflicts:** identifiers that resolve to different MPNs (BuckConverter
   U1) give one candidate each, and the group shows the disagreement.
5. **Checks per candidate:** DigiKey status `Active`; DigiKey stock ≥ 10 ×
   build quantity (required), with a warning below build quantity + 1000;
   the same limits at JLCPCB (when at JLCPCB); footprint check (step 6).
6. **Rank:** database → current part → footprint matches → stock floor met →
   at JLCPCB → JLCPCB Basic → DigiKey stock → price.

DigiKey calls per group: one per exact identifier, plus one keyword search
and at most `MAX_IC_LOOKUPS` (5) `product_for` calls for a partial MPN.

### Step 6 — footprint check

Compare the footprint's `PackageInfo` and pad count with the candidate's
package. Result:

- **match** — pins agree, family, body and pitch agree wherever both sides
  state them, and the footprint states no pitch that the part's side does
  not;
- **mismatch** (red) — any stated field disagrees, or the pad count differs
  from the pin count: "Footprint does not match the part — check it very
  carefully against the datasheet." The message names what disagrees
  (`pitch 0.5 mm vs 0.65 mm`, `36 pads vs 24 pins`). The pad count includes
  an exposed pad. For SOT-223 and SOT-89 it may or may not include the tab:
  KiCad's `SOT-223` numbers the tab as pad 4 (4 pads for 3 leads), and
  `SOT-223-3_TabPin2` gives it pad number 2 (3 pads);
- **cannot verify** (amber) — nothing comparable on one side (custom name
  and no pad count, or DigiKey states no package), or the footprint name
  states a pitch the part's side does not: "Footprint could not be checked
  against the part — check it very carefully against the datasheet."

DigiKey's package strings state no pitch. The part's pitch is only the one
the package standard fixes (SOIC 1.27 mm, DIP 2.54 mm, the JEDEC LQFP/TQFP
sizes). QFN, DFN, TSSOP, MSOP and SSOP are made in more than one pitch for
one body, so a footprint of those families that names its pitch (every
standard KiCad one) is "cannot verify", with the pitch named in the message.
Matching it would hide a footprint of the wrong pitch: the two LDL212
footprints differ only in pitch and both have 7 pads.

The check ranks candidates but never removes one.

### Step 7 — parts database

Schema version 2: `part.type` gains `ic`; new table
`ic (part_id, package TEXT, pins INTEGER)`. LCSC `supplier_pn` row optional
for ICs. Migration from version 1 adds the table.

### Step 8 — Cockpit

- Parts tab: IC groups as rows; Value shows the MPN or Value, Rating and
  Dielectric empty, a Footprint column with the check result's colour.
- Assign dialog for an IC: identifiers and footprint read-only; candidate
  table with MPN, manufacturer, DigiKey PN and stock, LCSC and stock (or
  "not at JLCPCB"), package, footprint check; the check message is shown
  above the Accept button for the selected candidate.
- Accept writes the part to the database and queues the write plan.

### Step 9 — live check

On scratch copies of ECG, MotorDriver, BarrelJackAdapter and BuckConverter:
every `ic` group gets a candidate or a clear reason; the LDL212 pair is
"cannot verify" on both boards, each message naming the footprint's pitch
(0.5 mm, 0.65 mm) as not stated by DigiKey (no field DigiKey states
disagrees, so neither is a mismatch); BuckConverter U1 shows the C/H
grade conflict; MotorDriver U9 finds `STM32L552CCT6` from its Value; ECG U3
(Arduino Nano) is searched like any other IC.

## As built (2026-10-30)

Steps 1–9 are done; the code is in the working tree, not yet committed.
Suite 1378 passing. Where the code differs from the plan:

Step 1, 2 (`intent.py`, `icrequirement.py`):

- `IcIntent` also carries `value` (the stripped Value, or `None`), so
  `description()` can name the part when there is no MPN, as `LedIntent` and
  `ConnectorIntent` do.
- `IcRequirement.problem()` is a method returning `""`, `"no part number"` or
  `"no footprint"`, with a `searchable` property, as on `PartRequirement`, so
  the GUI treats both requirement types the same way. It also has
  `value_text` and `kind` (`"ic"`).
- A symbol with no identifier at all is grouped by its normalized Value as
  well as its footprint, so unrelated unsearchable parts on one footprint
  (an "Arduino Nano" and an "OLED display") are separate groups.
- `IcGroup` uses `requirement.Member`.
- Guessed `U` parts are now `guessed` rather than `unclassified` in the
  Intents tab, so **Accept All** there writes `Intent:Type = ic` on them.

Step 3 (`package.py`):

- An unrecognized family (`SOP`, `DRQFN`, `TO-252`, vendor names such as
  `RHH0036C`) gives `family=None` and `pins=None`; no family is guessed.
- LQFP and TQFP are separate families, as are TSOT-23 and SOT-23. DFN and SON
  (WSON, VSON, X2SON) are both DFN; VSSOP and uMAX are MSOP; QSOP is SSOP;
  HTSSOP and PowerTSSOP are TSSOP with `ep=True`.
- `PackageInfo.known` says whether anything was parsed (used for "cannot
  verify").
- When DigiKey gives no Supplier Device Package, the first family in
  Package / Case is used (`8-TSSOP, 8-MSOP` gives TSSOP).

Step 4 (`pcb.py`, `footprints.py`):

- The board's copy of a footprint is used only when its lib id equals the
  schematic's `Footprint` field (or no lib id is given); otherwise the board
  has not been updated from the schematic, and the library file is read.
- In an `fp-lib-table` uri, `${KICADn_FOOTPRINT_DIR}` is the standard library
  folder, other variables come from the environment, and a uri with a
  variable that cannot be expanded leaves that library out. Relative uris
  are relative to the project folder. KiCad's global `fp-lib-table` is not
  read. A lib id with no nickname (`SOT-23-5`) gives no pad count.
- The exposed pad counts as a pad: `RHH0036C` has 37 (36 + EP), the two
  LDL212 footprints 7.

Step 5, 6 (`icsearch.py`, `suppliers.py`):

- Order: exact lookups first, then the database (an MPN found in step 1, the
  `MPN` field or a Value token, exact after normalization; the first hit per
  MPN, most used and Basic first), then the partial MPN, then JLCPCB by MPN.
- The partial-MPN step tries the `MPN` field (when its exact lookup found
  nothing) as well as the Value tokens. Each token tried is one keyword
  search; later tokens are tried only when earlier ones find nothing. When
  the exact lookups found a part, one token that disagrees with it is
  searched, so BuckConverter U1's Value finds the C grade part. The
  `MAX_IC_LOOKUPS` cap of 5 `product_for` calls covers the whole step. A
  Value token equal to the MPN field is not searched twice.
- The JLCPCB stock limits do not reject a candidate. A part below the
  minimum (10 × build quantity), or one JLCPCB marks unbuyable, keeps its
  LCSC number with a note saying why; it ranks as not at JLCPCB and its LCSC
  number is not written. Between the minimum and the floor (build quantity +
  1000) it is at JLCPCB with a warning. At DigiKey the minimum rejects and
  the floor only warns (`IcCandidate.digikey_warning`, `jlc_warning`,
  `below_floor`; the dialog tints the stock cell amber and shows the warning
  above Accept). Added 2026-11-01, after the live check below, which ran
  with the floor as a hard limit.
- An exact identifier that names no part, beside another that does
  (BarrelJackAdapter U1's `DigiKeyPN TPS563207DRLRCT-ND`), is a conflict.
- `DigiKeyProduct` parses `UnitPrice` and `Manufacturer.Name`. A candidate's
  price is JLCPCB's when it is at JLCPCB, else DigiKey's.
- Footprint check: TQFP counts as LQFP and TSOT-23 as SOT-23 (the pads
  are the same; the packages differ only in height). The pad count may be the pin count + 1 when the part has an
  exposed pad, because DigiKey's pin count leaves it out (`36-VFQFN Exposed
  Pad`). Pitch is compared only where the package standard fixes it
  (`package.standard_pitch`).
- The write plan is `icsearch.ic_assignment_plan(group, cand)`; `assign.py`
  is unchanged. It writes `MPN`, `Manufacturer`, `DigiKeyPN`, `LCSC` when the
  part is at JLCPCB (else removes an existing `LCSC`, so no stale number
  stays beside the new part), and `Intent:Type = ic` when guessed. Every
  alias spelling of the four part-number fields is removed, empty or not.

Step 7 (`partsdb.py`):

- Two package strings are stored: DigiKey's Supplier Device Package in
  `part.package` (as for capacitors and resistors) and its Package / Case in
  `ic.package` (`IcChoice.package` and `package_case`).
- `accept_part` and `reject_part` take a `PartChoice` or an `IcChoice`; there
  are no separate IC methods.

Step 8 (`gui/parts.py`, `gui/ic_assign_dialog.py`, `gui/main_window.py`):

- The IC dialog is its own class, `IcAssignDialog`, with the signals and
  methods `MainWindow` uses for `AssignDialog`; both share one search queue.
  It has a **Search again** button in place of **Search suppliers**, and the
  candidate table also has Source, JLC library and Unit price columns.
- The Footprint column is on every row (11 columns); capacitor and resistor
  rows show the footprint name with no colour.
- An IC row's footprint colour comes from the queued part, else from the
  last search the dialog showed (the candidate the symbol names exactly, or
  the only one). A cancelled dialog still records its search; reload or
  save clears it.
- An IC row is `conflict` only after a search found disagreeing identifiers,
  and a conflict does not block the search: every part is listed and the
  user picks one.
- The IC Package cell is the queued part's DigiKey package, else the package
  parsed from the footprint name, marked `(footprint)`.
- An `assigned` IC row can be opened to see its footprint check; a `not
  searchable` row does not open and the status bar gives the reason.
- Accepting an IC stores no settings.

Step 9 (live check, `/private/tmp/claude-501/ic-work/live_check.py`, 11
groups):

| Board | Ref | Result | Footprint check |
|---|---|---|---|
| ECG | U1 | AD8232ACPZ-R7; LCSC under the floor, not written | mismatch: 20 pads vs 20 pins + exposed pad |
| ECG | U2 | LMC7101BYM5-TR, from the Value by keyword search | match |
| ECG | U3 | rejected: DigiKey stock 90 | (cannot verify: no package) |
| MotorDriver | U1, U2 | rejected: DigiKey stock 164, need 1050 | (match: 37 pads vs 36 pins + EP) |
| MotorDriver | U3 | STUSB4500QTR, at JLCPCB | cannot verify: pitch 0.5 mm not stated |
| MotorDriver | U4 | rejected: DigiKey stock 0 | (cannot verify: pitch 0.5 mm) |
| MotorDriver | U9 | rejected: DigiKey stock 0 | (match: LQFP-48 7x7 P0.5) |
| MotorDriver | U13 | rejected: DigiKey stock 0 | (match: 21 pads) |
| BarrelJackAdapter | U1 | rejected: DigiKey stock 0; conflict on the DigiKeyPN | (match: SOT-563) |
| BarrelJackAdapter | U2 | rejected: DigiKey stock 0 | (cannot verify: pitch 0.65 mm) |
| BuckConverter | U1 | rejected: H grade DigiKey stock 798, C grade discontinued or stock 0; C/H grade conflict listed | (match: TSOT-23-6) |

A check in parentheses is for a rejected part, worked out offline; the
search result does not carry it (TBD 2).

- Every group gets a candidate or a one-line reason: met.
- The LDL212 pair is "cannot verify" on both boards, naming 0.5 mm and
  0.65 mm: met, but both are rejected on DigiKey stock, so neither result is
  shown in the dialog.
- BuckConverter U1 shows the C/H grade conflict: met.
- MotorDriver U9 finds `STM32L552CCT6` from its Value: met with the
  DigiKeyPN removed (`u9_value_probe.py`); the part is rejected on DigiKey
  stock 0.
- ECG U3 is searched like any other IC: met; DigiKey `1050-ABX00027-ND` and
  JLCPCB `C20627271` are found, and it fails only on stock.
- DigiKey stock 0 for LDL212PV33R and TPS563207DRLR was confirmed with a raw
  API probe (every packaging 0, lead times 30 and 20 weeks), so the stock
  parsing is not the cause.
- ECG U1's mismatch is a board finding: `CP_20_10_ADI_NOTH.kicad_mod` has no
  exposed pad. The keyword-search handling and the DigiKey package strings
  were confirmed against the live API.

## TBD

1. Transistors, diodes and LEDs: a separate plan on the resistor/capacitor
   path (search by value and rating), not this one.
2. A rejected part's footprint check is computed but not kept:
   `IcSearchResult.rejected` holds only (label, reason). Carry the
   `FootprintCheck` (or the `IcCandidate`) so the dialog can list rejected
   parts greyed out with their reason and footprint colour.
3. ~~Live check with the 10 × minimum.~~ Done (2026-11-03): the user ran a
   live check before shipping a board.
4. The LDL212 pair cannot show a mismatch from one board: DigiKey states no
   pitch and both footprints have 7 pads. Finding it needs a check across
   boards (one MPN or DigiKey PN placed on footprints of different pitch) or
   another source of the part's pitch.
5. An LCSC row already in the database cannot be removed when JLCPCB stops
   stocking the part (re-accepting with no LCSC keeps it), and nothing in the
   GUI clears it. Same open item as for capacitors.
6. `find_ics` matches only an exact MPN after normalization; a Value token
   (`STM32L552CCT6`) does not find a stored `STM32L552CCT6TR`.
7. Footprints with numbered mounting pads (`MP`) count them and will show as
   mismatches. None on the boards checked.
8. TI-style footprint names (`Texas_RGV_S-PVQFN-N16_EP2.1x2.1mm`) give only
   `ep=True`; family and pins are not parsed.
9. KiCad's global `fp-lib-table` is not read; a nickname not in the board's
   table is looked up only in `Libraries/kicad-footprints`.
10. Sorting the Parts tab by Status orders IC rows among capacitor and
    resistor rows by text only.
11. Not yet tried in the Cockpit GUI; the live check was scripted.
