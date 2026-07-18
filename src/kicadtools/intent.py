"""
Component model.

``ElectricalIntent`` is the base representation of a schematic symbol. Concrete
subclasses (resistor, capacitor, diode, …) register themselves against a custom
``Intent:Type`` schematic property, and ``ElectricalIntent.from_dict`` acts as a
factory that returns the most specific type it can build.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, ClassVar, Dict, List, Type, TypeVar

from kicadtools.partnum import extract_part_numbers


def _parse_preferred(value: Any) -> List[str]:
    """Parse a comma-separated string (or any iterable) into a clean list."""
    if isinstance(value, str):
        return [item.strip() for item in value.split(",") if item.strip()]
    try:
        return [str(item).strip() for item in value if str(item).strip()]
    except TypeError:
        cleaned = str(value).strip()
        return [cleaned] if cleaned else []


_T = TypeVar("_T", bound="ElectricalIntent")


@dataclass(kw_only=True)
class ElectricalIntent:
    """
    Base representation of a schematic symbol.

    Mandatory
    ---------
    * reference – designator on the schematic (e.g. ``R1``)
    * dnp       – is the part "Do-Not-Place"?
    * in_bom    – should the part appear in the BOM?
    """
    reference: str
    dnp: bool
    in_bom: bool
    intent_type: str = field(default="Unknown")
    footprint: str | None = None
    mfr_preferred: List[str] = field(default_factory=list)
    # Why a symbol ended up as a plain ``ElectricalIntent`` instead of a typed
    # subclass. ``None`` on a successfully classified (typed) component.
    classification_note: str | None = field(default=None, compare=False)
    # Set when the intent type was inferred (see ``kicadtools.classify``) rather
    # than read from an explicit ``Intent:Type`` property. ``guess_basis`` records
    # which signal was used, for reporting.
    intent_guessed: bool = field(default=False, compare=False)
    guess_basis: str | None = field(default=None, compare=False)
    # Provenance — which schematic file and which placed symbol this component
    # came from. Used to locate the symbol again when writing an accepted
    # ``Intent:Type`` back into the schematic (see ``kicadtools.writer``).
    source_file: "Path | None" = field(default=None, compare=False)
    symbol_uuid: str | None = field(default=None, compare=False)

    # Registry – maps lower-cased Intent:Type → concrete subclass
    _registry: ClassVar[Dict[str, Type["ElectricalIntent"]]] = {}

    def __post_init__(self) -> None:
        if not isinstance(self.reference, str) or not self.reference:
            raise ValueError("`Reference` must be a non-empty string")
        if not isinstance(self.intent_type, str) or not self.intent_type:
            self.intent_type = "Unknown"

    @classmethod
    def _register(cls, intent_type: str) -> None:
        """Register a concrete subclass under a case-insensitive key."""
        ElectricalIntent._registry[intent_type.lower()] = cls

    @classmethod
    def _base_kwargs(cls, data: Dict[str, Any]) -> Dict[str, Any]:
        """Return only the keys that belong to ``ElectricalIntent``."""
        # mandatory
        reference = data["Reference"]
        dnp = data["dnp"]
        in_bom = data["in_bom"]

        # optional
        intent_type = str(data.get("Intent:Type", "Unknown"))
        footprint = data.get("Footprint")
        mfr_preferred = (
            _parse_preferred(data["Mfr:Preferred"])
            if "Mfr:Preferred" in data
            else []
        )

        return {
            "reference": reference,
            "dnp": dnp,
            "in_bom": in_bom,
            "intent_type": intent_type,
            "footprint": footprint,
            "mfr_preferred": mfr_preferred,
        }

    @classmethod
    def from_dict(cls: Type[_T], data: Dict[str, Any]) -> _T:
        """Build the most specific ``ElectricalIntent`` possible from *data*.

        When a symbol can't be classified into a typed subclass, a plain
        ``ElectricalIntent`` is returned with ``classification_note`` set to a
        human-readable explanation of why.
        """
        base_kwargs = cls._base_kwargs(data)
        intent_type = base_kwargs["intent_type"]

        concrete_cls = ElectricalIntent._registry.get(intent_type.lower())

        if concrete_cls is None:
            base = cls(**base_kwargs)  # type: ignore[assignment]
            if intent_type == "Unknown":
                base.classification_note = (
                    "no 'Intent:Type' property on the symbol — nothing to "
                    "classify it by"
                )
            else:
                known = ", ".join(sorted(ElectricalIntent._registry)) or "(none)"
                base.classification_note = (
                    f"Intent:Type '{intent_type}' is not a recognized type "
                    f"(known types: {known})"
                )
            return base  # type: ignore[return-value]

        try:
            return concrete_cls._from_dict_impl(data)  # type: ignore[return-value]
        except Exception as exc:
            base = cls(**base_kwargs)  # type: ignore[assignment]
            base.classification_note = (
                f"Intent:Type '{intent_type}' is recognized, but building a "
                f"{concrete_cls.__name__} failed: {exc}"
            )
            return base  # type: ignore[return-value]

    def description(self) -> str:
        """Base description – concrete subclasses override this."""
        return "No details"


def intent_subclass(intent_type: str):
    """Decorator that registers a subclass under *intent_type*."""
    def wrapper(cls: Type[ElectricalIntent]) -> Type[ElectricalIntent]:
        cls.intent_type = intent_type  # type: ignore[attr-defined]
        cls._register(intent_type)
        return cls
    return wrapper


@intent_subclass("resistor")
@dataclass(kw_only=True)
class ResistorIntent(ElectricalIntent):
    """Resistor – expects ``Value``; optional ``Tolerance:Preferred`` & ``Power:Min``.

    ``power_min`` is the minimum acceptable power rating (an opaque string like
    ``"0.25W"``), mirroring the ``Voltage:Min`` floor semantics — a resistor may
    be substituted by any part rated at least this. It is optional: a symbol that
    only specifies ``Value`` still classifies, and the rating is filled later by
    parametric resolution.
    """
    value: str
    tolerance_preferred: List[str] = field(default_factory=list)
    power_min: str | None = None

    @classmethod
    def _from_dict_impl(cls, data: Dict[str, Any]) -> "ResistorIntent":
        try:
            value = data["Value"]
        except KeyError as exc:
            raise KeyError(
                f"Resistor '{data['Reference']}' missing required key 'Value'"
            ) from exc

        tolerance_preferred: List[str] = []
        if "Tolerance:Preferred" in data:
            tolerance_preferred = _parse_preferred(data["Tolerance:Preferred"])

        power_min = data.get("Power:Min")

        return cls(
            **cls._base_kwargs(data),
            value=value,
            tolerance_preferred=tolerance_preferred,
            power_min=power_min,
        )

    def description(self) -> str:
        tol = f" ±{self.tolerance_preferred[0]}" if self.tolerance_preferred else ""
        pwr = f" {self.power_min}" if self.power_min else ""
        return f"{self.reference}: {self.value}{tol}{pwr} resistor"


@intent_subclass("capacitor")
@dataclass(kw_only=True)
class CapacitorIntent(ElectricalIntent):
    """Capacitor – requires ``Value``; optional ``Voltage:Min`` & ``Class:Preferred``."""
    value: str
    voltage_min: str | None = None
    class_preferred: List[str] = field(default_factory=list)

    @classmethod
    def _from_dict_impl(cls, data: Dict[str, Any]) -> "CapacitorIntent":
        try:
            value = data["Value"]
        except KeyError as exc:
            raise KeyError(
                f"Capacitor '{data['Reference']}' missing required key 'Value'"
            ) from exc

        voltage_min = data.get("Voltage:Min")

        class_preferred: List[str] = []
        if "Class:Preferred" in data:
            class_preferred = _parse_preferred(data["Class:Preferred"])

        return cls(
            **cls._base_kwargs(data),
            value=value,
            voltage_min=voltage_min,
            class_preferred=class_preferred,
        )

    def description(self) -> str:
        vm = f", min-V {self.voltage_min}" if self.voltage_min else ""
        return f"{self.reference}: {self.value} capacitor{vm}"


@intent_subclass("diode")
@dataclass(kw_only=True)
class DiodeIntent(ElectricalIntent):
    """Diode – optional ``Voltage:Min`` and ``Class:Preferred``."""
    voltage_min: str | None = None
    class_preferred: List[str] = field(default_factory=list)

    @classmethod
    def _from_dict_impl(cls, data: Dict[str, Any]) -> "DiodeIntent":
        voltage_min = data.get("Voltage:Min")

        class_preferred: List[str] = []
        if "Class:Preferred" in data:
            class_preferred = _parse_preferred(data["Class:Preferred"])

        return cls(
            **cls._base_kwargs(data),
            voltage_min=voltage_min,
            class_preferred=class_preferred,
        )

    def description(self) -> str:
        vm = f", min-V {self.voltage_min}" if self.voltage_min else ""
        cp = f", class {self.class_preferred[0]}" if self.class_preferred else ""
        return f"{self.reference}: diode{vm}{cp}"


# LED colours, longest-first so "warm white" wins over "white". Used only to
# enrich an already-decided LED's description, never to classify.
_LED_COLORS: List[str] = [
    "warm white", "cool white", "infrared", "ultraviolet",
    "red", "green", "blue", "yellow", "amber", "white", "orange", "rgb",
]
_LED_WAVELENGTH_RE = re.compile(r"\b(\d{3,4}\s*nm)\b")


@intent_subclass("led")
@dataclass(kw_only=True)
class LedIntent(ElectricalIntent):
    """Light-emitting diode.

    All fields optional. Colour and wavelength are parsed out of the ``Value`` /
    ``Description`` text (``"LED_Green"``, ``"1515 rgb led"``, ``"Red 630nm"`` …)
    when present.
    """
    value: str | None = None
    color: str | None = None
    wavelength: str | None = None

    @classmethod
    def _from_dict_impl(cls, data: Dict[str, Any]) -> "LedIntent":
        value = data.get("Value") or None

        # Normalise punctuation to spaces so tokens survive separators like the
        # underscore in "LED_Green".
        blob = f"{data.get('Value', '')} {data.get('Description', '')}"
        text = re.sub(r"[^a-z0-9]+", " ", blob.lower()).strip()

        color = next(
            (c for c in _LED_COLORS if re.search(rf"\b{re.escape(c)}\b", text)),
            None,
        )
        m = _LED_WAVELENGTH_RE.search(text)
        wavelength = m.group(1).replace(" ", "") if m else None

        return cls(
            **cls._base_kwargs(data),
            value=value,
            color=color,
            wavelength=wavelength,
        )

    def description(self) -> str:
        head = f"{self.color} LED" if self.color else "LED"
        wl = f" ({self.wavelength})" if self.wavelength else ""
        return f"{self.reference}: {head}{wl}"


# Footprint-name tokens KiCad connector libraries use, e.g.
# "PinHeader_1x03_P2.54mm_Horizontal" or "JST_SH_SM04B-...1x04-1MP_P1.00mm_...".
_CONN_POSITIONS_RE = re.compile(r"(\d+)x(\d+)")
_CONN_PITCH_RE = re.compile(r"P(\d+(?:\.\d+)?mm)")


@intent_subclass("connector")
@dataclass(kw_only=True)
class ConnectorIntent(ElectricalIntent):
    """Connector / header.

    All fields are optional — a connector rarely carries a ``Value`` worth
    reporting, so the pin count and pitch are parsed out of the footprint name
    (``…_1x04_…P1.00mm…``) when they're present.
    """
    value: str | None = None
    positions: int | None = None   # total number of contacts
    pitch: str | None = None       # e.g. "2.54mm"

    @classmethod
    def _from_dict_impl(cls, data: Dict[str, Any]) -> "ConnectorIntent":
        value = data.get("Value") or None

        positions: int | None = None
        pitch: str | None = None
        footprint = data.get("Footprint")
        if isinstance(footprint, str):
            m = _CONN_POSITIONS_RE.search(footprint)
            if m:
                positions = int(m.group(1)) * int(m.group(2))
            p = _CONN_PITCH_RE.search(footprint)
            if p:
                pitch = p.group(1)

        return cls(
            **cls._base_kwargs(data),
            value=value,
            positions=positions,
            pitch=pitch,
        )

    def description(self) -> str:
        parts = [f"{self.value} connector" if self.value else "connector"]
        if self.positions:
            parts.append(f"{self.positions}-pin")
        if self.pitch:
            parts.append(f"{self.pitch} pitch")
        return f"{self.reference}: " + ", ".join(parts)


# Transistor family / polarity tokens, most-specific first. Matched against text
# normalised to spaces (so "P-Channel" reads as "p channel"). Used only to
# enrich an already-decided transistor's description, never to classify.
_TRANSISTOR_DEVICES: List[tuple[str, str]] = [
    ("transistor array", "transistor array"),
    ("mosfet", "MOSFET"),
    ("igbt", "IGBT"),
    ("jfet", "JFET"),
    ("darlington", "Darlington"),
    ("bjt", "BJT"),
]
_TRANSISTOR_POLARITY: List[tuple[str, str]] = [
    ("npn", "NPN"),
    ("pnp", "PNP"),
    ("nmos", "N-channel"),
    ("pmos", "P-channel"),
    ("n channel", "N-channel"),
    ("p channel", "P-channel"),
]


@intent_subclass("transistor")
@dataclass(kw_only=True)
class TransistorIntent(ElectricalIntent):
    """Transistor — FET, BJT, IGBT or array.

    All fields optional. The device family and polarity are parsed out of the
    ``Value`` / ``Description`` text (``"P-Channel MOSFET"``, ``"NPN"``,
    ``"Trench FET"`` …) when present.
    """
    value: str | None = None
    device: str | None = None     # e.g. "MOSFET", "BJT", "IGBT"
    polarity: str | None = None   # e.g. "N-channel", "PNP"

    @classmethod
    def _from_dict_impl(cls, data: Dict[str, Any]) -> "TransistorIntent":
        value = data.get("Value") or None

        blob = f"{data.get('Value', '')} {data.get('Description', '')}"
        text = re.sub(r"[^a-z0-9]+", " ", blob.lower()).strip()

        def _first(table: List[tuple[str, str]]) -> str | None:
            return next(
                (label for token, label in table
                 if re.search(rf"\b{re.escape(token)}\b", text)),
                None,
            )

        device = _first(_TRANSISTOR_DEVICES)
        polarity = _first(_TRANSISTOR_POLARITY)
        # A bare "FET" is a field-effect transistor when nothing more specific
        # matched; a lone NPN/PNP means a BJT; a lone channel means a FET.
        if device is None and re.search(r"\bfet\b", text):
            device = "FET"
        if device is None and polarity in ("NPN", "PNP"):
            device = "BJT"
        if device is None and polarity in ("N-channel", "P-channel"):
            device = "FET"

        return cls(
            **cls._base_kwargs(data),
            value=value,
            device=device,
            polarity=polarity,
        )

    def description(self) -> str:
        head = self.device or "transistor"
        if self.polarity:
            head = f"{self.polarity} {head}"
        return f"{self.reference}: {head}"


@intent_subclass("specificpart")
@dataclass(kw_only=True)
class SpecificPartIntent(ElectricalIntent):
    """Catch-all for a part pinned to a specific DigiKey PN or MPN."""
    digikey_pn: str | None = None
    mpn: str | None = None

    @classmethod
    def _from_dict_impl(cls, data: Dict[str, Any]) -> "SpecificPartIntent":
        # Read through the alias table so any spelling — DigikeyPN, Digi-Key_PN,
        # Manufacturer_PN, … — is honoured, not just the two canonical keys.
        pn = extract_part_numbers(data)

        if pn.digikey_pn is None and pn.mpn is None:
            raise KeyError("A specific part must define a DigiKey PN or an MPN")

        return cls(
            **cls._base_kwargs(data),
            digikey_pn=pn.digikey_pn,
            mpn=pn.mpn,
        )

    def description(self) -> str:
        dk = f", DigiKey {self.digikey_pn}" if self.digikey_pn else ""
        mp = f", MPN {self.mpn}" if self.mpn else ""
        return f"{self.reference}: (specified part){dk}{mp}"
