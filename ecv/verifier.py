"""ExpandContractVerify (ECV): Cross-commit dual-version compatibility verifier
and single-file linter baseline rules (strong_migrations parity + squawk runner).
"""
from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path

from .ir import DDLOp, OpVerdict, SymbolEvidence, norm_ident
from .orm_parser import ORMSnapshot


# Type families for PostgreSQL / ORM type compatibility checks
_TYPE_FAMILIES = {
    "int": "integer", "int2": "integer", "int4": "integer", "int8": "integer",
    "smallint": "integer", "integer": "integer", "bigint": "integer",
    "integerfield": "integer", "bigintegerfield": "integer", "smallintegerfield": "integer",
    "positiveintegerfield": "integer", "positivesmallintegerfield": "integer",
    "float": "numeric", "float4": "numeric", "float8": "numeric", "real": "numeric",
    "double": "numeric", "double precision": "numeric", "numeric": "numeric",
    "decimal": "numeric", "decimalfield": "numeric", "floatfield": "numeric",
    "text": "text", "varchar": "text", "character varying": "text", "char": "text",
    "character": "text", "string": "text", "charfield": "text", "textfield": "text",
    "slugfield": "text", "emailfield": "text", "urlfield": "text", "citext": "text",
    "bool": "boolean", "boolean": "boolean", "booleanfield": "boolean",
    "json": "json", "jsonb": "json", "jsonfield": "json",
    "uuid": "uuid", "uuidfield": "uuid",
    "timestamp": "datetime", "timestamptz": "datetime", "timestamp with time zone": "datetime",
    "timestamp without time zone": "datetime", "datetime": "datetime", "datetimefield": "datetime",
    "date": "date", "datefield": "date",
    "bytea": "binary", "binary": "binary", "binaryfield": "binary",
}


def _type_family(t: str | None) -> str:
    if not t:
        return "unknown"
    base = re.sub(r"\(.*\)", "", t).strip().lower()
    return _TYPE_FAMILIES.get(base, base)


def _is_safe_widening(old_t: str | None, new_t: str | None) -> bool:
    if not old_t or not new_t:
        return False
    o, n = old_t.strip().lower(), new_t.strip().lower()
    if o == n:
        return True
    # varchar(N) -> varchar(M) where M >= N, or varchar -> text
    if o.startswith(("varchar", "character varying", "charfield")) and n in ("text", "textfield"):
        return True
    mo = re.search(r"(?:varchar|character varying)\s*\(\s*(\d+)\s*\)", o)
    mn = re.search(r"(?:varchar|character varying)\s*\(\s*(\d+)\s*\)", n)
    if mo and mn and int(mn.group(1)) >= int(mo.group(1)):
        return True
    # smallint -> integer -> bigint
    int_rank = {
        "smallint": 1, "int2": 1, "smallintegerfield": 1,
        "integer": 2, "int": 2, "int4": 2, "integerfield": 2,
        "bigint": 3, "int8": 3, "bigintegerfield": 3,
    }
    if o in int_rank and n in int_rank and int_rank[n] >= int_rank[o]:
        return True
    return False


