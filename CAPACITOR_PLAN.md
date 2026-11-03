# Plan: capacitor part assignment

Goal: start from a schematic whose capacitors have a `Value` and possibly a
`Footprint`, and finish with every capacitor carrying a stated requirement, one
chosen part (available at both DigiKey and JLCPCB) and a footprint. Accepted
parts are recorded in a local SQLite database that later runs search first.

Target workflow: **Schematic Capture → Cockpit → PCB with footprints for
everything.**

Scope was capacitors first; resistors were added in revision 4 and inductors
in revision 5 on the same code path.

## Decisions already made

| Topic | Decision |
|---|---|
| Footprint | The Cockpit writes `Footprint` from the chosen package when the symbol has none. An existing footprint is never overwritten. |
| Voltage floor | Never derived from net or rail names. When a symbol has no `Voltage:Min`, the dialog prompts for one (pre-filled with the last value used), keeps it editable, and writes it to the symbol on accept. |
| Database | `Boards/parts.sqlite`, listed in `Boards/.gitignore`. |
| Search order | Parts database first. On a miss: filtered JLCPCB search (value keyword, MLCC category, package, in stock), manufacturer list, attribute filter, then DigiKey exact lookup by MPN for the top candidates (revision 3). |
| Alternates | Not stored on the symbol. Any accepted database part meeting the symbol's requirement properties is an alternate. |
| Database path | `~/Code/Boards/parts.sqlite`, overridable with `KICADTOOLS_PARTS_DB`. |
| Defaults | Dielectric: C0G/NP0 ≤ 1 nF, X7R between, X7R/X5R ≥ 10 µF. Tolerance: 10 % for X-class, 5 % for C0G. |
| Remembered defaults | Package, Voltage:Min, Dielectric and Tolerance are all remembered as last-used settings; the built-in defaults above apply only until a value has been used. |

## Data on the schematic

All hidden properties, written by the existing surgical writer
(`writer.set_or_insert_property`).

**Requirement** — what any acceptable part must meet:

| Property | Example | Notes |
|---|---|---|
| `Intent:Type` | `capacitor` | Written when the type was guessed. |
| `Value` | `100nF` | KiCad's own field; read, never written. |
| `Voltage:Min` | `16V` | |
| `Class:Preferred` | `X7R, X5R` | Dielectrics allowed, comma-separated. |
| `Tolerance:Preferred` | `10%` | Loosest tolerance allowed. New on `CapacitorIntent`. |

**Selection** — the chosen part, canonical keys from `partnum.CANONICAL_KEYS`:
`MPN`, `Manufacturer`, `DigiKeyPN`, `LCSC`.

**Footprint** — `Footprint`, from the package when the symbol has none.

## Database schema

`Boards/parts.sqlite`. Default path `~/Code/Boards/parts.sqlite` (the same
relative layout on every machine), overridable with `KICADTOOLS_PARTS_DB`.
Foreign keys on; `meta.schema_version` stamped at creation.

```sql
meta     (key TEXT PRIMARY KEY, value TEXT);          -- schema_version
settings (key TEXT PRIMARY KEY, value TEXT);          -- last-used defaults, see below

part (
  id            INTEGER PRIMARY KEY,
  mpn           TEXT NOT NULL,
  manufacturer  TEXT NOT NULL,
  type          TEXT NOT NULL,          -- 'capacitor'
  package       TEXT NOT NULL,          -- '0603'
  description   TEXT,
  datasheet_url TEXT,
  status        TEXT NOT NULL,          -- accepted | rejected | retired
  decided_at    TEXT NOT NULL,          -- ISO 8601
  notes         TEXT,
  UNIQUE (manufacturer, mpn)
);

capacitor (
  part_id       INTEGER PRIMARY KEY REFERENCES part(id) ON DELETE CASCADE,
  capacitance_f REAL NOT NULL,
  voltage_v     REAL NOT NULL,
  dielectric    TEXT NOT NULL,          -- normalized: C0G, NP0, X5R, X6S, X7R, X7S
  tolerance_pct REAL
);

supplier_pn (
  part_id       INTEGER NOT NULL REFERENCES part(id) ON DELETE CASCADE,
  supplier      TEXT NOT NULL,          -- 'digikey' | 'lcsc'
  pn            TEXT NOT NULL,
  packaging     TEXT,                   -- DigiKey: 'cut tape' …
  jlc_library   TEXT,                   -- LCSC: 'basic' | 'extended'
  verified_at   TEXT NOT NULL,
  PRIMARY KEY (supplier, pn)
);

usage (
  part_id       INTEGER NOT NULL REFERENCES part(id) ON DELETE CASCADE,
  board         TEXT NOT NULL,          -- schematic path relative to Boards/
  reference     TEXT NOT NULL,
  used_at       TEXT NOT NULL,
  PRIMARY KEY (part_id, board, reference)
);
```

