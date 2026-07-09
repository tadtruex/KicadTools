"""Build a list of components from a KiCad schematic tree."""
from pathlib import Path
from typing import List

from kicadtools.classify import guess_intent_type
from kicadtools.intent import ElectricalIntent
from kicadtools.sexpr_parser import (
    extract_properties,
    iter_symbol_instances,
    iter_symbols_from_sexpr,
    load_schematic_tree,
    symbol_uuid,
)


def bom_components(components: List[ElectricalIntent]) -> List[ElectricalIntent]:
    """Drop the parts the BOM listing hides: power/flag symbols (``#…`` refs),
    do-not-place parts, and parts excluded from the BOM. Used by both the CLI
    ``bom`` command and the GUI so they show the same set."""
    return [
        c
        for c in components
        if not c.reference.startswith("#") and not c.dnp and c.in_bom
    ]


def get_components(root_path: Path) -> List[ElectricalIntent]:
    """
    Return one ``ElectricalIntent`` per placed symbol instance in the schematic
    at *root_path* and all of its subsheets.

    Classification precedence: an explicit ``Intent:Type`` property always wins.
    When it's absent, the intent type is *guessed* from the other properties (see
    :mod:`kicadtools.classify`); such components are flagged with
    ``intent_guessed=True`` and a ``guess_basis`` describing the signal used.

    A symbol defined on a subsheet that is instantiated more than once yields one
    component per instance (each with its own reference designator).
    """
    components: List[ElectricalIntent] = []

    for sch_path, sexpr in load_schematic_tree(Path(root_path)):
        for symbol in iter_symbols_from_sexpr(sexpr):
            props = extract_properties(symbol)
            uuid = symbol_uuid(symbol)

            for ref, _sheet_path in iter_symbol_instances(symbol):
                props["Reference"] = ref

                # Explicit Intent:Type wins; only guess to fill the gap.
                guess = None
                if not props.get("Intent:Type"):
                    guess = guess_intent_type(props)
                    if guess is not None:
                        props["Intent:Type"] = guess.intent_type

                try:
                    intent = ElectricalIntent.from_dict(props)
                except Exception as exc:
                    print(f"❌  Construction failed for {ref}: {exc}")
                    continue

                intent.source_file = sch_path
                intent.symbol_uuid = uuid

                if guess is not None:
                    intent.intent_guessed = True
                    intent.guess_basis = guess.basis
                    # If the guessed type has no registered subclass yet, make the
                    # note say so plainly instead of "unrecognized Intent:Type".
                    if type(intent) is ElectricalIntent:
                        intent.classification_note = (
                            f"looks like '{guess.intent_type}' ({guess.basis}), "
                            f"but there's no classifier for it yet"
                        )

                components.append(intent)

    return components
