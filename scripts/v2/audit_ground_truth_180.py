"""V2 Phase 2C: Independent, Non-Circular Ground-Truth Forensic Extractor (Blind to ECV!).

CRITICAL METHODOLOGICAL GUARANTEE:
  - NEVER imports `ecv` and NEVER reads `c["ecv_classification"]` or `c["evidence"]`!
  - Uses ONLY primary Git artifacts + official `@prisma/internals` DMMF oracle:
      1. Verbatim newly-added migration file (checks same-commit table/temp-column creation,
         safe type widening like int->bigint / varchar->text, raw DDL semantics).
      2. Official Prisma DMMF at C_{t-1} (for Prisma) / raw `db/schema.rb` & `structure.sql` &
         `app/models/` at C_{t-1} (for Rails) / raw `models*.py` AST at C_{t-1} (for Django).
      3. Verbatim commit diff hunks in non-migration files (checks co-removal/co-rename in commit t).
      4. Concrete C_{t-1} non-migration application source references (with co-occurrence check
         for generic column names like `name`, `id`, `status`, `type`, `value`).
  - Outputs a compact blind inspection report for all 180 cases so every single case can be
    forensically verified and archived with 40-char commit SHAs in `benchmark_audit_180.md`.
"""
from __future__ import annotations

import ast
import json
import re
from collections import Counter
from pathlib import Path

TASK_DIR = Path(__file__).resolve().parent.parent.parent
V2_DIR = TASK_DIR / "research" / "v2"

GENERIC_COLS = {
    "id", "name", "type", "status", "data", "value", "key", "code", "state",
    "start", "end", "active", "enabled", "title", "description", "url", "slug",
    "created_at", "updated_at", "deleted_at", "version", "user_id", "email", "index", "source", "users",
}


def is_safe_value_widening(old_t: str | None, new_t: str | None, mig_text: str = "") -> bool:
    """Detect PostgreSQL value-compatible type widenings (lock hazard only, NOT C_{t-1} value crash)."""
    if old_t and new_t:
        o, n = old_t.strip().lower(), new_t.strip().lower()
        if o == n:
            return True
        if ("varchar" in o or "char" in o or "string" in o) and ("text" in n):
            return True
        mo = re.search(r"(?:varchar|character varying)\s*\(\s*(\d+)\s*\)", o)
        mn = re.search(r"(?:varchar|character varying)\s*\(\s*(\d+)\s*\)", n)
        if mo and mn and int(mn.group(1)) >= int(mo.group(1)):
            return True
        ranks = {"smallint": 1, "int2": 1, "smallintegerfield": 1, "int": 2, "integer": 2, "int4": 2, "integerfield": 2, "bigint": 3, "int8": 3, "bigintegerfield": 3}
        if o in ranks and n in ranks and ranks[n] >= ranks[o]:
            return True
    # Also detect Rails/SQL explicit bigint widening from integer PK/FK
    if re.search(r"(change_column\s+[^\n]*:bigint|TYPE\s+BIGINT)", mig_text, re.I) and (
        (old_t and "int" in old_t.lower() and "big" not in old_t.lower()) or re.search(r"\b(id|_id)\b", mig_text)
    ):
        if old_t and "int" in old_t.lower():
            return True
    return False


GENERIC_TBLS = {
    "store", "profile", "old", "new", "data", "item", "items", "log", "logs",
    "event", "events", "user", "users", "state", "status", "type", "config",
    "setting", "settings", "host", "hosts", "session", "sessions", "version", "versions",
}


def strip_rails_down_section(ruby_text: str) -> str:
    """Remove `def down ... end` and `dir.down do ... end` blocks so rollback SQL is never mistaken for forward DDL."""
    # Truncate at `def down` / `def self.down` (standard Rails migration layout places `down` after `up`)
    m = re.search(r"^\s*def\s+(?:self\.)?down\b", ruby_text, re.M)
    out = ruby_text[: m.start()] if m else ruby_text
    out = re.sub(r"dir\.down\s+do\b.*?^\s*end\b", "", out, flags=re.M | re.S)
    return out


