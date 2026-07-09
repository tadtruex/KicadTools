# KicadTools — working notes

Scratch pad for cross-machine handoff: where things stand and what's next.
Not user docs (see `README.md` for those).

## Where we are (2026-07-08)

Fresh project based on `../kicad_tools` (`/Users/tad/Code/kicad_tools`), started
clean. Reads `.kicad_sch` directly (no netlist export), walking hierarchical
subsheets. 10 tests passing.

Latest feature: **intent guessing** — when a symbol has no explicit
`Intent:Type` property, the type is inferred in `classify.py`, trying signals in
priority order and taking the first confident answer:

1. footprint library prefix (`Resistor_SMD`, `Capacitor_SMD`, `LED_SMD`, …)
2. unambiguous `Description` keyword
3. reference-designator prefix (`R`, `C`, `D`, `BT`, `J`, …)

Explicit `Intent:Type` always wins over a guess. CLI marks lines `✅` explicit /
`≈` guessed / `❌` unclassified; `kicadtools bom -v <sch>` explains each `≈`/`❌`.
On the ECG board: 34 guessed, 28 unclassified.

## Resume checklist (new machine)

```bash
git pull
git submodule update --init --recursive
cd Code/KicadTools
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
pytest                                    # expect 10 passed
kicadtools bom -v ../../ECG/ECG/ECG.kicad_sch
```

## Next steps / open decisions

- Add intent subclasses for types that are guessable but have no class yet:
  `led` (easiest), then `connector`, `battery`, `ic`, `switch`, `transistor`.
  Until a subclass exists they're guessed but stay `❌` (the `-v` reason names
  what they look like).
- Description matching only fires when exactly one type word is present
  (ambiguous → no guess). Revisit if that's too conservative.
- Latent mismatch: `SpecificPartIntent._from_dict_impl` reads `DigiKeyPN`, but
  the ECG schematics store the property as `DigikeyPN` (lowercase k). Every ECG
  part has a `DigikeyPN` — worth normalizing the field lookup.