def verify_op(
    op: DDLOp,
    orm_old: ORMSnapshot,
    orm_new: ORMSnapshot,
    removed_code_tokens: set[str] | None = None,
    added_code_tokens: set[str] | None = None,
    created_tables_in_commit: set[str] | None = None,
    app_refs_old: list[str] | None = None,
    app_refs_new: list[str] | None = None,
) -> OpVerdict:
    removed_code_tokens = removed_code_tokens or set()
    added_code_tokens = added_code_tokens or set()
    created_tables_in_commit = created_tables_in_commit or set()
    app_refs_old = app_refs_old or []
    app_refs_new = app_refs_new or []

    ev = SymbolEvidence(
        table=op.table,
        column=op.column,
        app_refs_old_count=len(app_refs_old),
        app_refs_new_count=len(app_refs_new),
        app_files_old=app_refs_old[:5],
    )

    # 0. v4: squashed Django migration (`replaces = [...]`): not executed on an existing database.
    if op.extra.get("squashed") and op.is_destructive:
        return OpVerdict(
            op=op, evidence=ev,
            compat_backward="SAFE", compat_forward="SAFE",
            classification="SQUASHED_REPLAY",
            reason="Squashed migration (`replaces`): skipped on databases that already applied the replaced migrations.",
        )

    # 1. Rollback-only section (down / downgrade / reverse_sql)
    if op.phase_section == "down" and op.is_destructive:
        return OpVerdict(
            op=op, evidence=ev,
            compat_backward="SAFE", compat_forward="SAFE",
            classification="ROLLBACK_ONLY_FALSE_ALARM",
            reason="Destructive DDL appears only inside rollback/down block, never executed in forward rollout.",
        )

    # Lookup models in C_{t-1} and C_t
    model_hint = op.extra.get("model_name")
    m_old = orm_old.find_model(op.table, model_hint=model_hint)
    m_new = orm_new.find_model(op.new_name or op.table, model_hint=op.extra.get("new_model_name") or model_hint)
    if m_old:
        ev.orm_files_old.append(m_old.file_path)

    # 2. Resolve Django ALTER_FIELD_CANDIDATE using C_{t-1} vs C_t model definitions
    if op.kind == "ALTER_FIELD_CANDIDATE":
        finfo = op.extra.get("field_info") or {}
        old_f = m_old.fields.get(op.column) if (m_old and op.column) else None
        if old_f:
            op.old_type = old_f.field_type
            new_null = finfo.get("null", False)
            if old_f.nullable and (not new_null):
                op.kind = "SET_NOT_NULL"
            elif old_f.field_type and op.new_type and _type_family(old_f.field_type) != _type_family(op.new_type):
                op.kind = "ALTER_COLUMN_TYPE"
            else:
                return OpVerdict(
                    op=op, evidence=ev,
                    compat_backward="SAFE", compat_forward="SAFE",
                    classification="SAFE_ADDITIVE",
                    reason="Django AlterField does not tighten nullability or change column type family.",
                )
        else:
            return OpVerdict(
                op=op, evidence=ev,
                compat_backward="SAFE", compat_forward="SAFE",
                classification="SAFE_ADDITIVE",
                reason="Django AlterField without prior nullable/type tightening evidence.",
            )

    if not op.is_destructive:
        return OpVerdict(
            op=op, evidence=ev,
            compat_backward="SAFE", compat_forward="SAFE",
            classification="SAFE_ADDITIVE",
            reason="Non-destructive additive DDL.",
        )

    # 2b. v3: post-deployment migrations (Rails db/post_migrate, Django is_post_deployment).
    # They run only after C_t is fully rolled out and C_{t-1} has drained, so the dual-version
    # window is already closed. The remaining hazard is C_t itself still using the symbol
    # (e.g. forgetting `ignored_columns`/`ignore_column` in C_t before a post-deploy drop).
    if op.extra.get("post_deploy"):
        ev.notes.append("post_deploy")
        if op.kind in ("DROP_COLUMN", "RENAME_COLUMN", "DROP_TABLE", "RENAME_TABLE"):
            mt = orm_new.find_model(op.table, model_hint=model_hint)
            if op.ecosystem == "rails":
                tbl_used = bool(mt and mt.has_model_class)
                col_used = bool(tbl_used and op.column and op.column not in mt.ignored_columns)
            else:
                tbl_used = mt is not None
                col_used = bool(mt and op.column and op.column in mt.fields)
            used = col_used if op.kind in ("DROP_COLUMN", "RENAME_COLUMN") else tbl_used
            ev.orm_active_new = used
            if used:
                return OpVerdict(
                    op=op, evidence=ev,
                    compat_backward="VIOLATION", compat_forward="SAFE",
                    classification="DUAL_VERSION_VIOLATION_BACKWARD",
                    reason=f"Post-deploy {op.kind} on '{op.table}.{op.column}' while C_t still loads/uses the symbol.",
                )
        return OpVerdict(
            op=op, evidence=ev,
            compat_backward="SAFE", compat_forward="SAFE",
            classification="SAFE_CONTRACT",
            reason="Post-deployment migration: runs after C_{t-1} has drained; C_t does not use the symbol.",
        )

    # Helper to check symbol presence in commit diff
    col_key = op.column
    tbl_key = op.table
    if col_key and (col_key in removed_code_tokens):
        ev.removed_in_commit_diff = True
    elif (not col_key) and tbl_key and (tbl_key in removed_code_tokens):
        ev.removed_in_commit_diff = True

    # Helper: is column active in C_{t-1} ORM?
    def _col_active_in_old() -> tuple[bool, bool]:
        if not m_old or not col_key:
            return False, False
        is_ignored = (op.ecosystem == "rails") and (col_key in m_old.ignored_columns)
        in_fields = (col_key in m_old.fields)
        if op.ecosystem == "rails":
            # In Rails, db/schema.rb lists columns; ActiveRecord loads all non-ignored columns
            active = in_fields and (not is_ignored)
            return active, is_ignored
        return in_fields, False

    orm_has_data = bool(orm_old.models_by_table or orm_old.enums)

    # v4: Rails tables that appear in db/schema.rb but have no ActiveRecord class in C_{t-1} app/models
    # (join tables, engine or gem tables, or classes the snapshot did not load). Declaration in the schema
    # does not show that C_{t-1} loads the table, so without other evidence the verdict is UNCERTAIN.
    if (op.ecosystem == "rails" and m_old is not None and not m_old.has_model_class
            and op.kind not in ("ADD_UNIQUE_OR_CHECK", "RENAME_TYPE_ENUM")
            and not ev.removed_in_commit_diff
            and not (tbl_key and tbl_key in created_tables_in_commit)):
        return OpVerdict(
            op=op, evidence=ev,
            compat_backward="UNCERTAIN", compat_forward="UNCERTAIN",
            classification="UNCERTAIN",
            reason=f"Table '{tbl_key}' has no ActiveRecord class in C_{{t-1}} app/models.",
        )

    # 3. Classify each destructive forward-only operator
    if op.kind == "DROP_COLUMN":
        active_old, ignored_old = _col_active_in_old()
        ev.orm_active_old = active_old
        ev.rails_ignored_old = ignored_old
        if m_new and col_key:
            ev.orm_active_new = col_key in m_new.fields

        if active_old or ev.app_refs_old_count > 0 or ev.removed_in_commit_diff:
            reasons = []
            if active_old:
                reasons.append(f"column '{col_key}' active in C_{{t-1}} ORM ({m_old.file_path})")
            if ev.app_refs_old_count > 0:
                reasons.append(f"{ev.app_refs_old_count} C_{{t-1}} app reference(s)")
            if ev.removed_in_commit_diff:
                reasons.append("identifier removed in same commit diff")
            return OpVerdict(
                op=op, evidence=ev,
                compat_backward="VIOLATION", compat_forward="SAFE",
                classification="DUAL_VERSION_VIOLATION_BACKWARD",
                reason="DROP_COLUMN breaks C_{t-1} |= S_t: " + "; ".join(reasons),
            )
        if m_old is not None and (ignored_old or m_old.has_schema_columns):
            why = "Rails ignored_columns active in C_{t-1}" if ignored_old else f"column '{col_key}' already absent from C_{{t-1}} ORM and app code"
            return OpVerdict(
                op=op, evidence=ev,
                compat_backward="SAFE", compat_forward="SAFE",
                classification="SAFE_CONTRACT",
                reason=f"Safe Contract phase: {why}.",
            )
        return OpVerdict(
            op=op, evidence=ev,
            compat_backward="UNCERTAIN", compat_forward="SAFE",
            classification="UNCERTAIN",
            reason=f"Table '{tbl_key}' columns not resolved in C_{{t-1}} schema snapshot.",
        )

    if op.kind == "DROP_TABLE":
        ev.orm_active_old = m_old is not None
        if ev.orm_active_old or ev.app_refs_old_count > 0 or ev.removed_in_commit_diff:
            return OpVerdict(
                op=op, evidence=ev,
                compat_backward="VIOLATION", compat_forward="SAFE",
                classification="DUAL_VERSION_VIOLATION_BACKWARD",
                reason=f"DROP_TABLE breaks C_{{t-1}} |= S_t: table '{tbl_key}' active in C_{{t-1}}.",
            )
        # v4: a table missing from O_{t-1} is only a safe contract with positive evidence of staging
        # (Django SeparateDatabaseAndState database-only step, Sentry SafeDeleteModel). Otherwise it may be
        # a third-party/engine table, a proxy model, or a snapshot gap -> UNCERTAIN.
        if orm_has_data and op.extra.get("staged"):
            return OpVerdict(
                op=op, evidence=ev,
                compat_backward="SAFE", compat_forward="SAFE",
                classification="SAFE_CONTRACT",
                reason=f"Safe Contract phase: staged drop; table '{tbl_key}' already absent from C_{{t-1}} ORM.",
            )
        if orm_has_data:
            return OpVerdict(
                op=op, evidence=ev,
                compat_backward="UNCERTAIN", compat_forward="SAFE",
                classification="UNCERTAIN",
                reason=f"Table '{tbl_key}' not resolved in C_{{t-1}} ORM snapshot.",
            )
        return OpVerdict(
            op=op, evidence=ev,
            compat_backward="UNCERTAIN", compat_forward="SAFE",
            classification="UNCERTAIN",
            reason="C_{t-1} ORM snapshot unavailable.",
        )

    if op.kind == "RENAME_COLUMN":
        active_old, ignored_old = _col_active_in_old()
        ev.orm_active_old = active_old
        ev.rails_ignored_old = ignored_old
        new_c = op.new_name
        active_new = bool(m_new and new_c and (new_c in m_new.fields))
        ev.orm_active_new = active_new
        b_broken = active_old or ev.app_refs_old_count > 0 or ev.removed_in_commit_diff
        f_broken = active_new or ev.app_refs_new_count > 0 or bool(new_c and new_c in added_code_tokens)
        if b_broken and f_broken:
            return OpVerdict(
                op=op, evidence=ev,
                compat_backward="VIOLATION", compat_forward="VIOLATION",
                classification="DUAL_VERSION_VIOLATION_BOTH",
                reason=f"RENAME_COLUMN '{col_key}'->'{new_c}' breaks both C_{{t-1}} |= S_t and C_t |= S_{{t-1}}.",
            )
        if b_broken:
            return OpVerdict(
                op=op, evidence=ev,
                compat_backward="VIOLATION", compat_forward="SAFE",
                classification="DUAL_VERSION_VIOLATION_BACKWARD",
                reason=f"RENAME_COLUMN '{col_key}'->'{new_c}' breaks C_{{t-1}} |= S_t.",
            )
        if f_broken:
            return OpVerdict(
                op=op, evidence=ev,
                compat_backward="SAFE", compat_forward="VIOLATION",
                classification="DUAL_VERSION_VIOLATION_FORWARD",
                reason=f"RENAME_COLUMN '{col_key}'->'{new_c}' breaks C_t |= S_{{t-1}}.",
            )
        if m_old is not None and (ignored_old or m_old.has_schema_columns):
            return OpVerdict(
                op=op, evidence=ev,
                compat_backward="SAFE", compat_forward="SAFE",
                classification="SAFE_CONTRACT",
                reason=f"Renamed column '{col_key}' not referenced in C_{{t-1}} or C_t.",
            )
        return OpVerdict(
            op=op, evidence=ev,
            compat_backward="UNCERTAIN", compat_forward="UNCERTAIN",
            classification="UNCERTAIN",
            reason=f"Table '{tbl_key}' columns not resolved in C_{{t-1}} schema snapshot.",
        )

    if op.kind == "RENAME_TABLE":
        ev.orm_active_old = m_old is not None
        ev.orm_active_new = m_new is not None
        b_broken = ev.orm_active_old or ev.app_refs_old_count > 0 or ev.removed_in_commit_diff
        f_broken = ev.orm_active_new or ev.app_refs_new_count > 0 or bool(op.new_name and op.new_name in added_code_tokens)
        if b_broken and f_broken:
            return OpVerdict(
                op=op, evidence=ev,
                compat_backward="VIOLATION", compat_forward="VIOLATION",
                classification="DUAL_VERSION_VIOLATION_BOTH",
                reason=f"RENAME_TABLE '{tbl_key}'->'{op.new_name}' breaks both directions.",
            )
        if b_broken:
            return OpVerdict(
                op=op, evidence=ev,
                compat_backward="VIOLATION", compat_forward="SAFE",
                classification="DUAL_VERSION_VIOLATION_BACKWARD",
                reason=f"RENAME_TABLE '{tbl_key}' breaks C_{{t-1}} |= S_t.",
            )
        if orm_has_data and op.extra.get("staged"):
            return OpVerdict(
                op=op, evidence=ev,
                compat_backward="SAFE", compat_forward="SAFE",
                classification="SAFE_CONTRACT",
                reason=f"Staged rename; table '{tbl_key}' not active in C_{{t-1}}.",
            )
        if orm_has_data:
            return OpVerdict(
                op=op, evidence=ev,
                compat_backward="UNCERTAIN", compat_forward="UNCERTAIN",
                classification="UNCERTAIN",
                reason=f"Table '{tbl_key}' not resolved in C_{{t-1}} ORM snapshot.",
            )
        return OpVerdict(
            op=op, evidence=ev,
            compat_backward="UNCERTAIN", compat_forward="UNCERTAIN",
            classification="UNCERTAIN",
            reason="C_{t-1} ORM snapshot unavailable.",
        )

    if op.kind == "ADD_NOT_NULL_NO_DEFAULT":
        # If table itself was newly created in commit t, C_{t-1} never inserts into it!
        if tbl_key and tbl_key in created_tables_in_commit:
            return OpVerdict(
                op=op, evidence=ev,
                compat_backward="SAFE", compat_forward="SAFE",
                classification="SAFE_ADDITIVE",
                reason=f"Table '{tbl_key}' was created in the same commit; C_{{t-1}} does not write to it.",
            )
        ev.orm_active_old = m_old is not None
        if ev.orm_active_old or ev.app_refs_old_count > 0:
            extra_note = " (Django drops DB default after backfill)" if op.extra.get("django_python_default_dropped_in_db") else ""
            return OpVerdict(
                op=op, evidence=ev,
                compat_backward="VIOLATION", compat_forward="SAFE",
                classification="DUAL_VERSION_VIOLATION_BACKWARD",
                reason=f"ADD_NOT_NULL_NO_DEFAULT on active C_{{t-1}} table '{tbl_key}.{col_key}' breaks C_{{t-1}} INSERTs{extra_note}.",
            )
        if orm_has_data:
            return OpVerdict(
                op=op, evidence=ev,
                compat_backward="UNCERTAIN", compat_forward="SAFE",
                classification="UNCERTAIN",
                reason=f"Table '{tbl_key}' not resolved in C_{{t-1}} ORM snapshot.",
            )
        return OpVerdict(
            op=op, evidence=ev,
            compat_backward="UNCERTAIN", compat_forward="SAFE",
            classification="UNCERTAIN",
            reason=f"Table '{tbl_key}' not resolved in C_{{t-1}} ORM snapshot.",
        )

    if op.kind == "SET_NOT_NULL":
        active_old, ignored_old = _col_active_in_old()
        ev.orm_active_old = active_old
        ev.rails_ignored_old = ignored_old
        old_f = m_old.fields.get(col_key) if (m_old and col_key) else None
        if old_f and old_f.nullable and (not ignored_old):
            return OpVerdict(
                op=op, evidence=ev,
                compat_backward="VIOLATION", compat_forward="SAFE",
                classification="DUAL_VERSION_VIOLATION_BACKWARD",
                reason=f"SET_NOT_NULL on '{tbl_key}.{col_key}' which was nullable in C_{{t-1}} ORM ({m_old.file_path}).",
            )
        if old_f and (not old_f.nullable) and (not ev.removed_in_commit_diff):
            return OpVerdict(
                op=op, evidence=ev,
                compat_backward="SAFE", compat_forward="SAFE",
                classification="SAFE_CONTRACT",
                reason=f"Safe Contract phase: '{tbl_key}.{col_key}' was already non-nullable in C_{{t-1}} ORM.",
            )
        if active_old or ev.app_refs_old_count > 0:
            return OpVerdict(
                op=op, evidence=ev,
                compat_backward="VIOLATION", compat_forward="SAFE",
                classification="DUAL_VERSION_VIOLATION_BACKWARD",
                reason=f"SET_NOT_NULL on active C_{{t-1}} column '{tbl_key}.{col_key}' without prior NOT NULL guarantee.",
            )
        if m_old is not None and (ignored_old or m_old.has_schema_columns):
            return OpVerdict(
                op=op, evidence=ev,
                compat_backward="SAFE", compat_forward="SAFE",
                classification="SAFE_CONTRACT",
                reason=f"Column '{tbl_key}.{col_key}' not active in C_{{t-1}} ORM.",
            )
        return OpVerdict(
            op=op, evidence=ev,
            compat_backward="UNCERTAIN", compat_forward="SAFE",
            classification="UNCERTAIN",
            reason=f"Table '{tbl_key}' columns not resolved in C_{{t-1}} schema snapshot.",
        )

    if op.kind == "ALTER_COLUMN_TYPE":
        active_old, ignored_old = _col_active_in_old()
        ev.orm_active_old = active_old
        ev.rails_ignored_old = ignored_old
        old_f = m_old.fields.get(col_key) if (m_old and col_key) else None
        old_t = op.old_type or (old_f.field_type if old_f else None)
        if _is_safe_widening(old_t, op.new_type):
            return OpVerdict(
                op=op, evidence=ev,
                compat_backward="SAFE", compat_forward="SAFE",
                classification="SAFE_ADDITIVE",
                reason=f"Type widening {old_t} -> {op.new_type} preserves C_{{t-1}} data compatibility.",
            )
        if active_old or ev.app_refs_old_count > 0 or ev.removed_in_commit_diff:
            return OpVerdict(
                op=op, evidence=ev,
                compat_backward="VIOLATION", compat_forward="SAFE",
                classification="DUAL_VERSION_VIOLATION_BACKWARD",
                reason=f"ALTER_COLUMN_TYPE on active C_{{t-1}} column '{tbl_key}.{col_key}' ({old_t} -> {op.new_type}).",
            )
        if m_old is not None and (ignored_old or m_old.has_schema_columns):
            return OpVerdict(
                op=op, evidence=ev,
                compat_backward="SAFE", compat_forward="SAFE",
                classification="SAFE_CONTRACT",
                reason=f"Column '{tbl_key}.{col_key}' not active in C_{{t-1}}.",
            )
        return OpVerdict(
            op=op, evidence=ev,
            compat_backward="UNCERTAIN", compat_forward="SAFE",
            classification="UNCERTAIN",
            reason=f"Table '{tbl_key}' columns not resolved in C_{{t-1}} schema snapshot.",
        )

    if op.kind == "RENAME_TYPE_ENUM":
        ename = op.table or op.column
        in_old_enums = bool(ename and (ename in orm_old.enums or (op.new_name and op.new_name in orm_old.enums)))
        ev.orm_active_old = in_old_enums
        if in_old_enums or ev.app_refs_old_count > 0 or ev.removed_in_commit_diff:
            return OpVerdict(
                op=op, evidence=ev,
                compat_backward="VIOLATION", compat_forward="VIOLATION",
                classification="DUAL_VERSION_VIOLATION_BOTH",
                reason=f"ENUM/TYPE drop or rename '{ename}' affects active C_{{t-1}} enum definition/code.",
            )
        if orm_has_data:
            return OpVerdict(
                op=op, evidence=ev,
                compat_backward="SAFE", compat_forward="SAFE",
                classification="SAFE_CONTRACT",
                reason=f"ENUM '{ename}' not present in C_{{t-1}} schema.",
            )
        return OpVerdict(
            op=op, evidence=ev,
            compat_backward="UNCERTAIN", compat_forward="UNCERTAIN",
            classification="UNCERTAIN",
            reason="C_{t-1} ORM snapshot unavailable.",
        )

    if op.kind == "ADD_UNIQUE_OR_CHECK":
        ev.orm_active_old = m_old is not None
        if tbl_key and tbl_key in created_tables_in_commit:
            return OpVerdict(
                op=op, evidence=ev,
                compat_backward="SAFE", compat_forward="SAFE",
                classification="SAFE_ADDITIVE",
                reason=f"Constraint added on newly created table '{tbl_key}'.",
            )
        if ev.orm_active_old:
            # v3: whether C_{t-1} writes rows that violate the new CHECK/UNIQUE predicate depends on
            # runtime data and predicate semantics, which static analysis cannot decide. Report it as
            # a separate DATA_DEPENDENT warning instead of a confirmed violation.
            return OpVerdict(
                op=op, evidence=ev,
                compat_backward="DATA_DEPENDENT", compat_forward="SAFE",
                classification="DATA_DEPENDENT",
                reason=f"CHECK/UNIQUE constraint added on active C_{{t-1}} table '{tbl_key}'; safety depends on C_{{t-1}} write values.",
            )
        return OpVerdict(
            op=op, evidence=ev,
            compat_backward="UNCERTAIN", compat_forward="SAFE",
            classification="UNCERTAIN",
            reason=f"Table '{tbl_key}' not resolved in C_{{t-1}}.",
        )

    return OpVerdict(
        op=op, evidence=ev,
        compat_backward="UNCERTAIN", compat_forward="UNCERTAIN",
        classification="UNCERTAIN",
        reason="Unhandled operator kind.",
    )


