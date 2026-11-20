"""Multi-ecosystem forward-only DDL parser with explicit up/down separation.

Supports:
  1. Prisma / raw PostgreSQL SQL via `sqlglot` (dialect="postgres")
  2. Rails ActiveRecord Ruby migrations via `tree_sitter` Ruby AST
  3. Django Python migrations via Python's built-in `ast`
"""
from __future__ import annotations

import ast
import logging
import re
from typing import Iterable

import sqlglot
from sqlglot import exp
from tree_sitter_language_pack import get_parser

logging.getLogger("sqlglot").setLevel(logging.ERROR)

from .ir import DDLOp, norm_ident

_RUBY_PARSER = None


def _get_ruby_parser():
    global _RUBY_PARSER
    if _RUBY_PARSER is None:
        _RUBY_PARSER = get_parser("ruby")
    return _RUBY_PARSER


def _snippet(text: str, max_len: int = 180) -> str:
    s = " ".join(text.strip().split())
    return s[:max_len]


# ---------------------------------------------------------------------------
# 1. PostgreSQL SQL (Prisma migration.sql + Rails execute + Django RunSQL)
# ---------------------------------------------------------------------------

def parse_postgres_sql(
    sql_text: str,
    ecosystem: str = "prisma",
    file_path: str = "",
    phase_section: str = "up",
    inherited_bypass: bool = False,
) -> list[DDLOp]:
    ops: list[DDLOp] = []
    if not sql_text or not sql_text.strip():
        return ops

    has_ignore_file = bool(re.search(r"squawk-ignore-file", sql_text, re.I))
    lines = sql_text.splitlines()

    def _has_line_bypass(stmt_sql: str) -> bool:
        if inherited_bypass or has_ignore_file:
            return True
        # Check if -- squawk-ignore appears near statement
        first_tok = stmt_sql.strip().splitlines()[0][:40] if stmt_sql.strip() else ""
        if not first_tok:
            return False
        for i, ln in enumerate(lines):
            if first_tok in ln:
                window = "\n".join(lines[max(0, i - 3): i + 1])
                if re.search(r"squawk-ignore", window, re.I):
                    return True
                break
        return False

    try:
        stmts = sqlglot.parse(sql_text, read="postgres", error_level=sqlglot.ErrorLevel.IGNORE)
    except Exception:
        stmts = []

    for stmt in stmts:
        if stmt is None:
            continue
        raw = stmt.sql(dialect="postgres")
        bypass = _has_line_bypass(raw)

        # DROP TABLE / DROP TYPE
        if isinstance(stmt, exp.Drop):
            kind_str = (stmt.args.get("kind") or "").upper()
            targets = list(stmt.args.get("tables") or [])
            if not targets and stmt.this is not None:
                targets = [stmt.this]
            for target in targets:
                tname = norm_ident(target.name if hasattr(target, "name") and target.name else str(target))
                if not tname or tname == "none":
                    continue
                if kind_str == "TABLE":
                    ops.append(DDLOp(
                        ecosystem=ecosystem,
                        kind="DROP_TABLE",
                        table=tname,
                        phase_section=phase_section,
                        has_safety_bypass=bypass,
                        file_path=file_path,
                        raw_snippet=_snippet(raw),
                    ))
                elif kind_str == "TYPE":
                    ops.append(DDLOp(
                        ecosystem=ecosystem,
                        kind="RENAME_TYPE_ENUM",
                        table=tname,
                        column=tname,
                        phase_section=phase_section,
                        has_safety_bypass=bypass,
                        file_path=file_path,
                        raw_snippet=_snippet(raw),
                        extra={"subtype": "DROP_TYPE"},
                    ))
            continue

        # ALTER TABLE / ALTER TYPE
        if isinstance(stmt, exp.Alter):
            alter_kind = (stmt.args.get("kind") or "TABLE").upper()
            tname = norm_ident(stmt.this.name if hasattr(stmt.this, "name") else str(stmt.this))

            if alter_kind == "TYPE":
                # ALTER TYPE ... RENAME TO ...
                m_ren = re.search(r'ALTER\s+TYPE\s+(?:"?\w+"?\.)?"?(\w+)"?\s+RENAME\s+TO\s+"?(\w+)"?', raw, re.I)
                if m_ren:
                    ops.append(DDLOp(
                        ecosystem=ecosystem,
                        kind="RENAME_TYPE_ENUM",
                        table=norm_ident(m_ren.group(1)),
                        column=norm_ident(m_ren.group(1)),
                        new_name=norm_ident(m_ren.group(2)),
                        phase_section=phase_section,
                        has_safety_bypass=bypass,
                        file_path=file_path,
                        raw_snippet=_snippet(raw),
                    ))
                continue

            if alter_kind != "TABLE" or not tname:
                continue

            actions = stmt.args.get("actions") or []
            for act in actions:
                act_sql = act.sql(dialect="postgres")

                # DROP COLUMN
                if isinstance(act, exp.Drop) and (act.args.get("kind") or "").upper() in ("COLUMN", ""):
                    targets = list(act.args.get("tables") or [])
                    if not targets and act.this is not None:
                        targets = [act.this]
                    for target in targets:
                        cname = norm_ident(target.name if hasattr(target, "name") and target.name else str(target))
                        if cname and cname != "none":
                            ops.append(DDLOp(
                                ecosystem=ecosystem,
                                kind="DROP_COLUMN",
                                table=tname,
                                column=cname,
                                phase_section=phase_section,
                                has_safety_bypass=bypass,
                                file_path=file_path,
                                raw_snippet=_snippet(f"ALTER TABLE {tname} {act_sql}"),
                            ))

                # RENAME TABLE
                elif isinstance(act, exp.AlterRename):
                    new_t = norm_ident(act.this.name if hasattr(act.this, "name") else str(act.this))
                    if new_t:
                        ops.append(DDLOp(
                            ecosystem=ecosystem,
                            kind="RENAME_TABLE",
                            table=tname,
                            new_name=new_t,
                            phase_section=phase_section,
                            has_safety_bypass=bypass,
                            file_path=file_path,
                            raw_snippet=_snippet(f"ALTER TABLE {tname} {act_sql}"),
                        ))

                # RENAME COLUMN
                elif isinstance(act, exp.RenameColumn):
                    old_c = norm_ident(act.this.name if hasattr(act.this, "name") else str(act.this))
                    to_node = act.args.get("to")
                    new_c = norm_ident(to_node.name if hasattr(to_node, "name") else str(to_node))
                    if old_c and new_c:
                        ops.append(DDLOp(
                            ecosystem=ecosystem,
                            kind="RENAME_COLUMN",
                            table=tname,
                            column=old_c,
                            new_name=new_c,
                            phase_section=phase_section,
                            has_safety_bypass=bypass,
                            file_path=file_path,
                            raw_snippet=_snippet(f"ALTER TABLE {tname} {act_sql}"),
                        ))

                # ALTER COLUMN ... SET NOT NULL / SET DATA TYPE
                elif isinstance(act, exp.AlterColumn):
                    cname = norm_ident(act.this.name if hasattr(act.this, "name") else str(act.this))
                    if not cname:
                        continue
                    # Check SET NOT NULL (allow_null is False when SET NOT NULL, True when DROP NOT NULL)
                    if act.args.get("allow_null") is False or re.search(r"\bSET\s+NOT\s+NULL\b", act_sql, re.I):
                        ops.append(DDLOp(
                            ecosystem=ecosystem,
                            kind="SET_NOT_NULL",
                            table=tname,
                            column=cname,
                            phase_section=phase_section,
                            has_safety_bypass=bypass,
                            file_path=file_path,
                            raw_snippet=_snippet(f"ALTER TABLE {tname} {act_sql}"),
                        ))
                    dtype = act.args.get("dtype")
                    if dtype is not None:
                        ops.append(DDLOp(
                            ecosystem=ecosystem,
                            kind="ALTER_COLUMN_TYPE",
                            table=tname,
                            column=cname,
                            new_type=dtype.sql(dialect="postgres"),
                            phase_section=phase_section,
                            has_safety_bypass=bypass,
                            file_path=file_path,
                            raw_snippet=_snippet(f"ALTER TABLE {tname} {act_sql}"),
                        ))

                # ADD COLUMN ... NOT NULL (without DEFAULT)
                elif isinstance(act, exp.ColumnDef):
                    cname = norm_ident(act.this.name if hasattr(act.this, "name") else str(act.this))
                    constraints = act.args.get("constraints") or []
                    has_not_null = any(
                        isinstance(c.kind, exp.NotNullColumnConstraint) and not c.kind.args.get("allow_null")
                        for c in constraints if isinstance(c, exp.ColumnConstraint)
                    )
                    has_default = any(
                        isinstance(c.kind, (exp.DefaultColumnConstraint, exp.GeneratedAsIdentityColumnConstraint, exp.AutoIncrementColumnConstraint))
                        for c in constraints if isinstance(c, exp.ColumnConstraint)
                    )
                    dtype_sql = act.args.get("kind").sql(dialect="postgres") if act.args.get("kind") else ""
                    if re.search(r"\bSERIAL\b", dtype_sql, re.I):
                        has_default = True
                    if cname and has_not_null and not has_default:
                        ops.append(DDLOp(
                            ecosystem=ecosystem,
                            kind="ADD_NOT_NULL_NO_DEFAULT",
                            table=tname,
                            column=cname,
                            new_type=dtype_sql,
                            phase_section=phase_section,
                            has_safety_bypass=bypass,
                            file_path=file_path,
                            raw_snippet=_snippet(f"ALTER TABLE {tname} ADD COLUMN {act_sql}"),
                        ))

                # ADD CONSTRAINT ... CHECK / UNIQUE (without NOT VALID)
                elif isinstance(act, exp.AddConstraint):
                    if re.search(r"\b(UNIQUE|CHECK)\b", act_sql, re.I) and not re.search(r"\bNOT\s+VALID\b", act_sql, re.I):
                        # Extract column(s) if present in parentheses
                        m_col = re.search(r"\bUNIQUE\s*\(([^)]+)\)", act_sql, re.I)
                        cname = norm_ident(m_col.group(1).split(",")[0]) if m_col else None
                        ops.append(DDLOp(
                            ecosystem=ecosystem,
                            kind="ADD_UNIQUE_OR_CHECK",
                            table=tname,
                            column=cname,
                            phase_section=phase_section,
                            has_safety_bypass=bypass,
                            file_path=file_path,
                            raw_snippet=_snippet(f"ALTER TABLE {tname} {act_sql}"),
                        ))

                # Fallback for raw Command/unparsed alter actions
                else:
                    m_rc = re.search(r'RENAME\s+COLUMN\s+"?(\w+)"?\s+TO\s+"?(\w+)"?', act_sql, re.I)
                    if m_rc:
                        ops.append(DDLOp(
                            ecosystem=ecosystem,
                            kind="RENAME_COLUMN",
                            table=tname,
                            column=norm_ident(m_rc.group(1)),
                            new_name=norm_ident(m_rc.group(2)),
                            phase_section=phase_section,
                            has_safety_bypass=bypass,
                            file_path=file_path,
                            raw_snippet=_snippet(f"ALTER TABLE {tname} {act_sql}"),
                        ))

        # Fallback regex for statements sqlglot parses as exp.Command (e.g. ALTER TYPE ... RENAME TO)
        elif isinstance(stmt, exp.Command):
            m_rt = re.search(r'ALTER\s+TYPE\s+(?:"?\w+"?\.)?"?(\w+)"?\s+RENAME\s+TO\s+"?(\w+)"?', raw, re.I)
            if m_rt:
                ops.append(DDLOp(
                    ecosystem=ecosystem,
                    kind="RENAME_TYPE_ENUM",
                    table=norm_ident(m_rt.group(1)),
                    column=norm_ident(m_rt.group(1)),
                    new_name=norm_ident(m_rt.group(2)),
                    phase_section=phase_section,
                    has_safety_bypass=bypass,
                    file_path=file_path,
                    raw_snippet=_snippet(raw),
                ))

    # Extra regex sweep for ALTER TYPE ... RENAME TO (sometimes skipped when surrounded by PG comments/DO blocks)
    existing_rt = {(o.kind, o.table, o.new_name) for o in ops if o.kind == "RENAME_TYPE_ENUM"}
    for m in re.finditer(r'ALTER\s+TYPE\s+(?:"?\w+"?\.)?"?(\w+)"?\s+RENAME\s+TO\s+"?(\w+)"?', sql_text, re.I):
        t1, t2 = norm_ident(m.group(1)), norm_ident(m.group(2))
        if ("RENAME_TYPE_ENUM", t1, t2) not in existing_rt:
            existing_rt.add(("RENAME_TYPE_ENUM", t1, t2))
            ops.append(DDLOp(
                ecosystem=ecosystem,
                kind="RENAME_TYPE_ENUM",
                table=t1,
                column=t1,
                new_name=t2,
                phase_section=phase_section,
                has_safety_bypass=inherited_bypass or has_ignore_file,
                file_path=file_path,
                raw_snippet=_snippet(m.group(0)),
            ))

    return ops


