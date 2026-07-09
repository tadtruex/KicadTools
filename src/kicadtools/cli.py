"""Command-line entry point for KicadTools."""
import argparse
import csv
from pathlib import Path

from kicadtools.components import bom_components, get_components
from kicadtools.intent import ElectricalIntent
from kicadtools.partnum import extract_part_numbers
from kicadtools.resolve import (
    Cache,
    Resolver,
    Status,
    iter_bom_part_records,
)
from kicadtools.suppliers import DigiKeyClient


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


_STATUS_GLYPH = {
    Status.OK: "✅",       # already had an LCSC number
    Status.RESOLVED: "➕",  # looked one up
    Status.FLAGGED: "⚑",   # needs manual attention
}

_DEFAULT_CACHE = Path.home() / ".kicadtools" / "lcsc_cache.sqlite"


def _lcsc(args: argparse.Namespace) -> int:
    records = iter_bom_part_records(args.schematic)

    digikey = DigiKeyClient()
    part_numbers = [extract_part_numbers(r.props) for r in records]
    needs_digikey = any(
        pn.digikey_pn and not pn.lcsc and not pn.mpn for pn in part_numbers
    )
    if needs_digikey and not digikey.configured:
        print(
            "⚠️  Some parts have only a DigiKey PN, but DIGIKEY_CLIENT_ID / "
            "DIGIKEY_CLIENT_SECRET are not set — those parts will be flagged.\n"
        )

    cache = None
    if not args.no_cache:
        args.cache.parent.mkdir(parents=True, exist_ok=True)
        cache = Cache(args.cache)

    resolver = Resolver(digikey=digikey, cache=cache, delay=args.delay)
    resolutions = resolver.resolve_all(records)

    counts = {Status.OK: 0, Status.RESOLVED: 0, Status.FLAGGED: 0}
    for rec, res in zip(records, resolutions):
        counts[res.status] += 1
        print(
            f"{_STATUS_GLYPH[res.status]}  "
            f"{res.reference:>4}  |  "
            f"{(res.lcsc or '—'):<10}  |  "
            f"{res.reason}"
        )

    total = len(records)
    print(
        f"\n{total} BOM parts "
        f"({counts[Status.OK]} already had LCSC, "
        f"{counts[Status.RESOLVED]} resolved, "
        f"{counts[Status.FLAGGED]} flagged)"
    )
    with_lcsc = counts[Status.OK] + counts[Status.RESOLVED]
    if total:
        print(f"{with_lcsc}/{total} parts now have an LCSC number.")

    if args.report:
        _write_report(args.report, records, resolutions)
        print(f"\nReport written to {args.report}")

    if cache:
        cache.close()

    # Non-zero exit when anything is still missing an LCSC, so scripts can gate.
    return 1 if counts[Status.FLAGGED] else 0


def _write_report(path, records, resolutions) -> None:
    fields = [
        "Reference", "Value", "Footprint", "Status", "Entry",
        "LCSC", "MPN", "DigiKeyPN", "Manufacturer", "Reason", "SourceFile",
    ]
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        for rec, res in zip(records, resolutions):
            pn = res.part
            writer.writerow({
                "Reference": res.reference,
                "Value": rec.props.get("Value", ""),
                "Footprint": rec.props.get("Footprint", ""),
                "Status": res.status.value,
                "Entry": res.entry,
                "LCSC": res.lcsc or "",
                "MPN": res.resolved_mpn or pn.mpn or "",
                "DigiKeyPN": pn.digikey_pn or "",
                "Manufacturer": pn.manufacturer or "",
                "Reason": res.reason,
                "SourceFile": Path(res.source_file).name,
            })


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

    lcsc = sub.add_parser(
        "lcsc",
        help="Resolve every BOM part to an LCSC (JLCPCB) part number",
    )
    lcsc.add_argument("schematic", type=Path, help="Path to a .kicad_sch file")
    lcsc.add_argument(
        "--report",
        type=Path,
        metavar="CSV",
        help="Write a detailed per-part CSV report to this path",
    )
    lcsc.add_argument(
        "--cache",
        type=Path,
        default=_DEFAULT_CACHE,
        help=f"SQLite lookup cache (default: {_DEFAULT_CACHE})",
    )
    lcsc.add_argument(
        "--no-cache", action="store_true", help="Disable the lookup cache"
    )
    lcsc.add_argument(
        "--delay",
        type=float,
        default=0.5,
        help="Seconds to wait after each live supplier call (default: 0.5)",
    )
    lcsc.set_defaults(func=_lcsc)

    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
