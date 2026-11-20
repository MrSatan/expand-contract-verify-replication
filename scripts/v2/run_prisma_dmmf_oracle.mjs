/**
 * Official Prisma DMMF (`@prisma/internals` v7.10.1) + PostgreSQL 18.7 (PGlite) Non-Circular Oracle.
 *
 * ZERO circularity:
 *   - NEVER reads ECV's verdict or `ground_contract_detached`!
 *   - Parses the repository's VERBATIM `schema.prisma` at C_{t-1} via Prisma's official WASM DMMF parser.
 *   - Derives the C_{t-1} Prisma Client SELECT/INSERT workload STRICTLY from the official DMMF model & fields.
 *   - Executes the C_{t-1} query on PGlite before and after the migration DDL to verify real PostgreSQL SQLSTATE behavior.
 *
 * Usage: node run_prisma_dmmf_oracle.mjs <input_prisma_cases.json> <output_oracle.json>
 */
import fs from "node:fs";
import path from "node:path";
import { createRequire } from "node:module";

const require = createRequire(import.meta.url);
const __here = path.dirname(new URL(import.meta.url).pathname).replace(/^\/([A-Za-z]:)/, "$1");
const toolsDir = process.env.ECV_TOOLS_DIR
  ? path.resolve(process.env.ECV_TOOLS_DIR)
  : fs.existsSync(path.resolve(__here, "../tools/node_modules/@electric-sql/pglite"))
    ? path.resolve(__here, "../tools")
    : path.resolve(__here, "../..");
const internals = require(path.join(toolsDir, "node_modules/@prisma/internals"));
const { PGlite } = await import(
  "file:///" + path.join(toolsDir, "node_modules/@electric-sql/pglite/dist/index.js").replace(/\\/g, "/")
);

function sanitizePrismaSchemaForDmmf(raw) {
  let s = String(raw || "");
  s = s.replace(/^\s*(url|directUrl|shadowDatabaseUrl|extensions|schemas)\s*=\s*.*$/gm, "");
  s = s.replace(/generator\s+\w+\s*\{[^}]*\}/gs, "");
  // Strip Prisma 2-3 legacy datasource properties and multi-schema @@schema(...)
  s = s.replace(/^\s*enabled\s*=\s*.*$/gm, "");
  s = s.replace(/@@schema\([^)]*\)/g, "");
  if (!/datasource\s+\w+\s*\{/.test(s)) {
    s = `datasource db {\n  provider = "postgresql"\n}\n` + s;
  }
  return s;
}

const q = (id) => `"${String(id).replace(/"/g, '""')}"`;

function pgTypeFromPrisma(pType) {
  const t = String(pType || "String");
  if (t === "Int" || t === "BigInt") return "INTEGER";
  if (t === "Float" || t === "Decimal") return "DOUBLE PRECISION";
  if (t === "Boolean") return "BOOLEAN";
  if (t === "DateTime") return "TIMESTAMPTZ";
  if (t === "Json") return "JSONB";
  return "TEXT";
}

function sampleLit(pgT) {
  const t = pgT.toUpperCase();
  if (t.includes("INT")) return "1";
  if (t.includes("DOUBLE")) return "1.5";
  if (t.includes("BOOL")) return "TRUE";
  if (t.includes("TIME")) return "NOW()";
  if (t.includes("JSON")) return `'{}'::jsonb`;
  return `'v1'`;
}

const [, , inPath, outPath] = process.argv;
const cases = JSON.parse(fs.readFileSync(inPath, "utf8"));
const db = new PGlite();
const results = [];