Settings keys for the remembered defaults. Dielectric and tolerance defaults
depend on the capacitance band and dielectric class, so each band or class has
its own key:

| Key | Built-in value | Applies to |
|---|---|---|
| `cap.package` | `0603` | every capacitor |
| `cap.voltage_min` | none — prompted | every capacitor |
| `cap.dielectric.small` | `C0G, NP0` | ≤ 1 nF |
| `cap.dielectric.mid` | `X7R` | > 1 nF and < 10 µF |
| `cap.dielectric.bulk` | `X7R, X5R` | ≥ 10 µF |
| `cap.tolerance.class1` | `5%` | C0G / NP0 |
| `cap.tolerance.class2` | `10%` | X5R, X6S, X7R, X7S |

Stock and price are not stored here. They stay in the existing 12-hour
`jlc_part` cache in `~/.kicadtools/lcsc_cache.sqlite`.

## Build steps

The names in steps 0–6 are as first built. Revision 4 renamed them for the
shared capacitor/resistor path (`capsearch` → `partsearch`,
`CapacitorRequirement` → `PartRequirement`, `find_capacitors` →
`find_parts`, `FOOTPRINTS` → `PartType.footprints`); see Revision 4.

Each step is independently testable; steps 0–4 need no Qt.

### Step 0 — move the unit parsers

`parse_capacitance`, `parse_volts`, `parse_percent`, `parse_watts`,
`parse_resistance` move from `confirm.py` into a new `units.py`, together with
the formatters now private to `substitute.py` (`_fmt_farads`, `_fmt_volts`,
`_fmt_percent`, …). `confirm.py` and `substitute.py` import from `units.py`.
No behaviour change; the existing tests are the check.

Reason: the new modules need the same parsers, and importing them from the
DigiKey-confirm module would couple unrelated code.

### Step 1 — `partsdb.py`

- `PartsDb(path)` — opens or creates the file, applies the schema, enforces
  `schema_version`.
- `default_path() -> Path` — `KICADTOOLS_PARTS_DB` or `~/Code/Boards/parts.sqlite`.
- `accept_capacitor(choice) -> int` — upserts `part`, `capacitor`, and both
  `supplier_pn` rows in one transaction; status `accepted`.
- `reject(manufacturer, mpn)` — status `rejected`; such parts never appear as
  candidates.
- `find_capacitors(req) -> list[DbPart]` — accepted parts with
  `capacitance_f` equal within 0.1 %, same `package`, `voltage_v >= floor`,
  `dielectric IN (allowed)`, `tolerance_pct <= max` (when the requirement has
  one). Ordered by usage count, then JLCPCB Basic first.
- `record_usage(part_id, board, references)`.
- `get_setting(key)` / `set_setting(key, value)`.
- Add `parts.sqlite`, `parts.sqlite-journal`, `parts.sqlite-wal` to
  `Boards/.gitignore`.

Tests: in-memory database; accept → find round trip; each filter rejects the
part it should; rejected parts excluded; upsert of an existing MPN updates
rather than duplicates; schema version mismatch raises.

### Step 2 — requirement and grouping (`requirement.py`)

- `CapacitorIntent` gains `tolerance_preferred: List[str]`
  (`Tolerance:Preferred`), matching `ResistorIntent`.
- `CapacitorRequirement` dataclass. Each field holds a value and a source
  (`schematic` | `default` | `unset`):
  `capacitance_f`, `package`, `voltage_min_v`, `dielectrics` (tuple),
  `tolerance_max_pct`.