# ---------------------------------------------------------------------------
# Baseline 1: strong_migrations single-file rule model (documented rules)
# ---------------------------------------------------------------------------

STRONG_MIGRATIONS_FLAGGED_KINDS = {
    "DROP_COLUMN",
    "DROP_TABLE",
    "RENAME_COLUMN",
    "RENAME_TABLE",
    "SET_NOT_NULL",
    "ALTER_COLUMN_TYPE",
    "ADD_UNIQUE_OR_CHECK",
    # Note: strong_migrations (PG >= 11) allows ADD_COLUMN with default, flags without default if NOT NULL on existing table
    "ADD_NOT_NULL_NO_DEFAULT",
}


def strong_migrations_baseline_verdict(op: DDLOp) -> dict:
    """Faithful single-file behavior of ankane/strong_migrations:
    - Inspects ONLY the single migration file (no C_{t-1} code/model awareness).
    - Flags destructive DDL in up/change UNLESS wrapped in `safety_assured`.
    - Because it has no cross-commit view, it:
        (a) raises FALSE POSITIVES on safe Contract migrations not wrapped in safety_assured, and
        (b) suffers FALSE NEGATIVES when developers wrap an unsafe drop in `safety_assured`
            without having deployed `ignored_columns` in C_{t-1}!
    """
    if op.phase_section == "down":
        # strong_migrations executes inside ActiveRecord::Migration#migrate(:up) so down() isn't flagged in forward run
        return {"flagged": False, "rule": None, "bypassed": False}
    if op.kind in STRONG_MIGRATIONS_FLAGGED_KINDS:
        if op.has_safety_bypass:
            return {"flagged": False, "rule": op.kind, "bypassed": True}
        return {"flagged": True, "rule": op.kind, "bypassed": False}
    return {"flagged": False, "rule": None, "bypassed": False}


