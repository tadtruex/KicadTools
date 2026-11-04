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
- 2026-11-04: solder jumpers and net ties are not parts (user decision).
  `thtfootprint.virtual_part` recognizes them by footprint name in any
  library (`SolderJumper…`, `NetTie…`), or by Value without a footprint, and
  `components.bom_components` and `resolve.iter_bom_part_records` drop them,
  so no BOM, CPL or Cockpit list has them. 22 solder jumpers on 10 boards
  (12 on `hbridge`); no net ties.
- 2026-11-04: package names no longer read as named parts. The short-name
  rule (no maker token) now needs a digit, skips KiCad's `Package_*`
  libraries, and skips package and size tokens (`TO-92`, `TO56-3`,
  `SDIP-10C`, `PLCC-20`, `HC35-U`, `PG-LLGA-5-1`, `D5.0mm-3`, `15x7.5RM7.6`,
  `PUSH-12mm`). Over KiCad's through-hole libraries the makerless named parts
  went from 234 to 143, almost all real parts (displays, LEM sensors,
  modules). `looks_like_part_number(token)` is public for step 6.

### Step 4 — DigiKey filtered search (`suppliers.py`)

`DigiKeyClient.category_search(keyword, category_id, filters, *,
manufacturers=(), min_stock, limit)`: one call returns the hits and the
category's filter options. Value names are mapped to IDs from those options,
which are cached per category for the session.

### Step 4 — as built (2026-11-03)