- `capacitor_requirement(intent, defaults) -> CapacitorRequirement`:
  - capacitance from `Value` (unparseable → requirement marked unsearchable);
  - package from the footprint name (`substitute.package_code`), else
    `Package:Preferred`, else `cap.package`;
  - voltage from `Voltage:Min`, else `cap.voltage_min`, else `unset` — the
    dialog requires a value before searching;
  - dielectrics from `Class:Preferred`, else the `cap.dielectric.*` setting for
    the capacitance band;
  - tolerance from `Tolerance:Preferred`, else the `cap.tolerance.*` setting for
    the dielectric class.

  `defaults` is a plain mapping read from the settings table with the built-in
  values filled in for missing keys, so this function stays pure.
- `group_capacitors(intents) -> list[RequirementGroup]` — groups on the values
  **stated on the schematic** (capacitance, footprint, `Voltage:Min`,
  `Class:Preferred`, `Tolerance:Preferred`), so a stated 50 V part never merges
  with an unstated one. A group lists its references and its
  `(source_file, symbol_uuid)` pairs; a subsheet symbol instantiated twice is
  one uuid with two references.
- A group whose members all have `MPN` and `LCSC` is marked `assigned`.

Tests: each default rule; each schematic value overriding its default; source
tags; grouping on the real `MotorDriver` and `ECG` fixtures; stated-vs-unstated
voltages split groups.

### Step 3 — candidate search (`capsearch.py`)

`find_candidates(req, *, db, jlc_search, jlc_part, digikey, needed, limit=10)`
with every network call injected.

1. **Database:** `db.find_capacitors(req)`. Each hit gets a stock refresh
   (`cached_jlcpcb_part` for the LCSC code, `digikey.product_for` for the DigiKey
   PN). Source `database`.
2. **Supplier search** (when the database returns nothing, or on request):
   - JLCPCB keyword search once per allowed dielectric
     (`"100nF 0603 X7R"`), as `SearchSpec.keywords` does today;
   - filter each hit on its JLCPCB attributes (capacitance, voltage, dielectric,
     tolerance, package) — the `substitute.evaluate` rules, rewritten against
     `CapacitorRequirement`;
   - drop hits that are not orderable;
   - rank and take the top `limit`;
   - for each, `digikey.product_for(mpn)`: require an exact MPN match
     (`_norm_mpn`), status `Active`, and DigiKey's `Capacitance`,
     `Voltage - Rated`, `Temperature Coefficient`, `Tolerance` and
     `Package / Case` agreeing with the requirement. Take the cut-tape PN
     (`preferred_digikey_pn`). Failures are dropped, with the reason kept for
     the dialog's "rejected N" note.
3. **Rank:** database usage → JLCPCB Basic → JLCPCB preferred → JLCPCB stock
   covers `needed` → DigiKey stock covers `needed` → unit price.

`Candidate` holds: `mpn`, `manufacturer`, `package`, the parsed parameters,
`lcsc`, `jlc_library`, `jlc_stock`, `jlc_price`, `digikey_pn`, `digikey_stock`,
`source`, `db_part_id`.

Tests: fakes for all four callables — database hit skips the search; JLCPCB hit
failing each attribute; DigiKey MPN mismatch, inactive status, and parameter
disagreement each drop a candidate; ranking order; the DigiKey call count never
exceeds `limit`.

### Step 4 — write plan (`assign.py`)

`assignment_plan(group, req, candidate) -> {source_file: {uuid: {key: value}}}`
— the shape `writer.write_properties` and `MainWindow._merged_edits` already use.

Per symbol in the group:
- `Intent:Type = capacitor` when the type was guessed;
- `Voltage:Min`, `Class:Preferred`, `Tolerance:Preferred` when the source was
  `default` (stated values are left as they are);
- `MPN`, `Manufacturer`, `DigiKeyPN`, `LCSC` from the candidate;
- `Footprint` from `FOOTPRINTS[package]` when the symbol's footprint is empty:

  | Package | Footprint |
  |---|---|
  | 0402 | `Capacitor_SMD:C_0402_1005Metric` |
  | 0603 | `Capacitor_SMD:C_0603_1608Metric` |
  | 0805 | `Capacitor_SMD:C_0805_2012Metric` |
  | 1206 | `Capacitor_SMD:C_1206_3216Metric` |
  | 1210 | `Capacitor_SMD:C_1210_3225Metric` |

