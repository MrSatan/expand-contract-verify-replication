"""v3: Give the strong_migrations runner the real C_{t-1} column metadata of each target table.

The v2 runner used a stub adapter whose `columns(table)` returned []. strong_migrations needs the
existing column (sql_type, limit, ...) to recognise safe type changes (e.g. varchar -> text), so the
stub produced artificial false positives on ALTER_COLUMN_TYPE. This script reads the target table
from C_{t-1} `db/schema.rb` (from the v2 snapshot) or, if absent, from `db/structure.sql` at the
parent commit, and writes `research/v3/rails_60_in_v3.json` with a `c_old_columns` list per case.
"""
from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

TASK = Path(__file__).resolve().parent.parent.parent
V2 = TASK / "research" / "v2"
V3 = TASK / "research" / "v3"
REPOS = TASK / "tmp" / "repos"

RAILS_SQL_TYPE = {
    "string": "character varying", "text": "text", "integer": "integer", "bigint": "bigint",
    "boolean": "boolean", "datetime": "timestamp(6) without time zone", "timestamp": "timestamp without time zone",
    "date": "date", "decimal": "numeric", "float": "double precision", "json": "json", "jsonb": "jsonb",
    "uuid": "uuid", "binary": "bytea", "inet": "inet", "citext": "citext", "time": "time without time zone",
}


def slug(repo: str) -> str:
    return repo.replace("/", "__")


def cols_from_schema_rb(schema: str, table: str) -> list[dict]:
    m = re.search(rf'create_table\s+["\':]{re.escape(table)}["\']?[^\n]*do\s*\|(\w+)\|(.*?)\n\s*end\b', schema, re.S)
    if not m:
        return []
    var, body = re.escape(m.group(1)), m.group(2)
    out = []
    for cm in re.finditer(rf'{var}\.(\w+)\s+["\':]([A-Za-z0-9_]+)["\']?(.*)', body):
        ctype, name, rest = cm.groups()
        if ctype in ("index", "check_constraint", "unique_constraint"):
            continue
        lim = re.search(r"limit:\s*(\d+)", rest)
        sql = RAILS_SQL_TYPE.get(ctype, ctype)
        if ctype == "string" and lim:
            sql = f"character varying({lim.group(1)})"
        out.append({
            "name": name, "type": ctype, "sql_type": sql,
            "limit": int(lim.group(1)) if lim else None,
            "null": "null: false" not in rest, "array": "array: true" in rest,
        })
    return out


def cols_from_structure_sql(sql: str, table: str) -> list[dict]:
    m = re.search(rf'CREATE\s+TABLE\s+(?:"?\w+"?\.)?"?{re.escape(table)}"?\s*\((.*?)\n\);', sql, re.S | re.I)
    if not m:
        return []
    out = []
    for ln in m.group(1).splitlines():
        s = ln.strip().rstrip(",")
        if not s or re.match(r"^(CONSTRAINT|PRIMARY|UNIQUE|FOREIGN|CHECK|EXCLUDE)\b", s, re.I):
            continue
        mc = re.match(r'^"?([A-Za-z_][A-Za-z0-9_]*)"?\s+(.+?)(\s+(NOT\s+NULL|DEFAULT|NULL)\b.*)?$', s, re.I)
        if not mc:
            continue
        sql_type = mc.group(2).strip()
        base = sql_type.lower()
        rtype = ("string" if base.startswith("character varying") else "text" if base == "text"
                 else "bigint" if base == "bigint" else "integer" if base == "integer"
                 else "boolean" if base == "boolean" else base.split("(")[0])
        lim = re.search(r"\((\d+)\)", sql_type)
        out.append({
            "name": mc.group(1), "type": rtype, "sql_type": sql_type,
            "limit": int(lim.group(1)) if lim and rtype == "string" else None,
            "null": "NOT NULL" not in s.upper(), "array": sql_type.endswith("[]"),
        })
    return out


def main() -> None:
    cases = json.loads((V2 / "rails_60_in.json").read_text(encoding="utf-8"))
    raw = {c["id"]: c for c in json.loads((V2 / "benchmark_v2_raw.json").read_text(encoding="utf-8"))}
    found = 0
    for c in cases:
        r = raw[c["id"]]
        table = (r["op"].get("table") or "").strip()
        cols: list[dict] = []
        snip = r.get("rails_schema_old_snippet") or {}
        for _, txt in (snip.items() if isinstance(snip, dict) else []):
            cols = cols_from_schema_rb(txt, table)
            if cols:
                break
        if not cols and table:
            for path in ("db/structure.sql", "db/schema.rb"):
                p = subprocess.run(["git", "-C", str(REPOS / slug(r["repo"])), "show", f"{r['parent']}:{path}"],
                                   capture_output=True, timeout=120)
                if p.returncode == 0:
                    txt = p.stdout.decode("utf-8", errors="ignore")
                    cols = cols_from_structure_sql(txt, table) if path.endswith(".sql") else cols_from_schema_rb(txt, table)
                    if cols:
                        break
        c["c_old_table"] = table
        c["c_old_columns"] = cols
        found += bool(cols)
    (V3 / "rails_60_in_v3.json").write_text(json.dumps(cases, indent=2), encoding="utf-8")
    print(f"column metadata found for {found}/{len(cases)} Rails cases")


if __name__ == "__main__":
    main()