# ---------------------------------------------------------------------------
# 2. Rails ActiveRecord Ruby Migrations via tree-sitter Ruby AST
# ---------------------------------------------------------------------------

def _ruby_lit(node, src: bytes) -> str | None:
    txt = src[node.start_byte:node.end_byte].decode("utf-8", errors="ignore").strip()
    if node.type in ("simple_symbol", "delimited_symbol", "string", "string_content", "bare_symbol"):
        return norm_ident(txt.lstrip(":").strip('"').strip("'"))
    if node.type == "identifier":
        return norm_ident(txt)
    return None


def _ruby_extract_args(call_node, src: bytes) -> tuple[list[tuple[str, object]], dict[str, object]]:
    pos: list[tuple[str, object]] = []
    kw: dict[str, object] = {}
    arg_list = None
    for ch in call_node.children:
        if ch.type == "argument_list":
            arg_list = ch
            break
    if arg_list is None:
        return pos, kw
    for ch in arg_list.named_children:
        if ch.type == "pair":
            k_node = ch.child_by_field_name("key")
            v_node = ch.child_by_field_name("value")
            if k_node and v_node:
                k = src[k_node.start_byte:k_node.end_byte].decode("utf-8", errors="ignore").strip().rstrip(":").lstrip(":").strip("'\"")
                v_txt = src[v_node.start_byte:v_node.end_byte].decode("utf-8", errors="ignore").strip()
                kw[k] = v_txt
        else:
            pos.append((ch.type, ch))
    return pos, kw