- empty alias spellings of the four part-number fields (e.g. an empty
  `Digikey PN`) are removed in the same pass, so the symbol is left with the
  canonical keys only. This needs the plan to express removals as well as
  set-or-insert; `writer.apply_key_edits` already supports `remove`.

Values are written in the terse form: `16V`, `10%`, `X7R, X5R`.

Tests: plan contents for guessed vs explicit type, stated vs defaulted fields,
empty vs present footprint, alias cleanup; apply the plan to a copy of a fixture
and re-read it through `get_components` — every capacitor in the group then
reads as assigned with the expected requirement.

### Step 5 — Cockpit

**Parts tab** (new, `gui/parts.py`): one row per capacitor requirement group.
Columns: Value · Package · Voltage · Dielectric · Tolerance · Qty · References ·
Status (`unassigned` / `assigned` / `queued`) · Chosen MPN. Double-click an
unassigned row to open the dialog.

**Assign dialog** (`gui/assign_dialog.py`):
- Header: references, quantity, build quantity (the existing Boards spinbox).
- Editable fields: Package, Voltage:Min, Dielectric, Tolerance. Each shows its
  source; defaulted ones are labelled "default — written to N symbols".
  Every field is pre-filled from its setting (step 1 table). Voltage:Min has no
  built-in value, so the first time it is blank and the search waits for a
  value.
- Candidate table, database hits first; a **Search suppliers** button for more.
  Editing any field re-runs the search. Search runs on a worker thread
  (the existing `MainWindow._start` pattern).
- Right-click a candidate → **Reject** (database status `rejected`).
- **Accept**: writes the part to the database, records usage, saves each
  entered value that was a default (not stated on the schematic) as the new
  setting for its key, and queues the step 4 plan. The window is marked dirty; **File → Save** writes it, merged with the
  other tabs' edits.

Tests: model tests for the Parts tab (grouping display, status after queueing,
`accepted_edits` shape); pure helpers for the dialog's field-source labels.
Headless smoke test on a scratch copy of a board.

### Step 6 — live check

On a scratch copy of a real board (BarrelJackAdapter or ECG): open, assign
every capacitor group, save, reopen. Then:
- every capacitor shows `assigned`;
- `kicadtools jlcpcb` exports every capacitor with an LCSC number;
- `kicadtools digikey` exports every capacitor with a DigiKey PN;
- the git diff of the schematic contains only added or changed property lines;
- KiCad opens the file and the PCB editor's "Update PCB from Schematic" picks
  up the new footprints.

## Revision 2: existing parts, descriptions, stock floor (2026-10-29)

Driven by the first live check. Real boards already have part numbers and
informative descriptions; the requirement starts from them.

What the boards contain (capacitors):

| Board | Value | MPN field | DigiKeyPN field | Description |
|---|---|---|---|---|
| BarrelJackAdapter | `C_4p7U` | C1608X6S1C475K080AC | 445-9118-1-ND | `4.7uF, 16V, X6S 603` |
| BarrelJackAdapter | `C_10U_805` | TMK212BBJ106KG-T | 587-2985-1-ND | `10uF, 25V, X5R, 805 package` |
| ECG | `100nF` | — | 1276-1000-1-ND | `Unpolarized capacitor, small symbol` |
| MotorDriver | `10uF` | 1276-1871-1-ND (a DigiKey PN) | 1276-1869-1-ND | generic |
| MotorDriver | `0.1uF` | CGA3E2X7R1E104K080AA | 13-CC0402JRX7R8BB104CT-ND (a different part) | `0.1uF, 25V, X7R 603` |
| BuckConverter | `10nF` | 1276-3388-1-ND (a DigiKey PN) | CL32B106KBJNNWE (an MPN, 10 µF) | `10uF, 50V, X7R, 1210 package` |
| MotorDriver | `1uF` | — | 1276-1445-1-ND | `1uF, 25V - 50V` |

So: Values in library style (`C_P1U` = 0.1 µF, `C_4p7U`, `C_10U_805` with a
size), MPN and DigiKeyPN fields swapped, two fields naming different parts, and
a Value that disagrees with its part (10 nF vs 10 µF).

### Evidence and precedence

