"""The lab inventory web app (``INVENTORY_PLAN.md``).

Server-rendered pages (Jinja2) with htmx for search-as-you-type and the
quantity buttons; every page also works as plain HTML forms. Pages:

- ``/`` search (the front page); the top-level location filter (the lab) is
  remembered in a cookie;
- ``/item/<id>`` an item: its fields, then its stock rows (location,
  ~quantity, − / +, move, remove) and "add to another location";
- ``/add`` quick add: name, location, quantity (``/add?location=school/C12/5``
  from a location's page fills the location). A name or part number that
  matches an item already known offers that item instead, so a second box of
  something becomes a stock row, not a second item;
- ``/location/<path>`` a location: its fields, the locations inside it, the
  stock in it and in those locations (the page a QR code opens), add item
  here, add location inside;
- ``/locations`` every location as a tree, with counts, and the items in no
  location.

Open-ended fields (category, manufacturer, tags, location) are one box with
a ``<datalist>`` of the values already used: the browser lists matching ones
while typing and keeps whatever new value is typed.

Run: ``kicadtools-web`` (see :func:`main`), on the database the KicadTools
configuration names (``KICADTOOLS_CONFIG`` or ``--config`` for another).
"""
from __future__ import annotations

import argparse
import os
from collections import OrderedDict
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional, Tuple, Union
from urllib.parse import quote, urlencode

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from kicadtools.inventory import SearchHit, search_items
from kicadtools.inventorydb import (SEP, InventoryDb, InventoryError, Item, Location, Stock,
                                    natural_key, path_key)

HERE = Path(__file__).parent
TOP_COOKIE = "top"
# Search results shown at most; the count says when there are more.
MAX_RESULTS = 200
ITEM_TEXT_FIELDS = ("category", "manufacturer", "mpn", "digikey_pn", "lcsc", "url",
                    "description", "notes")
LOCATION_TEXT_FIELDS = ("description", "notes")

Opener = Callable[[], Tuple[InventoryDb, Optional[Callable[[], None]]]]


# ----- helpers -------------------------------------------------------------

def location_url(path: Optional[str]) -> str:
    """``/location/school/C12/5``, each name quoted (names may hold spaces)."""
    return "/location/" + SEP.join(quote(p, safe="") for p in (path or "").split(SEP))


def fmt_qty(quantity: Optional[int]) -> str:
    return "—" if quantity is None else f"~{quantity}"


def _text(form: Mapping[str, Any], key: str) -> Optional[str]:
    value = form.get(key)
    if value is None:
        return None
    value = str(value).strip()
    return value or None


def parse_quantity(text: Optional[str]) -> Optional[int]:
    """``""`` → ``None``; ``"~50"`` and ``"50"`` → 50; anything else refused."""
    if text is None:
        return None
    text = str(text).strip().lstrip("~").strip()
    if not text:
        return None
    try:
        return int(text)
    except ValueError:
        raise InventoryError(f"the quantity must be a whole number, not {text!r}") from None


def parse_tags(text: Optional[str]) -> List[str]:
    """Tags typed as one box, separated by commas."""
    return [t.strip() for t in (text or "").split(",") if t.strip()]


def location_chain(by_path: Mapping[str, Location], path: Optional[str]) -> List[Location]:
    """The location at *path* and every location it is inside, outermost
    first."""
    if not path:
        return []
    parts = path.split(SEP)
    return [by_path[p] for p in (SEP.join(parts[:d]) for d in range(1, len(parts) + 1))
            if p in by_path]


def item_counts(items: List[Item]) -> Tuple[Dict[str, int], Dict[str, int]]:
    """Items with stock directly in each location, and in it or any
    location inside it (an item counted once per location)."""
    direct: Dict[str, int] = {}
    total: Dict[str, int] = {}
    for item in items:
        seen = set()
        for row in item.stock:
            direct[row.location_path] = direct.get(row.location_path, 0) + 1
            parts = row.location_path.split(SEP)
            for d in range(1, len(parts) + 1):
                seen.add(SEP.join(parts[:d]))
        for key in seen:
            total[key] = total.get(key, 0) + 1
    return direct, total


# ----- the app -------------------------------------------------------------

