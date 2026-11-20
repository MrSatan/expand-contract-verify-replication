"""ExpandContractSynth (ECS): Automated 3-phase Expand -> Dual-Write/Backfill -> Contract
plan generator (PostgreSQL SQL + PL/pgSQL bidirectional sync triggers + ORM shim guidance).
"""
from __future__ import annotations

from dataclasses import dataclass, asdict
from .ir import DDLOp


@dataclass
class SynthPlan:
    supported: bool
    op_kind: str
    table: str
    column: str | None
    phase1_expand_sql: list[str]
    phase2_backfill_sql: list[str]
    phase2_orm_shim: str
    phase3_contract_sql: list[str]
    notes: str = ""
    executable_sql: bool = True

    def to_dict(self) -> dict:
        return asdict(self)


def _q(ident: str) -> str:
    clean = ident.replace('"', '""')
    return f'"{clean}"'


def _infer_pg_type(hint: str | None) -> str:
    """Preserve exact valid PostgreSQL types verbatim (e.g., TIMESTAMP(3), BIGINT, VARCHAR(255));
    map ORM-specific type names (Django/Rails/Prisma) to canonical PostgreSQL types."""
    if not hint:
        return "TEXT"
    raw = hint.strip()
    h = raw.lower()
    # Preserve explicit PostgreSQL SQL types verbatim (fixing v1 TIMESTAMP(3)->TIMESTAMPTZ bug!)
    pg_exact_prefixes = (
        "timestamp", "timestamptz", "varchar", "character varying", "char(", "character(",
        "bigint", "smallint", "integer", "int2", "int4", "int8", "numeric", "decimal",
        "double precision", "real", "boolean", "bool", "jsonb", "json", "uuid", "text", "bytea",
    )
    if (h == "date" or any(h.startswith(p) for p in pg_exact_prefixes)) and "field" not in h:
        return raw.upper()
    if "bigint" in h or "biginteger" in h:
        return "BIGINT"
    if "smallint" in h or "smallinteger" in h:
        return "SMALLINT"
    if any(k in h for k in ("int", "serial")):
        return "INTEGER"
    if any(k in h for k in ("bool",)):
        return "BOOLEAN"
    if any(k in h for k in ("float", "double", "dec", "num")):
        return "DOUBLE PRECISION"
    if any(k in h for k in ("json",)):
        return "JSONB"
    if any(k in h for k in ("time", "date")):
        return "TIMESTAMP(3)" if "datetime" in h else "TIMESTAMPTZ"
    if "uuid" in h:
        return "UUID"
    return "TEXT"


def _default_literal_for_type(pg_type: str) -> str:
    t = pg_type.upper()
    if "INT" in t or "DOUBLE" in t or "NUMERIC" in t or "DECIMAL" in t:
        return "0"
    if "BOOL" in t:
        return "FALSE"
    if "JSON" in t:
        return "'{}'::jsonb"
    if "TIME" in t or "DATE" in t:
        return "NOW()"
    if "UUID" in t:
        return "'00000000-0000-0000-0000-000000000000'::uuid"
    return "''"


