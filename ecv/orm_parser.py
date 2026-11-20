"""Cross-ecosystem ORM schema & model parser for C_{t-1} and C_t snapshots.

Extracts active ORM models, columns/fields, table mappings, nullability/types,
and Rails `ignored_columns` declarations across:
  - Prisma (`schema.prisma` / `*.prisma`)
  - Rails ActiveRecord (`db/schema.rb` + `app/models/**/*.rb`)
  - Django (`**/models.py` / `**/models/**/*.py`)
"""
from __future__ import annotations

import ast
import re
from dataclasses import dataclass, field

from .ir import norm_ident


@dataclass
class ORMField:
    name: str            # ORM field name (lowercase)
    db_column: str       # PostgreSQL column name (lowercase)
    field_type: str = ""
    nullable: bool = True
    has_default: bool = False
    is_relation: bool = False


@dataclass
class ORMModel:
    model_name: str      # Class/Model name (lowercase)
    table_name: str      # PostgreSQL table name (lowercase)
    file_path: str = ""
    fields: dict[str, ORMField] = field(default_factory=dict)  # keyed by db_column (lower)
    ignored_columns: set[str] = field(default_factory=set)     # Rails self.ignored_columns
    has_schema_columns: bool = False                           # True iff columns came from schema/models (not stub)
    has_model_class: bool = False                              # Rails: an app/models class maps to this table


@dataclass
class ORMSnapshot:
    ecosystem: str
    models_by_table: dict[str, ORMModel] = field(default_factory=dict)
    models_by_name: dict[str, ORMModel] = field(default_factory=dict)
    enums: dict[str, list[str]] = field(default_factory=dict)  # enum_name_lower -> values

    def find_model(self, table: str | None, model_hint: str | None = None) -> ORMModel | None:
        if table:
            t = norm_ident(table)
            if t in self.models_by_table:
                return self.models_by_table[t]
            # Try singular/plural variants for Rails/Prisma naming
            for alt in _table_variants(t):
                if alt in self.models_by_table:
                    return self.models_by_table[alt]
                if alt in self.models_by_name:
                    return self.models_by_name[alt]
        if model_hint:
            m = norm_ident(model_hint)
            if m in self.models_by_name:
                return self.models_by_name[m]
            # Suffix match for Django app_model vs model
            for k, v in self.models_by_table.items():
                if k.endswith("_" + m) or k == m:
                    return v
        return None


def _table_variants(t: str) -> list[str]:
    v = [t]
    if t.endswith("ies") and len(t) > 3:
        v.append(t[:-3] + "y")
    elif t.endswith("ses") and len(t) > 3:
        v.append(t[:-2])
    elif t.endswith("s") and len(t) > 1:
        v.append(t[:-1])
    else:
        v.append(t + "s")
        if t.endswith("y"):
            v.append(t[:-1] + "ies")
    # Camel/snake normalization (strip underscores)
    v.append(t.replace("_", ""))
    return v


# ---------------------------------------------------------------------------
# 1. Prisma Schema Parser (`schema.prisma`)
# ---------------------------------------------------------------------------

PRISMA_SCALARS = {
    "string", "boolean", "int", "bigint", "float", "decimal",
    "datetime", "json", "bytes", "unsupported",
}