def parse_rails_migration(ruby_text: str, file_path: str = "") -> list[DDLOp]:
    ops: list[DDLOp] = []
    if not ruby_text or not ruby_text.strip():
        return ops

    src = ruby_text.encode("utf-8", errors="ignore")
    tree = _get_ruby_parser().parse(src)

    def walk(node, section: str, in_safety_assured: bool, current_change_table: str | None):
        ntype = node.type

        # Method definition: def up / def change / def down
        if ntype == "method":
            name_node = node.child_by_field_name("name")
            mname = src[name_node.start_byte:name_node.end_byte].decode("utf-8", errors="ignore") if name_node else ""
            new_sec = "down" if mname == "down" else ("up" if mname in ("up", "change") else section)
            for ch in node.children:
                walk(ch, new_sec, in_safety_assured, current_change_table)
            return

        if ntype == "call":
            meth_node = node.child_by_field_name("method")
            mname = src[meth_node.start_byte:meth_node.end_byte].decode("utf-8", errors="ignore") if meth_node else ""
            recv_node = node.child_by_field_name("receiver")
            rname = src[recv_node.start_byte:recv_node.end_byte].decode("utf-8", errors="ignore") if recv_node else ""
            call_txt = src[node.start_byte:node.end_byte].decode("utf-8", errors="ignore")
            line_no = node.start_point[0] + 1

            # safety_assured { ... }
            if mname == "safety_assured":
                for ch in node.children:
                    walk(ch, section, True, current_change_table)
                return

            # dir.down { ... } / dir.up { ... } inside reversible
            if mname == "down" and rname:
                for ch in node.children:
                    walk(ch, "down", in_safety_assured, current_change_table)
                return
            if mname == "up" and rname:
                for ch in node.children:
                    walk(ch, "up", in_safety_assured, current_change_table)
                return

            # change_table :users do |t| ... end
            if mname == "change_table":
                pos, _ = _ruby_extract_args(node, src)
                tbl = _ruby_lit(pos[0][1], src) if pos else None
                for ch in node.children:
                    walk(ch, section, in_safety_assured, tbl or current_change_table)
                return

            pos, kw = _ruby_extract_args(node, src)

            if mname in ("remove_column", "remove_columns"):
                if current_change_table and rname:
                    tbl = current_change_table
                    col_nodes = [p[1] for p in pos]
                elif len(pos) >= 2:
                    tbl = _ruby_lit(pos[0][1], src)
                    col_nodes = [pos[1][1]] if mname == "remove_column" else [p[1] for p in pos[1:]]
                else:
                    tbl, col_nodes = None, []
                for cn in col_nodes:
                    col = _ruby_lit(cn, src)
                    if tbl and col:
                        ops.append(DDLOp(
                            ecosystem="rails", kind="DROP_COLUMN", table=tbl, column=col,
                            phase_section=section, has_safety_bypass=in_safety_assured,
                            file_path=file_path, line=line_no, raw_snippet=_snippet(call_txt),
                        ))

            elif mname in ("remove_reference", "remove_belongs_to"):
                if len(pos) >= 2:
                    tbl = _ruby_lit(pos[0][1], src)
                    ref = _ruby_lit(pos[1][1], src)
                    if tbl and ref:
                        col = ref if ref.endswith("_id") else f"{ref}_id"
                        ops.append(DDLOp(
                            ecosystem="rails", kind="DROP_COLUMN", table=tbl, column=col,
                            phase_section=section, has_safety_bypass=in_safety_assured,
                            file_path=file_path, line=line_no, raw_snippet=_snippet(call_txt),
                        ))

            elif mname == "drop_table" and pos:
                tbl = _ruby_lit(pos[0][1], src)
                if tbl:
                    ops.append(DDLOp(
                        ecosystem="rails", kind="DROP_TABLE", table=tbl,
                        phase_section=section, has_safety_bypass=in_safety_assured,
                        file_path=file_path, line=line_no, raw_snippet=_snippet(call_txt),
                    ))

            elif mname == "rename_column" and len(pos) >= 3:
                tbl = _ruby_lit(pos[0][1], src)
                old_c = _ruby_lit(pos[1][1], src)
                new_c = _ruby_lit(pos[2][1], src)
                if tbl and old_c and new_c:
                    ops.append(DDLOp(
                        ecosystem="rails", kind="RENAME_COLUMN", table=tbl, column=old_c, new_name=new_c,
                        phase_section=section, has_safety_bypass=in_safety_assured,
                        file_path=file_path, line=line_no, raw_snippet=_snippet(call_txt),
                    ))

            elif mname == "rename_table" and len(pos) >= 2:
                old_t = _ruby_lit(pos[0][1], src)
                new_t = _ruby_lit(pos[1][1], src)
                if old_t and new_t:
                    ops.append(DDLOp(
                        ecosystem="rails", kind="RENAME_TABLE", table=old_t, new_name=new_t,
                        phase_section=section, has_safety_bypass=in_safety_assured,
                        file_path=file_path, line=line_no, raw_snippet=_snippet(call_txt),
                    ))

            elif mname == "change_column_null" and len(pos) >= 3:
                tbl = _ruby_lit(pos[0][1], src)
                col = _ruby_lit(pos[1][1], src)
                null_flag = src[pos[2][1].start_byte:pos[2][1].end_byte].decode("utf-8", errors="ignore").strip()
                if tbl and col and null_flag == "false":
                    ops.append(DDLOp(
                        ecosystem="rails", kind="SET_NOT_NULL", table=tbl, column=col,
                        phase_section=section, has_safety_bypass=in_safety_assured,
                        file_path=file_path, line=line_no, raw_snippet=_snippet(call_txt),
                    ))

            elif mname == "change_column" and len(pos) >= 3:
                tbl = _ruby_lit(pos[0][1], src)
                col = _ruby_lit(pos[1][1], src)
                ntype_str = _ruby_lit(pos[2][1], src)
                if tbl and col:
                    ops.append(DDLOp(
                        ecosystem="rails", kind="ALTER_COLUMN_TYPE", table=tbl, column=col, new_type=ntype_str,
                        phase_section=section, has_safety_bypass=in_safety_assured,
                        file_path=file_path, line=line_no, raw_snippet=_snippet(call_txt),
                    ))

            elif mname == "add_column" and len(pos) >= 3:
                tbl = _ruby_lit(pos[0][1], src)
                col = _ruby_lit(pos[1][1], src)
                ntype_str = _ruby_lit(pos[2][1], src)
                if tbl and col and kw.get("null") == "false" and "default" not in kw:
                    ops.append(DDLOp(
                        ecosystem="rails", kind="ADD_NOT_NULL_NO_DEFAULT", table=tbl, column=col, new_type=ntype_str,
                        phase_section=section, has_safety_bypass=in_safety_assured,
                        file_path=file_path, line=line_no, raw_snippet=_snippet(call_txt),
                    ))

            elif mname == "add_check_constraint" and len(pos) >= 2:
                tbl = _ruby_lit(pos[0][1], src)
                if tbl and kw.get("validate") != "false":
                    ops.append(DDLOp(
                        ecosystem="rails", kind="ADD_UNIQUE_OR_CHECK", table=tbl,
                        phase_section=section, has_safety_bypass=in_safety_assured,
                        file_path=file_path, line=line_no, raw_snippet=_snippet(call_txt),
                    ))

            elif mname == "execute" and pos:
                raw_sql = src[pos[0][1].start_byte:pos[0][1].end_byte].decode("utf-8", errors="ignore")
                # Strip Ruby string delimiters or heredoc wrapper
                raw_sql_clean = re.sub(r"^<<[~-]?\w+\s*", "", raw_sql.strip()).strip().strip('"').strip("'")
                ops.extend(parse_postgres_sql(
                    raw_sql_clean, ecosystem="rails", file_path=file_path,
                    phase_section=section, inherited_bypass=in_safety_assured,
                ))

        for ch in node.children:
            walk(ch, section, in_safety_assured, current_change_table)

    walk(tree.root_node, "up", False, None)
    return ops


