"""Command-line entry point for KicadTools."""
import argparse
from pathlib import Path

from kicadtools.components import get_components
from kicadtools.intent import ElectricalIntent


def _bom(args: argparse.Namespace) -> int:
    components = get_components(args.schematic)

    # Drop power flags (#PWR…, #FLG…), do-not-place parts, and parts the
    # designer excluded from the BOM.
    components = [c for c in components if not c.reference.startswith("#")]
    components = [c for c in components if not c.dnp]
    components = [c for c in components if c.in_bom]

    components = sorted(components, key=lambda c: c.__class__.__name__)

    for c in components:
        # An un-typed (base) component means the schematic didn't tag it with a
        # recognized Intent:Type — flag it so it can be classified later.
        status = "❌" if type(c) is ElectricalIntent else "✅"
        print(
            f"{status}  "
            f"{c.reference:>4}  |  "
            f"{(c.footprint or ''):<40}  |  "
            f"{c.description()}"
        )

    print(f"\n{len(components)} components")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="kicadtools",
        description="Read and analyze KiCad schematics (.kicad_sch).",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    bom = sub.add_parser("bom", help="List the components in a schematic")
    bom.add_argument("schematic", type=Path, help="Path to a .kicad_sch file")
    bom.set_defaults(func=_bom)

    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
