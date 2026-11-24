"""The lab inventory web app (``INVENTORY_PLAN.md``, step 4).

Server-rendered pages (Jinja2) with htmx for search-as-you-type and the
quantity buttons; every page also works as plain HTML forms. Pages:

- ``/`` search (the front page); the site filter is remembered in a cookie;
- ``/item/<id>`` an item: every field, quantity − / +, move to another bin;
- ``/add`` quick add (``/add?bin=C12/5`` from a bin page fills the bin);
- ``/bin/<path>`` a bin: its fields, the bins inside it, everything in it and
  in those bins (the page a QR code opens), add item here, add bin inside;
- ``/bins`` every bin, by site, with item counts.

Open-ended fields (site, category, manufacturer, tags, bin) are one box with
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

from kicadtools.inventory import SearchHit, effective_sites, search_items
from kicadtools.inventorydb import SEP, Bin, InventoryDb, InventoryError, Item, natural_key

HERE = Path(__file__).parent
SITE_COOKIE = "site"
# Search results shown at most; the count says when there are more.
MAX_RESULTS = 200
ITEM_TEXT_FIELDS = ("category", "manufacturer", "mpn", "digikey_pn", "lcsc", "url",
                    "description", "notes")
BIN_TEXT_FIELDS = ("site", "description", "location", "notes")

Opener = Callable[[], Tuple[InventoryDb, Optional[Callable[[], None]]]]


# ----- helpers -------------------------------------------------------------

def bin_url(path: Optional[str]) -> str:
    """``/bin/C12/5``, each name quoted (names may hold spaces)."""
    return "/bin/" + SEP.join(quote(p, safe="") for p in (path or "").split(SEP))


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


def bin_chain(bins_by_path: Mapping[str, Bin], path: Optional[str]) -> List[Bin]:
    """The bin at *path* and every bin it is inside, outermost first."""
    if not path:
        return []
    parts = path.split(SEP)
    return [bins_by_path[p] for p in (SEP.join(parts[:d]) for d in range(1, len(parts) + 1))
            if p in bins_by_path]


def where_text(chain: List[Bin]) -> Optional[str]:
    """The location of the innermost bin in *chain* that has one."""
    for b in reversed(chain):
        if b.location:
            return b.location
    return None


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
    templates.env.filters["bin_url"] = bin_url
    templates.env.filters["qty"] = fmt_qty
    templates.env.globals["urlencode"] = urlencode

    def inv(request: Request) -> InventoryDb:
        return request.app.state.inv

    def choices(i: InventoryDb) -> Dict[str, List[str]]:
        """The values already used in each open-ended field."""
        return {
            "site": i.values("site"),
            "category": i.values("category"),
            "manufacturer": i.values("manufacturer"),
            "tag": i.tags(),
            "bin": [b.path for b in i.bins()],
        }

    def page(request: Request, name: str, status: int = 200, **context) -> HTMLResponse:
        return templates.TemplateResponse(request, name, context, status_code=status)

    def back(url: str) -> RedirectResponse:
        return RedirectResponse(url, status_code=303)

    def is_htmx(request: Request) -> bool:
        return request.headers.get("hx-request") == "true"

    # ----- search ----------------------------------------------------------

    @app.get("/", response_class=HTMLResponse)
    def search_page(request: Request, q: str = "", site: Optional[str] = None,
                    category: str = "", in_stock: str = ""):
        i = inv(request)
        remembered = site is None
        if remembered:
            site = request.cookies.get(SITE_COOKIE, "")
        site = site.strip()
        stock = in_stock not in ("", "0", "false")
        filtered = bool(q.strip() or category.strip())
        hits: List[SearchHit] = []
        if filtered:
            bins = i.bins()
            hits = search_items(i.items(), bins, q, site=site or None,
                                category=category or None, in_stock=stock)
            by_path = {b.path: b for b in bins}
            wheres = {h.item.id: where_text(bin_chain(by_path, h.item.bin_path))
                      for h in hits[:MAX_RESULTS]}
        else:
            wheres = {}
        context = dict(q=q, site=site, category=category, in_stock=stock, hits=hits[:MAX_RESULTS],
                       total=len(hits), filtered=filtered, wheres=wheres)
        if is_htmx(request) and request.headers.get("hx-target") == "results":
            response = page(request, "_results.html", **context)
        else:
            response = page(request, "search.html", choices=choices(i), **context)
        if not remembered:
            response.set_cookie(SITE_COOKIE, site, max_age=365 * 24 * 3600, samesite="lax")
        return response

    # ----- items -----------------------------------------------------------

    def item_context(i: InventoryDb, item: Item) -> Dict[str, Any]:
        by_path = {b.path: b for b in i.bins()}
        chain = bin_chain(by_path, item.bin_path)
        site = effective_sites(by_path.values()).get(item.bin_path) if item.bin_path else None
        return dict(item=item, chain=chain, site=site, where=where_text(chain))

    @app.get("/item/{item_id}", response_class=HTMLResponse)
    def item_page(request: Request, item_id: int, saved: str = ""):
        i = inv(request)
        item = i.item(item_id)
        if item is None:
            return page(request, "missing.html", 404, what=f"item {item_id}")
        return page(request, "item.html", choices=choices(i), saved=bool(saved),
                    error=None, **item_context(i, item))

    @app.post("/item/{item_id}", response_class=HTMLResponse)
    async def item_save(request: Request, item_id: int):
        i = inv(request)
        form = await request.form()
        item = i.item(item_id)
        if item is None:
            return page(request, "missing.html", 404, what=f"item {item_id}")
        try:
            i.update_item(
                item_id,
                name=_text(form, "name"),
                quantity=parse_quantity(_text(form, "quantity")),
                bin=_text(form, "bin"),
                tags=parse_tags(_text(form, "tags")),
                **{f: _text(form, f) for f in ITEM_TEXT_FIELDS},
            )
        except InventoryError as e:
            return page(request, "item.html", 400, choices=choices(i), saved=False,
                        error=str(e), form=form, **item_context(i, item))
        return back(f"/item/{item_id}?saved=1")

    @app.post("/item/{item_id}/qty", response_class=HTMLResponse)
    async def item_qty(request: Request, item_id: int):
        i = inv(request)
        form = await request.form()
        try:
            delta = int(str(form.get("delta", "0")))
            item = i.adjust_quantity(item_id, delta)
        except (InventoryError, ValueError) as e:
            return HTMLResponse(f'<span class="error">{e}</span>', status_code=400)
        if is_htmx(request):
            return page(request, "_qty.html", item=item)
        return back(f"/item/{item_id}")

    @app.post("/item/{item_id}/delete")
    def item_delete(request: Request, item_id: int):
        i = inv(request)
        item = i.item(item_id)
        if item is not None:
            i.delete_item(item_id)
        return back(bin_url(item.bin_path) if item and item.bin_path else "/")

    # ----- quick add -------------------------------------------------------

    @app.get("/add", response_class=HTMLResponse)
    def add_page(request: Request, bin: str = "", added: Optional[int] = None):
        i = inv(request)
        just_added = i.item(added) if added else None
        return page(request, "add.html", choices=choices(i), bin=bin, error=None,
                    form={}, added=just_added)

    @app.post("/add", response_class=HTMLResponse)
    async def add_save(request: Request):
        i = inv(request)
        form = await request.form()
        bin_path = _text(form, "bin")
        try:
            item = i.add_item(
                _text(form, "name") or "",
                bin=bin_path,
                quantity=parse_quantity(_text(form, "quantity")),
                tags=parse_tags(_text(form, "tags")),
                **{f: _text(form, f) for f in ITEM_TEXT_FIELDS},
            )
        except InventoryError as e:
            return page(request, "add.html", 400, choices=choices(i), bin=bin_path or "",
                        error=str(e), form=form, added=None)
        # Back to an empty form with the same bin, for the next item.
        return back("/add?" + urlencode({"bin": item.bin_path or "", "added": item.id}))

    # ----- bins ------------------------------------------------------------

    def counts(i: InventoryDb) -> Tuple[Dict[str, int], Dict[str, int]]:
        """Items directly in each bin, and in it plus every bin inside it."""
        direct: Dict[str, int] = {}
        for item in i.items():
            if item.bin_path:
                direct[item.bin_path] = direct.get(item.bin_path, 0) + 1
        total: Dict[str, int] = {}
        for path, n in direct.items():
            parts = path.split(SEP)
            for d in range(1, len(parts) + 1):
                key = SEP.join(parts[:d])
                total[key] = total.get(key, 0) + n
        return direct, total

    def bin_context(i: InventoryDb, b: Bin) -> Dict[str, Any]:
        all_bins = i.bins()
        by_path = {x.path: x for x in all_bins}
        sites = effective_sites(all_bins)
        groups: "OrderedDict[str, List[Item]]" = OrderedDict()
        for item in i.items(b.path):
            groups.setdefault(item.bin_path, []).append(item)
        _, total = counts(i)
        return dict(
            bin=b, chain=bin_chain(by_path, b.path), site=sites.get(b.path),
            inherited_site=b.site is None and sites.get(b.path) is not None,
            children=i.children(b.path), groups=groups, total=total,
        )

    @app.get("/bin/{path:path}", response_class=HTMLResponse)
    def bin_page(request: Request, path: str, saved: str = ""):
        i = inv(request)
        b = i.bin(path)
        if b is None:
            return page(request, "missing.html", 404, what=f"bin {path}",
                        make_bin=path)
        return page(request, "bin.html", choices=choices(i), saved=bool(saved), error=None,
                    **bin_context(i, b))

    @app.post("/bin/{path:path}", response_class=HTMLResponse)
    async def bin_action(request: Request, path: str):
        """One form endpoint per bin; the form's ``action`` field says what:
        ``save`` (site, description, location, notes), ``add_bin`` (a bin
        inside), ``rename``, ``move`` (to another parent; empty = top level),
        ``delete`` (only an empty bin)."""
        i = inv(request)
        form = await request.form()
        b = i.bin(path)
        if b is None:
            return page(request, "missing.html", 404, what=f"bin {path}")
        action = str(form.get("action", "save"))
        try:
            if action == "save":
                i.update_bin(b.path, **{f: _text(form, f) for f in BIN_TEXT_FIELDS})
                return back(bin_url(b.path) + "?saved=1")
            if action == "add_bin":
                child = i.add_bin(_text(form, "name") or "", b.path)
                return back(bin_url(child.path))
            if action == "rename":
                moved = i.rename_bin(b.path, _text(form, "name") or "")
                return back(bin_url(moved.path))
            if action == "move":
                moved = i.move_bin(b.path, _text(form, "parent"))
                return back(bin_url(moved.path))
            if action == "delete":
                parent = SEP.join(b.path.split(SEP)[:-1])
                i.delete_bin(b.path)
                return back(bin_url(parent) if parent else "/bins")
            raise InventoryError(f"unknown action {action!r}")
        except InventoryError as e:
            return page(request, "bin.html", 400, choices=choices(i), saved=False,
                        error=str(e), **bin_context(i, b))

    @app.get("/bins", response_class=HTMLResponse)
    def bins_page(request: Request):
        i = inv(request)
        all_bins = i.bins()
        sites = effective_sites(all_bins)
        direct, total = counts(i)
        by_site: Dict[str, List[Bin]] = {}
        for b in sorted(all_bins, key=lambda x: [natural_key(n) for n in x.path.split(SEP)]):
            by_site.setdefault(sites.get(b.path) or "", []).append(b)
        ordered = sorted(by_site.items(), key=lambda kv: (kv[0] == "", natural_key(kv[0])))
        return page(request, "bins.html", by_site=ordered, direct=direct, total=total,
                    unbinned=i.unbinned(), choices=choices(i), error=None)

    @app.post("/bins", response_class=HTMLResponse)
    async def bins_add(request: Request):
        i = inv(request)
        form = await request.form()
        try:
            parent = _text(form, "parent")
            if parent is not None:
                parent = i.ensure_bin(parent).path
            b = i.add_bin(_text(form, "name") or "", parent, site=_text(form, "site"))
        except InventoryError as e:
            return page(request, "error.html", 400, error=str(e), back_url="/bins")
        return back(bin_url(b.path))

    @app.get("/healthz")
    def healthz(request: Request) -> Response:
        inv(request).bins()
        return Response("ok", media_type="text/plain")

    return app


# ----- running it ----------------------------------------------------------

def open_configured() -> Tuple[InventoryDb, Callable[[], None]]:
    """The inventory on the database the KicadTools configuration names."""
    from kicadtools.dbconfig import load_config, terminal_ask
    from kicadtools.dbconnect import open_parts_db

    session = open_parts_db(load_config(), terminal_ask)
    return InventoryDb(session.db), session.close


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
