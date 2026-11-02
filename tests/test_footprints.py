"""Tests for pad counts read from footprints (kicadtools.footprints)."""
from pathlib import Path

import pytest

from kicadtools.footprints import (
    FootprintPads,
    default_standard_dir,
    expand_uri,
    footprint_pad_count,
    pad_count,
    parse_fp_lib_table,
    parse_kicad_mod_pads,
    split_lib_id,
)
from kicadtools.pcb import Pad

DATA = Path(__file__).parent / "data"
MINI = DATA / "mini.kicad_pcb"

# A QFN-style footprint: 4 signal pads, an exposed pad split into two copper
# pads that share number 5, unnumbered paste apertures and an unnumbered
# mechanical hole. Pad count = 5.
_QFN_MOD = """(footprint "QFN-4-1EP_Test" (version 20221018) (generator pcbnew)
  (layer "F.Cu")
  (pad "1" smd rect (at -1 -0.5) (size 0.5 0.3) (layers "F.Cu" "F.Paste" "F.Mask"))
  (pad "2" smd rect (at -1 0.5) (size 0.5 0.3) (layers "F.Cu" "F.Paste" "F.Mask"))
  (pad "3" smd rect (at 1 0.5) (size 0.5 0.3) (layers "F.Cu" "F.Paste" "F.Mask"))
  (pad "4" smd rect (at 1 -0.5) (size 0.5 0.3) (layers "F.Cu" "F.Paste" "F.Mask"))
  (pad "5" smd rect (at 0 -0.3) (size 1 0.6) (layers "F.Cu" "F.Mask"))
  (pad "5" smd rect (at 0 0.3) (size 1 0.6) (layers "F.Cu" "F.Mask"))
  (pad "" smd rect (at 0 -0.3) (size 0.8 0.4) (layers "F.Paste"))
  (pad "" smd rect (at 0 0.3) (size 0.8 0.4) (layers "F.Paste"))
  (pad "" np_thru_hole circle (at 0 2) (size 1 1) (drill 1) (layers "*.Cu" "*.Mask"))
)
"""

_KICAD5_MOD = """(module SOT-23 (layer F.Cu)
  (pad 1 smd rect (at -1 -0.95) (size 0.9 0.8) (layers F.Cu F.Paste F.Mask))
  (pad 2 smd rect (at -1 0.95) (size 0.9 0.8) (layers F.Cu F.Paste F.Mask))
  (pad 3 smd rect (at 1 0) (size 0.9 0.8) (layers F.Cu F.Paste F.Mask))
)
"""


def _write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


# --- pad_count / .kicad_mod --------------------------------------------------


def test_pad_count_distinct_non_empty_numbers():
    pads = [Pad(0, 0, 1, 1, 0, n) for n in ("1", "2", "2", "", "EP")]
    assert pad_count(pads) == 3
    assert pad_count([]) == 0


def test_kicad_mod_exposed_pad_once_paste_and_holes_not_counted():
    pads = parse_kicad_mod_pads(_QFN_MOD)
    assert len(pads) == 9
    assert pad_count(pads) == 5


def test_kicad5_module_with_bare_numbers():
    assert pad_count(parse_kicad_mod_pads(_KICAD5_MOD)) == 3


def test_kicad_mod_junk_gives_no_pads():
    assert parse_kicad_mod_pads("(kicad_pcb)") == ()


# --- fp-lib-table --------------------------------------------------------------


def test_expand_uri_both_kiprjmod_spellings(tmp_path):
    std = tmp_path / "std"
    assert expand_uri("$KIPRJMOD/../lib.pretty", project_dir=tmp_path, standard_dir=std) == Path(
        f"{tmp_path}/../lib.pretty"
    )
    assert expand_uri("${KIPRJMOD}/lib.pretty", project_dir=tmp_path, standard_dir=std) == (
        tmp_path / "lib.pretty"
    )
    assert expand_uri("${KICAD9_FOOTPRINT_DIR}/Package_SO.pretty", project_dir=tmp_path, standard_dir=std) == (
        std / "Package_SO.pretty"
    )


