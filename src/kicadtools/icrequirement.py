"""
IC identifiers, requirements and grouping.

An IC is found from its part number, not from electrical values (compare
:mod:`kicadtools.requirement` for capacitors and resistors). The identifiers a
symbol carries are:

* ``MPN``, ``DigiKeyPN`` and ``LCSC``, read through
  :func:`kicadtools.partnum.extract_part_numbers` (every alias spelling);
* ``value_mpns`` — the MPN-shaped tokens in the ``Value``
  (:func:`value_mpns`), each tried as a partial MPN by the search.

An :class:`IcRequirement` holds the identifiers, the ``Footprint`` (a guide
for ranking and checking candidates; never written) and two fields the search
step fills in: the package parsed from the footprint name and the
footprint's pad count. It is searchable when it has at least one identifier;
with no footprint the search runs and each candidate's footprint check reads
"cannot verify" (:data:`kicadtools.icsearch.NO_FOOTPRINT_REASON`).

:func:`group_ics` collects the BOM's IC records into :class:`IcGroup` s keyed
on the normalized identifiers and the footprint, so the same part drawn with
two different footprints gives two groups.

Pure: no schematic I/O and no network I/O.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from kicadtools.evidence import ExistingPart
from kicadtools.intent import IcIntent
from kicadtools.naturalsort import natural_key
from kicadtools.package import PackageInfo
from kicadtools.partnum import extract_part_numbers
from kicadtools.requirement import Member, _prop

# The ``part.type`` / requirement kind of an IC.
IC_KIND = "ic"

# Problem line (see IcRequirement.problem).
NO_PART_NUMBER = "no part number"

# Separators the Value is split on to find MPN-shaped tokens. Hyphens, dots,
# slashes and "#" stay inside a token: they are part of real MPNs
# ("AD8232ACPZ-R7", "LTC6992CS6-2", "LTC6992HS6-2#TRMPBF").
_VALUE_SPLIT_RE = re.compile(r"[\s_,]+")
# A token is MPN-shaped when it has at least this many characters and holds
# both a letter and a digit.
_MPN_MIN_LEN = 5
_LETTER_RE = re.compile(r"[A-Za-z]")
_DIGIT_RE = re.compile(r"[0-9]")


def normalize_identifier(text: object) -> str:
    """*text* upper-cased with all whitespace removed; ``""`` for anything
    that is not a non-empty string. Two identifiers that normalize to the
    same text name the same part."""
    if not isinstance(text, str):
        return ""
    return re.sub(r"\s+", "", text).upper()


def value_mpns(value: object) -> Tuple[str, ...]:
    """The MPN-shaped tokens in *value*, longest first (ties keep their order
    in the Value), without repeats (compared normalized).

    The Value is split on whitespace, ``_`` and ``,``; a token is kept when it
    has at least 5 characters and contains both a letter and a digit.
    ``"MotorControl_STM32L552CCT6_V2"`` gives ``("STM32L552CCT6",)``
    (``MotorControl`` has no digit, ``V2`` is too short);
    ``"1515 rgb led"`` gives ``()``."""
    if not isinstance(value, str):
        return ()
    seen = set()
    tokens: List[str] = []
    for token in _VALUE_SPLIT_RE.split(value.strip()):
        if (len(token) >= _MPN_MIN_LEN and _LETTER_RE.search(token)
                and _DIGIT_RE.search(token)):
            norm = normalize_identifier(token)
            if norm not in seen:
                seen.add(norm)
                tokens.append(token)
    # sorted() is stable, so equal-length tokens keep their Value order.
    return tuple(sorted(tokens, key=len, reverse=True))


@dataclass(frozen=True)
class Identifiers:
    """The part numbers an IC symbol carries, as written (stripped). Each
    field is a non-empty string or ``None``; ``value_mpns`` may be empty."""
    mpn: Optional[str] = None
    digikey_pn: Optional[str] = None
    lcsc: Optional[str] = None
    value_mpns: Tuple[str, ...] = ()

    @property
    def empty(self) -> bool:
        """No identifier of any kind: the symbol cannot be searched."""
        return not (self.mpn or self.digikey_pn or self.lcsc or self.value_mpns)

    def key(self) -> Tuple[Any, ...]:
        """The identifiers normalized (:func:`normalize_identifier`), for
        grouping and comparison."""
        return (
            normalize_identifier(self.mpn),
            normalize_identifier(self.digikey_pn),
            normalize_identifier(self.lcsc),
            tuple(normalize_identifier(t) for t in self.value_mpns),
        )


def ic_identifiers(props: Mapping[str, object]) -> Identifiers:
    """The identifiers in a symbol's raw *props*: ``MPN``, ``DigiKeyPN`` and
    ``LCSC`` under any alias spelling, and the MPN-shaped tokens of the
    ``Value`` (:func:`value_mpns`)."""
    pn = extract_part_numbers(props)
    return Identifiers(
        mpn=pn.mpn or None,
        digikey_pn=pn.digikey_pn or None,
        lcsc=pn.lcsc or None,
        value_mpns=value_mpns(props.get("Value")),
    )


@dataclass(frozen=True)
class IcRequirement:
    """What the search needs to find one IC part, and the footprint the
    chosen part is checked against.

    ``package`` (a :class:`kicadtools.package.PackageInfo` parsed from the
    footprint name) and ``pad_count`` (distinct pad numbers on the footprint)
    are ``None`` until the search step fills them in, and stay ``None`` when
    they cannot be found."""
    identifiers: Identifiers
    value_text: str = ""                 # the symbol's Value, as written
    footprint: str = ""                  # the symbol's Footprint, "" when empty
    package: Optional[PackageInfo] = None  # from the footprint name
    pad_count: Optional[int] = None      # pads on the footprint
    kind: str = IC_KIND
    # A connector with no footprint (smdconnector.py): the footprint its text
    # implies (kicadtools.impliedfootprint.ImpliedFootprint), which the
    # footprint check reads and the write plan writes when the part fits it.
    implied: Optional[Any] = None

    @property
    def check_footprint(self) -> str:
        """The footprint candidates are checked against: the symbol's, else
        the implied one; ``""`` for neither."""
        if self.footprint:
            return self.footprint
        return self.implied.lib_id if self.implied is not None else ""

    @property
    def searchable(self) -> bool:
        """At least one identifier."""
        return not self.problem()

    def problem(self) -> str:
        """``""`` when searchable, else ``"no part number"``."""
        if self.identifiers.empty:
            return NO_PART_NUMBER
        return ""


def ic_requirement(props: Mapping[str, object]) -> IcRequirement:
    """The requirement for one IC from its raw *props*: its identifiers
    (:func:`ic_identifiers`), Value and Footprint. ``package`` and
    ``pad_count`` are left ``None``."""
    return IcRequirement(
        identifiers=ic_identifiers(props),
        value_text=_prop(props, "Value"),
        footprint=_prop(props, "Footprint"),
    )


@dataclass
class IcGroup:
    """IC symbols with the same normalized identifiers and the same
    footprint, which share one requirement and one part choice.

    Members are ``requirement.Member`` records holding an IcIntent, one per
    placed symbol. Each unit of a multi-unit symbol is its own member (it has
    its own uuid and properties, and the write plan writes every unit), so
    MotorDriver's U9, drawn as units 1 and 2, is two members with one
    reference; :attr:`references` and :attr:`quantity` count it once. Do not
    call ``Member.description`` on them: it parses the Description field as
    capacitor or resistor evidence and has no meaning for an IC."""
    requirement: IcRequirement
    members: List[Member]

    @property
    def kind(self) -> str:
        return IC_KIND

    @property
    def existing(self) -> ExistingPart:
        """The first member's existing part numbers. The group key includes
        the normalized MPN, DigiKey PN and LCSC number, so every member holds
        the same ones up to case and whitespace."""
        return self.members[0].existing

    @property
    def references(self) -> List[str]:
        """The distinct member references in natural order (``U2`` before
        ``U10``); the units of a multi-unit symbol share one."""
        return sorted({m.reference for m in self.members}, key=natural_key)

    @property
    def quantity(self) -> int:
        """Parts placed per board: the number of distinct references."""
        return len(self.references)

    @property
    def assigned(self) -> bool:
        """Every member has an MPN, Manufacturer, DigiKey PN and a Footprint.
        An LCSC number is not required: a part JLCPCB does not stock is
        accepted without one."""
        for m in self.members:
            pn = extract_part_numbers(m.props)
            if not (pn.mpn and pn.manufacturer and pn.digikey_pn):
                return False
            if not _prop(m.props, "Footprint"):
                return False
        return True


def ic_group_key(props: Mapping[str, object]) -> Tuple[Any, ...]:
    """The group key of one IC symbol: its normalized identifiers
    (:meth:`Identifiers.key`) and its Footprint. A symbol with no identifier
    also keys on its normalized Value, so two unrelated parts that cannot be
    searched (an "Arduino Nano" and an "OLED display" on the same custom
    footprint) are not merged."""
    ids = ic_identifiers(props)
    value = normalize_identifier(_prop(props, "Value")) if ids.empty else ""
    return ids.key() + (_prop(props, "Footprint"), value)


def group_ics(records: Sequence[Any]) -> List[IcGroup]:
    """Group the IC records (from
    :func:`kicadtools.resolve.iter_bom_part_records`; those whose intent is an
    :class:`~kicadtools.intent.IcIntent`) by :func:`ic_group_key`. Records of
    other types are skipped. Each group's requirement is built from its first
    member (:func:`ic_requirement`). Groups are ordered by the natural key of
    their first reference."""
    by_key: Dict[Tuple[Any, ...], IcGroup] = {}
    for rec in records:
        intent = rec.intent
        if not isinstance(intent, IcIntent):
            continue
        key = ic_group_key(rec.props)
        member = Member(
            reference=rec.reference,
            source_file=rec.source_file,
            symbol_uuid=rec.symbol_uuid,
            props=rec.props,
            intent=intent,
        )
        group = by_key.get(key)
        if group is None:
            by_key[key] = IcGroup(ic_requirement(rec.props), [member])
        else:
            group.members.append(member)
    return sorted(by_key.values(), key=lambda g: natural_key(g.references[0]))