Each requirement field takes the first source that has it:

| Field | 1 | 2 | 3 | 4 | 5 |
|---|---|---|---|---|---|
| Capacitance | Value (incl. library style) | existing part | Description | — | — |
| Package | Footprint | `Package:Preferred` | Value size suffix (`_805`) | existing part | Description, then default |
| Voltage:Min | `Voltage:Min` | existing part's rating | Description (lowest of a range) | remembered default | prompt |
| Dielectric | `Class:Preferred` | existing part (+ upgrades X5R/X6S/X7S → X7R, C0G ↔ NP0) | Description (+ same upgrades) | band default | — |
| Tolerance | `Tolerance:Preferred` | existing part | — | class default | — |

New sources `part` and `description`. Like `default`, their values are
written to the symbol on accept, so the requirement becomes explicit; unlike
`default`, they never update the remembered settings.

The Description fills only fields no other source states. Its disagreements
with the value used are warnings, not conflicts, and the group key compares
only the Description fields the schematic properties leave open, so library
boilerplate (`Unpolarized capacitor, small symbol`) does not split a group.

**Conflicts:** when two schematic sources (Value, Footprint,
`Package:Preferred`, the Value's size suffix) state capacitance or package and
disagree, the group is marked `conflict`, is not searchable, and the dialog
lists what each source says. Resolving conflicts in the Cockpit is TBD; for
now they are fixed on the schematic.

**Several existing parts** (decided 2026-10-29): part-number fields that name
different parts (an `LCSC` number picked independently of the DigiKey PN, a
stale `MPN`) are each a current-part option:

- An option fits when its capacitance and package agree with the schematic
  and with the first fitting option. Every fitting option is a `current`
  candidate; the one available at both suppliers and meeting the stock floor
  is kept, ranked as usual.
- The requirement's part fields come from the fitting options, loosest wins:
  lowest voltage, widest tolerance, every dielectric.
- An option that does not fit is not used, with a warning naming the
  disagreement.
- When no option fits, all are ignored (with warnings) and the search runs
  from the schematic values (BuckConverter C9: Value 10nF on 0603, part
  10 µF 1210, searches 10nF 0603).

### Existing part

- Each non-empty `MPN` / `DigiKeyPN` value is looked up at DigiKey with the
  exact-match endpoint, which accepts either kind, so swapped fields resolve.
- The LCSC number comes from the `LCSC` field, else a JLCPCB search by the
  resolved MPN (exact match).
- When the existing part is active at DigiKey, orderable at JLCPCB, meets the
  requirement and the stock floor, it is the first candidate (source
  `current`) and ranks above everything else: no churn when the part is fine.
  Otherwise the reason it was passed over is shown, and the search proceeds
  with the requirement it supplied.
- JLCPCB exact-MPN matches listed under JLCPCB's own name
  (`JLCPCB Assembly`, e.g. placeholder C9900013546 with no stock) are skipped.
- Group key includes the normalized existing part numbers, so symbols with
  different existing parts are separate groups.

### Stock floor

Generic parts (every capacitor and resistor) need at least **1000** in stock at
JLCPCB and at DigiKey, and at least the build quantity. Parts below that are
not candidates; the rejection reason says so. `MIN_STOCK = 1000`.

## Revision 3: filtered search, manufacturers, cleared fields (2026-10-30)

A search for 4.7 µF 0603 X7R ≥16 V found nothing although JLCPCB lists over
12,000 4.7 µF parts. The keyword `4.7uF 0603 16V X7R` was ranked by loose text
relevance: of 600 hits, 16 were 4.7 µF. The fix and the decisions with it:

- **One filtered JLCPCB search** (`suppliers.jlcpcb_mlcc_search`): keyword
  is the capacitance alone, with the MLCC category, package and in-stock as
  request filters, every page. For 4.7 µF 0603: 196 hits, all 4.7 µF 0603.
- **Manufacturer list** (`cap.manufacturers` setting): Murata, Samsung,
  Yageo, TDK, Taiyo Yuden, KEMET, Vishay, Wurth, Kyocera, AVX. Chosen as
  makers stocked at both suppliers; the JLCPCB counts across seven common
  values put Murata, Samsung, Taiyo Yuden, Yageo, KEMET, TDK and Walsin at the
  top among them. Walsin, Samwha, Holy Stone and Knowles were on the first
  list and removed on 2026-10-30 (user decision). Hits from other makers
  (CCTC, HRE, FH, FOJAN …) are rejected before any DigiKey lookup, which also
  cuts DigiKey calls (see item 7 below).
- **Cleared fields** (source `any`): Voltage:Min, Dielectric and Tolerance
  can be emptied (or set to `any`) in the dialog; the search applies no limit
  on them and the chosen part's own value is written (source `relaxed`).
- **Values popup** (`capsearch.facets`): per field, the values in the JLCPCB
  pool and how many parts have each, counting parts that meet the other
  fields. Shown for cleared fields, or for every field when nothing was
  found; double-click fills the field.

Result for the case above: with X7R, 71 parts in the pool and one X7R part
meeting 16 V (Murata GRM188Z71C475KE21D, DigiKey stock 37, rejected by the
floor); the popup lists X5R 28, X6S 6, X7S 3, X7R 1. With Dielectric cleared,
X5R parts from Taiyo Yuden, Samsung, Murata and Yageo are confirmed.

## Revision 4: resistors (2026-10-30)

Resistors use the capacitor code path. Decisions:

- **One code path.** `parttypes.py` holds what differs between the types
  (`PartType`: value and rating parsers and formats, the rating property,
  dielectric or none, the JLCPCB category and attribute names, the DigiKey
  parameter names, footprints, settings prefix). `capsearch.py` became
  `partsearch.py`; `CapacitorRequirement` became `PartRequirement` with a
  `kind`; `capacitance_f` / `voltage_min_v` became `nominal` / `rating_min`.
  The parts database gained a `resistor` table (resistance, power,
  tolerance) beside `capacitor`; `accept_part` / `find_parts` take the type.
- **Power floor** (`Power:Min`): stated, else the existing part's rating,
  else the Description's, else the remembered `res.power_min`, else `any` —
  no limit, and the chosen part's rating is written (for example `100mW`).
- **Tolerance:** default `res.tolerance` = 1 %. Every resistor Description on
  the boards states 1 %; resistor Descriptions are read for tolerance and
  wattage as well as value and package.
- **Manufacturers** (`res.manufacturers`): Yageo, Vishay, Panasonic,
  Stackpole, Rohm, TE Connectivity, Bourns. Chosen from the JLCPCB makers
  with at least 1000 in stock across eight common 0603 values, keeping those
  DigiKey also sells. UNI-ROYAL (most JLCPCB Basic resistors), Fenghua,
  FOJAN, HKR and RESI are left out: DigiKey does not sell them. Viking, KOA,
  Walsin and Susumu were on the first list and removed on 2026-10-30 (user
  decision).
- **JLCPCB search:** category `Resistors` / `Chip Resistor - Surface Mount`,
  keyword `10kΩ`. For 10 kΩ 0603: 254 hits, all 10 kΩ.
- **Parts tab:** capacitors and resistors in one table; the Voltage column
  became Rating (volts or watts); Dielectric is empty for a resistor.

Live check (scripted, 24 resistor groups on BarrelJackAdapter, ECG,
MotorDriver and BuckConverter): every group has at least one candidate. 14
keep their current part. 10 are searched: the current part does not fit the
schematic (MotorDriver R3/R6 `102K` with a 100 kΩ part, R17 0603 footprint
with an 0402 part), or it fails the requirement or the stock floor.

## Revision 5: inductors (2026-11-03)

Inductors use the same code path. Decisions (user, 2026-11-03):

- **Scope:** chip inductors (0402–1210) and power inductors. Every inductor
  on the boards is a power inductor (Coilcraft XGL4040 on BuckConverter and
  BarrelJackAdapter, XGL3520 on MotorDriver) with a part-specific footprint.
- **Current floor** (`Current:Min`) is compared with the saturation current
  only. A part whose supplier lists no saturation current (`-`, most chip
  inductors) is compared on its rated current. With no stated or remembered
  floor it is `any`, as a resistor's power floor, and the chosen part's
  current is written.
- **Shielding** takes the dielectric's place (`Shielding:Preferred`, default
  `ind.shielding` = `Shielded`): `Shielded`, `Semi-Shielded` or
  `Unshielded`, each accepting the better-shielded classes.
