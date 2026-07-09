"""
Component model.

``ElectricalIntent`` is the base representation of a schematic symbol. Concrete
subclasses (resistor, capacitor, diode, …) register themselves against a custom
``Intent:Type`` schematic property, and ``ElectricalIntent.from_dict`` acts as a
factory that returns the most specific type it can build.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, ClassVar, Dict, List, Type, TypeVar


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
        """Build the most specific ``ElectricalIntent`` possible from *data*."""
        base_kwargs = cls._base_kwargs(data)
        intent_type = base_kwargs["intent_type"]

        concrete_cls = ElectricalIntent._registry.get(intent_type.lower())

        if concrete_cls is not None:
            try:
                return concrete_cls._from_dict_impl(data)  # type: ignore[return-value]
            except Exception as exc:
                print(
                    f"[ElectricalIntent] Failed to construct subclass "
                    f"{concrete_cls.__name__} for Intent:Type '{intent_type}' "
                    f"(reference '{base_kwargs['reference']}'): {exc}. "
                    f"Falling back to base class."
                )
                return cls(**base_kwargs)  # type: ignore[return-value]

        return cls(**base_kwargs)  # type: ignore[return-value]

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
    """Resistor – expects ``Value`` and optional ``Tolerance:Preferred``."""
    value: str
    tolerance_preferred: List[str] = field(default_factory=list)

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

        return cls(
            **cls._base_kwargs(data),
            value=value,
            tolerance_preferred=tolerance_preferred,
        )

    def description(self) -> str:
        tol = f" ±{self.tolerance_preferred[0]}" if self.tolerance_preferred else ""
        return f"{self.reference}: {self.value}{tol} resistor"


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


@intent_subclass("specificpart")
@dataclass(kw_only=True)
class SpecificPartIntent(ElectricalIntent):
    """Catch-all for a part pinned to a specific DigiKey PN or MPN."""
    digikey_pn: str | None = None
    mpn: str | None = None

    @classmethod
    def _from_dict_impl(cls, data: Dict[str, Any]) -> "SpecificPartIntent":
        digikey_pn = data.get("DigiKeyPN")
        mpn = data.get("mpn")

        if digikey_pn is None and mpn is None:
            raise KeyError("A specific part must define a DigiKey PN or an MPN")

        return cls(
            **cls._base_kwargs(data),
            digikey_pn=digikey_pn,
            mpn=mpn,
        )

    def description(self) -> str:
        dk = f", DigiKey {self.digikey_pn}" if self.digikey_pn else ""
        mp = f", MPN {self.mpn}" if self.mpn else ""
        return f"{self.reference}: (specified part){dk}{mp}"
