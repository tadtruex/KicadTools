"""List through-hole parts without a DigiKey PN across the boards, by tier.

Run from Code/KicadTools: ``.venv/bin/python scripts/tht_survey.py [boards.txt]``.
*boards.txt* lists schematic paths relative to the repo root, one per line;
without it every ``.kicad_sch`` with a ``.kicad_pcb`` beside it is surveyed
(Libraries and backups skipped). See THT_PLAN.md, "Evidence from the boards".
"""
import re, sys
from collections import defaultdict
from pathlib import Path
from kicadtools.resolve import iter_bom_part_records
from kicadtools.partnum import extract_part_numbers
from kicadtools.classify import guess_intent_type
from kicadtools.pcb import read_mounting_for_schematic, THT, SMD, sibling_pcb
from kicadtools.footprints import FootprintPads
from kicadtools.repo import repo_root

def lib_mounting(fpads, lib_id):
    path = fpads.footprint_file(lib_id) if lib_id else None
    if path is None:
        return None, ""
    t = path.read_text(errors="replace")
    m = re.search(r"\(attr ([^)]*)\)", t)
    attr = m.group(1) if m else ""
    th = len(re.findall(r"\(pad \S+ thru_hole", t)); smd = len(re.findall(r"\(pad \S+ smd", t))
    pads = f"thru={th} smd={smd}"
    if "smd" in attr: return SMD, pads
    if "through_hole" in attr: return THT, pads
    return ("tht?" if th else ("smd?" if smd else None)), pads

B = repo_root()
if B is None:
    sys.exit("not in a Boards checkout; set KICADTOOLS_REPO_ROOT")
if len(sys.argv) > 1:
    BOARDS = [l.strip() for l in open(sys.argv[1]) if l.strip()]
else:
    BOARDS = sorted(
        str(p.with_suffix(".kicad_sch").relative_to(B))
        for p in B.rglob("*.kicad_pcb")
        if p.with_suffix(".kicad_sch").exists()
        and not any(x in p.parts for x in ("Libraries", ".history"))
        and "backup" not in str(p).lower())
INTENT = {"capacitor", "resistor", "inductor", "crystal", "potentiometer", "ferrite"}
NOPART = re.compile(r"SolderWirePad|MountingHole_\d|Fiducial|NetTie|SolderJumper", re.I)
GENERIC = re.compile(r"^(Connector_PinHeader|Connector_PinSocket|TerminalBlock|Connector_IDC|"
                     r"Connector_JST|Connector_Molex|Connector_Phoenix|Connector_Wago|"
                     r"Connector_Dsub|Connector_USB|Connector_Audio|Connector_BarrelJack|"
                     r"Connector_Coaxial|Connector_Card|Button_Switch_THT|Package_DIP|Socket)", re.I)
GENERIC_NAME = re.compile(r"PinHeader|PinSocket|TerminalBlock_[A-Za-z]+_|IDC-Header|DIP-\d+", re.I)
VENDOR = re.compile(r"JST|CUI|Samtec|Bourns|CalTest|Harwin|Keystone|Molex|TE_|Phoenix|Wurth|"
                    r"Amphenol|Hirose|Kycon|Switchcraft|Littelfuse|Omron|C&K|E-Switch|MPD", re.I)
GENERIC_VALUE = re.compile(r"^(Conn_|Screw_Terminal|TestPoint|Jumper|SW_|Barrel_Jack|USB_|"
                           r"[0-9.]+[pnuµmkMRF]?\w*$|[A-Z]_\w+$)")

rows = defaultdict(list)
counts = defaultdict(lambda: defaultdict(int))
for sch in BOARDS:
    path = B / sch
    mounting, pcb = read_mounting_for_schematic(path)
    board = sch.split("/")[0] + ("/" + Path(sch).stem if sch.count("/") > 1 else "")
    fpads = FootprintPads(pcb, project_dir=path.parent)
    for rec in iter_bom_part_records(path):
        m = mounting.get(rec.reference)
        src = "board"
        lm, pads = lib_mounting(fpads, str(rec.props.get("Footprint") or ""))
        if m in (None, "unknown"):
            m, src = lm, "lib"
        counts[board][(m or "none") + "/" + src] += 1
        if m not in (THT, "tht?"):
            continue
        if "smd=0" not in pads and pads:
            src += " PADS:" + pads
        pn = extract_part_numbers(rec.props)
        fp = str(rec.props.get("Footprint") or "")
        lib, _, name = fp.partition(":")
        value = str(rec.props.get("Value") or "")
        desc = str(rec.props.get("Description") or "")
        guess = guess_intent_type(rec.props)
        kind = guess.intent_type if guess else "-"
        if pn.digikey_pn:
            tier = "has-dk"
        elif NOPART.search(name):
            tier = "0-no-part"
        elif kind in INTENT:
            tier = "P-passive"
        elif GENERIC_NAME.search(name) or (GENERIC.search(lib) and not VENDOR.search(name)):
            tier = "2-generic"
        elif VENDOR.search(name) or VENDOR.search(lib) or (pn.mpn) or not GENERIC_VALUE.match(value):
            tier = "1-named"
        else:
            tier = "3-other"
        rows[tier].append((board + ('' if src == 'board' else ' ['+src+']'), rec.reference, fp, value, kind, pn.mpn or "", desc[:60]))

print("Mounting per board (mounting/source):")
for b, c in counts.items():
    print(f"  {b:36}", "  ".join(f"{k}={v}" for k, v in sorted(c.items())))
for tier in sorted(rows):
    items = rows[tier]
    print(f"\n== {tier}: {len(items)} parts")
    if tier == "has-dk":
        continue
    groups = defaultdict(list)
    for board, ref, fp, value, kind, mpn, desc in items:
        groups[(fp, value, kind, mpn, desc)].append(f"{board}:{ref}")
    for (fp, value, kind, mpn, desc), refs in sorted(groups.items()):
        r = ", ".join(refs[:4]) + (f" (+{len(refs)-4})" if len(refs) > 4 else "")
        print(f"  {len(refs):3}  {fp}  | {value} | {kind} | mpn={mpn} | {desc} | {r}")