- **Tolerance:** default `ind.tolerance` = 20 %.
- **Manufacturers** (`ind.manufacturers`): Coilcraft, Murata, TDK, Taiyo
  Yuden, Bourns, Wurth, Vishay, Sunlord, Samsung, Eaton, Pulse, Abracon.
- **Package:** a chip size or a body size in millimetres (`4x4mm`). The plan
  was for the footprint to fix the body size. Neither the footprint name nor
  the footprint file gives it: Coilcraft's and Bourns' digits are length and
  height (XGL3520 is 3.5 × 3.2 mm, 2.0 mm high), and the KiCad fab outline is
  drawn larger than the body (`L_Coilcraft_XxL4040` measures 4.5 × 4.5 mm,
  `IND_XGL3520_COC` 4.2 × 3.9 mm). So a footprint with no chip size leaves the
  body size to `Package:Preferred`, the existing part or the Description,
  and the dialog's Package field takes one. Body sizes within 0.25 mm per
  side match. The footprint is kept as it is; none is written for a body
  size, and a power inductor without one stays `unassigned` with a tooltip
  saying to add one in KiCad. `Package:Preferred` is written for a body size
  so the next open reads it from the schematic.
- **JLCPCB search:** a chip size is searched in `Inductors (SMD)` and in
  `Power Inductors` with the size as the package filter; a body size in
  `Power Inductors` with no package filter (JLCPCB writes sizes as
  `SMD,4x4mm`, `SMD,4.1x4.1mm` and so on), compared after the search.
  JLCPCB lists no shielding for most chip inductors; such a hit is not
  rejected for it, DigiKey's `Shielding` decides, and the candidate takes
  DigiKey's value so the parts database records it.
