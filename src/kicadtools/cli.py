"""Command-line entry point for KicadTools."""
import argparse
import csv
import shutil
import sys
from pathlib import Path

from kicadtools.changes import ChangeSet, changes_from_confirm
from kicadtools.components import bom_components, get_components
from kicadtools.intent import ElectricalIntent, ResistorIntent
from kicadtools.jlcpcb import (
    build_jlcpcb_bom,
    missing_lcsc,
    undetermined_references,
    write_jlcpcb_csv,
)
from kicadtools.digikey_bom import (
    BOTH,
    MOUNTING_FILTERS,
    SMD_ONLY,
    build_digikey_bom,
    unorderable_lines,
    write_digikey_csv,
)
from kicadtools.cpl import cpl_from_schematic, write_cpl_csv
from kicadtools.partrotation import PartRotationStore, manual_rotation_for_refs
from kicadtools.mounting import mounting_kinds
from kicadtools.pcb import sibling_pcb
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
from kicadtools.writer import write_properties


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


_CONFIRM_GLYPH = {"conflict": "⚠️", "fill": "➕", "clean": "✅", "skip": "⚑"}


def run_confirm(records, resolver, *, write=False, out=print):
    """Confirm each resistor against DigiKey and collect its proposed changes.

    Network-free by construction: *resolver* supplies ``confirm(record)`` (the
    live :class:`~kicadtools.resolve.Resolver`, or a fake in tests). Prints one
    line per part via *out*, applies the accumulated plan when *write* is set,
    and returns ``(ChangeSet, tallies)`` for the caller to summarize.
    """
    changeset = ChangeSet()
    tallies = dict(confirmed=0, conflicted=0, filled=0, skipped=0, unsupported=0)

    for rec in records:
        if not isinstance(rec.intent, ResistorIntent):
            tallies["unsupported"] += 1
            continue

        result = resolver.confirm(rec)
        if result is None:
            pn = extract_part_numbers(rec.props)
            why = "no part number" if not (pn.digikey_pn or pn.mpn) else "not found on DigiKey"
            out(f"{_CONFIRM_GLYPH['skip']}  {rec.reference:>4}  |  skipped ({why})")
            tallies["skipped"] += 1
            continue

        tallies["confirmed"] += 1
        cs = changes_from_confirm(rec, result)
        changeset.extend(cs)
        if result.conflicts:
            tallies["conflicted"] += 1
            kind = "conflict"
        elif cs.changes:
            tallies["filled"] += 1
            kind = "fill"
        else:
            kind = "clean"
        out(f"{_CONFIRM_GLYPH[kind]}  {rec.reference:>4}  |  {result.summary()}")

    if write:
        plan = changeset.write_plan()
        for path, uuid_to_props in plan.items():
            write_properties(path, uuid_to_props)

    return changeset, tallies


def _confirm(args: argparse.Namespace) -> int:
    digikey = DigiKeyClient()
    if not digikey.configured:
        print(
            "⚠️  Confirmation reads the parametric truth from DigiKey, but "
            "DIGIKEY_CLIENT_ID / DIGIKEY_CLIENT_SECRET are not set. Nothing to do."
        )
        return 1

    records = iter_bom_part_records(args.schematic)
    total = len(records)
    status = _StatusLine() if args.verbose else None

    resolver = Resolver(digikey=digikey, delay=args.delay)
    if status:
        resolver.progress = lambda ref, msg: status.show(
            f"[{status.index}/{total}] {ref}: {msg}"
        )

    def out(line: str) -> None:
        if status:
            status.clear()
        print(line, flush=True)

    # Advance the status index as we go (the resolver reads it live).
    def _iter():
        for i, rec in enumerate(records, 1):
            if status:
                status.index = i
                status.show(f"[{i}/{total}] {rec.reference}: confirming…")
            yield rec

    changeset, t = run_confirm(_iter(), resolver, write=args.write, out=out)
    if status:
        status.clear()

    n_changes = len(changeset.changes)
    print(
        f"\n{total} BOM part(s): {t['confirmed']} confirmed "
        f"({t['filled']} with fills, {t['conflicted']} with conflicts), "
        f"{t['skipped']} skipped, {t['unsupported']} not a resistor (unsupported)."
    )
    if changeset.conflicts:
        print(f"\n{len(changeset.conflicts)} conflict(s) — reported, never written:")
        for c in changeset.conflicts:
            print(f"  {c.describe()}")

    if not n_changes:
        print("\nNo intent fields to fill. Nothing to write.")
        return 1 if changeset.conflicts else 0

    if args.write:
        plan = changeset.write_plan()
        print(f"\nWrote {n_changes} propert{'y' if n_changes == 1 else 'ies'} "
              f"across {len(plan)} file(s).")
    else:
        print(f"\n{n_changes} proposed change(s):")
        for ch in changeset.changes:
            print(f"  {ch.describe()}")
        print("\nReport only — re-run with --write to apply these changes.")

    return 1 if changeset.conflicts else 0


