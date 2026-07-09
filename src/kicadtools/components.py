"""Build a list of components from a KiCad schematic tree."""
from pathlib import Path
from typing import List

from kicadtools.intent import ElectricalIntent
from kicadtools.sexpr_parser import (
    extract_properties,
    iter_symbol_instances,
    iter_symbols_from_sexpr,
    load_schematic_tree,
)


def get_components(root_path: Path) -> List[ElectricalIntent]:
    """
    Return one ``ElectricalIntent`` per placed symbol instance in the schematic
    at *root_path* and all of its subsheets.

    A symbol defined on a subsheet that is instantiated more than once yields one
    component per instance (each with its own reference designator).
    """
    components: List[ElectricalIntent] = []

    for _sch_path, sexpr in load_schematic_tree(Path(root_path)):
        for symbol in iter_symbols_from_sexpr(sexpr):
            props = extract_properties(symbol)

            for ref, _sheet_path in iter_symbol_instances(symbol):
                props["Reference"] = ref
                try:
                    intent = ElectricalIntent.from_dict(props)
                except Exception as exc:
                    print(f"❌  Construction failed for {ref}: {exc}")
                else:
                    components.append(intent)

    return components