- **Ferrite beads:** a symbol with an `Inductor_SMD` footprint is a ferrite
  bead when its reference starts with `FB` or its Value or Description says
  ferrite or bead (BuckConverter FB1, `FB_120_100MHz` on
  `L_0603_1608Metric`). Ferrite beads are not assigned yet (TBD 11).
- **Parts database:** an `inductor` table (inductance, saturation current,
  shielding, tolerance), created on open within schema version 2.
- **Parts tab:** the Dielectric column became Class (dielectric or
  shielding).

Live check (scripted, five inductor groups on BuckConverter,
BarrelJackAdapter, MotorDriver and NeoPMOD):

- BuckConverter L1 (XGL4040-223MEC) and BarrelJackAdapter L1 (XGL4040-103MEC):
  DigiKey no longer lists either part (exact lookup and keyword search; the
  XGL4040-151MEC sibling resolves), so the body size is not known until it
  is entered. With `4x4mm` entered, BarrelJackAdapter L1 (10 µH, 1 A from the
  Description, shielded) gives three candidates: Abracon ASPI-4030S-100M-T
  (1.95 A), Bourns SRN4018TA-100M (1.5 A) and SRN4018-100M (1.4 A). The
  search checked 1000 JLCPCB hits in 34 s; 596 were from manufacturers not
  in the list.
- MotorDriver L1 (XGL3520-472MEC, LCSC C6616438): the body size 3.5x3.2mm
  and a 2.3 A floor come from the JLCPCB part, which DigiKey does not list,
  so it is not a candidate; no JLCPCB part of that size from a listed maker
  is in stock.
- NeoPMOD L1 (`1u`, no footprint): needs a package. With `0805`, five TDK and
  Murata candidates.

## TBD (after the main loop is working)

Decisions from the review of the first build (2026-10-29):

1. **Overwrite stated schematic values.** The assign dialog shows values stated
   on the schematic read-only. Add an option to edit them there and overwrite
   the schematic property.