def _export_jlcpcb(args: argparse.Namespace) -> int:
    records = iter_bom_part_records(args.schematic)

    mounting: dict = {}
    if args.smd:
        mounting, pcb = mounting_kinds(records, args.schematic)
        if pcb is None:
            print(
                f"⚠️  --smd: no {Path(args.schematic).with_suffix('.kicad_pcb').name} "
                "found next to the schematic — mounting is read from the library "
                "footprints instead.",
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


def _export_digikey(args: argparse.Namespace) -> int:
    records = iter_bom_part_records(args.schematic)
    mode = args.mounting

    # Mounting is only needed when the export is filtered; "both" keeps
    # everything and never touches the board.
    mounting: dict = {}
    if mode != BOTH:
        mounting, pcb = mounting_kinds(records, args.schematic)
        if pcb is None and mode == SMD_ONLY:
            print(
                f"⚠️  --mounting smd: no {Path(args.schematic).with_suffix('.kicad_pcb').name} "
                "found next to the schematic — mounting is read from the library "
                "footprints instead.",
                file=sys.stderr,
            )
        # non-smd with no PCB needs no warning: every part reads "unknown", which
        # is not SMD, so the whole board is kept — the documented behaviour.

    lines = build_digikey_bom(records, mounting=mounting, mounting_filter=mode)

    if mode == SMD_ONLY:
        undetermined = undetermined_references(records, mounting)
        if undetermined:
            print(
                f"⚠️  {len(undetermined)} part(s) excluded from the SMD BOM — "
                f"undetermined mounting: {', '.join(undetermined)}",
                file=sys.stderr,
            )

    if args.output:
        with open(args.output, "w", newline="", encoding="utf-8") as f:
            write_digikey_csv(lines, f)
        print(f"Wrote {len(lines)} line(s) to {args.output}", file=sys.stderr)
    else:
        # No -o: emit pure CSV on stdout so `… > bom.csv` works; keep all
        # human-facing output on stderr.
        write_digikey_csv(lines, sys.stdout)

    unorderable = unorderable_lines(lines)
    if unorderable:
        refs = ", ".join(ln.references[0] for ln in unorderable)
        print(
            f"⚠️  {len(unorderable)} of {len(lines)} line(s) have no DigiKey or "
            f"manufacturer part number (blank cells — DigiKey can't match them): {refs}",
            file=sys.stderr,
        )
    # Non-zero exit when anything is unorderable, so scripts can gate — same
    # stance as the `jlcpcb` and `lcsc` commands. The CSV is written in full.
    return 1 if unorderable else 0


def _export_cpl(args: argparse.Namespace) -> int:
    pcb = sibling_pcb(args.schematic)
    if pcb is None:
        print(
            f"⚠️  no {Path(args.schematic).with_suffix('.kicad_pcb').name} found next "
            "to the schematic — the CPL is built from the board, so place the parts "
            "first. Nothing written.",
            file=sys.stderr,
        )
        return 1

    # Restrict the CPL to the BOM's reference designators so BOM and CPL match
    # exactly (a JLCPCB requirement). With --smd, that BOM ref set is SMD-only.
    records = iter_bom_part_records(args.schematic)
    mounting: dict = {}
    if args.smd:
        mounting, _ = mounting_kinds(records, args.schematic)
        include = {r.reference for r in records if mounting.get(r.reference) == "smd"}
    else:
        include = {r.reference for r in records}

    # Apply any per-LCSC rotation overrides the user has cached (e.g. SOT-23
    # transistors the family table leaves at 0°). Map each reference to its LCSC
    # part, look the override up, and hand build_cpl a {ref: degrees} dict.
    # With the parts database on the server (DB_PLAN.md), the overrides are
    # there; a server that cannot be reached is an error, not a silent use of
    # an old local copy, since a wrong rotation puts parts on the board turned.
    ref_to_lcsc = {
        r.reference: extract_part_numbers(r.props).lcsc or "" for r in records
    }
    try:
        manual_rotation = _server_rotations(ref_to_lcsc)
    except RuntimeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    if manual_rotation is None:
        store = PartRotationStore(args.cache)
        manual_rotation = manual_rotation_for_refs(ref_to_lcsc, store)
        store.close()

    lines = cpl_from_schematic(pcb, include=include, manual_rotation=manual_rotation)

    placed = {ln.designator for ln in lines}
    if manual_rotation:
        applied = sorted(r for r in manual_rotation if r in placed)
        if applied:
            print(
                f"↻  applied cached rotation to {len(applied)} part(s): "
                f"{', '.join(applied)}",
                file=sys.stderr,
            )

    missing = sorted(include - placed, key=lambda r: r)
    if missing:
        print(
            f"⚠️  {len(missing)} BOM part(s) have no footprint on the board (not in "
            f"the CPL): {', '.join(missing)}",
            file=sys.stderr,
        )

    if args.output:
        with open(args.output, "w", newline="", encoding="utf-8") as f:
            write_cpl_csv(lines, f)
        print(f"Wrote {len(lines)} placement(s) to {args.output}", file=sys.stderr)
    else:
        write_cpl_csv(lines, sys.stdout)
    return 0


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


def _server_rotations(ref_to_lcsc):
    """``{ref: degrees}`` from the server's rotation overrides when the
    configuration names the server; ``None`` when it names a SQLite file.
    Raises ``RuntimeError`` when the server cannot be opened."""
    from kicadtools.dbconfig import MARIADB, ConfigError, load_config, terminal_ask
    from kicadtools.dbconnect import open_parts_db
    from kicadtools.partrotation import DbRotationStore

    try:
        config = load_config()
    except ConfigError as exc:
        raise RuntimeError(str(exc)) from None
    if config.backend != MARIADB:
        return None
    try:
        session = open_parts_db(config, terminal_ask)
    except Exception as exc:  # noqa: BLE001 - reported
        raise RuntimeError(f"could not open {config.label} for the rotation overrides: "
                           f"{exc or 'cancelled'}") from None
    try:
        return manual_rotation_for_refs(ref_to_lcsc, DbRotationStore(lambda: session.db))
    finally:
        session.close()


def _db_pull(args) -> int:
    """``kicadtools db pull [PATH]``: write the server's parts database to a
    SQLite file, by default a dated backup in ~/.kicadtools/backups
    (DB_PLAN.md step 7)."""
    from kicadtools.dbbackup import backup_now, pull
    from kicadtools.dbconfig import MARIADB, ConfigError, PasswordCancelled, load_config, terminal_ask
    from kicadtools.dbconnect import open_parts_db

    try:
        config = load_config()
    except ConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    if config.backend != MARIADB:
        print(f"error: the configuration ({config.source}) names no server", file=sys.stderr)
        return 2
    try:
        session = open_parts_db(config, terminal_ask)
    except PasswordCancelled:
        print("cancelled", file=sys.stderr)
        return 1
    except Exception as exc:  # noqa: BLE001 - TunnelError, PyMySQL errors: reported
        print(f"error: could not open {config.label}: {exc}", file=sys.stderr)
        return 1
    try:
        if args.path:
            target = Path(args.path).expanduser()
            counts = pull(session.db, target)
        else:
            target = backup_now(session.db)
            counts = None
    finally:
        session.close()
    print(f"{config.label} -> {target}")
    if counts is not None:
        print("  " + ", ".join(f"{t} {n}" for t, n in counts.items()))
    return 0


def _db_import(args) -> int:
    """``kicadtools db import``: the one-time copy of the local parts file and
    the CPL rotation overrides into the empty server (DB_PLAN.md step 5)."""
    from kicadtools import partsdb
    from kicadtools.dbconfig import MARIADB, ConfigError, PasswordCancelled, load_config, terminal_ask
    from kicadtools.dbconnect import open_parts_db
    from kicadtools.dbimport import DbImportError, import_parts, imported_name

    try:
        config = load_config()
    except ConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    if config.backend != MARIADB:
        print(f"error: the configuration ({config.source}) names no server; set "
              "[parts_db] backend = \"mariadb\" in ~/.kicadtools/config.toml",
              file=sys.stderr)
        return 2
    source = Path(args.source).expanduser() if args.source else partsdb.default_path()
    cache = Path(args.cache).expanduser()
    rotations = PartRotationStore(cache).all() if cache.exists() else {}
    try:
        session = open_parts_db(config, terminal_ask)
    except PasswordCancelled:
        print("cancelled", file=sys.stderr)
        return 1
    except Exception as exc:  # noqa: BLE001 - TunnelError, PyMySQL errors: reported
        print(f"error: could not open {config.label}: {exc}", file=sys.stderr)
        return 1
    try:
        report = import_parts(source, session.db, rotations, dry_run=args.dry_run)
    except DbImportError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    finally:
        session.close()
    print(f"{source} -> {config.label}" + (" (dry run: rolled back)" if args.dry_run else ""))
    for line in report.lines():
        print("  " + line)
    # Only the default file is renamed, so no tool opens it again by mistake;
    # a file named with --from (a pulled export, a backup) is left as it is.
    if not args.dry_run and source.resolve() == partsdb.default_path().resolve():
        target = imported_name(source)
        source.rename(target)
        print(f"renamed {source.name} to {target.name}; the server is now the parts database")
    return 0


def _inventory_import(args) -> int:
    """``kicadtools inventory import FILE --site SITE``: the one-time copy of
    the master component list into the empty inventory of the configured
    parts database (INVENTORY_PLAN.md step 2)."""
    from kicadtools.dbconfig import ConfigError, PasswordCancelled, load_config, terminal_ask
    from kicadtools.dbconnect import open_parts_db
    from kicadtools.inventorydb import InventoryDb
    from kicadtools.inventoryimport import (InventoryImportError, convert, import_items,
                                            read_table)

    source = Path(args.file).expanduser()
    try:
        items, report = convert(read_table(source))
    except InventoryImportError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    try:
        config = load_config()
    except ConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    try:
        session = open_parts_db(config, terminal_ask)
    except PasswordCancelled:
        print("cancelled", file=sys.stderr)
        return 1
    except Exception as exc:  # noqa: BLE001 - TunnelError, PyMySQL errors: reported
        print(f"error: could not open {config.label}: {exc}", file=sys.stderr)
        return 1
    try:
        report = import_items(InventoryDb(session.db), items, site=args.site,
                              report=report, dry_run=args.dry_run)
    except InventoryImportError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    finally:
        session.close()
    print(f"{source} -> {config.label}, site {args.site}")
    for line in report.lines():
        print("  " + line)
    return 0


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

    confirm = sub.add_parser(
        "confirm",
        help="Confirm each resistor against DigiKey and fill missing intent fields",
    )
    confirm.add_argument("schematic", type=Path, help="Path to a .kicad_sch file")
    confirm.add_argument(
        "--write",
        action="store_true",
        help="Apply the proposed fills to the schematic (default: report only)",
    )
    confirm.add_argument(
        "--delay",
        type=float,
        default=0.5,
        help="Seconds to wait after each live DigiKey call (default: 0.5)",
    )
    confirm.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="Show a live status line (current part + lookup in flight)",
    )
    confirm.set_defaults(func=_confirm)

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
        ".kicad_pcb, else the library footprint; parts of undetermined mounting "
        "are excluded and reported)",
    )
    jlc.set_defaults(func=_export_jlcpcb)

    dk = sub.add_parser(
        "digikey",
        help="Export a BOM CSV in the format DigiKey's cart / BOM uploader ingests",
    )
    dk.add_argument("schematic", type=Path, help="Path to a .kicad_sch file")
    dk.add_argument(
        "-o",
        "--output",
        type=Path,
        metavar="CSV",
        help="Write the BOM here (default: stdout)",
    )
    dk.add_argument(
        "--mounting",
        choices=MOUNTING_FILTERS,
        default=BOTH,
        help="Which parts to include by mounting, read from the sibling .kicad_pcb "
        "or else the library footprint: "
        "'smd' = surface-mount only (undetermined parts excluded and reported); "
        "'non-smd' = everything else (through-hole and undetermined); "
        "'both' = every part, no PCB needed (default: both)",
    )
    dk.set_defaults(func=_export_digikey)

    cpl = sub.add_parser(
        "cpl",
        help="Export a CPL (pick-and-place) CSV in the format JLCPCB expects",
    )
    cpl.add_argument("schematic", type=Path, help="Path to a .kicad_sch file")
    cpl.add_argument(
        "-o",
        "--output",
        type=Path,
        metavar="CSV",
        help="Write the CPL here (default: stdout)",
    )
    cpl.add_argument(
        "--smd",
        action="store_true",
        help="Only include surface-mount parts (mounting read from the .kicad_pcb "
        "or the library footprint), "
        "matching a --smd BOM",
    )
    cpl.add_argument(
        "--cache",
        type=Path,
        default=_DEFAULT_CACHE,
        metavar="SQLITE",
        help=f"SQLite store of per-LCSC rotation overrides (default: {_DEFAULT_CACHE})",
    )
    cpl.set_defaults(func=_export_cpl)

    db = sub.add_parser("db", help="The parts database on the server (DB_PLAN.md)")
    db_sub = db.add_subparsers(dest="db_command", required=True)
    imp = db_sub.add_parser(
        "import",
        help="Copy the local parts database and rotation overrides into the empty server, once",
    )
    imp.add_argument("--from", dest="source", default=None,
                     help="the SQLite parts file, such as one written by `kicadtools db "
                          "pull` (default: ~/Code/Boards/parts.sqlite, renamed after the "
                          "import)")
    imp.add_argument("--cache", default=str(_DEFAULT_CACHE),
                     help="the cache holding older CPL rotation overrides; the source "
                          "file's own overrides are used where both have one "
                          "(default: ~/.kicadtools/lcsc_cache.sqlite)")
    imp.add_argument("--dry-run", action="store_true",
                     help="copy and compare, then roll back; the local file is not renamed")
    imp.set_defaults(func=_db_import)
    pull_cmd = db_sub.add_parser(
        "pull", help="Write the server's parts database to a SQLite file (a backup)")
    pull_cmd.add_argument("path", nargs="?", default=None,
                          help="the file to write (default: a dated backup in "
                               "~/.kicadtools/backups, keeping the newest 10)")
    pull_cmd.set_defaults(func=_db_pull)

    inv = sub.add_parser("inventory", help="The lab inventory (INVENTORY_PLAN.md)")
    inv_sub = inv.add_subparsers(dest="inventory_command", required=True)
    inv_imp = inv_sub.add_parser(
        "import",
        help="Copy the master component list (.numbers or .csv) into the empty inventory, once")
    inv_imp.add_argument("file", help="the spreadsheet: .numbers, or a CSV export of it")
    inv_imp.add_argument("--site", required=True,
                         help="the site of every imported bin, such as school")
    inv_imp.add_argument("--dry-run", action="store_true",
                         help="convert and write everything, then roll back")
    inv_imp.set_defaults(func=_inventory_import)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
