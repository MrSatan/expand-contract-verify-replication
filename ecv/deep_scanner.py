"""ExpandContractVerify Deep-Mode AST Call-Site & Raw-SQL Scanner (`ECV-Deep`).

Uses `tree-sitter` (TypeScript, Ruby) and Python `ast` to detect explicit call-site
references (property accesses, ORM filter keyword arguments, SQL string literals) to
a target (table, column) in C_{t-1} application code that were missed by ORM-schema-only
analysis. Designed for fast per-PR CI execution (<2s per PR).
"""
from __future__ import annotations

import ast
import re
from tree_sitter_language_pack import get_parser

_TS_PARSER = None
_RB_PARSER = None

# Very common generic names that require tighter co-occurrence with table/ORM/SQL context
GENERIC_COL_NAMES = {
    "id", "name", "type", "status", "data", "value", "key", "code", "state",
    "start", "end", "active", "enabled", "title", "description", "url", "slug",
    "created_at", "updated_at", "deleted_at", "version", "user_id", "email", "index",
}

RX_SQL_KW = re.compile(r"\b(SELECT|INSERT\s+INTO|UPDATE|DELETE\s+FROM|WHERE|ORDER\s+BY|GROUP\s+BY|RETURNING)\b", re.I)


def _get_ts():
    global _TS_PARSER
    if _TS_PARSER is None:
        _TS_PARSER = get_parser("typescript")
    return _TS_PARSER


def _get_rb():
    global _RB_PARSER
    if _RB_PARSER is None:
        _RB_PARSER = get_parser("ruby")
    return _RB_PARSER


def _camel_variants(ident: str) -> set[str]:
    low = ident.lower()
    out = {low}
    if "_" in low:
        parts = low.split("_")
        out.add(parts[0] + "".join(p.capitalize() for p in parts[1:]))
    return out


def scan_file_ast_for_symbol(
    ecosystem: str,
    file_path: str,
    content: str,
    table: str | None,
    column: str | None,
) -> dict:
    """Return AST-confirmed reference signals `{hit: bool, kind: str | None}` for one C_{t-1} source file."""
    if not content or not (column or table):
        return {"hit": False, "kind": None}

    tbl = (table or "").lower()
    col = (column or "").lower()
    col_vars = _camel_variants(col) if col else set()
    tbl_short = tbl.split("_")[-1].rstrip("s") if tbl else ""
    is_generic = col in GENERIC_COL_NAMES

    # Fast pre-filter before full AST walk
    low_content = content.lower()
    if col and not any(v.lower() in low_content for v in col_vars):
        return {"hit": False, "kind": None}
    if is_generic and tbl_short and len(tbl_short) >= 3 and tbl_short not in low_content:
        # Require table/model co-occurrence in the same file for generic column names to prevent FPs!
        return {"hit": False, "kind": None}

    # 1. Python AST (Django)
    if ecosystem == "django" and file_path.endswith(".py"):
        try:
            tree = ast.parse(content)
        except SyntaxError:
            return {"hit": False, "kind": None}
        for node in ast.walk(tree):
            # Raw SQL string literal
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                s = node.value
                if RX_SQL_KW.search(s) and col and re.search(rf"\b{re.escape(col)}\b", s, re.I):
                    return {"hit": True, "kind": "ast_raw_sql"}
            # Attribute access obj.col
            elif isinstance(node, ast.Attribute) and col and node.attr.lower() in col_vars:
                return {"hit": True, "kind": "ast_attr_access"}
            # ORM keyword arg e.g. filter(col=...) / filter(col__isnull=...) / values('col')
            elif isinstance(node, ast.keyword) and node.arg and col:
                arg_l = node.arg.lower()
                if arg_l in col_vars or arg_l.startswith(col + "__"):
                    return {"hit": True, "kind": "ast_orm_kwarg"}
            elif isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr in ("values", "values_list", "only", "defer", "order_by"):
                for a in node.args:
                    if isinstance(a, ast.Constant) and isinstance(a.value, str) and a.value.lstrip("-").lower() in col_vars:
                        return {"hit": True, "kind": "ast_queryset_arg"}
        return {"hit": False, "kind": None}

    # 2. TypeScript / JavaScript tree-sitter AST (Prisma)
    if ecosystem == "prisma" and file_path.endswith((".ts", ".tsx", ".js", ".jsx")):
        src = content.encode("utf-8", errors="ignore")
        tree = _get_ts().parse(src)
        stack = [tree.root_node]
        while stack:
            n = stack.pop()
            nt = n.type
            if nt in ("string", "template_string"):
                txt = src[n.start_byte:n.end_byte].decode("utf-8", errors="ignore")
                if RX_SQL_KW.search(txt) and col and re.search(rf"\b{re.escape(col)}\b", txt, re.I):
                    return {"hit": True, "kind": "ast_raw_sql"}
            elif nt in ("property_identifier", "shorthand_property_identifier", "shorthand_property_identifier_pattern"):
                txt = src[n.start_byte:n.end_byte].decode("utf-8", errors="ignore").lower()
                if col and txt in {v.lower() for v in col_vars}:
                    return {"hit": True, "kind": "ast_ts_property"}
            stack.extend(n.children)
        return {"hit": False, "kind": None}

    # 3. Ruby tree-sitter AST (Rails)
    if ecosystem == "rails" and file_path.endswith(".rb"):
        src = content.encode("utf-8", errors="ignore")
        tree = _get_rb().parse(src)
        stack = [tree.root_node]
        while stack:
            n = stack.pop()
            nt = n.type
            if nt in ("string", "heredoc_body"):
                txt = src[n.start_byte:n.end_byte].decode("utf-8", errors="ignore")
                if RX_SQL_KW.search(txt) and col and re.search(rf"\b{re.escape(col)}\b", txt, re.I):
                    return {"hit": True, "kind": "ast_raw_sql"}
            elif nt in ("simple_symbol", "hash_key_symbol", "identifier"):
                txt = src[n.start_byte:n.end_byte].decode("utf-8", errors="ignore").lstrip(":").lower()
                if col and txt == col:
                    # Ignore `ignored_columns` line itself!
                    line_start = src.rfind(b"\n", 0, n.start_byte) + 1
                    line_end = src.find(b"\n", n.end_byte)
                    line_txt = src[line_start: line_end if line_end != -1 else len(src)].decode("utf-8", errors="ignore")
                    if "ignored_columns" not in line_txt:
                        return {"hit": True, "kind": "ast_ruby_symbol"}
            stack.extend(n.children)
        return {"hit": False, "kind": None}

    return {"hit": False, "kind": None}