2. **More package options.** A footprint without a chip-size code (radial and
   axial electrolytics, e.g. MotorDriver's 47 µF `CP_Radial_D6.3mm_P2.50mm`)
   reads as not searchable. Add those packages to the requirement, the search
   and the part type's footprint table (`PartType.footprints`). Until then they show "not searchable".
3. **Manufacturer near-match.** Search candidates are recorded under JLCPCB's
   manufacturer name (`SAMSUNG`), and rejections match on manufacturer + MPN.
   Add near-match logic backed by a manufacturer synonym table in the parts
   database (`SAMSUNG` = `Samsung Electro-Mechanics`).
4. **Candidates missing a supplier number** (open, to be discussed
   separately): a candidate without a DigiKey PN or an LCSC number cannot be
   accepted today; the Accept button names the missing field.
5. **Loosest requirement from several current parts** (check after a Cockpit
   try): the loosened voltage, tolerance and dielectric also apply to the
   supplier search (MotorDriver C21 accepts X5R because its LCSC part is
   X5R). Alternative: loosen only for the current parts themselves.
6. **Search speed** (revisit after R0.1 is ready): a group with no usable
   current part takes about 20 s, almost all of it up to 30 DigiKey lookups
   run one after another (the filtered JLCPCB search takes about 5 s).
   Candidate: run the DigiKey lookups in parallel.
7. **DigiKey rate limiting** (2026-10-29): the DigiKey API blocked the
   token for too many requests. Each group makes one `productdetails` call
   per distinct existing part-number field, one per LCSC-only part, and up to
   30 for search confirmation; the live check over four boards makes a few
   hundred, and none are cached between runs (only within one
   `find_candidates` call). To look at:
   - cache DigiKey answers in the parts database or the LCSC cache, with an
     age limit for stock;
   - count calls and back off on HTTP 429, with a visible note;
   - confirm fewer search hits at DigiKey (lower `MAX_LOOKUP_FACTOR`, or
     confirm only the hit the user picks);
   - scraping the DigiKey product page instead of the API. Check DigiKey's
     terms of use and bot protection before relying on it.
8. **Zero-ohm resistors** (2026-10-30): a Value of `0` reads as "not a
   resistance", since a value of zero cannot be matched within 0.1 %. Jumpers
   need an exact-zero match and no tolerance.
9. **Resistor composition and TCR** (2026-10-30): thick or thin film and the
   temperature coefficient are not part of the requirement; a precision
   divider that needs thin film states it on the schematic today only by
   MPN.
10. **Manufacturer lists in the Cockpit** (2026-10-30): `cap.manufacturers`,
    `res.manufacturers` and `ind.manufacturers` are settings with built-in
    values; there is no editor for them yet, so a change means editing the
    `settings` table in `parts.sqlite`.
11. **Ferrite beads** (2026-11-03): classified as `ferrite` but not assigned.
    Their requirement is impedance at a frequency (`120 Ω @ 100 MHz`) and a
    current, not an inductance.
12. **Power-inductor footprints** (2026-11-03): a candidate of the same body
    size can have a different pad layout from the footprint on the symbol.
    The dialog does not compare them; reading the footprint's pads (as the
    IC footprint check does) and the part's land pattern would.
13. **Current in an inductor Value** (2026-11-03): `L_10U_1A` and
    `L_4p7U_13p5A` state a current that is not read; the Description's is.
    MotorDriver L1's Value and Description say 13.5 A, while its part's
    saturation current is 2.3 A.
14. **Power-inductor search speed** (2026-11-03): a body size has no
    package filter, so the search reads ten pages of the value in
    `Power Inductors` (about 30 s for 10 µH). Candidate: filter on JLCPCB's
    own size strings once they are known for the size.

Done in the same review: when no part meets a **default** tolerance limit, the
search drops the limit, the dialog shows an amber note naming it, and the
chosen part's own tolerance is written (`partsearch.requirement_for`, source
`relaxed`). A stated or edited tolerance is never dropped.

## Not in this plan

These overlap with the new path and should be retired or migrated in a follow-up
plan, so the code has one way to do each thing:

- `substitute.py`'s `SearchSpec` and the LCSC tab's substitute dialog, for
  capacitors and resistors — replaced by `PartRequirement` + the assign
  dialog.
- The `Alternates` property written by the substitute flow.
- `partrotation.py`'s `part_rotation` table — candidate to move into
  `supplier_pn` (LCSC rows). Gitignoring `parts.sqlite` keeps it machine-local.
- The LCSC tab's Resolve for capacitors and resistors, which the Parts tab
  supersedes.
- Other types (resistors: revision 4; inductors: revision 5; ICs:
  `IC_PLAN.md`).
- Seeding the database from capacitors and resistors already assigned on
  existing boards.