# ---------------------------------------------------------------------------
# 3. Django Python Migrations via Python `ast`
# ---------------------------------------------------------------------------

def _py_str(node: ast.AST | None) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def _py_bool(node: ast.AST | None) -> bool | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, bool):
        return node.value
    return None


def _django_app_label(file_path: str) -> str | None:
    # e.g. posthog/migrations/0123_foo.py -> posthog
    parts = file_path.replace("\\", "/").split("/")
    for i, p in enumerate(parts):
        if p == "migrations" and i > 0:
            return parts[i - 1].lower()
    return None


def _django_field_info(field_node: ast.AST | None) -> dict:
    info = {"field_type": None, "null": False, "has_default": False, "has_db_default": False}
    if not isinstance(field_node, ast.Call):
        return info
    if isinstance(field_node.func, ast.Attribute):
        info["field_type"] = field_node.func.attr
    elif isinstance(field_node.func, ast.Name):
        info["field_type"] = field_node.func.id
    for kw in field_node.keywords:
        if kw.arg == "null":
            val = _py_bool(kw.value)
            if val is not None:
                info["null"] = val
        elif kw.arg == "default":
            info["has_default"] = True
        elif kw.arg == "db_default":
            info["has_db_default"] = True
    return info


def parse_django_migration(py_text: str, file_path: str = "") -> list[DDLOp]:
    ops: list[DDLOp] = []
    if not py_text or not py_text.strip():
        return ops

    try:
        tree = ast.parse(py_text)
    except SyntaxError:
        return ops

    app_label = _django_app_label(file_path)

    def _call_name(call: ast.Call) -> str:
        if isinstance(call.func, ast.Attribute):
            return call.func.attr
        if isinstance(call.func, ast.Name):
            return call.func.id
        return ""

    def _kw_map(call: ast.Call) -> dict[str, ast.AST]:
        return {kw.arg: kw.value for kw in call.keywords if kw.arg}

    def _tbl(model_name: str | None) -> str | None:
        m = norm_ident(model_name)
        if not m:
            return None
        return f"{app_label}_{m}" if app_label else m

    def visit_op(call: ast.Call, is_state_only: bool = False):
        op_name = _call_name(call)
        kw = _kw_map(call)
        line_no = getattr(call, "lineno", 0)
        snip = _snippet(ast.get_source_segment(py_text, call) or op_name)

        # SeparateDatabaseAndState: database_operations run on DB; state_operations do NOT touch DB!
        if op_name == "SeparateDatabaseAndState":
            db_ops = kw.get("database_operations")
            if isinstance(db_ops, (ast.List, ast.Tuple)):
                n_before = len(ops)
                for el in db_ops.elts:
                    if isinstance(el, ast.Call):
                        visit_op(el, is_state_only=False)
                # v4: an explicit database-only step is positive evidence of a staged (contract) change.
                for o in ops[n_before:]:
                    o.extra["staged"] = True
            return

        if is_state_only:
            return

        # v3: Sentry SafeRemoveField / SafeDeleteModel. Only `deletion_action=...DELETE` emits DDL;
        # `MOVE_TO_PENDING` is a state-only detach step (the Expand-side of Expand-Contract).
        if op_name in ("SafeRemoveField", "SafeDeleteModel"):
            da = kw.get("deletion_action")
            da_src = (ast.get_source_segment(py_text, da) or "") if da is not None else ""
            if "DELETE" not in da_src.upper() or "PENDING" in da_src.upper():
                return
            op_name = "RemoveField" if op_name == "SafeRemoveField" else "DeleteModel"
            n_safe_before = len(ops)
            visit_op_plain(call, op_name, kw, line_no, snip)
            for o in ops[n_safe_before:]:
                o.extra["staged"] = True
            return

        visit_op_plain(call, op_name, kw, line_no, snip)

    def visit_op_plain(call: ast.Call, op_name: str, kw: dict, line_no: int, snip: str):

        if op_name == "RemoveField":
            model = _py_str(kw.get("model_name")) or (_py_str(call.args[0]) if len(call.args) > 0 else None)
            field = _py_str(kw.get("name")) or (_py_str(call.args[1]) if len(call.args) > 1 else None)
            if model and field:
                ops.append(DDLOp(
                    ecosystem="django", kind="DROP_COLUMN",
                    table=_tbl(model), column=norm_ident(field),
                    phase_section="up", file_path=file_path, line=line_no,
                    raw_snippet=snip, extra={"model_name": norm_ident(model), "app_label": app_label},
                ))

        elif op_name == "DeleteModel":
            model = _py_str(kw.get("name")) or (_py_str(call.args[0]) if len(call.args) > 0 else None)
            if model:
                ops.append(DDLOp(
                    ecosystem="django", kind="DROP_TABLE",
                    table=_tbl(model),
                    phase_section="up", file_path=file_path, line=line_no,
                    raw_snippet=snip, extra={"model_name": norm_ident(model), "app_label": app_label},
                ))

        elif op_name == "RenameField":
            model = _py_str(kw.get("model_name")) or (_py_str(call.args[0]) if len(call.args) > 0 else None)
            old_f = _py_str(kw.get("old_name")) or (_py_str(call.args[1]) if len(call.args) > 1 else None)
            new_f = _py_str(kw.get("new_name")) or (_py_str(call.args[2]) if len(call.args) > 2 else None)
            if model and old_f and new_f:
                ops.append(DDLOp(
                    ecosystem="django", kind="RENAME_COLUMN",
                    table=_tbl(model), column=norm_ident(old_f), new_name=norm_ident(new_f),
                    phase_section="up", file_path=file_path, line=line_no,
                    raw_snippet=snip, extra={"model_name": norm_ident(model), "app_label": app_label},
                ))

        elif op_name == "RenameModel":
            old_m = _py_str(kw.get("old_name")) or (_py_str(call.args[0]) if len(call.args) > 0 else None)
            new_m = _py_str(kw.get("new_name")) or (_py_str(call.args[1]) if len(call.args) > 1 else None)
            if old_m and new_m:
                ops.append(DDLOp(
                    ecosystem="django", kind="RENAME_TABLE",
                    table=_tbl(old_m), new_name=_tbl(new_m),
                    phase_section="up", file_path=file_path, line=line_no,
                    raw_snippet=snip, extra={"model_name": norm_ident(old_m), "new_model_name": norm_ident(new_m), "app_label": app_label},
                ))

        elif op_name == "AddField":
            model = _py_str(kw.get("model_name")) or (_py_str(call.args[0]) if len(call.args) > 0 else None)
            field = _py_str(kw.get("name")) or (_py_str(call.args[1]) if len(call.args) > 1 else None)
            fnode = kw.get("field") or (call.args[2] if len(call.args) > 2 else None)
            finfo = _django_field_info(fnode)
            # In Django, NOT NULL AddField WITHOUT `db_default` drops the DB default immediately after backfill!
            # Exceptions: ManyToManyField (creates join table, not column), AutoField/BigAutoField.
            ftype = finfo["field_type"] or ""
            is_m2m = "ManyToMany" in ftype or "AutoField" in ftype
            if model and field and (not finfo["null"]) and (not finfo["has_db_default"]) and (not is_m2m):
                ops.append(DDLOp(
                    ecosystem="django", kind="ADD_NOT_NULL_NO_DEFAULT",
                    table=_tbl(model), column=norm_ident(field), new_type=ftype,
                    phase_section="up", file_path=file_path, line=line_no,
                    raw_snippet=snip,
                    extra={
                        "model_name": norm_ident(model), "app_label": app_label,
                        "django_python_default_dropped_in_db": finfo["has_default"],
                    },
                ))

        elif op_name == "AlterField":
            model = _py_str(kw.get("model_name")) or (_py_str(call.args[0]) if len(call.args) > 0 else None)
            field = _py_str(kw.get("name")) or (_py_str(call.args[1]) if len(call.args) > 1 else None)
            fnode = kw.get("field") or (call.args[2] if len(call.args) > 2 else None)
            finfo = _django_field_info(fnode)
            # Note: AlterField is ambiguous without prior field state (could just change help_text/choices).
            # We emit it with extra metadata so the verifier checks whether nullability or type ACTUALLY tightened!
            if model and field:
                ops.append(DDLOp(
                    ecosystem="django", kind="ALTER_FIELD_CANDIDATE",
                    table=_tbl(model), column=norm_ident(field), new_type=finfo["field_type"],
                    phase_section="up", file_path=file_path, line=line_no,
                    raw_snippet=snip,
                    extra={"model_name": norm_ident(model), "app_label": app_label, "field_info": finfo},
                ))

        elif op_name == "RunSQL":
            sql_node = kw.get("sql") or (call.args[0] if len(call.args) > 0 else None)
            rev_node = kw.get("reverse_sql") or (call.args[1] if len(call.args) > 1 else None)
            for s in _extract_py_strings(sql_node):
                ops.extend(parse_postgres_sql(s, ecosystem="django", file_path=file_path, phase_section="up"))
            for s in _extract_py_strings(rev_node):
                ops.extend(parse_postgres_sql(s, ecosystem="django", file_path=file_path, phase_section="down"))

    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef):
            for item in node.body:
                if isinstance(item, ast.Assign):
                    if any(isinstance(t, ast.Name) and t.id == "operations" for t in item.targets):
                        if isinstance(item.value, (ast.List, ast.Tuple)):
                            for el in item.value.elts:
                                if isinstance(el, ast.Call):
                                    visit_op(el)
    return ops