def synthesize_expand_contract(op: DDLOp, old_pg_type: str = "TEXT", new_pg_type: str | None = None) -> SynthPlan:
    """Synthesize a verified PostgreSQL 3-phase Expand-Contract migration plan for `op`."""
    # v3: use the original quoted spelling when the parser recovered it (Prisma mixed-case identifiers).
    tbl = op.extra.get("table_sql") or op.table or "target_table"
    col = op.extra.get("column_sql") or op.column
    qt = _q(tbl)
    qc = _q(col) if col else None
    safe_id = f"{tbl}_{col or 'tbl'}".replace("-", "_")

    # 1. DROP_COLUMN
    if op.kind == "DROP_COLUMN" and col:
        shim = {
            "rails": f"Add `self.ignored_columns += [\"{col}\"]` to model for `{tbl}` and remove app reads/writes in Release 1; run Phase 3 DROP in Release 2.",
            "prisma": f"Remove field `{col}` from `schema.prisma` (or mark nullable) and deploy Release 1 without dropping DB column; run Phase 3 DROP in Release 2.",
            "django": f"Use `migrations.SeparateDatabaseAndState(state_operations=[RemoveField(...)], database_operations=[])` in Release 1; run Phase 3 DROP in Release 2.",
        }.get(op.ecosystem, f"Stop reading/writing `{tbl}.{col}` in Release 1; drop in Release 2.")
        return SynthPlan(
            supported=True, op_kind=op.kind, table=tbl, column=col,
            # Phase 1: Drop NOT NULL if present so C_new (which omits col) can INSERT while C_old still reads/writes col!
            phase1_expand_sql=[
                f"ALTER TABLE {qt} ALTER COLUMN {qc} DROP NOT NULL;",
            ],
            phase2_backfill_sql=[],
            phase2_orm_shim=shim,
            phase3_contract_sql=[
                f"ALTER TABLE {qt} DROP COLUMN IF EXISTS {qc};",
            ],
            notes="Dropping NOT NULL in Phase 1 lets C_t omit the column while C_{t-1} still reads/writes it.",
        )

    # 2. DROP_TABLE
    if op.kind == "DROP_TABLE":
        return SynthPlan(
            supported=True, op_kind=op.kind, table=tbl, column=None,
            phase1_expand_sql=[
                f"-- Phase 1 (Expand): Retain table {qt} intact during rolling cutover so C_{{t-1}} reads/writes succeed.",
            ],
            phase2_backfill_sql=[],
            phase2_orm_shim=f"Remove ORM model and all application references to `{tbl}` in Release 1; drop table only after all C_{{t-1}} pods drain.",
            phase3_contract_sql=[
                f"DROP TABLE IF EXISTS {qt};",
            ],
            notes="Two-release separation: code removal precedes physical DROP TABLE.",
        )

    # 3. RENAME_COLUMN
    if op.kind == "RENAME_COLUMN" and col and op.new_name:
        new_c = op.extra.get("new_name_sql") or op.new_name
        qnew = _q(new_c)
        pg_t = _infer_pg_type(old_pg_type)
        fn = f"ec_sync_{safe_id}_to_{new_c}"
        trg = f"trg_{fn}"
        return SynthPlan(
            supported=True, op_kind=op.kind, table=tbl, column=col,
            phase1_expand_sql=[
                f"ALTER TABLE {qt} ADD COLUMN IF NOT EXISTS {qnew} {pg_t};",
                f"ALTER TABLE {qt} ALTER COLUMN {qc} DROP NOT NULL;",
                (
                    f"CREATE OR REPLACE FUNCTION {fn}() RETURNS trigger AS $$\n"
                    f"BEGIN\n"
                    f"  IF TG_OP = 'INSERT' THEN\n"
                    f"    IF NEW.{qnew} IS NULL AND NEW.{qc} IS NOT NULL THEN NEW.{qnew} := NEW.{qc}; END IF;\n"
                    f"    IF NEW.{qc} IS NULL AND NEW.{qnew} IS NOT NULL THEN NEW.{qc} := NEW.{qnew}; END IF;\n"
                    f"  ELSIF TG_OP = 'UPDATE' THEN\n"
                    f"    IF NEW.{qnew} IS DISTINCT FROM OLD.{qnew} THEN NEW.{qc} := NEW.{qnew}; "
                    f"ELSIF NEW.{qc} IS DISTINCT FROM OLD.{qc} THEN NEW.{qnew} := NEW.{qc}; END IF;\n"
                    f"  END IF;\n"
                    f"  RETURN NEW;\n"
                    f"END;\n"
                    f"$$ LANGUAGE plpgsql;"
                ),
                f"DROP TRIGGER IF EXISTS {trg} ON {qt};",
                f"CREATE TRIGGER {trg} BEFORE INSERT OR UPDATE ON {qt} FOR EACH ROW EXECUTE FUNCTION {fn}();",
            ],
            phase2_backfill_sql=[
                f"UPDATE {qt} SET {qnew} = {qc} WHERE {qnew} IS NULL AND {qc} IS NOT NULL;",
            ],
            phase2_orm_shim=f"Deploy C_t reading/writing `{new_c}` while PL/pgSQL trigger `{trg}` keeps `{col}` and `{new_c}` synchronized for live C_{{t-1}} pods.",
            phase3_contract_sql=[
                f"DROP TRIGGER IF EXISTS {trg} ON {qt};",
                f"DROP FUNCTION IF EXISTS {fn}();",
                f"ALTER TABLE {qt} DROP COLUMN IF EXISTS {qc};",
            ],
            notes="Bidirectional PL/pgSQL trigger guarantees both C_{t-1} and C_t read/write consistent values concurrently.",
        )

    # 4. RENAME_TABLE
    if op.kind == "RENAME_TABLE" and op.new_name:
        new_t = op.extra.get("new_name_sql") or op.new_name
        qnew_t = _q(new_t)
        return SynthPlan(
            supported=True, op_kind=op.kind, table=tbl, column=None,
            phase1_expand_sql=[
                f"CREATE OR REPLACE VIEW {qnew_t} AS SELECT * FROM {qt};",
            ],
            phase2_backfill_sql=[],
            phase2_orm_shim=f"PostgreSQL updatable view `{new_t}` allows C_t to read/write `{new_t}` while C_{{t-1}} continues using `{tbl}`.",
            phase3_contract_sql=[
                f"BEGIN; DROP VIEW IF EXISTS {qnew_t}; ALTER TABLE {qt} RENAME TO {qnew_t}; COMMIT;",
            ],
            notes="PostgreSQL simple views are automatically updatable (INSERT/UPDATE/DELETE) during dual-version rollout.",
        )

    # 5. ADD_NOT_NULL_NO_DEFAULT
    if op.kind == "ADD_NOT_NULL_NO_DEFAULT" and col:
        pg_t = _infer_pg_type(new_pg_type or op.new_type)
        dflt = _default_literal_for_type(pg_t)
        chk = f"chk_{safe_id}_nn"
        return SynthPlan(
            supported=True, op_kind=op.kind, table=tbl, column=col,
            phase1_expand_sql=[
                f"ALTER TABLE {qt} ADD COLUMN IF NOT EXISTS {qc} {pg_t} DEFAULT {dflt};",
            ],
            phase2_backfill_sql=[
                f"UPDATE {qt} SET {qc} = {dflt} WHERE {qc} IS NULL;",
            ],
            phase2_orm_shim=f"Deploy C_t explicitly populating `{tbl}.{col}`; DB DEFAULT {dflt} keeps C_{{t-1}} INSERTs valid without 23502 violation.",
            phase3_contract_sql=[
                f"ALTER TABLE {qt} ADD CONSTRAINT {chk} CHECK ({qc} IS NOT NULL) NOT VALID;",
                f"ALTER TABLE {qt} VALIDATE CONSTRAINT {chk};",
                f"ALTER TABLE {qt} ALTER COLUMN {qc} SET NOT NULL;",
                f"ALTER TABLE {qt} DROP CONSTRAINT {chk};",
                f"ALTER TABLE {qt} ALTER COLUMN {qc} DROP DEFAULT;",
            ],
            notes="PostgreSQL 12+ skips full-table ACCESS EXCLUSIVE scan when a validated NOT NULL CHECK constraint exists; Phase 3 drops temporary DEFAULT so final schema equals S_t.",
        )

    # 6. SET_NOT_NULL
    if op.kind == "SET_NOT_NULL" and col:
        pg_t = _infer_pg_type(old_pg_type)
        dflt = _default_literal_for_type(pg_t)
        chk = f"chk_{safe_id}_nn"
        fn = f"ec_coalesce_{safe_id}"
        trg = f"trg_{fn}"
        return SynthPlan(
            supported=True, op_kind=op.kind, table=tbl, column=col,
            phase1_expand_sql=[
                f"ALTER TABLE {qt} ALTER COLUMN {qc} SET DEFAULT {dflt};",
                (
                    f"CREATE OR REPLACE FUNCTION {fn}() RETURNS trigger AS $$\n"
                    f"BEGIN\n"
                    f"  IF NEW.{qc} IS NULL THEN NEW.{qc} := {dflt}; END IF;\n"
                    f"  RETURN NEW;\n"
                    f"END;\n"
                    f"$$ LANGUAGE plpgsql;"
                ),
                f"DROP TRIGGER IF EXISTS {trg} ON {qt};",
                f"CREATE TRIGGER {trg} BEFORE INSERT OR UPDATE ON {qt} FOR EACH ROW EXECUTE FUNCTION {fn}();",
            ],
            phase2_backfill_sql=[
                f"UPDATE {qt} SET {qc} = {dflt} WHERE {qc} IS NULL;",
            ],
            phase2_orm_shim=f"Deploy C_t guaranteeing non-null `{tbl}.{col}`; trigger `{trg}` intercepts explicit NULL writes from C_{{t-1}}.",
            phase3_contract_sql=[
                f"DROP TRIGGER IF EXISTS {trg} ON {qt};",
                f"DROP FUNCTION IF EXISTS {fn}();",
                f"ALTER TABLE {qt} ADD CONSTRAINT {chk} CHECK ({qc} IS NOT NULL) NOT VALID;",
                f"ALTER TABLE {qt} VALIDATE CONSTRAINT {chk};",
                f"ALTER TABLE {qt} ALTER COLUMN {qc} SET NOT NULL;",
                f"ALTER TABLE {qt} DROP CONSTRAINT {chk};",
                f"ALTER TABLE {qt} ALTER COLUMN {qc} DROP DEFAULT;",
            ],
            notes="Coalesce trigger handles explicit NULL writes from C_{t-1}; Phase 3 drops temporary DEFAULT so final schema equals S_t.",
        )

    # 7. ALTER_COLUMN_TYPE
    if op.kind == "ALTER_COLUMN_TYPE" and col:
        shadow = f"{col}__ec_v2"
        qsh = _q(shadow)
        src_t = _infer_pg_type(old_pg_type)
        tgt_t = _infer_pg_type(new_pg_type or op.new_type)
        fn = f"ec_cast_{safe_id}"
        trg = f"trg_{fn}"
        return SynthPlan(
            supported=True, op_kind=op.kind, table=tbl, column=col,
            phase1_expand_sql=[
                f"ALTER TABLE {qt} ADD COLUMN IF NOT EXISTS {qsh} {tgt_t};",
                (
                    f"CREATE OR REPLACE FUNCTION {fn}() RETURNS trigger AS $$\n"
                    f"BEGIN\n"
                    f"  IF TG_OP = 'INSERT' THEN\n"
                    f"    IF NEW.{qsh} IS NULL AND NEW.{qc} IS NOT NULL THEN\n"
                    f"      BEGIN NEW.{qsh} := NEW.{qc}::{tgt_t}; EXCEPTION WHEN OTHERS THEN NEW.{qsh} := NULL; END;\n"
                    f"    END IF;\n"
                    f"    IF NEW.{qc} IS NULL AND NEW.{qsh} IS NOT NULL THEN\n"
                    f"      BEGIN NEW.{qc} := NEW.{qsh}::{src_t}; EXCEPTION WHEN OTHERS THEN NEW.{qc} := NULL; END;\n"
                    f"    END IF;\n"
                    f"  ELSIF TG_OP = 'UPDATE' THEN\n"
                    f"    IF NEW.{qsh} IS DISTINCT FROM OLD.{qsh} THEN\n"
                    f"      BEGIN NEW.{qc} := NEW.{qsh}::{src_t}; EXCEPTION WHEN OTHERS THEN NEW.{qc} := NULL; END;\n"
                    f"    ELSIF NEW.{qc} IS DISTINCT FROM OLD.{qc} THEN\n"
                    f"      BEGIN NEW.{qsh} := NEW.{qc}::{tgt_t}; EXCEPTION WHEN OTHERS THEN NEW.{qsh} := NULL; END;\n"
                    f"    END IF;\n"
                    f"  END IF;\n"
                    f"  RETURN NEW;\n"
                    f"END;\n"
                    f"$$ LANGUAGE plpgsql;"
                ),
                f"DROP TRIGGER IF EXISTS {trg} ON {qt};",
                f"CREATE TRIGGER {trg} BEFORE INSERT OR UPDATE ON {qt} FOR EACH ROW EXECUTE FUNCTION {fn}();",
            ],
            phase2_backfill_sql=[
                f"UPDATE {qt} SET {qsh} = {qc}::{tgt_t} WHERE {qsh} IS NULL AND {qc} IS NOT NULL;",
            ],
            phase2_orm_shim=f"Introduce shadow typed column `{shadow}` ({tgt_t}); deploy C_t dual-reading `{shadow}` with fallback to `{col}`; swap in Phase 3.",
            phase3_contract_sql=[
                f"DROP TRIGGER IF EXISTS {trg} ON {qt};",
                f"DROP FUNCTION IF EXISTS {fn}();",
                f"ALTER TABLE {qt} DROP COLUMN IF EXISTS {qc};",
                f"ALTER TABLE {qt} RENAME COLUMN {qsh} TO {qc};",
            ],
            notes="Shadow column + safe cast trigger avoids ACCESS EXCLUSIVE table rewrite and protects C_{t-1} writes.",
        )

    # 8. RENAME_TYPE_ENUM
    if op.kind == "RENAME_TYPE_ENUM":
        old_e = op.table or op.column or "old_enum"
        new_e = op.new_name or f"{old_e}_v2"
        return SynthPlan(
            supported=True, op_kind=op.kind, table=old_e, column=old_e,
            phase1_expand_sql=[
                f"-- Phase 1 (Expand): Use `ALTER TYPE {_q(old_e)} ADD VALUE IF NOT EXISTS ...` instead of drop/rename enum in-place.",
            ],
            phase2_backfill_sql=[],
            phase2_orm_shim=f"Keep existing enum type `{old_e}` intact while adding new values; migrate deprecated values via batched UPDATE before dropping unused type `{old_e}`.",
            phase3_contract_sql=[
                f"-- Phase 3 (Contract): After all C_{{t-1}} pods drain, swap enum or drop obsolete `{old_e}`.",
            ],
            notes="Additive PostgreSQL ENUM evolution avoids `42704 type does not exist` crashes on C_{t-1} casts.",
            executable_sql=False,
        )

    # 9. ADD_UNIQUE_OR_CHECK
    if op.kind == "ADD_UNIQUE_OR_CHECK":
        chk = f"chk_{safe_id}_ec"
        return SynthPlan(
            supported=True, op_kind=op.kind, table=tbl, column=col,
            phase1_expand_sql=[
                f"-- Phase 1 (Expand): Deploy application-level validation in C_t first; add NOT VALID constraint or CONCURRENT unique index.",
            ],
            phase2_backfill_sql=[],
            phase2_orm_shim=f"Ensure C_t enforces the invariant on writes; clean historical violations before VALIDATE CONSTRAINT.",
            phase3_contract_sql=[
                f"-- Phase 3 (Contract): ALTER TABLE {qt} VALIDATE CONSTRAINT {chk};",
            ],
            notes="Split ADD ... NOT VALID and VALIDATE CONSTRAINT to avoid blocking concurrent writes.",
            executable_sql=False,
        )

    return SynthPlan(
        supported=False, op_kind=op.kind, table=tbl, column=col,
        phase1_expand_sql=[], phase2_backfill_sql=[], phase2_orm_shim="",
        phase3_contract_sql=[], notes="Unsupported operator kind.",
        executable_sql=False,
    )
