"""
Represent the set of changes a resolution proposes for a schematic symbol.

Confirming a part against DigiKey (see :mod:`kicadtools.confirm`) turns up two
kinds of proposed edit: a **fill** for an intent field the author left blank
(``Tolerance:Preferred``, ``Power:Min``), and — when the symbol named only a
DigiKey PN — the **manufacturer part number** DigiKey resolved for it. This
module captures those as an ordered :class:`ChangeSet` with enough provenance to
apply them later, alongside any :class:`Conflict` (a value the named part
disagrees with) which is *recorded but never auto-applied* — a conflict means
the human picked the wrong part, not that we should overwrite their intent.

Nothing here touches the filesystem. :meth:`ChangeSet.write_plan` lowers the
accepted changes to the ``{file: {uuid: {key: value}}}`` shape that
:func:`kicadtools.writer.write_properties` consumes, so the CLI/GUI ``--write``
path (task #7) stays a thin driver over this representation.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

from kicadtools.confirm import ConfirmResult, Outcome
from kicadtools.partnum import CANONICAL_KEYS, extract_part_numbers
from kicadtools.resolve import PartRecord


@dataclass(frozen=True)
class ProposedChange:
    """One property to write onto a symbol, with why and where it came from."""
    reference: str
    source_file: Optional[Path]
    symbol_uuid: Optional[str]
    property_key: str            # canonical schematic property to set/insert
    value: str                   # value to write
    kind: str                    # "intent-fill" | "part-number"
    rationale: str               # human-readable justification

    @property
    def applicable(self) -> bool:
        """True when the change carries the provenance a write-back needs."""
        return bool(self.source_file and self.symbol_uuid)

    def describe(self) -> str:
        """One-line summary for dry-run output."""
        return f'{self.reference}: set "{self.property_key}" = "{self.value}"  ({self.rationale})'


@dataclass(frozen=True)
class Conflict:
    """A field where the named part disagrees with the author's stated intent.

    Recorded for reporting only — resolving it means the author changes the spec
    or the chosen part, so it is never turned into a write-back.
    """
    reference: str
    label: str
    property_key: str
    intent_value: Optional[str]
    digikey_value: Optional[str]

    def describe(self) -> str:
        return (
            f'{self.reference}: {self.label} conflict — symbol says '
            f'{self.intent_value!r}, DigiKey says {self.digikey_value!r}'
        )


@dataclass
class ChangeSet:
    """An ordered set of proposed changes plus the conflicts found alongside."""
    changes: List[ProposedChange] = field(default_factory=list)
    conflicts: List[Conflict] = field(default_factory=list)

    @property
    def is_empty(self) -> bool:
        return not self.changes and not self.conflicts

    def extend(self, other: "ChangeSet") -> None:
        self.changes.extend(other.changes)
        self.conflicts.extend(other.conflicts)

    def write_plan(self) -> Dict[Path, Dict[str, Dict[str, str]]]:
        """Group the *applicable* changes into
        ``{source_file: {symbol_uuid: {key: value}}}`` for
        :func:`kicadtools.writer.write_properties`.

        Changes without provenance are dropped (nowhere to write them); conflicts
        are never included. A later change to the same (file, uuid, key) wins.
        """
        plan: Dict[Path, Dict[str, Dict[str, str]]] = defaultdict(dict)
        for c in self.changes:
            if not c.applicable:
                continue
            plan[Path(c.source_file)].setdefault(c.symbol_uuid, {})[
                c.property_key
            ] = c.value
        return {path: dict(byuuid) for path, byuuid in plan.items()}


def changes_from_confirm(record: PartRecord, result: ConfirmResult) -> ChangeSet:
    """Derive the :class:`ChangeSet` a confirmed resistor proposes.

    * each :data:`~kicadtools.confirm.Outcome.FILL` field -> a ``"intent-fill"``
      change writing the canonical value to its property key;
    * each :data:`~kicadtools.confirm.Outcome.CONFLICT` -> a :class:`Conflict`;
    * a symbol that carried a DigiKey PN but no MPN -> a ``"part-number"`` change
      adding the manufacturer part number DigiKey resolved.
    """
    cs = ChangeSet()
    mpn = result.product.mpn

    for check in result.checks:
        if check.outcome is Outcome.FILL and check.proposed is not None:
            cs.changes.append(
                ProposedChange(
                    reference=result.reference,
                    source_file=record.source_file,
                    symbol_uuid=record.symbol_uuid,
                    property_key=check.prop_key,
                    value=check.proposed,
                    kind="intent-fill",
                    rationale=f"{check.label} from DigiKey {mpn}",
                )
            )
        elif check.outcome is Outcome.CONFLICT:
            cs.conflicts.append(
                Conflict(
                    reference=result.reference,
                    label=check.label,
                    property_key=check.prop_key,
                    intent_value=check.intent_value,
                    digikey_value=check.digikey_value,
                )
            )

    # Record the resolved manufacturer part number when the symbol lacked one.
    if mpn and not extract_part_numbers(record.props).mpn:
        cs.changes.append(
            ProposedChange(
                reference=result.reference,
                source_file=record.source_file,
                symbol_uuid=record.symbol_uuid,
                property_key=CANONICAL_KEYS["mpn"],
                value=mpn,
                kind="part-number",
                rationale="MPN resolved from DigiKey",
            )
        )

    return cs