# ---------------------------------------------------------------------------
# Baseline 2: Real `squawk` v2.73.0 CLI runner via WSL
# ---------------------------------------------------------------------------

SQUAWK_WSL_BIN = os.environ.get("SQUAWK_BIN", "squawk")  # path to the squawk v2.73.0 binary


def run_squawk_sql(sql_text: str) -> dict:
    """Run actual squawk v2.73.0 CLI on PostgreSQL migration SQL (direct or via WSL on Windows)."""
    if not sql_text or not sql_text.strip():
        return {"flagged": False, "rules": [], "raw_count": 0, "error": None}
    try:
        use_wsl = (os.name == "nt") and (
            os.environ.get("SQUAWK_USE_WSL", "").lower() in ("1", "true", "yes")
            or SQUAWK_WSL_BIN.startswith("/")
        )
        cmd = (["wsl", SQUAWK_WSL_BIN] if use_wsl else [SQUAWK_WSL_BIN]) + [
            "--stdin-filepath", "migration.sql", "--reporter", "json",
        ]
        proc = subprocess.run(
            cmd,
            input=sql_text.encode("utf-8", errors="ignore"),
            capture_output=True,
            timeout=20,
        )
        out = proc.stdout.decode("utf-8", errors="ignore").strip()
        if not out:
            return {"flagged": False, "rules": [], "raw_count": 0, "error": None}
        data = json.loads(out)
        rules = sorted({item.get("rule_name", "") for item in data if isinstance(item, dict) and item.get("rule_name")})
        return {"flagged": len(rules) > 0, "rules": rules, "raw_count": len(data), "error": None}
    except Exception as e:
        return {"flagged": False, "rules": [], "raw_count": 0, "error": str(e)}