def test_expand_uri_relative_and_unknown_variable(tmp_path, monkeypatch):
    monkeypatch.delenv("KICADTOOLS_NO_SUCH_VAR", raising=False)
    assert expand_uri("libs/x.pretty", project_dir=tmp_path, standard_dir=None) == tmp_path / "libs/x.pretty"
    assert expand_uri("${KICADTOOLS_NO_SUCH_VAR}/x.pretty", project_dir=tmp_path, standard_dir=None) is None
    monkeypatch.setenv("KICADTOOLS_NO_SUCH_VAR", str(tmp_path / "env"))
    assert expand_uri("${KICADTOOLS_NO_SUCH_VAR}/x.pretty", project_dir=None, standard_dir=None) == (
        tmp_path / "env/x.pretty"
    )


# Same shape as Motors/MotorDriver/fp-lib-table: one library a bare folder of
# .kicad_mod files (no .pretty suffix), one a .pretty folder, plus ${KIPRJMOD}.
_TABLE = """(fp_lib_table
  (version 7)
  (lib (name "TexasInstruments")(type "KiCad")(uri "$KIPRJMOD/../Libraries/TexasInstruments")(options "")(descr ""))
  (lib (name "Board_Footprints")(type "KiCad")(uri "${KIPRJMOD}/Library/footprints.pretty")(options "")(descr ""))
  (lib (name "Broken")(type "KiCad")(uri "${KICADTOOLS_NO_SUCH_VAR}/x.pretty")(options "")(descr ""))
)
"""


def test_parse_fp_lib_table(tmp_path, monkeypatch):
    monkeypatch.delenv("KICADTOOLS_NO_SUCH_VAR", raising=False)
    libs = parse_fp_lib_table(_TABLE, project_dir=tmp_path, standard_dir=None)
    assert libs == {
        "TexasInstruments": Path(f"{tmp_path}/../Libraries/TexasInstruments"),
        "Board_Footprints": tmp_path / "Library/footprints.pretty",
    }


def test_split_lib_id():
    assert split_lib_id("Package_SO:SOIC-8") == ("Package_SO", "SOIC-8")
    assert split_lib_id("SOT-23-5") is None
    assert split_lib_id("") is None


@pytest.fixture
def project(tmp_path, monkeypatch):
    """A temp repo: board/ with an fp-lib-table, a project library, a bare
    TexasInstruments folder beside it, and a standard library folder."""
    monkeypatch.delenv("KICADTOOLS_NO_SUCH_VAR", raising=False)
    board = tmp_path / "board"
    _write(board / "fp-lib-table", _TABLE)
    _write(board / "Library/footprints.pretty/QFN-4-1EP_Test.kicad_mod", _QFN_MOD)
    _write(tmp_path / "Libraries/TexasInstruments/SOT-23.kicad_mod", _KICAD5_MOD)
    std = tmp_path / "std"
    _write(std / "Package_TO_SOT_SMD.pretty/SOT-23-3.kicad_mod", _KICAD5_MOD)
    return board, std


def test_library_resolution_through_fp_lib_table(project):
    board, std = project
    fp = FootprintPads(project_dir=board, standard_dir=std)
    assert fp.library_count("Board_Footprints:QFN-4-1EP_Test") == 5
    assert fp.library_count("TexasInstruments:SOT-23") == 3
    assert fp.footprint_file("Board_Footprints:QFN-4-1EP_Test") == (
        board / "Library/footprints.pretty/QFN-4-1EP_Test.kicad_mod"
    )


def test_library_resolution_standard_libraries(project):
    board, std = project
    fp = FootprintPads(project_dir=board, standard_dir=std)
    assert fp.library_count("Package_TO_SOT_SMD:SOT-23-3") == 3


def test_library_unknown_is_none(project):
    board, std = project
    fp = FootprintPads(project_dir=board, standard_dir=std)
    assert fp.library_count("Board_Footprints:Missing") is None
    assert fp.library_count("NoSuchLib:SOT-23") is None
    assert fp.library_count("Broken:x") is None
    assert fp.library_count("SOT-23-5") is None  # no nickname
    assert fp.count("U9", None) is None


def test_no_fp_lib_table_uses_standard_only(tmp_path):
    std = tmp_path / "std"
    _write(std / "Package_TO_SOT_SMD.pretty/SOT-23-3.kicad_mod", _KICAD5_MOD)
    fp = FootprintPads(project_dir=tmp_path / "nowhere", standard_dir=std)
    assert fp.library_count("Package_TO_SOT_SMD:SOT-23-3") == 3


