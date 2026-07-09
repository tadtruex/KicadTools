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

## BOM Cockpit (GUI)

A PySide6 desktop app for reviewing the intent-type guesses and writing the
accepted ones back into the schematic (so the next read has fewer guesses). See
`BOMCOCKPIT.md` for the full vision; this is Milestone 1.

```bash
pip install -e ".[dev,gui]"
kicadtools-cockpit path/to/board.kicad_sch     # path is optional; File → Open too
```

- The table mirrors the `bom` listing (Ref · Status · Type · Footprint ·
  Description); each guessed (`≈`) row has an **Accept** checkbox, and hovering
  it shows why the type was guessed.
- **Accept Checked** / **Accept All** mark guesses valid (status → ✔).
- **Write Accepted Intents…** inserts a `Intent:Type` property onto each accepted
  symbol, **in place**, as a minimal edit (only new lines added — every other
  byte is untouched, so the git diff is clean). It edits each source file in the
  hierarchy and reloads; the accepted parts then read as explicit `✅`.

The write is a surgical text insertion, not a re-serialization: round-tripping
the whole schematic through the s-expression library would collapse it onto one
line. Rely on git as the backstop.

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
- `src/kicadtools/components.py` — build the component list from a schematic tree
- `src/kicadtools/cli.py` — command-line entry point (`kicadtools`)