def parse_prisma_schema(texts: dict[str, str]) -> ORMSnapshot:
    snap = ORMSnapshot(ecosystem="prisma")

    # First pass: collect enums
    for path, content in texts.items():
        if not content:
            continue
        for m in re.finditer(r"^\s*enum\s+(\w+)\s*\{([^}]*)\}", content, re.M | re.S):
            ename = norm_ident(m.group(1))
            vals = []
            for ln in m.group(2).splitlines():
                ln = re.sub(r"//.*", "", ln).strip()
                if ln and not ln.startswith("@@"):
                    vals.append(ln.split()[0])
            if ename:
                snap.enums[ename] = vals

    # Second pass: collect models
    for path, content in texts.items():
        if not content:
            continue
        for m in re.finditer(r"^\s*model\s+(\w+)\s*\{([^}]*)\}", content, re.M | re.S):
            mname_raw = m.group(1)
            body = m.group(2)
            mname = norm_ident(mname_raw)
            if not mname:
                continue

            # Check @@map("db_table")
            mm = re.search(r'@@map\s*\(\s*"([^"]+)"\s*\)', body)
            tname = norm_ident(mm.group(1)) if mm else mname

            model = ORMModel(model_name=mname, table_name=tname, file_path=path, has_schema_columns=True)
            for ln in body.splitlines():
                ln_clean = re.sub(r"//.*", "", ln).strip()
                if not ln_clean or ln_clean.startswith("@@"):
                    continue
                mf = re.match(r"^(\w+)\s+([A-Za-z0-9_]+(?:\([^)]*\))?)(\[\])?(\?)?(.*)$", ln_clean)
                if not mf:
                    continue
                fname_raw, ftype_raw, is_list, is_opt, rest = mf.groups()
                ftype_base = ftype_raw.split("(")[0].lower()
                is_rel = bool(is_list) or (
                    "@relation" in rest
                ) or (
                    ftype_base not in PRISMA_SCALARS and ftype_base not in snap.enums
                )
                if is_rel:
                    continue  # Relation virtual field is not a DB column
                cmap = re.search(r'@map\s*\(\s*"([^"]+)"\s*\)', rest)
                db_col = norm_ident(cmap.group(1)) if cmap else norm_ident(fname_raw)
                if not db_col:
                    continue
                f = ORMField(
                    name=norm_ident(fname_raw) or db_col,
                    db_column=db_col,
                    field_type=ftype_raw,
                    nullable=bool(is_opt),
                    has_default=("@default(" in rest or "@updatedAt" in rest),
                    is_relation=False,
                )
                model.fields[db_col] = f
                if f.name != db_col:
                    model.fields[f.name] = f

            snap.models_by_table[tname] = model
            snap.models_by_name[mname] = model
            # Also index by exact case-folded raw model name without underscores
            snap.models_by_table.setdefault(mname, model)

    return snap


# ---------------------------------------------------------------------------
# 2. Rails ActiveRecord Parser (`db/schema.rb` + `app/models/**/*.rb`)
# ---------------------------------------------------------------------------

def _ruby_class_to_table(cls_name: str) -> str:
    # Convert CamelCase to snake_case and pluralize simply
    s = re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1_\2", cls_name)
    s = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", s).lower()
    if s.endswith("y") and not s.endswith(("ay", "ey", "iy", "oy", "uy")):
        return s[:-1] + "ies"
    if s.endswith(("s", "x", "z", "ch", "sh")):
        return s + "es"
    return s + "s"


def _parse_rails_ignored_columns(ruby_text: str) -> set[str]:
    ignored: set[str] = set()
    # Patterns:
    #   self.ignored_columns += ["col1", :col2]
    #   self.ignored_columns = %w[col1 col2]
    #   self.ignored_columns = %i[col1 col2]
    #   self.ignored_columns << "col1"
    #   ignored_columns += %w(...)
    for m in re.finditer(
        r"(?:self\.)?ignored_columns\s*(?:\+?=|<<)\s*(?:%[wWiI]\s*[\[\(\{]([^\]\)\}]+)[\]\)\}]|\[([^\]]+)\]|[:'\"]([A-Za-z0-9_]+)['\"]?)",
        ruby_text,
        re.S,
    ):
        g1, g2, g3 = m.groups()
        if g1:
            for tok in g1.split():
                n = norm_ident(tok)
                if n:
                    ignored.add(n)
        elif g2:
            for tok in re.findall(r"[:'\"]([A-Za-z0-9_]+)['\"]?", g2):
                n = norm_ident(tok)
                if n:
                    ignored.add(n)
        elif g3:
            n = norm_ident(g3)
            if n:
                ignored.add(n)
    # GitLab-style IgnorableColumns helper (v3):
    #   ignore_column :col, remove_with: '17.0', remove_after: '2024-05-01'
    #   ignore_columns %i[a b], remove_with: ...
    #   ignore_columns :a, :b, remove_with: ...
    for m in re.finditer(r"^\s*ignore_columns?\s*\(?\s*(.+)$", ruby_text, re.M):
        args = m.group(1)
        args = re.split(r"\b(?:remove_with|remove_after|remove_never)\s*:", args)[0]
        pct = re.search(r"%[wWiI]\s*[\[\(\{]([^\]\)\}]+)[\]\)\}]", args)
        toks = pct.group(1).split() if pct else re.findall(r"[:'\"]([A-Za-z0-9_]+)['\"]?", args)
        for tok in toks:
            n = norm_ident(tok)
            if n:
                ignored.add(n)
    return ignored