# --- board first ---------------------------------------------------------------

_BOARD = """(kicad_pcb
  (footprint "Board_Footprints:QFN-4-1EP_Test" (layer "F.Cu") (at 10 10)
    (property "Reference" "U1")
    (pad "1" smd rect (at 0 0) (size 1 1) (layers "F.Cu"))
    (pad "2" smd rect (at 1 0) (size 1 1) (layers "F.Cu"))
    (pad "" smd rect (at 0 0) (size 1 1) (layers "F.Paste"))
  )
)"""


def test_board_copy_used_first(project):
    board, std = project
    pcb = _write(board / "board.kicad_pcb", _BOARD)
    fp = FootprintPads(pcb, standard_dir=std)
    assert fp.project_dir == board
    # The board copy has 2 numbered pads; the library file has 5.
    assert fp.count("U1", "Board_Footprints:QFN-4-1EP_Test") == 2
    assert fp.count("U1") == 2


def test_stale_board_copy_falls_back_to_library(project):
    board, std = project
    pcb = _write(board / "board.kicad_pcb", _BOARD)
    fp = FootprintPads(pcb, standard_dir=std)
    assert fp.count("U1", "TexasInstruments:SOT-23") == 3
    # Not on the board at all.
    assert fp.count("U7", "Board_Footprints:QFN-4-1EP_Test") == 5


def test_missing_pcb_file(project):
    board, std = project
    assert footprint_pad_count(
        "U1", "Board_Footprints:QFN-4-1EP_Test", pcb_path=board / "absent.kicad_pcb", standard_dir=std
    ) == 5


def test_mini_board_resolves_through_library(tmp_path):
    # mini.kicad_pcb's footprints carry no (at …), so none is a placement; the
    # count comes from the library.
    std = tmp_path / "std"
    _write(
        std / "Resistor_SMD.pretty/R_0402_1005Metric.kicad_mod",
        '(footprint "R_0402_1005Metric" (pad "1" smd rect (at -0.5 0) (size 0.5 0.5))'
        ' (pad "2" smd rect (at 0.5 0) (size 0.5 0.5)))',
    )
    fp = FootprintPads(MINI, standard_dir=std)
    assert fp.count("R1", "Resistor_SMD:R_0402_1005Metric") == 2
    assert fp.count("U1", "MyLib:WeirdPart") is None
    assert fp.count("R1") is None


def test_default_standard_dir_walks_up(tmp_path):
    std = tmp_path / "Libraries" / "kicad-footprints"
    std.mkdir(parents=True)
    deep = tmp_path / "a" / "b"
    deep.mkdir(parents=True)
    assert default_standard_dir(deep) == std


# --- damaged files -------------------------------------------------------------


@pytest.mark.parametrize(
    "content",
    [
        b"",  # empty: sexpdata raises AssertionError
        b'(footprint "x" (pad "1" smd',  # truncated: ExpectClosingBracket
        b'(footprint "x" (pad "1',  # unterminated string
        b"\xff\xfe(footprint)",  # not UTF-8
    ],
)
def test_damaged_library_file_is_unknown(project, content):
    board, std = project
    path = board / "Library/footprints.pretty/Bad.kicad_mod"
    path.write_bytes(content)
    fp = FootprintPads(project_dir=board, standard_dir=std)
    assert fp.library_count("Board_Footprints:Bad") is None
    # The other footprints in the same library are still counted.
    assert fp.library_count("Board_Footprints:QFN-4-1EP_Test") == 5


def test_damaged_fp_lib_table_falls_back_to_standard(project):
    board, std = project
    (board / "fp-lib-table").write_text("(fp_lib_table (lib (name", encoding="utf-8")
    fp = FootprintPads(project_dir=board, standard_dir=std)
    assert fp.library_count("Board_Footprints:QFN-4-1EP_Test") is None
    assert fp.library_count("Package_TO_SOT_SMD:SOT-23-3") == 3


def test_damaged_board_falls_back_to_library(project):
    board, std = project
    pcb = _write(board / "board.kicad_pcb", "")
    fp = FootprintPads(pcb, standard_dir=std)
    assert fp.count("U1", "Board_Footprints:QFN-4-1EP_Test") == 5
    assert fp.count("U1") is None