def check_transient_same_migration(eco: str, mig_text: str, tbl: str, col: str | None) -> str | None:
    """Check if the target table or column was CREATED inside the FORWARD section of the same migration."""
    if not mig_text:
        return None
    fwd_text = strip_rails_down_section(mig_text) if eco == "rails" else mig_text
    low = fwd_text.lower()
    if tbl:
        if eco == "prisma" and re.search(rf'create\s+table\s+(?:if\s+not\s+exists\s+)?(?:"?\w+"?\.)?"?{re.escape(tbl)}"?\b', low):
            return "table_created_same_migration"
        if eco == "rails" and re.search(rf'create_table\s+[:\'"]{re.escape(tbl)}[\'"]?', low):
            return "table_created_same_migration"
        if eco == "django":
            mshort = tbl.split("_")[-1] if "_" in tbl else tbl
            if re.search(rf'createmodel\s*\(\s*name\s*=\s*[\'"]{re.escape(mshort)}[\'"]', low):
                return "table_created_same_migration"
    if col:
        if eco == "prisma" and re.search(rf'add\s+column\s+(?:if\s+not\s+exists\s+)?"?{re.escape(col)}"?\b', low):
            return "col_added_same_migration"
        if eco == "rails" and re.search(rf'add_column\s+[^\n]*[:\'"]{re.escape(col)}[\'"]?', low):
            return "col_added_same_migration"
        if eco == "django" and re.search(rf'addfield\s*\([^)]*name\s*=\s*[\'"]{re.escape(col)}[\'"]', low, re.S):
            return "col_added_same_migration"
    return None


def inspect_rails_c_old(c: dict) -> dict:
    tbl = (c["op"].get("table") or "").lower()
    col = (c["op"].get("column") or "").lower()
    snips = c.get("rails_schema_old_snippet") or {}
    models = c.get("rails_models_old") or {}
    tbl_in_schema = bool(snips)
    col_in_schema = False
    col_nullable_in_schema = None
    col_type_in_schema = None

    for fp, txt in snips.items():
        if col:
            if fp.endswith("schema.rb"):
                m = re.search(rf'\.(\w+)\s+["\':]{re.escape(col)}["\']?(.*)', txt, re.I)
                if not m and col.endswith("_id"):
                    base = col[:-3]
                    m = re.search(rf'\.(references|belongs_to)\s+["\':]{re.escape(base)}["\']?(.*)', txt, re.I)
                if m:
                    col_in_schema = True
                    col_type_in_schema = m.group(1).lower()
                    rest = m.group(2)
                    col_nullable_in_schema = ("null: false" not in rest and ":null => false" not in rest)
            else:
                m = re.search(rf'^\s*"?{re.escape(col)}"?\s+([a-z0-9_ ()]+)(.*)', txt, re.M | re.I)
                if m:
                    col_in_schema = True
                    col_type_in_schema = m.group(1).strip().split()[0].lower()
                    col_nullable_in_schema = ("NOT NULL" not in m.group(0).upper())

    # Check ignored_columns and presence validation across C_{t-1} model files (require exact singular/plural match!)
    ignored_in_model = False
    presence_guard = False
    model_file_matched = None
    tbl_singular = tbl[:-3] + "y" if tbl.endswith("ies") else (tbl[:-1] if tbl.endswith("s") else tbl)
    for fp, txt in models.items():
        stem = Path(fp).stem.lower()
        explicit_tn = re.search(rf'self\.table_name\s*=\s*["\':]{re.escape(tbl)}["\']?', txt, re.I)
        matches_tbl = bool(explicit_tn) or (stem == tbl_singular) or (stem == tbl)
        if matches_tbl:
            model_file_matched = fp
        if col and matches_tbl and re.search(rf'ignored_columns[^\n]*\b{re.escape(col)}\b', txt, re.I | re.S):
            ignored_in_model = True
        if col and matches_tbl and re.search(rf'validates\s+[:\'"]{re.escape(col)}[\'"]?[^\n]*presence:\s*true', txt, re.I):
            presence_guard = True

    return {
        "has_schema_file": bool(c.get("rails_has_schema_file")),
        "tbl_in_schema": tbl_in_schema,
        "col_in_schema": col_in_schema,
        "col_nullable": col_nullable_in_schema,
        "col_type": col_type_in_schema,
        "ignored_in_model": ignored_in_model,
        "presence_guard": presence_guard,
        "model_file": model_file_matched,
    }


