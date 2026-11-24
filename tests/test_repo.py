"""The Boards repo root is found from where KicadTools is, never a fixed path."""
from pathlib import Path

from kicadtools import footprints, partsdb, repo
from kicadtools.gui.parts import board_label


def _fake_repo(base: Path) -> Path:
    root = base / "Some" / "Renamed Boards"
    (root / "Libraries" / "kicad-footprints").mkdir(parents=True)
    (root / ".gitmodules").write_text("")
    (root / "Code" / "Libraries").mkdir(parents=True)   # like the real Code/
    return root


def test_found_from_this_checkout(monkeypatch):
    monkeypatch.delenv(repo.ENV_VAR, raising=False)
    root = repo.repo_root()
    assert root is not None
    assert Path(repo.__file__).resolve().is_relative_to(root)
    assert repo.is_repo_root(root)


def test_search_skips_code_libraries(tmp_path):
    root = _fake_repo(tmp_path)
    assert repo.find_repo_root(root / "Code" / "Libraries") == root
    assert repo.find_repo_root(tmp_path) is None


def test_env_var_wins(monkeypatch, tmp_path):
    monkeypatch.setenv(repo.ENV_VAR, str(tmp_path / "elsewhere"))
    assert repo.repo_root() == tmp_path / "elsewhere"


def test_defaults_follow_the_root(monkeypatch, tmp_path):
    root = _fake_repo(tmp_path)
    monkeypatch.setenv(repo.ENV_VAR, str(root))
    monkeypatch.delenv(partsdb.ENV_VAR, raising=False)
    assert partsdb.default_path() == root / "parts.sqlite"
    assert footprints.default_standard_dir(tmp_path / "scratch") == (
        root / "Libraries" / "kicad-footprints")
    board = root / "ECG" / "ECG.kicad_sch"
    assert board_label(board) == "ECG/ECG.kicad_sch"


def test_no_repo(monkeypatch, tmp_path):
    monkeypatch.setenv(repo.ENV_VAR, str(tmp_path / "no_repo"))
    assert footprints.default_standard_dir(tmp_path) is None
    assert footprints.FootprintPads(None).footprint_file("Lib:Name") is None
