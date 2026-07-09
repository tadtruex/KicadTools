"""Command-line entry point for KicadTools."""
import argparse
from pathlib import Path

from kicadtools.components import bom_components, get_components
from kicadtools.intent import ElectricalIntent


def _bom(args: argparse.Namespace) -> int:
    components = bom_components(get_components(args.schematic))
    components = sorted(components, key=lambda c: c.__class__.__name__)

    typed = guessed = untyped_count = 0
    for c in components:
        # Three outcomes: explicitly typed (✅), typed by inference (≈), or a
        # plain base component we couldn't classify (❌).
        is_untyped = type(c) is ElectricalIntent
        if is_untyped:
            status = "❌"
            untyped_count += 1
        elif c.intent_guessed:
            status = "≈"
            guessed += 1
        else:
            status = "✅"
            typed += 1

        print(
            f"{status}  "
            f"{c.reference:>4}  |  "
            f"{(c.footprint or ''):<40}  |  "
            f"{c.description()}"
        )
        if args.verbose:
            if is_untyped:
                print(f"        ↳ {c.classification_note or 'could not be classified'}")
            elif c.intent_guessed:
                print(f"        ↳ guessed from {c.guess_basis}")

    print(
        f"\n{len(components)} components "
        f"({typed} typed, {guessed} guessed, {untyped_count} unclassified)"
    )
    if guessed and not args.verbose:
        print("Re-run with -v to see how guessed (≈) and unclassified (❌) parts were handled.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="kicadtools",
        description="Read and analyze KiCad schematics (.kicad_sch).",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    bom = sub.add_parser("bom", help="List the components in a schematic")
    bom.add_argument("schematic", type=Path, help="Path to a .kicad_sch file")
    bom.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="Explain each ≈ (guessed) and ❌ (unclassified) component",
    )
    bom.set_defaults(func=_bom)

    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