def create_app(source: Union[InventoryDb, Opener]) -> FastAPI:
    """The app over *source*: an open :class:`InventoryDb` (tests), or a
    function returning ``(inventory, close)`` called when the app starts
    (``close`` is called when it stops)."""

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        close = None
        if not isinstance(source, InventoryDb):
            app.state.inv, close = source()
        try:
            yield
        finally:
            if close is not None:
                close()

    app = FastAPI(title="Lab inventory", lifespan=lifespan, docs_url=None, redoc_url=None)
    if isinstance(source, InventoryDb):
        app.state.inv = source
    app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")
    templates = Jinja2Templates(directory=HERE / "templates")
    templates.env.filters["location_url"] = location_url
    templates.env.filters["qty"] = fmt_qty
    templates.env.globals["urlencode"] = urlencode

    def inv(request: Request) -> InventoryDb:
        return request.app.state.inv

    def choices(i: InventoryDb) -> Dict[str, List[str]]:
        """The values already used in each open-ended field."""
        locations = i.locations()
        return {
            "top": [loc.path for loc in locations if loc.parent_id is None],
            "category": i.values("category"),
            "manufacturer": i.values("manufacturer"),
            "tag": i.tags(),
            "location": [loc.path for loc in locations],
        }

    def page(request: Request, name: str, status: int = 200, **context) -> HTMLResponse:
        return templates.TemplateResponse(request, name, context, status_code=status)

    def back(url: str) -> RedirectResponse:
        return RedirectResponse(url, status_code=303)

    def is_htmx(request: Request) -> bool:
        return request.headers.get("hx-request") == "true"

    def missing(request: Request, what: str, **context) -> HTMLResponse:
        return page(request, "missing.html", 404, what=what, **context)

    # ----- search ----------------------------------------------------------

    @app.get("/", response_class=HTMLResponse)
    def search_page(request: Request, q: str = "", top: Optional[str] = None,
                    category: str = "", in_stock: str = ""):
        i = inv(request)
        remembered = top is None
        if remembered:
            top = request.cookies.get(TOP_COOKIE, "")
        top = top.strip()
        stock = in_stock not in ("", "0", "false")
        filtered = bool(q.strip() or category.strip())
        hits: List[SearchHit] = []
        if filtered:
            try:
                hits = search_items(i.items(), i.locations(), q, top=top or None,
                                    category=category or None, in_stock=stock)
            except InventoryError:          # a remembered top that is no name now
                top = ""
                hits = search_items(i.items(), i.locations(), q, category=category or None,
                                    in_stock=stock)
        context = dict(q=q, top=top, category=category, in_stock=stock,
                       hits=hits[:MAX_RESULTS], total=len(hits), filtered=filtered)
        if is_htmx(request) and request.headers.get("hx-target") == "results":
            response = page(request, "_results.html", **context)
        else:
            response = page(request, "search.html", choices=choices(i), **context)
        if not remembered:
            response.set_cookie(TOP_COOKIE, top, max_age=365 * 24 * 3600, samesite="lax")
        return response

    # ----- items -----------------------------------------------------------

    def item_page_response(request: Request, i: InventoryDb, item: Item, *, status: int = 200,
                           saved: bool = False, error: Optional[str] = None,
                           form: Any = None) -> HTMLResponse:
        return page(request, "item.html", status, choices=choices(i), item=item, saved=saved,
                    error=error, form=form)

    @app.get("/item/{item_id}", response_class=HTMLResponse)
    def item_page(request: Request, item_id: int, saved: str = ""):
        i = inv(request)
        item = i.item(item_id)
        if item is None:
            return missing(request, f"item {item_id}")
        return item_page_response(request, i, item, saved=bool(saved))

    @app.post("/item/{item_id}", response_class=HTMLResponse)
    async def item_save(request: Request, item_id: int):
        i = inv(request)
        form = await request.form()
        item = i.item(item_id)
        if item is None:
            return missing(request, f"item {item_id}")
        try:
            i.update_item(
                item_id,
                name=_text(form, "name"),
                tags=parse_tags(_text(form, "tags")),
                **{f: _text(form, f) for f in ITEM_TEXT_FIELDS},
            )
        except InventoryError as e:
            return item_page_response(request, i, item, status=400, error=str(e), form=form)
        return back(f"/item/{item_id}?saved=1")

    @app.post("/item/{item_id}/stock", response_class=HTMLResponse)
    async def item_add_stock(request: Request, item_id: int):
        """Put some of the item in a location ("add to another location", and
        quick add's "add to this item")."""
        i = inv(request)
        form = await request.form()
        item = i.item(item_id)
        if item is None:
            return missing(request, f"item {item_id}")
        try:
            where = _text(form, "location")
            if where is None:
                raise InventoryError("a location is needed")
            i.add_stock(item_id, where, parse_quantity(_text(form, "quantity")))
        except InventoryError as e:
            return item_page_response(request, i, item, status=400, error=str(e))
        return back(f"/item/{item_id}")

    @app.post("/item/{item_id}/delete")
    def item_delete(request: Request, item_id: int):
        i = inv(request)
        item = i.item(item_id)
        if item is not None:
            i.delete_item(item_id)
        return back(location_url(item.locations[0]) if item and item.locations else "/")

    # ----- stock rows ------------------------------------------------------

    @app.post("/stock/{stock_id}/qty", response_class=HTMLResponse)
    async def stock_qty(request: Request, stock_id: int):
        i = inv(request)
        form = await request.form()
        try:
            delta = int(str(form.get("delta", "0")))
            row = i.adjust_stock(stock_id, delta)
        except (InventoryError, ValueError) as e:
            return HTMLResponse(f'<span class="error">{e}</span>', status_code=400)
        if is_htmx(request):
            return page(request, "_qty.html", row=row)
        return back(f"/item/{row.item_id}")

    @app.post("/stock/{stock_id}", response_class=HTMLResponse)
    async def stock_action(request: Request, stock_id: int):
        """One form endpoint per stock row; ``action`` says what: ``set``
        (quantity, notes), ``move`` (to another location; one the item is
        already in takes the quantity), ``delete`` (the item is no longer
        kept there)."""
        i = inv(request)
        form = await request.form()
        row = i.stock_row(stock_id)
        if row is None:
            return missing(request, f"stock row {stock_id}")
        action = str(form.get("action", "set"))
        try:
            if action == "set":
                i.update_stock(stock_id, quantity=parse_quantity(_text(form, "quantity")),
                               notes=_text(form, "notes"))
            elif action == "move":
                where = _text(form, "location")
                if where is None:
                    raise InventoryError("a location to move to is needed")
                i.move_stock(stock_id, where)
            elif action == "delete":
                i.delete_stock(stock_id)
            else:
                raise InventoryError(f"unknown action {action!r}")
        except InventoryError as e:
            return item_page_response(request, i, i.item(row.item_id), status=400, error=str(e))
        return back(f"/item/{row.item_id}")

    # ----- quick add -------------------------------------------------------

    @app.get("/add", response_class=HTMLResponse)
    def add_page(request: Request, location: str = "", added: Optional[int] = None):
        i = inv(request)
        just_added = i.item(added) if added else None
        return page(request, "add.html", choices=choices(i), location=location, error=None,
                    form={}, added=just_added, matches=[])

    @app.post("/add", response_class=HTMLResponse)
    async def add_save(request: Request):
        i = inv(request)
        form = await request.form()
        where = _text(form, "location")
        try:
            quantity = parse_quantity(_text(form, "quantity"))
            if not form.get("new"):
                matches = i.matching(name=_text(form, "name"), mpn=_text(form, "mpn"),
                                     digikey_pn=_text(form, "digikey_pn"))
                if matches:
                    return page(request, "add.html", choices=choices(i), location=where or "",
                                error=None, form=form, added=None, matches=matches)
            item = i.add_item(
                _text(form, "name") or "",
                location=where,
                quantity=quantity,
                tags=parse_tags(_text(form, "tags")),
                **{f: _text(form, f) for f in ITEM_TEXT_FIELDS},
            )
        except InventoryError as e:
            return page(request, "add.html", 400, choices=choices(i), location=where or "",
                        error=str(e), form=form, added=None, matches=[])
        # Back to an empty form with the same location, for the next item.
        return back("/add?" + urlencode({"location": where or "", "added": item.id}))

    # ----- locations -------------------------------------------------------

    def location_context(i: InventoryDb, loc: Location) -> Dict[str, Any]:
        all_locations = i.locations()
        by_path = {x.path: x for x in all_locations}
        items = i.items()
        names = {item.id: item for item in items}
        groups: "OrderedDict[str, List[Tuple[Stock, Item]]]" = OrderedDict()
        for row in i.stock_in(loc.path):
            groups.setdefault(row.location_path, []).append((row, names[row.item_id]))
        for rows in groups.values():
            rows.sort(key=lambda pair: natural_key(pair[1].name))
        _, total = item_counts(items)
        return dict(location=loc, chain=location_chain(by_path, loc.path),
                    children=i.children(loc.path), groups=groups, total=total)

    @app.get("/location/{path:path}", response_class=HTMLResponse)
    def location_page(request: Request, path: str, saved: str = ""):
        i = inv(request)
        try:
            loc = i.location(path)
        except InventoryError:
            loc = None
        if loc is None:
            return missing(request, f"location {path}", make_location=path)
        return page(request, "location.html", choices=choices(i), saved=bool(saved),
                    error=None, **location_context(i, loc))

    @app.post("/location/{path:path}", response_class=HTMLResponse)
    async def location_action(request: Request, path: str):
        """One form endpoint per location; the form's ``action`` field says
        what: ``save`` (description, notes), ``add_location`` (one inside),
        ``rename``, ``move`` (to another parent; empty = top level),
        ``delete`` (only an empty location)."""
        i = inv(request)
        form = await request.form()
        loc = i.location(path)
        if loc is None:
            return missing(request, f"location {path}")
        action = str(form.get("action", "save"))
        try:
            if action == "save":
                i.update_location(loc.path, **{f: _text(form, f) for f in LOCATION_TEXT_FIELDS})
                return back(location_url(loc.path) + "?saved=1")
            if action == "add_location":
                child = i.add_location(_text(form, "name") or "", loc.path)
                return back(location_url(child.path))
            if action == "rename":
                moved = i.rename_location(loc.path, _text(form, "name") or "")
                return back(location_url(moved.path))
            if action == "move":
                moved = i.move_location(loc.path, _text(form, "parent"))
                return back(location_url(moved.path))
            if action == "delete":
                parent = SEP.join(loc.path.split(SEP)[:-1])
                i.delete_location(loc.path)
                return back(location_url(parent) if parent else "/locations")
            raise InventoryError(f"unknown action {action!r}")
        except InventoryError as e:
            return page(request, "location.html", 400, choices=choices(i), saved=False,
                        error=str(e), **location_context(i, loc))

    @app.get("/locations", response_class=HTMLResponse)
    def locations_page(request: Request):
        i = inv(request)
        all_locations = sorted(i.locations(), key=lambda x: path_key(x.path))
        _, total = item_counts(i.items())
        return page(request, "locations.html", locations=all_locations, total=total,
                    unplaced=i.unplaced(), choices=choices(i), error=None)

    @app.post("/locations", response_class=HTMLResponse)
    async def locations_add(request: Request):
        i = inv(request)
        form = await request.form()
        try:
            parent = _text(form, "parent")
            if parent is not None:
                parent = i.ensure_location(parent).path
            loc = i.add_location(_text(form, "name") or "", parent)
        except InventoryError as e:
            return page(request, "error.html", 400, error=str(e), back_url="/locations")
        return back(location_url(loc.path))

    @app.get("/healthz")
    def healthz(request: Request) -> Response:
        inv(request).locations()
        return Response("ok", media_type="text/plain")

    return app