**Rate limit.** DigiKey's Product Information API allows 120 calls a minute
and 1000 a day per application ([Shared Concepts](https://developer.digikey.com/documentations));
every response carries `x-ratelimit-limit: 1000` and `x-ratelimit-remaining`
(no per-minute header was sent). Over a limit DigiKey answers 429 with
`Retry-After`. `suppliers.DigiKeyRateLimiter`, one per process
(`DIGIKEY_LIMITER`, thread-safe, used by every `DigiKeyClient` for
`product_for`, `keyword_search` and the category search):

- calls at least 0.6 s apart (100 a minute, `DIGIKEY_PER_MINUTE`);
- reads `x-ratelimit-remaining` from each response and stops all DigiKey
  calls for the rest of the process when 50 or fewer are left
  (`DIGIKEY_DAILY_RESERVE`), with a printed warning;
- on 429 waits `Retry-After` and retries once when it is 60 s or less, else
  stops;
- `status()` gives a one-line count ("DigiKey: 4 call(s) this session, 994
  of 1000 left today"). Not shown in the Cockpit yet.

Tests run with a fresh unspaced limiter and with the DigiKey credentials
removed from the environment (`tests/conftest.py`), so no test reaches the
real API.

**Filtered search.** `src/kicadtools/dkfilters.py` (no I/O): parses a
category's `FilterOptions` into `CategoryFilters`, matches wanted values per
parameter (names without case, numbers against the mm figure in
`0.100" (2.54mm)` or a plain number, predicates, lists), manufacturers by
whole-word name prefix without accents (`Wurth` → `Würth Elektronik`, `TE` →
`TE Connectivity …` but not `Technetix`), and builds the request body.
`DigiKeyClient.category_search(keyword, category_id, filters, *,
manufacturers, min_stock, limit)` returns a `CategorySearchResult`
(products cheapest first, total, options, `unmatched`, `error`).

- The options are cached for the session per (category, keyword): DigiKey's
  options depend on the keyword too. First search of a pair: 2 calls; later:
  1.
- A wanted value with no ID is not dropped: the search is not made and the
  result lists it under `unmatched`, since the search would be broader than
  asked.
- Mounting Type gives the orientation: vertical = "Through Hole",
  horizontal = "Through Hole, Right Angle".
- Live check (2026-11-03, 4 calls), male headers, in stock ≥ 100, seven
  makers: 1×4 vertical 172 hits (Sullins `PREC004SAAN-RC`, $0.10); 1×6 right
  angle 100 (`PREC006SBAN-M71RC`); 2×5 vertical 216 (`PREC005DAAN-RC`). The
  plan's earlier 213 for 1×4 had no Contact Type or Connector Type filter.
- Tests: `tests/test_dkfilters.py`, `tests/test_digikey_limiter.py`.

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

### Step 5 — as built (2026-11-03)

`src/kicadtools/headersearch.py`: `HeaderRun(client, makers=HEADER_MAKERS)`
with `search(signature, needed)` → `HeaderSearchResult` (ranked candidates,
`proposed`, total, the series used, notes) and `pick(product, signature)`.
`header_filters`, `series_from_pick`, `rank_for_series`, `stock_note`.
Tests: `tests/test_headersearch.py`.

- Sockets are leaf category 315 (Headers, Receptacles, Female Sockets),
  contact type "Female Socket". Headers and sockets each get their own series
  in a run.
- Stock: the search asks for at least 10 × the build quantity
  (`icsearch.stock_minimum`); a proposed part below build + 1000 gets a note,
  as for ICs.
- DigiKey's "Number of Rows" filter in category 315 (parameter 2172) returns
  no hits for any value, though the parameter is on the parts. For sockets
  the rows are checked on each hit after the search, with 50 hits asked.
- Single-pin headers: no pitch filter (DigiKey lists them with pitch "-").
- The series fixes manufacturer, Contact Finish – Mating and Connector Type
  for every later group, and the two contact lengths only for groups with
  the pick's orientation and mounting (right-angle lengths differ).
- MPN pattern: the position digits are the longest suffix of the first digit
  run that reads as the count per row or total (`PRPC004SAAN-RC`,
  `TSW-104-07-G-S` → `04`), else the longest shorter prefix
  (`PPTC041LFBN-RC` → `04`; the 1 is the row count). Hits are ranked: the
  predicted MPN, then other pattern matches, then the rest, by price within
  each.
- A series search with no hit falls back to the maker list, with a note.
- Live check (2026-11-03, 8 calls, build quantity 5), picking each proposed
  part: 1×4 → `PREC004SAAN-RC` (maker list, 275 hits) sets the series; then
  1×6 right angle `PREC006SBAN-M71RC`, 1×1 `PREC001SAAN-RC`, 1×7
  `PREC007SAAN-RC`, 2×5 `PREC005DAAN-RC`, 2×12 right angle
  `PREC012DBAN-M71RC` (stock 155, noted); sockets 1×4 → `PPTC041LFBN-RC`
  sets the socket series; 1×8 `PPTC081LFBN-RC`, 1×15 `PPTC151LFBN-RC`.
- Not done: `hdr.manufacturers` is not a setting yet (`HEADER_MAKERS` is the
  default; the Cockpit wiring is step 9).

### Step 6 — part-named footprints

Search the MPN token from the footprint (or the Value) with the IC search
(`icsearch.py`): same prefix match, same pad-count check.

### Step 6 — as built (2026-11-04)

`src/kicadtools/namedsearch.py`: `find_named_candidates(signature, props, *,
digikey, needed, pad_count)` → `NamedSearchResult` (ranked `candidates`,
`proposed`, `rejected`, notes, `problem`). Tests: `tests/test_namedsearch.py`.
It does not call `icsearch.find_ic_candidates`: that path also searches
JLCPCB and the parts database, and checks packages by IC family. It reuses
the IC stock rules (`stock_minimum`, `stock_floor`).

- Texts tried, the first that gives a part ends the search: the MPN field
  (exact `product_for`); Value tokens that fit the footprint token; the
  footprint token; the other Value tokens. A Value token is kept when
  `looks_like_part_number` (5+ characters, a digit or hyphen, not geometry,
  package or size), so `USB-A-S-F-B-TH` counts though it has no digit.
- A partial text is one keyword search, keeping MPNs that start with it
  (`norm_mpn`, hyphens kept), exact first then shortest, confirmed with
  `product_for` up to `MAX_LOOKUPS` (5) per group. Placeholder segments
  (`X`, `XX`) match any segment; the keyword is the text before the first.
- Rejected: status not Active, stock below 10 × the build quantity. Below
  build + 1000: kept with a warning.
- Pad check: pads against "Number of Positions" or the IC pin parameters.
  Equal is a match; more pads is "cannot verify" (mounting and shield pads);
  fewer is a mismatch. It ranks, never removes.
- Rank: exact MPN, pad check, stock floor met, DigiKey stock, price.
- Live check (2026-11-04, about 24 calls, build quantity 5):
  - Breakout J101 (`JST_PH_S3B-PH-K`): `S3B-PH-K-S`, 36 852 in stock, pads
    match. The exact `S3B-PH-K` and three other variants have no stock.
  - Breakout J102 (`CUI_PJ-002B`): `PJ-002B` (Same Sky, `CP-002B-ND`), then
    `PJ-002BH`; three SMT variants rejected as obsolete. DigiKey gives barrel
    jacks no positions, so the pad check cannot verify.
  - Breakout J2 (`SAMTEC_USB-A-S-X-X-TH`, Value `USB-A-S-F-B-TH`, already
    has a DigiKey PN): the footprint keyword `USB-A-S` alone returns other
    USB parts; the Value finds `USB-A-S-F-B-TH` exactly. This is why fitting
    Value tokens go first.
  - BuckConverter RV1 (`Bourns_3386P`, Value `200K`, already has a DigiKey
    PN): 50 hits for `3386P`; the five looked up are the shortest MPNs, not
    the 200 kΩ one. See TBD 5.
- Not done: accepted parts are not stored (step 8); the Cockpit wiring is
  step 9.

### Step 7 — through-hole passives

Value and intent from the existing requirement, with the footprint signature
as the package: lead spacing must match, diameter or body size must not
exceed the footprint's. DigiKey only, category 58 for aluminum electrolytics,
film and ceramic categories to be found.

### Step 8 — parts database

Accepted through-hole parts are stored with their footprint signature, so a
later board with the same signature finds the stored part first.

### Step 8 — as built (2026-11-04)

`partsdb.py`: table `tht` (part_id, family, signature, value_key,
footprint, positions; index on signature and value_key), `part.type` =
`"tht"`, `ThtChoice` / `DbTht`, `PartsDb.find_tht(signature, value_key)`
(accepted only; usage count, then the latest decision, then MPN). Added
within schema version 2 with `CREATE TABLE IF NOT EXISTS`, as the inductor
table was: an existing file gains the table on open and keeps every row.
`src/kicadtools/thtdb.py`: `signature_key`, `value_key`, `db_key`,
`tht_choice`, `stored_candidates`, and the DigiKey checks shared with
`namedsearch` (`reject_reason`, `stock_warning`, `positions_of`). Tests:
`tests/test_thtdb.py`.

- The key is the footprint signature (family, rows, positions, pitch,
  orientation, SMD variant, passive body and spacings, and the part-number
  token of a part-named footprint), not the library id: the same header
  geometry from two libraries finds the same part.
- Headers, sockets and terminal blocks key on the signature alone. Every
  other family keys on the normalized Value too: `Bourns_3386P` takes a 200K
  and a 10K pot alike. A different Value gives a fresh search instead of a
  wrong part. A part-named footprint whose Value differs between boards
  (`I2C_Conn` here, `JST_3` there) is searched again on the second board.
- A stored part is refreshed at DigiKey (one `product_for`, by DigiKey PN)
  and offered only when it passes the search's checks (active, stock at
  least 10 × the build quantity). When one passes, DigiKey is not searched;
  `HeaderSearchResult.from_database` and `NamedCandidate.stored` say so.
  This keeps within DigiKey's 1000 calls a day; the Cockpit (step 9) can
  offer a search anyway.
- Parts recorded as rejected are left out of the header and part-named
  search results, with the reason in `rejected`.
- A stored header the user picks sets the run's series, as any first pick
  does (`HeaderRun.pick`).
- Checked on a copy of `parts.sqlite`: the table was added, the 15 stored
  parts were kept. Live: Breakout J101 searched (6 DigiKey calls), accepted
  `S3B-PH-K-S`, searched again from the database (1 call).
- Not done: accepting and rejecting from the Cockpit, and recording usage,
  are step 9.

### Step 9 — Cockpit

Through-hole rows on the Parts tab: class "THT", status "no part" for pads,
"needs part" otherwise, an assign dialog for the header and passive paths.
The header dialog shows the run's series once it is set.

### Step 9 — as built (2026-11-04)

`src/kicadtools/thtassign.py` (no Qt): `ThtRequirement`, `ThtGroup`,
`group_tht(records, board_mounting)`, `ThtCandidate`, `ThtSearchResult`,
`assess_tht(group, *, run, digikey, db, needed, use_stored)`,
`tht_assignment_plan`. `gui/parts.py`: through-hole rows, status `no part`,
`ThtCandidateWorker`. `gui/tht_assign_dialog.py`: `ThtAssignDialog`, with the
IC dialog's interface so the window's search queue, accept and reject paths
take it unchanged. `gui/main_window.py`: rows loaded after the IC rows, one
`HeaderRun` per opened schematic, the series set on the first header or
socket accepted, `hdr.manufacturers` read from the settings table, and the
DigiKey call count (`DIGIKEY_LIMITER.status()`) as a permanent status-bar
label updated after each search. Tests: `tests/test_thtassign.py`,
`tests/test_gui_tht.py`.

- Rows: parts that are not capacitors, resistors, inductors or ICs, with a
  through-hole footprint (step 1's mounting). Grouped by footprint, Value,
  MPN and DigiKey PN. Through-hole capacitors with an intent stay on their
  capacitor rows (step 7, `CAPACITOR_PLAN.md` TBD 2).
- Status: `no part`, `not searchable` (terminal blocks, loop test points,
  `other` footprints), `assigned` (MPN, Manufacturer and DigiKeyPN; no LCSC),
  `unassigned`, `queued`.
- Beyond the plan: the symbol's own `DigiKeyPN`, else `MPN`, is looked up
  exactly first, for every family. Many through-hole symbols on the boards
  hold a DigiKey PN and no MPN or Manufacturer (BuckConverter J1, RV1,
  TP1–6); this fills them in, and makes a terminal block or loop test point
  with a part number searchable.
- Beyond the plan: header ranking outside the series pattern prefers MPNs
  that share a longer prefix with the series MPN. Live, a series set by the
  dual-row `PREC012DAAN-RC` proposed `PEC01SAAN` for a 1×01 before
  `PREC001SAAN-RC`; it now proposes `PREC001SAAN-RC`.
- The dialog's first search uses stored parts; **Search DigiKey** skips them.
- Write plan: `MPN`, `Manufacturer`, `DigiKeyPN`; `LCSC` and alias
  spellings removed; `Value` and `Footprint` never written.
- Live check (2026-11-04, offscreen Cockpit, copy of `parts.sqlite`, no
  save; 10 DigiKey calls): BarrelJackAdapter J2/J4 → `PREC004SAAN-RC` (20
  shown, pads match); SX1509 J5 (2×12) → `PREC012DAAN-RC`, which set the
  series; SX1509 J7 (1×01) → two Sullins parts; Breakout J101 →
  `S3B-PH-K-S`. Reopened, J101 came from the database in 1 call.
- Not done: the Parts tab does not show SMD mounting for other rows.

### Step 10 — live check

BarrelJackAdapter J2/J4, SX1509 J5/J7, MiDi J1/J2, Breakout J101/J102/J5.

## TBD

1. IDC and shrouded headers: their categories and parameters are not checked
   yet. Female sockets are category 315 (step 5 as built).
2. Mating parts (JST housings and crimp contacts) are not assigned.
3. BlackBox and NeoPMOD footprints are not resolved (see "Evidence").
4. Whether a series picked in one run is offered as the default for the next.
5. Series-prefix footprints (`Bourns_3386P`) give many variants that differ
   by value; the search does not use the Value (`200K`) to choose which five
   to look up. A resistance or capacitance code in the MPN (`204` = 200 kΩ)
   or DigiKey's Resistance parameter on the keyword hits could.
6. MiDi J1 (`DIN5_MIDI`, Value `DIN-5_180degree`) names no part; it is
   "other" and is not searched.
