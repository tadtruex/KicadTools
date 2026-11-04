# LED part assignment — plan

Find parts for LEDs on the Parts tab, as for capacitors, resistors and
inductors (`CAPACITOR_PLAN.md`), with colour where those have a value.
Surface-mount LEDs are placed by JLCPCB, so a part needs an LCSC number and a
DigiKey PN, as a capacitor does. Through-hole LEDs (3 mm, 5 mm) are hand
soldered and belong with the through-hole passives (`THT_PLAN.md`, step 7).

## Decisions (2026-11-04)

| Topic | Decision |
|---|---|
| Plain LEDs | One colour on a chip footprint (0402, 0603, 0805, 1206): the full passive treatment. Grouping, the parts database, the existing part, a filtered JLCPCB search confirmed at DigiKey, the assign dialog, Autopopulate. |
| Multicolour and smart LEDs | Bicolour, RGB and LEDs with a built-in driver IC are found from what the symbol names (MPN, LCSC, DigiKeyPN, a part-number token in the Value or the footprint name) and from the parts database, as ICs are. No parametric search in the first version; a symbol that names nothing gets "name a part". |
| Colour | Matched by dominant wavelength, not by the supplier's colour label (option b). Each colour name has a wavelength band (table below), shown in the dialog. White, warm white, cool white and pink have no dominant wavelength and match by label. |
| Brightness | No default. A stated `Intensity:Min` is a minimum. |
| Single-letter Values | `R`, `G`, `B`, `Y`, `W`, `O`, `A` read as colours on an LED footprint (AtTinyDemo D3, Value `G`). |
| Ranges | As for passives: a stated minimum (intensity) or maximum (forward voltage) is met by a better part. |
| Stock | As `CAPACITOR_PLAN.md` revision 7: search hits need 1000 at both suppliers; the current part and stored parts below that are kept with a warning. |

### Colour bands (dominant wavelength)

| Colour (and spellings) | Band |
|---|---|
| ultraviolet, violet, purple | 380–435 nm |
| blue | 455–480 nm |
| cyan, blue green, ice blue | 480–510 nm |
| green, emerald green, true green | 510–540 nm |
| yellow green, lime | 555–575 nm |
| yellow | 580–595 nm |
| amber | 588–600 nm |
| orange | 598–615 nm |
| red | 615–645 nm |
| deep red | 645–670 nm |
| infrared | 700–1000 nm (peak wavelength; IR parts list no dominant one) |
| white, warm white, cool white, pink | label only |

A stated `Wavelength` (`525nm`) replaces the band with ±10 nm around it.
Bands overlap where suppliers' parts do (yellow and amber).

## Evidence from the boards

| Board | References | Footprint | Symbol |
|---|---|---|---|
| SX1509 | D1–D8 green, D9 red | `LEDs:LED_0603` | `LED_Green` / `LED_Red`, MPN `150060GS75000` / `150060RS75000` (Würth) |
| ECG | D1, D4 red | `LED_SMD:LED_0603_1608Metric` | `LED_Red`, Würth MPN |
| MotorDriver | D6, D8 green | `LED_SMD:LED_0603_1608Metric` | `LED_Green`, no part number |
| MotorDriver | D1, D4, D5 bicolour | `StockedComponents:LTST-C155GEKT` | Value `LED_RG_LTSTC155GEKT`; the footprint names the part |
| MotorDriver | U5–U8 smart RGB | `StockedComponents:4492_ADA` | Value `1515 rgb led`, no part number (the test fixture copy has LCSC `C3028831`, MEIHUA MHP1515ICRGBDT) |
| AtTinyDemo | D3 | `LED_SMD:LED_0603_1608Metric` | Value `G` |

## Supplier data (tested 2026-11-04)

JLCPCB:

- Plain LEDs: category `("Optoelectronics", "LED Indication - Discrete")`.
  The filtered search with the package filter works; a colour keyword does
  not (`green` and `blue` return nothing; `red` returns red parts, likely
  as a substring). The search lists the whole category for the package with
  an empty keyword and matches colour locally: 987 in-stock 0603 parts in
  9 s, 482 0805 parts in 3 s. One listing per package per run (cached).
- Smart RGB LEDs: category `RGB LEDs(Built-in IC)`, with per-channel
  wavelengths (`Dominant Wavelength - R`) and `Voltage - Supply`.