# ----- running it ----------------------------------------------------------

def open_configured() -> Tuple[InventoryDb, Callable[[], None]]:
    """The inventory on the database the KicadTools configuration names."""
    from kicadtools.dbconfig import load_config, terminal_ask
    from kicadtools.dbconnect import open_parts_db

    session = open_parts_db(load_config(), terminal_ask)
    try:
        return InventoryDb(session.db), session.close
    except Exception:
        session.close()
        raise


def configured_app() -> FastAPI:
    """For ``uvicorn --factory kicadtools.web.app:configured_app``."""
    return create_app(open_configured)


def main(argv: Optional[List[str]] = None) -> None:
    parser = argparse.ArgumentParser(
        prog="kicadtools-web", description="Run the lab inventory web app.")
    parser.add_argument("--host", default="127.0.0.1",
                        help="address to listen on (default 127.0.0.1; 0.0.0.0 for the LAN)")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--config", help="KicadTools config file (default: "
                        "$KICADTOOLS_CONFIG or ~/.kicadtools/config.toml)")
    parser.add_argument("--reload", action="store_true",
                        help="restart when the code changes (development)")
    args = parser.parse_args(argv)
    if args.config:
        os.environ["KICADTOOLS_CONFIG"] = str(Path(args.config).expanduser())

    import uvicorn
    uvicorn.run("kicadtools.web.app:configured_app", factory=True, host=args.host,
                port=args.port, reload=args.reload)


if __name__ == "__main__":
    main()