def _extract_py_strings(node: ast.AST | None) -> list[str]:
    if node is None:
        return []
    s = _py_str(node)
    if s:
        return [s]
    out: list[str] = []
    if isinstance(node, (ast.List, ast.Tuple)):
        for el in node.elts:
            out.extend(_extract_py_strings(el))
    return out


def is_post_deploy_migration(ecosystem: str, content: str, file_path: str = "") -> bool:
    """v3: migrations that run AFTER the new release C_t is fully rolled out.

    - Rails: files under `db/post_migrate/` (GitLab, Mastodon convention; skipped during
      pre-deploy via SKIP_POST_DEPLOYMENT_MIGRATIONS and run after rollout).
    - Django: `is_post_deployment = True` on the Migration class (Sentry convention).
    """
    fp = (file_path or "").replace("\\", "/").lower()
    if ecosystem == "rails":
        return "/post_migrate/" in f"/{fp}"
    if ecosystem == "django":
        return bool(re.search(r"^\s*is_post_deployment\s*=\s*True\b", content or "", re.M))
    return False


def is_squashed_migration(ecosystem: str, content: str) -> bool:
    """v4: Django squashed migration (class attribute `replaces = [...]`)."""
    if ecosystem != "django":
        return False
    return bool(re.search(r"^\s*replaces\s*(?::[^=]+)?=\s*[\[\(]", content or "", re.M))


