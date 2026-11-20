"""Intermediate Representation (IR) for cross-ecosystem dual-version migration analysis."""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Any


DESTRUCTIVE_KINDS = {
    "DROP_COLUMN",
    "DROP_TABLE",
    "RENAME_COLUMN",
    "RENAME_TABLE",
    "SET_NOT_NULL",
    "ADD_NOT_NULL_NO_DEFAULT",
    "ALTER_COLUMN_TYPE",
    "RENAME_TYPE_ENUM",
    "DROP_ENUM_VALUE",
    "ADD_UNIQUE_OR_CHECK",
}

BOTH_DIRECTION_KINDS = {
    "RENAME_COLUMN",
    "RENAME_TABLE",
    "RENAME_TYPE_ENUM",
}


def norm_ident(name: str | None) -> str | None:
    if not name:
        return None
    s = name.strip().strip('"').strip("'").strip("`").strip(":")
    # Strip schema qualifier e.g. public.users -> users
    if "." in s:
        s = s.split(".")[-1].strip('"').strip("'").strip("`")
    return s.lower() if s else None


@dataclass
class DDLOp:
    ecosystem: str
    kind: str
    table: str | None
    column: str | None = None
    new_name: str | None = None
    old_type: str | None = None
    new_type: str | None = None
    phase_section: str = "up"  # "up" (forward) or "down" (rollback)
    has_safety_bypass: bool = False
    file_path: str = ""
    line: int = 0
    raw_snippet: str = ""
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def is_destructive(self) -> bool:
        return self.kind in DESTRUCTIVE_KINDS

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["is_destructive"] = self.is_destructive
        return d


@dataclass
class SymbolEvidence:
    table: str | None
    column: str | None
    orm_active_old: bool = False
    orm_active_new: bool = False
    rails_ignored_old: bool = False
    app_refs_old_count: int = 0
    app_refs_new_count: int = 0
    removed_in_commit_diff: bool = False
    orm_files_old: list[str] = field(default_factory=list)
    app_files_old: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class OpVerdict:
    op: DDLOp
    evidence: SymbolEvidence
    compat_backward: str  # SAFE | VIOLATION | UNCERTAIN
    compat_forward: str   # SAFE | VIOLATION | UNCERTAIN
    classification: str   # DUAL_VERSION_VIOLATION_BACKWARD | DUAL_VERSION_VIOLATION_BOTH | SAFE_CONTRACT | ROLLBACK_ONLY_FALSE_ALARM | SAFE_ADDITIVE | UNCERTAIN
    reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "op": self.op.to_dict(),
            "evidence": self.evidence.to_dict(),
            "compat_backward": self.compat_backward,
            "compat_forward": self.compat_forward,
            "classification": self.classification,
            "reason": self.reason,
        }
