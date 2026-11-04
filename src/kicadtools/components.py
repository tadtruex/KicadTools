"""Build a list of components from a KiCad schematic tree."""
from pathlib import Path
from typing import List, Optional

from kicadtools.classify import guess_intent_type
from kicadtools.intent import ElectricalIntent
from kicadtools.thtfootprint import virtual_part
from kicadtools.sexpr_parser import (
    extract_properties,
    iter_symbol_instances,
    iter_symbols_from_sexpr,
    load_schematic_tree,
    symbol_uuid,
)


def build_intent(
    props: dict,
    source_file: Optional[Path] = None,
    symbol_uuid: Optional[str] = None,
) -> Optional[ElectricalIntent]:
    """Build one typed :class:`ElectricalIntent` from a raw property dict.

    ``props`` must already carry ``Reference``. When it has no explicit
    ``Intent:Type`` the type is *guessed* (see :mod:`kicadtools.classify`) and
    the result is flagged with ``intent_guessed=True`` + a ``guess_basis``.
    Provenance (*source_file*, *symbol_uuid*) is stamped on for later write-back.
    Returns ``None`` if the intent can't be constructed at all.

    The caller's dict is not mutated. This is the single classification path
    shared by :func:`get_components` (the BOM/GUI view) and the resolver's
    :func:`kicadtools.resolve.iter_bom_part_records`, so both agree on the type.
    """
    props = dict(props)  # never mutate the caller's dict

    guess = None
    if not props.get("Intent:Type"):
        guess = guess_intent_type(props)
        if guess is not None:
            props["Intent:Type"] = guess.intent_type

    try:
        intent = ElectricalIntent.from_dict(props)
    except Exception as exc:
        print(f"❌  Construction failed for {props.get('Reference')}: {exc}")
        return None

    intent.source_file = source_file
    intent.symbol_uuid = symbol_uuid

    if guess is not None:
        intent.intent_guessed = True
        intent.guess_basis = guess.basis
        # If the guessed type has no registered subclass yet, make the note say
        # so plainly instead of "unrecognized Intent:Type".
        if type(intent) is ElectricalIntent:
            intent.classification_note = (
                f"looks like '{guess.intent_type}' ({guess.basis}), "
                f"but there's no classifier for it yet"
            )

    return intent


def is_virtual_part(footprint: object, value: object = "") -> bool:
    """True for a solder jumper or net tie: copper on the board, not a part
    to buy or place, so it is left out of every BOM, the CPL and the
    Cockpit's lists whatever its ``in_bom`` flag says
    (:func:`kicadtools.thtfootprint.virtual_part`)."""
    return bool(virtual_part(footprint if isinstance(footprint, str) else "",
                             value if isinstance(value, str) else ""))


def bom_components(components: List[ElectricalIntent]) -> List[ElectricalIntent]:
    """Drop the parts the BOM listing hides: power/flag symbols (``#…`` refs),
    do-not-place parts, parts excluded from the BOM, and solder jumpers and
    net ties (:func:`is_virtual_part`). Used by both the CLI ``bom`` command
    and the GUI so they show the same set."""
    return [
        c
        for c in components
        if not c.reference.startswith("#") and not c.dnp and c.in_bom
        and not is_virtual_part(c.footprint, getattr(c, "value", ""))
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
                intent = build_intent(props, sch_path, uuid)
                if intent is not None:
                    components.append(intent)

    return components
