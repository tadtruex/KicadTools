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
filtered out. Each remaining component is classified from its `Intent:Type`
property (resistor, capacitor, diode, …) and printed with a short description.

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
