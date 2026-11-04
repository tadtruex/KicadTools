"""Solder jumpers and net ties are left out of every BOM list
(components.bom_components, resolve.iter_bom_part_records)."""
from types import SimpleNamespace

from kicadtools.components import bom_components, is_virtual_part
from kicadtools.resolve import iter_bom_part_records


def _c(ref, footprint, value="X"):
    return SimpleNamespace(reference=ref, dnp=False, in_bom=True, footprint=footprint, value=value)


def test_bom_components_drops_jumpers_and_net_ties():
    parts = [_c("R1", "Resistor_SMD:R_0603_1608Metric"),
             _c("JP1", "Jumper:SolderJumper-2_P1.3mm_Bridged_RoundedPad1.0x1.5mm"),
             _c("NT1", "NetTie:NetTie-2_SMD_Pad0.5mm")]
    assert [c.reference for c in bom_components(parts)] == ["R1"]


def test_is_virtual_part_handles_missing_fields():
    assert not is_virtual_part(None, None)
    assert is_virtual_part(None, "NetTie_2")


def test_records_drop_a_solder_jumper(tmp_path):
    sch = tmp_path / "t.kicad_sch"
    sym = '''  (symbol (lib_id "{lib}") (at 0 0 0) (unit 1) (in_bom yes) (on_board yes) (dnp no)
    (uuid "{uuid}")
    (property "Reference" "{ref}" (at 0 0 0))
    (property "Value" "{value}" (at 0 0 0))
    (property "Footprint" "{fp}" (at 0 0 0))
    (instances (project "t" (path "/00000000-0000-0000-0000-000000000000" (reference "{ref}") (unit 1))))
  )
'''
    body = (sym.format(lib="Device:R", uuid="11111111-1111-1111-1111-111111111111", ref="R1",
                       value="10k", fp="Resistor_SMD:R_0603_1608Metric")
            + sym.format(lib="Jumper:SolderJumper_2_Bridged", uuid="22222222-2222-2222-2222-222222222222",
                         ref="JP1", value="SolderJumper_2_Bridged",
                         fp="Jumper:SolderJumper-2_P1.3mm_Bridged_RoundedPad1.0x1.5mm"))
    sch.write_text('(kicad_sch (version 20231120) (generator "eeschema")\n'
                   '  (uuid "00000000-0000-0000-0000-000000000000")\n' + body + ')\n')
    assert [r.reference for r in iter_bom_part_records(sch)] == ["R1"]