def inspect_django_c_old(c: dict) -> dict:
    tbl = (c["op"].get("table") or "").lower()
    col = (c["op"].get("column") or "").lower()
    mhint = ((c["op"].get("extra") or {}).get("model_name") or "").lower()
    tbl_short = mhint or (tbl.split("_")[-1] if "_" in tbl else tbl)
    models = c.get("django_models_old") or {}

    model_found = None
    col_found = False
    col_nullable = None
    col_type = None

    for fp, txt in models.items():
        try:
            tree = ast.parse(txt)
        except SyntaxError:
            continue
        for node in tree.body:
            if not isinstance(node, ast.ClassDef):
                continue
            cname = node.name.lower()
            meta_tbl = None
            for it in node.body:
                if isinstance(it, ast.ClassDef) and it.name == "Meta":
                    for mi in it.body:
                        if isinstance(mi, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "db_table" for t in mi.targets):
                            if isinstance(mi.value, ast.Constant) and isinstance(mi.value.value, str):
                                meta_tbl = mi.value.value.lower()
            if cname == tbl_short or (meta_tbl and meta_tbl == tbl) or cname == tbl.replace("_", ""):
                model_found = f"{fp}:{node.name}"
                if col:
                    for it in node.body:
                        fn = None
                        val = None
                        if isinstance(it, ast.Assign) and len(it.targets) == 1 and isinstance(it.targets[0], ast.Name):
                            fn, val = it.targets[0].id.lower(), it.value
                        elif isinstance(it, ast.AnnAssign) and isinstance(it.target, ast.Name):
                            fn, val = it.target.id.lower(), it.value
                        if fn and (fn == col or (col.endswith("_id") and fn == col[:-3])):
                            col_found = True
                            if isinstance(val, ast.Call):
                                ftype = val.func.attr if isinstance(val.func, ast.Attribute) else (val.func.id if isinstance(val.func, ast.Name) else "")
                                col_type = ftype
                                kw = {k.arg: k.value for k in val.keywords if k.arg}
                                if "null" in kw and isinstance(kw["null"], ast.Constant):
                                    col_nullable = bool(kw["null"].value)
                                else:
                                    col_nullable = False
                break
    return {
        "models_files_count": len(models),
        "model_found": model_found,
        "col_found": col_found,
        "col_nullable": col_nullable,
        "col_type": col_type,
    }


def has_contextual_app_ref(c: dict) -> tuple[bool, str | None]:
    """Check whether C_{t-1} app source files genuinely reference the target symbol (guarding generic names)."""
    tbl = (c["op"].get("table") or "").lower()
    col = (c["op"].get("column") or "").lower()
    tbl_stem = tbl.rstrip("s").split("_")[-1] if tbl else ""
    hits = c.get("c_old_code_hits_col") or []
    if not hits:
        return False, None
    is_generic = col in GENERIC_COLS
    for h in hits:
        fp = h["file"].lower()
        txt = h["text"].lower()
        if "ignored_columns" in txt:
            continue
        if not is_generic:
            return True, f"{h['file']}:{h['line']}"
        # For generic names like `name`, `id`, `status`, require table/model context in the file path or line!
        if tbl_stem and len(tbl_stem) >= 3 and (tbl_stem in fp or tbl_stem in txt):
            return True, f"{h['file']}:{h['line']}"
    return False, None


