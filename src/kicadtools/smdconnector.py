"""Surface-mount connectors on the Parts tab: searched by part number at
DigiKey and JLCPCB, as ICs are (:mod:`kicadtools.icsearch`), so the chosen
part gets an LCSC number and JLCPCB can place it.

A connector record takes this path when its footprint is surface mount
(:mod:`kicadtools.mounting`) and names a part
(:func:`kicadtools.thtfootprint.parse_signature` family ``named part``,
``Connector_JST:JST_SH_SM04B-SRSS-TB_…``), or the symbol has an MPN,
DigiKeyPN or LCSC number; or when it has no footprint and the footprint its
text implies (:func:`kicadtools.impliedfootprint.implied_footprint`: Stemma
QT, JST SH) is surface mount.

The identifiers are the symbol's part-number fields and the footprint's
part number, searched as a partial MPN. The Value is not read for part
numbers: on a connector it names what plugs in (``Stemma QT (LSM303AGR)``
names a sensor). The footprint check compares the candidate with the
symbol's footprint, else the implied one. On accept the write plan
(:func:`kicadtools.icsearch.ic_assignment_plan`) writes the implied
footprint when the part's MPN fits its part number (:func:`implied_fits`).

Pure: the *board* passed in reads footprint files; nothing here does
network I/O.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from kicadtools.icrequirement import IcGroup, IcRequirement, ic_identifiers
from kicadtools.impliedfootprint import ImpliedFootprint, implied_footprint
from kicadtools.intent import ConnectorIntent
from kicadtools.namedsearch import mpn_fits
from kicadtools.naturalsort import natural_key
from kicadtools.pcb import SMD
from kicadtools.requirement import Member, _prop
from kicadtools.thtfootprint import NAMED_PART, parse_signature

CONNECTOR_KIND = "connector"


@dataclass
class SmdConnectorGroup(IcGroup):
    """Surface-mount connectors that share identifiers and a footprint;
    searched and written as an IC group is."""

    @property
    def kind(self) -> str:
        return CONNECTOR_KIND


def footprint_token(lib_id: str) -> str:
    """The part number a footprint name gives (``SM04B-SRSS-TB``), else
    ``""``."""
    sig = parse_signature(lib_id) if lib_id else None
    return sig.mpn if sig is not None and sig.family == NAMED_PART and sig.mpn else ""


def implied_fits(req: IcRequirement, mpn: str) -> bool:
    """Whether a part with *mpn* fits *req*'s implied footprint: its MPN
    starts with the footprint's part number (``SM04B-SRSS-TB(LF)(SN)`` on
    ``JST_SH_SM04B-SRSS-TB``). ``False`` with no implied footprint."""
    implied = req.implied
    if implied is None:
        return False
    token = footprint_token(implied.lib_id)
    return bool(token) and mpn_fits(mpn or "", token)


def _identifiers(props: Mapping[str, object], lib_id: str):
    ids = replace(ic_identifiers(props), value_mpns=())
    token = footprint_token(lib_id)
    return replace(ids, value_mpns=(token,)) if token else ids


def _smd_lib(rec: Any, board: Any) -> Tuple[str, Optional[ImpliedFootprint]]:
    """``(lib_id, implied)`` for a record that takes this path, else
    ``("", None)``: the symbol's footprint (``implied`` ``None``), or the
    implied footprint of a symbol with none."""
    if not isinstance(rec.intent, ConnectorIntent):
        return "", None
    lib_id = _prop(rec.props, "Footprint")
    if lib_id:
        if board.mounting(rec.reference, lib_id).kind != SMD:
            return "", None
        if not (footprint_token(lib_id) or not ic_identifiers(rec.props).empty):
            return "", None
        return lib_id, None
    implied = implied_footprint(rec.props)
    if implied is None or board.mounting(rec.reference, implied.lib_id).kind != SMD:
        return "", None
    return implied.lib_id, implied


def is_smd_connector(rec: Any, board: Any) -> bool:
    """Whether *rec* takes this path (see the module docstring)."""
    return bool(_smd_lib(rec, board)[0])


def group_smd_connectors(records: Sequence[Any], board: Any) -> List[SmdConnectorGroup]:
    """The surface-mount connector groups among *records*, keyed on
    identifiers and footprint (the implied one for a symbol with none).
    Ordered by first reference."""
    by_key: Dict[Tuple[Any, ...], SmdConnectorGroup] = {}
    for rec in records:
        lib_id, implied = _smd_lib(rec, board)
        if not lib_id:
            continue
        ids = _identifiers(rec.props, lib_id)
        key = ids.key() + (_prop(rec.props, "Footprint"), lib_id)
        member = Member(reference=rec.reference, source_file=rec.source_file,
                        symbol_uuid=rec.symbol_uuid, props=rec.props, intent=rec.intent)
        group = by_key.get(key)
        if group is not None:
            group.members.append(member)
            continue
        pads = board.footprints.count(rec.reference, lib_id)
        req = IcRequirement(identifiers=ids, value_text=_prop(rec.props, "Value"),
                            footprint=_prop(rec.props, "Footprint"), pad_count=pads,
                            kind=CONNECTOR_KIND, implied=implied)
        by_key[key] = SmdConnectorGroup(req, [member])
    return sorted(by_key.values(), key=lambda g: natural_key(g.references[0]))