def _parse_rails_structure_sql(structure_sql: str, snap: ORMSnapshot) -> None:
    """Parse PostgreSQL CREATE TABLE blocks from Rails db/structure.sql."""
    for m in re.finditer(
        r'CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?(?:"?\w+"?\.)?"?([A-Za-z0-9_]+)"?\s*\((.*?)\n\);',
        structure_sql,
        re.S | re.I,
    ):
        tname = norm_ident(m.group(1))
        body = m.group(2)
        if not tname or tname in snap.models_by_table:
            continue
        model = ORMModel(model_name=tname, table_name=tname, file_path="db/structure.sql", has_schema_columns=True)
        for ln in body.splitlines():
            ln_s = ln.strip().rstrip(",")
            if not ln_s or re.match(r"^(CONSTRAINT|PRIMARY\s+KEY|UNIQUE|FOREIGN\s+KEY|CHECK|EXCLUDE)\b", ln_s, re.I):
                continue
            mc = re.match(r'^"?([A-Za-z_][A-Za-z0-9_]*)"?\s+([A-Za-z0-9_\[\] ()]+)', ln_s)
            if not mc:
                continue
            cname = norm_ident(mc.group(1))
            ctype = mc.group(2).strip().split()[0]
            if not cname:
                continue
            model.fields[cname] = ORMField(
                name=cname,
                db_column=cname,
                field_type=ctype,
                nullable=("NOT NULL" not in ln_s.upper()),
                has_default=("DEFAULT " in ln_s.upper()),
            )
        if model.fields:
            snap.models_by_table[tname] = model
            snap.models_by_name[tname] = model