- Attributes: `Illumination Color`, `Wavelength - Dominant`,
  `Peak Wavelength`, `Luminous Intensity`, `Voltage - Forward(Vf)`,
  `Test Current`, `Lens Color`, `Viewing Angle`. A bicolour part lists both
  values in one attribute (`Orange Red, Yellow Green`, `569nm、621nm`).
- The colour labels do not follow the wavelength (0603, single colour):
  `Green` 486–573 nm (median 525), `Emerald Green` 510–573 (520),
  `Yellow Green` 520–575 (568), `Red` 615–650 (624), `White` 469–588 (a
  blue die's wavelength). 47 parts have no colour label.
- Basic parts: 0603 red C2286 `KT-0603R` (6.7 M in stock) and white C2290
  `KT-0603W`; 0805 yellow C2296, emerald green C2297, white C34499, red
  C84256. No 0603 green is Basic.

DigiKey (`product_for("150060RS75000")`): `Color`, `Wavelength - Dominant`,
`Wavelength - Peak`, `Millicandela Rating`, `Voltage - Forward (Vf) (Typ)`,
`Current - Test`, `Lens Color`, `Lens Transparency`, `Viewing Angle`,
`Package / Case` (`0603 (1608 Metric)`), `Configuration`.

## Build steps

### Step 1 — colour and wavelength (`ledcolor.py`)

- Colour names and spellings to bands (table above); `parse_color(text)`
  reads the Value and Description, single letters only on an LED footprint.
- `led_kind(props)`: plain (one colour), multicolour (two or more colours,
  `RG`, `RGB`, `bicolor`), smart (`WS2812`, `SK6812`, `APA102`, `NeoPixel`,
  `addressable`, `built-in IC`).
- Supplier values to numbers: `625nm`, `569nm、621nm`, `250mcd`, `2V`,
  `2V、2.1V`; colour labels for the label-only colours.
- `in_band(color, part)`: the part's dominant wavelength (peak for
  infrared) is in the band, or its label is the colour for label-only ones.

### Step 2 — LED requirement and grouping (`ledrequirement.py`)

- Fields, each with a source as for passives: package (from the footprint),
  colour and band, intensity minimum (`Intensity:Min`, mcd), forward-voltage
  maximum (`Vf:Max`), lens (`Lens:Preferred`, clear or diffused).
- The existing part (MPN, DigiKeyPN, LCSC) is looked up as for passives; its
  colour and wavelength are evidence. A stated colour whose band does not
  hold the part's wavelength is a conflict line, and the row is not
  searchable until it is resolved (the passive rule for disagreeing fields).
- Groups by package and fields by meaning (`requirement._stated_key`).
- Multicolour and smart LEDs become IC-style groups: identifiers from the
  fields, the Value tokens and the footprint-name token
  (`thtfootprint.looks_like_part_number`).

### Step 3 — search (`ledsearch.py`)

- Order as for passives: the current part, stored parts, then the JLCPCB
  category listing for the package, filtered locally by band, intensity,
  forward voltage and lens, then the maker list (`led.manufacturers`).
- Ranked as for passives (current part, uses, Basic, preferred, stock,
  price); the best hits are confirmed at DigiKey with the parameter names
  above.
- Named LEDs: the IC lookups (`find_ics`-style at JLCPCB and DigiKey), and
  a pad-count check of the footprint where the part's package gives one.

### Step 4 — parts database

- Table `led` (`CREATE TABLE IF NOT EXISTS`, as `tht` was added): package,
  colour, dominant and peak wavelength, intensity, forward voltage, lens,
  and the colour list for multicolour parts. Stored parts are found by
  package and band.

### Step 5 — Cockpit

- Parts-tab rows with class `LED`; plain rows show the colour in the Value
  column and the band in the Class column.
- Assign dialog: colour, intensity, forward voltage and lens fields; the
  band shown beside the colour; the stock warning as for passives.
- Named LEDs use the IC dialog.
- Accept writes MPN, Manufacturer, DigiKeyPN and LCSC, and `Color:Preferred`
  when the colour was read from the Value or guessed.
- Autopopulate grades LED matches as passives: exact when the stated fields
  equal the part's, near when it exceeds one (brighter, lower Vf), less sure
  when a field is unstated.

### Step 6 — live check

SX1509, ECG, MotorDriver and AtTinyDemo on scratch copies, then a Cockpit
try.

## TBD

1. Smart-LED parametric search in `RGB LEDs(Built-in IC)` (package, supply
   voltage, protocol).
2. The band table in settings, editable.
3. White colour temperature (warm and cool white by label only for now).
4. Through-hole LEDs, with `THT_PLAN.md` step 7.