def parse_migration(ecosystem: str, content: str, file_path: str = "") -> list[DDLOp]:
    if ecosystem == "prisma":
        ops = parse_postgres_sql(content, ecosystem="prisma", file_path=file_path, phase_section="up")
    elif ecosystem == "rails":
        ops = parse_rails_migration(content, file_path=file_path)
    elif ecosystem == "django":
        ops = parse_django_migration(content, file_path=file_path)
    else:
        return []
    if is_post_deploy_migration(ecosystem, content, file_path):
        for o in ops:
            o.extra["post_deploy"] = True
    # v4: Django squashed migrations (`replaces = [...]`) are never executed on a database that has
    # already applied the replaced migrations, so their DDL does not run during a rollout.
    if is_squashed_migration(ecosystem, content):
        for o in ops:
            o.extra["squashed"] = True
    # v3: keep the original spelling of quoted SQL identifiers (Prisma emits "Chat"."threadId").
    # Identifiers are lower-cased for matching, but generated SQL must use the exact quoted name.
    for o in ops:
        quoted = {m.lower(): m for m in re.findall(r'"([^"]+)"', (o.raw_snippet or "") + "\n" + (content if ecosystem == "prisma" else ""))}
        for attr, key in (("table", "table_sql"), ("column", "column_sql"), ("new_name", "new_name_sql")):
            val = getattr(o, attr, None)
            if val and val in quoted and quoted[val] != val:
                o.extra[key] = quoted[val]
    return ops
