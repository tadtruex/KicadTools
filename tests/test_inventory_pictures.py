"""Tests for item pictures (pictures.py and the item_picture table of
inventorydb.py), on SQLite and, when configured, MariaDB."""
from __future__ import annotations

import io

import pytest

PIL = pytest.importorskip("PIL")
from PIL import Image  # noqa: E402

from kicadtools.dbbackup import pull  # noqa: E402
from kicadtools.inventorydb import InventoryDb, InventoryError  # noqa: E402
from kicadtools.partsdb import PartsDb  # noqa: E402
from kicadtools.pictures import MAX_SIDE, THUMB_SIDE, PictureError, prepare  # noqa: E402
from tests.dbbackends import BACKENDS, new_db, use_backend  # noqa: E402

STAMP = "2026-11-25T12:00:00+00:00"


@pytest.fixture(autouse=True, params=BACKENDS)
def backend(request):
    yield from use_backend(request.param)


@pytest.fixture
def inv():
    return InventoryDb(new_db(now=lambda: STAMP))


def _image(size=(400, 300), mode="RGB", color=(200, 30, 30), fmt="JPEG", exif=None) -> bytes:
    out = io.BytesIO()
    kwargs = {"exif": exif} if exif is not None else {}
    Image.new(mode, size, color).save(out, fmt, **kwargs)
    return out.getvalue()


def _open(data: bytes) -> Image.Image:
    return Image.open(io.BytesIO(data))


# ----- prepare -------------------------------------------------------------

def test_a_large_photo_is_shrunk_and_given_a_thumbnail():
    p = prepare(_image((4000, 3000)))
    assert (p.width, p.height) == (MAX_SIDE, 1200)
    assert p.content_type == "image/jpeg"
    big, small = _open(p.data), _open(p.thumb)
    assert big.format == small.format == "JPEG"
    assert big.size == (MAX_SIDE, 1200)
    assert max(small.size) == THUMB_SIDE


def test_a_small_picture_keeps_its_size():
    p = prepare(_image((200, 100), fmt="PNG"))
    assert (p.width, p.height) == (200, 100)
    assert _open(p.thumb).size == (200, 100)


def test_the_exif_orientation_is_applied_and_the_metadata_dropped():
    exif = Image.Exif()
    exif[0x0112] = 6            # orientation: rotate 90° to view
    exif[0x010F] = "PhoneMaker"
    p = prepare(_image((400, 300), exif=exif.tobytes()))
    assert (p.width, p.height) == (300, 400)
    stored = _open(p.data)
    assert not stored.getexif()


def test_transparency_becomes_white():
    p = prepare(_image((50, 50), mode="RGBA", color=(0, 0, 0, 0), fmt="PNG"))
    r, g, b = _open(p.data).convert("RGB").getpixel((25, 25))
    assert min(r, g, b) > 245


@pytest.mark.parametrize("raw", [b"", b"not a picture at all", b"\x89PNG\r\n\x1a\n broken"])
def test_what_is_not_a_picture_is_refused(raw):
    with pytest.raises(PictureError):
        prepare(raw)


# ----- the item_picture table ----------------------------------------------

def test_an_item_starts_with_no_pictures(inv):
    item = inv.add_item("Nano Every")
    assert item.pictures == ()
    assert item.picture is None
    assert inv.pictures(item.id) == []


def test_pictures_are_kept_in_order_the_first_is_the_main_one(inv):
    item = inv.add_item("Nano Every")
    first = inv.add_picture(item.id, _image(), caption="  front \n")
    second = inv.add_picture(item.id, _image(color=(0, 0, 200)))
    assert (first.position, second.position) == (0, 1)
    assert first.caption == "front"
    again = inv.item(item.id)
    assert again.pictures == (first.id, second.id)
    assert again.picture == first.id
    assert [p.id for p in inv.pictures(item.id)] == [first.id, second.id]


def test_picture_data_returns_the_image_and_its_thumbnail(inv):
    item = inv.add_item("Nano Every")
    pic = inv.add_picture(item.id, _image((3200, 800)))
    kind, data = inv.picture_data(pic.id)
    assert kind == "image/jpeg"
    assert _open(data).size == (MAX_SIDE, 400) == (pic.width, pic.height)
    kind, thumb = inv.picture_data(pic.id, thumb=True)
    assert _open(thumb).size == (THUMB_SIDE, 80)
    assert inv.picture_data(pic.id + 100) is None


def test_move_and_delete_keep_the_positions_dense(inv):
    item = inv.add_item("Nano Every")
    a, b, c = (inv.add_picture(item.id, _image()) for _ in range(3))
    inv.move_picture(c.id, 0)
    assert inv.item(item.id).pictures == (c.id, a.id, b.id)
    inv.move_picture(c.id, 99)
    assert inv.item(item.id).pictures == (a.id, b.id, c.id)
    inv.delete_picture(a.id)
    assert inv.item(item.id).pictures == (b.id, c.id)
    assert [p.position for p in inv.pictures(item.id)] == [0, 1]
    assert inv.picture(a.id) is None


def test_update_picture_sets_and_clears_the_caption(inv):
    item = inv.add_item("Nano Every")
    pic = inv.add_picture(item.id, _image())
    assert inv.update_picture(pic.id, caption="pinout side").caption == "pinout side"
    assert inv.update_picture(pic.id, caption="").caption is None


def test_bad_picture_calls_are_refused_and_change_nothing(inv):
    item = inv.add_item("Nano Every")
    with pytest.raises(PictureError):
        inv.add_picture(item.id, b"junk")
    with pytest.raises(InventoryError):
        inv.add_picture(item.id + 100, _image())
    with pytest.raises(InventoryError):
        inv.delete_picture(12345)
    assert inv.pictures(item.id) == []


def test_deleting_an_item_deletes_its_pictures(inv):
    keep = inv.add_item("STLink V3")
    gone = inv.add_item("Nano Every")
    kept = inv.add_picture(keep.id, _image())
    inv.add_picture(gone.id, _image())
    inv.delete_item(gone.id)
    assert [tuple(r) for r in inv.db._execute("SELECT id FROM item_picture")] == [(kept.id,)]


def test_items_list_their_pictures(inv):
    a = inv.add_item("Nano Every", bin="C12/5")
    inv.add_item("STLink V3", bin="C12/5")
    pic = inv.add_picture(a.id, _image())
    assert {i.name: i.pictures for i in inv.items("C12")} == {
        "Nano Every": (pic.id,), "STLink V3": ()}


def test_pull_copies_the_pictures(inv, tmp_path):
    item = inv.add_item("Nano Every")
    pic = inv.add_picture(item.id, _image(), caption="front")
    counts = pull(inv.db, tmp_path / "copy.sqlite")
    assert counts["item_picture"] == 1
    copy = InventoryDb(PartsDb.open_read_only(tmp_path / "copy.sqlite"))
    assert copy.item(item.id).pictures == (pic.id,)
    assert copy.picture_data(pic.id) == inv.picture_data(pic.id)


def test_a_read_only_database_from_before_pictures_lists_none(tmp_path):
    # The real database, opened read-only by newer code before a release
    # with pictures has created the table there.
    path = tmp_path / "old.sqlite"
    db = PartsDb(path, now=lambda: STAMP)
    inv = InventoryDb(db)
    inv.add_item("Nano Every")
    db._execute("DROP TABLE item_picture")
    db.conn.commit()
    db.close()
    old = InventoryDb(PartsDb.open_read_only(path))
    assert [(i.name, i.pictures) for i in old.items()] == [("Nano Every", ())]
