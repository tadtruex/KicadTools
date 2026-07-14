"""Command-line entry point for KicadTools."""
import argparse
import csv
import shutil
import sys
from pathlib import Path

from kicadtools.components import bom_components, get_components
from kicadtools.intent import ElectricalIntent
from kicadtools.jlcpcb import (
    build_jlcpcb_bom,
    missing_lcsc,
    undetermined_references,
    write_jlcpcb_csv,
)
from kicadtools.pcb import read_mounting_for_schematic
from kicadtools.normalize import (
    compute_file_changes,
    plan_normalization,
    write_file_changes,
)
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


class _StatusLine:
    """A single, in-place status line written to stderr via carriage return.

    Kept off stdout so the normal per-part result lines (and any redirect of
    them to a file) stay clean. ``index`` is the current 1-based part number,
    read live by the resolver's progress callback.
    """

    def __init__(self) -> None:
        self.index = 0
        self._prev_len = 0

    def show(self, text: str) -> None:
        width = shutil.get_terminal_size((80, 20)).columns - 1
        text = text[:width]
        pad = " " * max(0, self._prev_len - len(text))
        sys.stderr.write("\r" + text + pad)
        sys.stderr.flush()
        self._prev_len = len(text)

    def clear(self) -> None:
        if self._prev_len:
            sys.stderr.write("\r" + " " * self._prev_len + "\r")
            sys.stderr.flush()
            self._prev_len = 0


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

    total = len(records)
    status = _StatusLine() if args.verbose else None

    resolver = Resolver(digikey=digikey, cache=cache, delay=args.delay)
    if status:
        # index is set per-part just below; the callback reads it live.
        resolver.progress = lambda ref, msg: status.show(
            f"[{status.index}/{total}] {ref}: {msg}"
        )

    counts = {Status.OK: 0, Status.RESOLVED: 0, Status.FLAGGED: 0}
    resolutions = []
    for i, rec in enumerate(records, 1):
        if status:
            status.index = i
            status.show(f"[{i}/{total}] {rec.reference}: reading part numbers…")
        res = resolver.resolve(rec)
        resolutions.append(res)
        counts[res.status] += 1
        if status:
            status.clear()
        # flush so progress is visible even when stdout is piped to a file.
        print(
            f"{_STATUS_GLYPH[res.status]}  "
            f"{res.reference:>4}  |  "
            f"{(res.lcsc or '—'):<10}  |  "
            f"{res.reason}",
            flush=True,
        )

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


def _normalize(args: argparse.Namespace) -> int:
    plan = plan_normalization(args.schematic)
    if not plan:
        print("✅  All part-number keys are already canonical — nothing to do.")
        return 0

    changes = compute_file_changes(plan)
    total_edits = sum(c.edit_count for c in changes)
    total_syms = sum(len(c.symbols) for c in changes)

    for ch in changes:
        print(f"\n{ch.path.name}  ({ch.edit_count} edits, {len(ch.symbols)} symbols)")
        for sym in ch.symbols:
            print(f"  {sym.reference}")
            for edit in sym.edits:
                print(f"      {edit.describe()}")

    verb = "Would normalize" if args.dry_run else "Normalized"
    noun = "property" if total_edits == 1 else "properties"
    print(
        f"\n{verb} {total_edits} {noun} across {total_syms} "
        f"symbol(s) in {len(changes)} file(s)."
    )

    if args.dry_run:
        print("Dry run — no files written. Re-run without -n/--dry-run to apply.")
        return 0

    written = write_file_changes(changes)
    print(f"Wrote {len(written)} file(s).")
    return 0


def _export_jlcpcb(args: argparse.Namespace) -> int:
    records = iter_bom_part_records(args.schematic)

    mounting: dict = {}
    if args.smd:
        mounting, pcb = read_mounting_for_schematic(args.schematic)
        if pcb is None:
            print(
                f"⚠️  --smd: no {Path(args.schematic).with_suffix('.kicad_pcb').name} "
                "found next to the schematic — mounting can't be determined, so the "
                "SMD BOM will be empty. Lay out / place the board first.",
                file=sys.stderr,
            )

    lines = build_jlcpcb_bom(records, mounting=mounting, smd_only=args.smd)

    if args.smd:
        undetermined = undetermined_references(records, mounting)
        if undetermined:
            print(
                f"⚠️  {len(undetermined)} part(s) excluded from the SMD BOM — "
                f"undetermined mounting: {', '.join(undetermined)}",
                file=sys.stderr,
            )

    if args.output:
        with open(args.output, "w", newline="", encoding="utf-8") as f:
            write_jlcpcb_csv(lines, f)
        print(f"Wrote {len(lines)} line(s) to {args.output}", file=sys.stderr)
    else:
        # No -o: emit pure CSV on stdout so `… > bom.csv` works; keep all
        # human-facing output on stderr.
        write_jlcpcb_csv(lines, sys.stdout)

    miss = missing_lcsc(lines)
    if miss:
        refs = ", ".join(ln.references[0] for ln in miss)
        print(
            f"⚠️  {len(miss)} of {len(lines)} line(s) have no LCSC Part # "
            f"(blank cell — match them manually in JLCPCB's uploader): {refs}",
            file=sys.stderr,
        )
    # Non-zero exit when anything lacks an LCSC, so scripts can gate — same
    # stance as the `lcsc` command. The CSV is written in full regardless.
    return 1 if miss else 0


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


def main(argv=None) -> int:
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

    norm = sub.add_parser(
        "normalize",
        help="Rewrite part-number property keys to their canonical spelling",
    )
    norm.add_argument("schematic", type=Path, help="Path to a .kicad_sch file")
    norm.add_argument(
        "-n",
        "--dry-run",
        action="store_true",
        dest="dry_run",
        help="Print what would change without writing any files",
    )
    norm.set_defaults(func=_normalize)

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
    lcsc.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="Show a live status line (current part + lookup in flight)",
    )
    lcsc.set_defaults(func=_lcsc)

    jlc = sub.add_parser(
        "jlcpcb",
        help="Export a BOM CSV in the format JLCPCB's assembly service expects",
    )
    jlc.add_argument("schematic", type=Path, help="Path to a .kicad_sch file")
    jlc.add_argument(
        "-o",
        "--output",
        type=Path,
        metavar="CSV",
        help="Write the BOM here (default: stdout)",
    )
    jlc.add_argument(
        "--smd",
        action="store_true",
        help="Only include surface-mount parts (mounting read from the sibling "
        ".kicad_pcb; parts of undetermined mounting are excluded and reported)",
    )
    jlc.set_defaults(func=_export_jlcpcb)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