def main():
    cases = json.loads((V2_DIR / "benchmark_v2_raw.json").read_text(encoding="utf-8"))
    bo = json.loads((V2_DIR / "baselines_and_oracles_v2.json").read_text(encoding="utf-8"))
    dmmf_by_id = bo["prisma_dmmf_oracle"]

    adjudicated = []
    needs_manual_eyes = []

    for c in cases:
        cid = c["id"]
        eco = c["ecosystem"]
        op = c["op"]
        kind = op["kind"]
        tbl = (op.get("table") or "").lower()
        col = (op.get("column") or "").lower()
        mig_txt = c.get("verbatim_migration_content") or ""
        transient = check_transient_same_migration(eco, mig_txt, tbl, col)
        diff_hits = c.get("diff_hits") or []
        removed_in_diff = any(len(d.get("removed") or []) > 0 for d in diff_hits)
        has_app_ref, app_ref_loc = has_contextual_app_ref(c)
        tbl_app_hits_raw = c.get("c_old_code_hits_tbl") or []
        tbl_short_name = tbl.split("_")[-1] if (eco == "django" and "_" in tbl) else tbl
        # Filter generic table-word false hits (e.g. `store`, `profile`, `old`) unless exact table/class context matches
        tbl_app_hits = [
            h for h in tbl_app_hits_raw
            if (tbl_short_name not in GENERIC_TBLS) or (tbl in h["text"].lower()) or (f"class {tbl_short_name}" in h["text"].lower()) or (f"{tbl_short_name}.objects" in h["text"].lower())
        ]

        # Ecosystem-specific independent C_{t-1} ORM inspection (ZERO ECV fields!)
        if eco == "prisma":
            dm = dmmf_by_id[cid]
            orm_info = {
                "dmmf_ok": dm["dmmf_parsed_ok"],
                "model_found": dm["dmmf_model_found"],
                "col_found": dm["dmmf_col_found"],
                "col_req": dm["dmmf_col_is_required"],
                "col_type": dm["dmmf_col_type"],
                "enum_found": dm["dmmf_enum_found"],
                "pg_crash": dm["pg_c_old_at_s_new_crash"],
                "pg_sqlstate": dm["pg_sqlstate"],
            }
            old_t = dm["dmmf_col_type"] or op.get("old_type")
            tbl_active = bool(dm["dmmf_model_found"])
            col_active = bool(dm["dmmf_col_found"])
            col_nullable = (not dm["dmmf_col_is_required"]) if dm["dmmf_col_found"] else None
            enum_active = bool(dm["dmmf_enum_found"])
            ignored = False
            presence = False
            schema_resolved = bool(dm["dmmf_parsed_ok"])
        elif eco == "rails":
            ri = inspect_rails_c_old(c)
            orm_info = ri
            old_t = ri["col_type"] or op.get("old_type")
            tbl_active = ri["tbl_in_schema"] or bool(ri["model_file"])
            col_active = ri["col_in_schema"] and (not ri["ignored_in_model"])
            col_nullable = ri["col_nullable"]
            enum_active = False
            ignored = ri["ignored_in_model"]
            presence = ri["presence_guard"]
            schema_resolved = ri["has_schema_file"]
        else:
            di = inspect_django_c_old(c)
            orm_info = di
            old_t = di["col_type"] or op.get("old_type")
            tbl_active = bool(di["model_found"])
            col_active = bool(di["col_found"])
            col_nullable = di["col_nullable"]
            enum_active = False
            ignored = False
            presence = False
            schema_resolved = di["models_files_count"] > 0

        # Independent backward-compatibility (C_{t-1} |= S_t) adjudication rules
        gt_viol = None
        rationale = ""
        confidence = "HIGH"

        # Rule 0: Transient table/column created inside the exact same migration -> C_{t-1} never saw it!
        if transient == "table_created_same_migration":
            gt_viol = False
            rationale = f"SAFE: target table `{tbl}` was created in the same migration (`{transient}`); C_{{t-1}} never accesses it."
        elif transient == "col_added_same_migration" and kind in ("DROP_COLUMN", "RENAME_COLUMN", "SET_NOT_NULL", "ALTER_COLUMN_TYPE"):
            gt_viol = False
            rationale = f"SAFE: transient column `{col}` was added and modified/dropped within the same migration; absent from C_{{t-1}}."
        elif kind == "ALTER_COLUMN_TYPE" and is_safe_value_widening(old_t, op.get("new_type"), mig_txt):
            gt_viol = False
            rationale = f"SAFE: value-compatible type widening ({old_t} -> {op.get('new_type')}); lock hazard only, no C_{{t-1}} value crash."
        elif kind in ("DROP_COLUMN", "RENAME_COLUMN"):
            if eco == "prisma" and orm_info["pg_crash"] is True:
                gt_viol = True
                rationale = f"VIOL: Official Prisma DMMF C_{{t-1}} query crashed on PGlite ({orm_info['pg_sqlstate']})."
            elif ignored and not has_app_ref and not removed_in_diff:
                gt_viol = False
                rationale = f"SAFE: Rails `ignored_columns` detaches `{col}` in C_{{t-1}} with no app references."
            elif col_active:
                gt_viol = True
                rationale = f"VIOL: column `{tbl}.{col}` is active in C_{{t-1}} ORM schema (implicit SELECT/INSERT projection)."
            elif has_app_ref:
                gt_viol = True
                rationale = f"VIOL: C_{{t-1}} app code directly references `{col}` at `{app_ref_loc}`."
            elif removed_in_diff:
                gt_viol = True
                rationale = f"VIOL: commit diff removes references to `{col}` in same commit as DDL."
            elif schema_resolved or (eco == "prisma" and orm_info["pg_crash"] is False):
                gt_viol = False
                rationale = f"SAFE: column `{tbl}.{col}` absent from C_{{t-1}} ORM schema, app code, and commit diff."
            else:
                gt_viol = False
                confidence = "REVIEW"
                rationale = f"REVIEW: C_{{t-1}} schema unresolved for `{tbl}.{col}`; 0 app/diff refs found."
        elif kind in ("DROP_TABLE", "RENAME_TABLE"):
            if eco == "prisma" and orm_info["pg_crash"] is True:
                gt_viol = True
                rationale = f"VIOL: Official Prisma DMMF C_{{t-1}} query on `{tbl}` crashed on PGlite ({orm_info['pg_sqlstate']})."
            elif tbl_active:
                gt_viol = True
                rationale = f"VIOL: table/model `{tbl}` is active in C_{{t-1}} ORM."
            elif len(tbl_app_hits) > 0:
                gt_viol = True
                rationale = f"VIOL: C_{{t-1}} app code references table `{tbl}` at `{tbl_app_hits[0]['file']}:{tbl_app_hits[0]['line']}`."
            elif removed_in_diff:
                gt_viol = True
                rationale = f"VIOL: commit diff removes model/table `{tbl}` in same commit as DDL."
            elif schema_resolved:
                gt_viol = False
                rationale = f"SAFE: table `{tbl}` already absent from C_{{t-1}} ORM, app code, and commit diff."
            else:
                gt_viol = False
                confidence = "REVIEW"
                rationale = f"REVIEW: C_{{t-1}} schema unresolved for table `{tbl}`; 0 app/diff refs."
        elif kind == "ADD_NOT_NULL_NO_DEFAULT":
            if eco == "prisma" and orm_info["pg_crash"] is True:
                gt_viol = True
                rationale = f"VIOL: Official Prisma DMMF C_{{t-1}} INSERT omitting `{col}` crashed on PGlite ({orm_info['pg_sqlstate']})."
            elif tbl_active or len(tbl_app_hits) > 0:
                gt_viol = True
                rationale = f"VIOL: adds NOT NULL column `{col}` without DB default onto active C_{{t-1}} table `{tbl}` (breaks C_{{t-1}} INSERTs)."
            elif schema_resolved:
                gt_viol = False
                rationale = f"SAFE: target table `{tbl}` not active in C_{{t-1}} ORM or app code."
            else:
                gt_viol = False
                confidence = "REVIEW"
                rationale = f"REVIEW: target table `{tbl}` unresolved in C_{{t-1}}."
        elif kind == "SET_NOT_NULL":
            if eco == "prisma" and orm_info["pg_crash"] is True:
                gt_viol = True
                rationale = f"VIOL: Official Prisma DMMF C_{{t-1}} optional-field NULL INSERT crashed on PGlite ({orm_info['pg_sqlstate']})."
            elif presence:
                gt_viol = False
                rationale = f"SAFE: C_{{t-1}} model enforces `validates :{col}, presence: true` preventing NULL writes."
            elif col_active and col_nullable is False and not removed_in_diff:
                gt_viol = False
                rationale = f"SAFE: column `{tbl}.{col}` was already non-nullable in C_{{t-1}} ORM schema."
            elif (col_active and col_nullable is not False) or has_app_ref or removed_in_diff:
                gt_viol = True
                rationale = f"VIOL: tightens nullable/active C_{{t-1}} column `{tbl}.{col}` to NOT NULL without prior presence guarantee."
            elif schema_resolved:
                gt_viol = False
                rationale = f"SAFE: column `{tbl}.{col}` absent or non-nullable in C_{{t-1}}."
            else:
                gt_viol = False
                confidence = "REVIEW"
                rationale = f"REVIEW: `{tbl}.{col}` schema unresolved in C_{{t-1}}."
        elif kind == "ALTER_COLUMN_TYPE":
            if ignored and not has_app_ref:
                gt_viol = False
                rationale = f"SAFE: column `{col}` ignored in C_{{t-1}} Rails model."
            elif col_active or has_app_ref or removed_in_diff:
                gt_viol = True
                confidence = "REVIEW"  # Always review ALTER_COLUMN_TYPE to verify actual type incompatibility!
                rationale = f"VIOL (check type): alters active C_{{t-1}} column `{tbl}.{col}` ({old_t} -> {op.get('new_type')})."
            elif schema_resolved:
                gt_viol = False
                rationale = f"SAFE: column `{tbl}.{col}` absent from C_{{t-1}} ORM and app code."
            else:
                gt_viol = False
                confidence = "REVIEW"
                rationale = f"REVIEW: `{tbl}.{col}` unresolved in C_{{t-1}}."
        elif kind == "RENAME_TYPE_ENUM":
            if enum_active or has_app_ref or removed_in_diff:
                gt_viol = True
                rationale = f"VIOL: enum/type `{tbl}` active in C_{{t-1}} Prisma DMMF or code while dropped/renamed in place."
            elif schema_resolved:
                gt_viol = False
                rationale = f"SAFE: enum/type `{tbl}` not present in C_{{t-1}} schema or code."
            else:
                gt_viol = False
                confidence = "REVIEW"
                rationale = f"REVIEW: enum `{tbl}` unresolved."
        elif kind == "ADD_UNIQUE_OR_CHECK":
            if tbl_active or len(tbl_app_hits) > 0:
                gt_viol = True
                rationale = f"VIOL: adds validated UNIQUE/CHECK constraint onto active C_{{t-1}} table `{tbl}`."
            elif schema_resolved:
                gt_viol = False
                rationale = f"SAFE: target table `{tbl}` absent from C_{{t-1}}."
            else:
                gt_viol = False
                confidence = "REVIEW"
                rationale = f"REVIEW: table `{tbl}` unresolved."

        row = {
            "id": cid,
            "ecosystem": eco,
            "repo": c["repo"],
            "sha": c["sha"],
            "git_stratum": c["git_stratum"],
            "op_kind": kind,
            "table": tbl,
            "column": col,
            "new_name": op.get("new_name"),
            "new_type": op.get("new_type"),
            "old_type_observed": old_t,
            "file_path": c["file_path"],
            "subject": c["subject"],
            "raw_snippet": op.get("raw_snippet", "")[:160],
            "transient": transient,
            "orm_info": orm_info,
            "removed_in_diff": removed_in_diff,
            "app_ref_loc": app_ref_loc,
            "tbl_app_hits_count": len(tbl_app_hits),
            "gt_violation": bool(gt_viol),
            "confidence": confidence,
            "rationale": rationale,
        }
        adjudicated.append(row)
        if confidence == "REVIEW":
            needs_manual_eyes.append(row)

    (V2_DIR / "gt_v2_initial.json").write_text(json.dumps(adjudicated, indent=2), encoding="utf-8")
    print("Total cases:", len(adjudicated))
    print("GT distribution overall:", Counter(r["gt_violation"] for r in adjudicated))
    print("GT by ecosystem:", Counter((r["ecosystem"], r["gt_violation"]) for r in adjudicated))
    print("Needs manual review count:", len(needs_manual_eyes))
    for r in needs_manual_eyes:
        print(f"  {r['id']} | {r['repo']}@{r['sha'][:8]} | {r['op_kind']} {r['table']}.{r['column']} ({r['old_type_observed']}->{r['new_type']}) | {r['raw_snippet'][:80]} | {r['rationale']}")


if __name__ == "__main__":
    main()
