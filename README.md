# KicadTools

Read and analyze KiCad schematics (`.kicad_sch`) directly — no netlist export step.

The parser walks the schematic and all of its hierarchical subsheets, reading
each placed symbol's properties and per-instance reference designators.

## Install

```bash
cd Code/KicadTools
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
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
2. **Description** — an unambiguous type word in the `Description` text.
3. **Reference designator** — the prefix letter(s): `R`, `C`, `D`, `L`, `J`, …

If none is confident, the part is left unclassified (`❌`) for now. A guessed
type whose subclass doesn't exist yet (e.g. connector, battery) also stays `❌`,
but the reason line names what it looks like.

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