for (let i = 0; i < cases.length; i++) {
  const c = cases[i];
  const op = c.op || {};
  const tbl = (op.table || "").toLowerCase();
  const col = (op.column || "").toLowerCase();
  const newName = (op.new_name || "").toLowerCase();
  const kind = op.kind;

  const out = {
    id: c.id,
    dmmf_parsed_ok: false,
    dmmf_error: null,
    dmmf_models_count: 0,
    dmmf_model_found: null,
    dmmf_table_name: null,
    dmmf_col_found: null,
    dmmf_col_is_required: null,
    dmmf_col_has_default: null,
    dmmf_col_type: null,
    dmmf_enum_found: null,
    dmmf_scalar_fields_count: 0,
    pg_c_old_at_s_old_ok: null,
    pg_c_old_at_s_new_crash: null,
    pg_sqlstate: null,
    pg_error_msg: null,
  };

  // Try parsing EVERY *.prisma file at C_{t-1} (monorepos like amplication/langfuse have multiple schema.prisma files!)
  const schemas = c.prisma_schemas_old || {};
  const schemaPaths = Object.keys(schemas);
  const allModels = [];
  const allEnums = [];
  let parsedAny = false;
  let lastErr = null;

  for (const sp of schemaPaths) {
    const raw = schemas[sp] || "";
    if (!raw.trim() || (!raw.includes("model ") && !raw.includes("enum "))) continue;
    try {
      const cleaned = sanitizePrismaSchemaForDmmf(raw);
      const dm = await internals.getDMMF({ datamodel: cleaned });
      parsedAny = true;
      allModels.push(...(dm.datamodel?.models || []));
      allEnums.push(...(dm.datamodel?.enums || []));
    } catch (e) {
      lastErr = String(e.message || e).slice(0, 200);
    }
  }
  // Fallback: combined multi-file prismaFolder schema (where datasource is in schema.prisma and models are in *.prisma)
  if (!parsedAny && schemaPaths.length > 1) {
    try {
      const cleaned = sanitizePrismaSchemaForDmmf(schemaPaths.map((p) => schemas[p]).join("\n\n"));
      const dm = await internals.getDMMF({ datamodel: cleaned });
      parsedAny = true;
      allModels.push(...(dm.datamodel?.models || []));
      allEnums.push(...(dm.datamodel?.enums || []));
    } catch (e) {
      lastErr = String(e.message || e).slice(0, 200);
    }
  }

  let dmmf = parsedAny ? { datamodel: { models: allModels, enums: allEnums } } : null;
  out.dmmf_parsed_ok = parsedAny;
  if (!parsedAny) out.dmmf_error = lastErr || "no_schema_prisma_at_parent";

  if (dmmf && dmmf.datamodel) {
    const models = dmmf.datamodel.models || [];
    const enums = dmmf.datamodel.enums || [];
    out.dmmf_models_count = models.length;

    // Match model by dbName (@@map) or model name (case-insensitive)
    const matchedModel = models.find((m) => {
      const dn = (m.dbName || m.name).toLowerCase();
      const mn = m.name.toLowerCase();
      return dn === tbl || mn === tbl || dn.replace(/_/g, "") === tbl.replace(/_/g, "");
    });

    const matchedEnum = enums.find((e) => {
      const en = (e.dbName || e.name).toLowerCase();
      return en === tbl || en === col || (newName && en === newName);
    });
    if (matchedEnum) {
      out.dmmf_enum_found = matchedEnum.name;
    }

    if (matchedModel) {
      out.dmmf_model_found = matchedModel.name;
      out.dmmf_table_name = matchedModel.dbName || matchedModel.name;
      const scalarFields = (matchedModel.fields || []).filter((f) => f.kind === "scalar" || f.kind === "enum");
      out.dmmf_scalar_fields_count = scalarFields.length;

      const matchedField = col
        ? scalarFields.find((f) => {
            const fn = (f.dbName || f.name).toLowerCase();
            return fn === col || f.name.toLowerCase() === col;
          })
        : null;

      if (matchedField) {
        out.dmmf_col_found = matchedField.dbName || matchedField.name;
        out.dmmf_col_is_required = Boolean(matchedField.isRequired);
        out.dmmf_col_has_default = Boolean(matchedField.hasDefaultValue);
        out.dmmf_col_type = matchedField.type;
      }

      // Execute DMMF-derived C_{t-1} query on PGlite (NO label branching! Strictly derived from official DMMF!)
      const sch = `dmmf_s_${i}`;
      await db.exec(`DROP SCHEMA IF EXISTS "${sch}" CASCADE; CREATE SCHEMA "${sch}"; SET search_path TO "${sch}";`);
      try {
        const qt = q(out.dmmf_table_name);
        // Build table from official DMMF scalar fields (plus target column if missing from DMMF so S_{t-1} can drop it!)
        const colDefs = [];
        const dmmfCols = scalarFields.map((f) => ({
          name: f.dbName || f.name,
          pgT: pgTypeFromPrisma(f.type),
          req: Boolean(f.isRequired),
          def: Boolean(f.hasDefaultValue),
        }));
        const hasTargetInDmmf = Boolean(matchedField);
        for (const fc of dmmfCols) {
          colDefs.push(`${q(fc.name)} ${fc.pgT}`);
        }
        if (col && !hasTargetInDmmf && kind !== "ADD_NOT_NULL_NO_DEFAULT") {
          colDefs.push(`${q(col)} TEXT`);
        }
        if (colDefs.length === 0) colDefs.push(`"id" INTEGER`);

        await db.exec(`CREATE TABLE ${qt} (${colDefs.join(", ")});`);
        // Seed 1 row
        const seedCols = dmmfCols.map((fc) => q(fc.name));
        const seedVals = dmmfCols.map((fc) => (fc.req ? sampleLit(fc.pgT) : "NULL"));
        if (seedCols.length > 0) {
          await db.exec(`INSERT INTO ${qt} (${seedCols.join(", ")}) VALUES (${seedVals.join(", ")});`);
        }

        // Build C_{t-1} Prisma Client query STRICTLY from official DMMF:
        // 1. Prisma Client findMany projects EVERY scalar field in DMMF: SELECT "c1", "c2", ... FROM "T"
        // 2. Prisma Client create inserts ONLY DMMF scalar fields (passing NULL for optional fields, omitting non-DMMF columns)
        const cOldSelect = dmmfCols.length
          ? `SELECT ${dmmfCols.map((fc) => q(fc.name)).join(", ")} FROM ${qt} LIMIT 5;`
          : `SELECT 1 FROM ${qt} LIMIT 1;`;
        const cOldInsert = dmmfCols.length
          ? `INSERT INTO ${qt} (${dmmfCols.map((fc) => q(fc.name)).join(", ")}) VALUES (${dmmfCols
              .map((fc) => (fc.req ? sampleLit(fc.pgT) : "NULL"))
              .join(", ")});`
          : null;

        // Sanity check C_{t-1} @ S_{t-1}
        let sOldOk = true;
        try {
          await db.query(cOldSelect);
          if (cOldInsert) await db.query(cOldInsert);
        } catch {
          sOldOk = false;
        }
        out.pg_c_old_at_s_old_ok = sOldOk;

        // Apply migration DDL (S_{t-1} -> S_t)
        const targetColActual = out.dmmf_col_found || col;
        const qc = targetColActual ? q(targetColActual) : null;
        let migOk = true;
        try {
          if (kind === "DROP_COLUMN" && qc) {
            await db.exec(`ALTER TABLE ${qt} DROP COLUMN ${qc};`);
          } else if (kind === "DROP_TABLE") {
            await db.exec(`DROP TABLE ${qt};`);
          } else if (kind === "RENAME_COLUMN" && qc && newName) {
            await db.exec(`ALTER TABLE ${qt} RENAME COLUMN ${qc} TO ${q(newName)};`);
          } else if (kind === "RENAME_TABLE" && newName) {
            await db.exec(`ALTER TABLE ${qt} RENAME TO ${q(newName)};`);
          } else if (kind === "SET_NOT_NULL" && qc) {
            await db.exec(`UPDATE ${qt} SET ${qc} = ${sampleLit(pgTypeFromPrisma(out.dmmf_col_type))} WHERE ${qc} IS NULL;`);
            await db.exec(`ALTER TABLE ${qt} ALTER COLUMN ${qc} SET NOT NULL;`);
          } else if (kind === "ADD_NOT_NULL_NO_DEFAULT" && col) {
            const newT = pgTypeFromPrisma(op.new_type || "String");
            await db.exec(`ALTER TABLE ${qt} ADD COLUMN ${q(col)} ${newT} DEFAULT ${sampleLit(newT)} NOT NULL;`);
            await db.exec(`ALTER TABLE ${qt} ALTER COLUMN ${q(col)} DROP DEFAULT;`);
          } else {
            migOk = false; // ALTER_COLUMN_TYPE / RENAME_TYPE_ENUM / ADD_UNIQUE_OR_CHECK evaluated via full SQL + audit
          }
        } catch {
          migOk = false;
        }

        if (migOk) {
          try {
            await db.query(cOldSelect);
            if (cOldInsert) await db.query(cOldInsert);
            out.pg_c_old_at_s_new_crash = false;
          } catch (err) {
            out.pg_c_old_at_s_new_crash = true;
            out.pg_sqlstate = err.code || "PG_ERR";
            out.pg_error_msg = String(err.message || err).slice(0, 200);
          }
        }
      } finally {
        await db.exec(`SET search_path TO public; DROP SCHEMA IF EXISTS "${sch}" CASCADE;`).catch(() => {});
      }
    }
  }
  results.push(out);
}

await db.close();
fs.writeFileSync(outPath, JSON.stringify(results, null, 2), "utf8");
const parsed = results.filter((r) => r.dmmf_parsed_ok).length;
const crashed = results.filter((r) => r.pg_c_old_at_s_new_crash === true).length;
const safe = results.filter((r) => r.pg_c_old_at_s_new_crash === false).length;
console.error(`prisma_dmmf_oracle: total=${results.length} dmmf_ok=${parsed} pg_crash=${crashed} pg_safe=${safe}`);