def parse_rails_schema_and_models(
    schema_rb: str | None,
    model_files: dict[str, str],
    structure_sql: str | None = None,
) -> ORMSnapshot:
    snap = ORMSnapshot(ecosystem="rails")

    # 1. Parse db/schema.rb tables & columns
    if schema_rb:
        # Match create_table "users" ... do |t| ... end
        for m in re.finditer(
            r'create_table\s+["\':]([A-Za-z0-9_]+)["\']?[^\n]*do\s*\|(\w+)\|(.*?)\n\s*end\b',
            schema_rb,
            re.S,
        ):
            tname = norm_ident(m.group(1))
            var = re.escape(m.group(2))
            body = m.group(3)
            if not tname:
                continue
            model = ORMModel(model_name=tname, table_name=tname, file_path="db/schema.rb", has_schema_columns=True)
            for cm in re.finditer(
                rf'{var}\.(\w+)\s+["\':]([A-Za-z0-9_]+)["\']?(.*)',
                body,
            ):
                ctype, cname_raw, rest = cm.groups()
                if ctype in ("index", "check_constraint", "unique_constraint"):
                    continue
                cname = norm_ident(cname_raw)
                if not cname:
                    continue
                if ctype in ("references", "belongs_to") and not cname.endswith("_id"):
                    cname = f"{cname}_id"
                model.fields[cname] = ORMField(
                    name=cname,
                    db_column=cname,
                    field_type=ctype,
                    nullable=("null: false" not in rest and ":null => false" not in rest),
                    has_default=("default:" in rest or ":default =>" in rest),
                )
            snap.models_by_table[tname] = model
            snap.models_by_name[tname] = model

    # 1b. Parse db/structure.sql when schema.rb is absent or incomplete
    if structure_sql:
        _parse_rails_structure_sql(structure_sql, snap)

    # 2. Parse app/models/**/*.rb for table_name overrides & ignored_columns
    for path, content in model_files.items():
        if not content:
            continue
        classes = re.findall(r"class\s+([A-Za-z0-9_:]+)\s*<", content)
        ignored = _parse_rails_ignored_columns(content)
        mt = re.search(r'self\.table_name\s*=\s*["\':]([A-Za-z0-9_]+)["\']?', content)
        explicit_table = norm_ident(mt.group(1)) if mt else None

        inferred_tables: list[str] = []
        if explicit_table:
            inferred_tables.append(explicit_table)
        for cls in classes:
            short = cls.split("::")[-1]
            inferred_tables.append(_ruby_class_to_table(short))
            if "::" in cls:
                full_snake = "_".join(_ruby_class_to_table(p) for p in cls.split("::")[:-1]) + "_" + _ruby_class_to_table(short)
                inferred_tables.append(full_snake)
                # Rails namespace prefix is usually singular e.g. Captain::McpServer -> captain_mcp_servers
                ns_prefix = "_".join(re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", p).lower() for p in cls.split("::")[:-1])
                inferred_tables.append(f"{ns_prefix}_{_ruby_class_to_table(short)}")

        # Also infer from file path e.g. app/models/captain/mcp_server.rb -> captain_mcp_servers
        mp = re.search(r"models/(.+)\.rb$", path.replace("\\", "/"))
        if mp:
            parts = mp.group(1).split("/")
            base = parts[-1]
            plural_base = base[:-1] + "ies" if base.endswith("y") else base + "s"
            inferred_tables.append(plural_base)
            if len(parts) > 1:
                inferred_tables.append("_".join(parts[:-1] + [plural_base]))

        matched = False
        for t in inferred_tables:
            if t in snap.models_by_table:
                snap.models_by_table[t].ignored_columns.update(ignored)
                if classes:
                    snap.models_by_table[t].has_model_class = True
                if snap.models_by_table[t].file_path == "db/schema.rb":
                    snap.models_by_table[t].file_path = path
                matched = True
        if not matched and inferred_tables:
            t0 = inferred_tables[0]
            m_obj = ORMModel(
                model_name=t0, table_name=t0, file_path=path, ignored_columns=set(ignored),
                has_model_class=bool(classes),
            )
            snap.models_by_table[t0] = m_obj
            snap.models_by_name[t0] = m_obj

    return snap


# ---------------------------------------------------------------------------
# 3. Django Models Parser (`**/models.py` and `**/models/**/*.py`)
# ---------------------------------------------------------------------------

def _django_app_from_model_path(path: str) -> str | None:
    parts = path.replace("\\", "/").split("/")
    for i in range(len(parts) - 1, -1, -1):
        if parts[i] == "models.py" and i > 0:
            return parts[i - 1].lower()
        if parts[i] == "models" and i > 0:
            return parts[i - 1].lower()
    return None


def parse_django_models(model_files: dict[str, str]) -> ORMSnapshot:
    snap = ORMSnapshot(ecosystem="django")

    for path, content in model_files.items():
        if not content:
            continue
        try:
            tree = ast.parse(content)
        except SyntaxError:
            continue

        app_label = _django_app_from_model_path(path)

        for node in tree.body:
            if not isinstance(node, ast.ClassDef):
                continue
            cls_lower = node.name.lower()
            db_table = f"{app_label}_{cls_lower}" if app_label else cls_lower
            is_abstract = False

            # Inspect Meta inner class for db_table / abstract
            for item in node.body:
                if isinstance(item, ast.ClassDef) and item.name == "Meta":
                    for mitem in item.body:
                        if isinstance(mitem, ast.Assign):
                            for tgt in mitem.targets:
                                if isinstance(tgt, ast.Name) and tgt.id == "db_table":
                                    if isinstance(mitem.value, ast.Constant) and isinstance(mitem.value.value, str):
                                        db_table = mitem.value.value.lower()
                                elif isinstance(tgt, ast.Name) and tgt.id == "abstract":
                                    if isinstance(mitem.value, ast.Constant) and mitem.value.value is True:
                                        is_abstract = True

            model = ORMModel(model_name=cls_lower, table_name=db_table, file_path=path, has_schema_columns=True)

            for item in node.body:
                fname = None
                val = None
                if isinstance(item, ast.Assign) and len(item.targets) == 1 and isinstance(item.targets[0], ast.Name):
                    fname = item.targets[0].id
                    val = item.value
                elif isinstance(item, ast.AnnAssign) and isinstance(item.target, ast.Name):
                    fname = item.target.id
                    val = item.value
                if not fname or not isinstance(val, ast.Call):
                    continue

                ftype = ""
                if isinstance(val.func, ast.Attribute):
                    ftype = val.func.attr
                elif isinstance(val.func, ast.Name):
                    ftype = val.func.id

                # Heuristic: Django fields end with Field or are ForeignKey/OneToOneField
                if not (ftype.endswith("Field") or ftype in ("ForeignKey", "OneToOneField", "ManyToManyField")):
                    continue
                if "ManyToMany" in ftype:
                    continue

                kw = {k.arg: k.value for k in val.keywords if k.arg}
                nullable = False
                if "null" in kw and isinstance(kw["null"], ast.Constant):
                    nullable = bool(kw["null"].value)
                has_default = "default" in kw or "db_default" in kw
                db_col = norm_ident(fname)
                if "db_column" in kw and isinstance(kw["db_column"], ast.Constant) and isinstance(kw["db_column"].value, str):
                    db_col = norm_ident(kw["db_column"].value) or db_col

                if not db_col:
                    continue
                f_obj = ORMField(
                    name=norm_ident(fname) or db_col,
                    db_column=db_col,
                    field_type=ftype,
                    nullable=nullable,
                    has_default=has_default,
                )
                model.fields[db_col] = f_obj
                if f_obj.name != db_col:
                    model.fields[f_obj.name] = f_obj
                if ftype in ("ForeignKey", "OneToOneField") and not db_col.endswith("_id"):
                    model.fields[f"{db_col}_id"] = f_obj

            if model.fields or not is_abstract:
                snap.models_by_table[db_table] = model
                snap.models_by_name[cls_lower] = model
                if app_label:
                    snap.models_by_name[f"{app_label}_{cls_lower}"] = model

    return snap
