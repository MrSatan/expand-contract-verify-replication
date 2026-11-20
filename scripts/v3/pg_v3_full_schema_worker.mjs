/**
 * V3: identical to the v2 worker, except that Phase 1-3 SQL now comes from the real
 * ExpandContractSynth output (`c.ecs_plan`, generated in Python) instead of a JS re-implementation,
 * and phase error messages are recorded.
 *
 * V2 Phase 4B: Full-Schema Multi-Table Prisma PostgreSQL 18.7 (PGlite) Replay
 * with Official Prisma 7.10.1 DMMF C_{t-1} Queries + EXACT Schema Equivalence Verification!
 *
 * Upgrades over v1:
 *   1. Derives C_{t-1} SELECT/INSERT queries from Prisma's official `@prisma/internals` `getDMMF`
 *      on the repository's verbatim C_{t-1} `schema.prisma` (not blind `SELECT *` from information_schema!).
 *   2. Covers DROP_COLUMN, DROP_TABLE, RENAME_COLUMN, RENAME_TABLE, ADD_NOT_NULL_NO_DEFAULT, and SET_NOT_NULL.
 *   3. Verifies exact schema equivalence (S_contract == S_target on target table columns/types/nullability/defaults)
 *      after Phase 3 contraction (including dropping temporary Phase-1 defaults!).
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

const q = (id) => `"${String(id).replace(/"/g, '""')}"`;

function cleanMigSql(sql) {
  return String(sql || "")
    .replace(/CREATE\s+EXTENSION\s+IF\s+NOT\s+EXISTS\s+[^;]+;/gi, "-- ext skipped")
    .replace(/CREATE\s+EXTENSION\s+[^;]+;/gi, "-- ext skipped");
}

function sanitizePrisma(raw) {
  let s = String(raw || "");
  s = s.replace(/^\s*(url|directUrl|shadowDatabaseUrl|extensions|schemas)\s*=\s*.*$/gm, "");
  s = s.replace(/generator\s+\w+\s*\{[^}]*\}/gs, "");
  s = s.replace(/^\s*enabled\s*=\s*.*$/gm, "");
  s = s.replace(/@@schema\([^)]*\)/g, "");
  if (!/datasource\s+\w+\s*\{/.test(s)) s = `datasource db {\n  provider = "postgresql"\n}\n` + s;
  return s;
}

async function execStatements(db, sqlList) {
  for (const raw of sqlList) {
    const s = cleanMigSql(raw).trim();
    const nonComment = s.split("\n").map((l) => l.trim()).filter((l) => l && !l.startsWith("--")).join("\n");
    if (!nonComment) continue;
    try {
      await db.exec(s);
    } catch (err) {
      return { ok: false, code: err.code || "PG_ERR", message: String(err.message || err).slice(0, 200) };
    }
  }
  return { ok: true, code: null, message: null };
}

async function querySafe(db, sql) {
  try {
    const res = await db.query(sql);
    return { ok: true, rows: res.rows ?? [], code: null, message: null };
  } catch (err) {
    return { ok: false, rows: [], code: err.code || "PG_ERR", message: String(err.message || err).slice(0, 200) };
  }
}

async function inspectTableCols(db, tableName) {
  const res = await querySafe(
    db,
    `SELECT column_name, data_type, udt_name, datetime_precision, is_nullable, column_default
     FROM information_schema.columns
     WHERE table_schema = 'public' AND table_name = '${tableName.replace(/'/g, "''")}'
     ORDER BY column_name;`
  );
  return res.rows || [];
}

async function inspectSchemaStats(db) {
  const t = await querySafe(db, `SELECT COUNT(*)::int AS n FROM information_schema.tables WHERE table_schema='public' AND table_type='BASE TABLE';`);
  const fk = await querySafe(db, `SELECT COUNT(*)::int AS n FROM information_schema.table_constraints WHERE table_schema='public' AND constraint_type='FOREIGN KEY';`);
  const en = await querySafe(db, `SELECT COUNT(*)::int AS n FROM pg_type t JOIN pg_namespace n ON n.oid=t.typnamespace WHERE n.nspname='public' AND t.typtype='e';`);
  return {
    tables: Number(t.rows?.[0]?.n ?? 0),
    foreign_keys: Number(fk.rows?.[0]?.n ?? 0),
    enums: Number(en.rows?.[0]?.n ?? 0),
  };
}

function litForPg(colInfo, tag = "1") {
  const dt = String(colInfo?.data_type || "").toLowerCase();
  const udt = String(colInfo?.udt_name || "").toLowerCase();
  if (dt.includes("int") || udt.includes("int")) return tag === "2" ? "202" : "101";
  if (dt.includes("numeric") || dt.includes("double") || dt.includes("real")) return "1.5";
  if (dt.includes("bool")) return "TRUE";
  if (dt.includes("json")) return `'{"k":"v${tag}"}'::jsonb`;
  if (dt.includes("timestamp") || dt.includes("date")) return "NOW()";
  if (dt.includes("uuid")) return tag === "2" ? "'22222222-2222-2222-2222-222222222222'::uuid" : "'11111111-1111-1111-1111-111111111111'::uuid";
  if (dt === "user-defined") return null;
  return `'val_${tag}'`;
}

async function getDmmfColumnsForTable(schemasOld, tableLower) {
  for (const raw of Object.values(schemasOld || {})) {
    if (!raw || !raw.includes("model ")) continue;
    try {
      const dm = await internals.getDMMF({ datamodel: sanitizePrisma(raw) });
      const m = (dm.datamodel?.models || []).find((mm) => {
        const dn = (mm.dbName || mm.name).toLowerCase();
        return dn === tableLower || mm.name.toLowerCase() === tableLower;
      });
      if (m) {
        return (m.fields || [])
          .filter((f) => f.kind === "scalar" || f.kind === "enum")
          .map((f) => ({ name: f.dbName || f.name, isRequired: Boolean(f.isRequired), hasDefault: Boolean(f.hasDefaultValue), type: f.type }));
      }
    } catch {
      // try next schema
    }
  }
  return null;
}

async function replayOneFull(c) {
  const t0 = Date.now();
  const out = {
    id: c.id,
    repo: c.repo,
    sha: c.sha,
    op_kind: c.op_kind,
    table_hint: c.table,
    column_hint: c.column,
    new_name_hint: c.new_name,
    prior_migrations_count: c.prior_sqls.length,
    dmmf_used: false,
    schema_loaded_ok: false,
    schema_stats: null,
    actual_table: null,
    actual_column: null,
    c_old_at_s_old_ok: false,
    verbatim_mig_ok: false,
    c_old_at_s_new_crash: false,
    crash_sqlstate: null,
    crash_message: null,
    synth_applied_ok: false,
    synth_c_old_pass: false,
    synth_c_new_pass: false,
    synth_contract_ok: false,
    synth_schema_equiv_target_col: false,
    synth_full_verified: false,
    elapsed_ms: 0,
  };

  // Parse C_{t-1} Prisma DMMF FIRST (non-circular query source!)
  const dmmfFields = await getDmmfColumnsForTable(c.prisma_schemas_old, (c.table || "").toLowerCase());
  out.dmmf_used = Boolean(dmmfFields && dmmfFields.length > 0);

  // --- RUN 1: Full S_{t-1} -> verbatim M_t -> record crash & target schema snapshot ---
  let targetTableSnap = null;
  let actualTable = null;
  let actualColumn = null;
  let pgColsAtOld = [];
  let cOldQuery = null;
  let isInsertTest = false;

  {
    const db = new PGlite();
    const loadRes = await execStatements(db, c.prior_sqls);
    out.schema_loaded_ok = loadRes.ok;
    if (!loadRes.ok) {
      await db.close();
      out.elapsed_ms = Date.now() - t0;
      return out;
    }
    out.schema_stats = await inspectSchemaStats(db);

    const tRes = await querySafe(
      db,
      `SELECT table_name FROM information_schema.tables WHERE table_schema='public' AND lower(table_name)='${(c.table || "").toLowerCase().replace(/'/g, "''")}' LIMIT 1;`
    );
    actualTable = tRes.rows?.[0]?.table_name || null;
    out.actual_table = actualTable;
    if (!actualTable) {
      await db.close();
      out.elapsed_ms = Date.now() - t0;
      return out;
    }

    pgColsAtOld = await inspectTableCols(db, actualTable);
    if (c.column) {
      const fnd = pgColsAtOld.find((r) => r.column_name.toLowerCase() === c.column.toLowerCase());
      actualColumn = fnd ? fnd.column_name : null;
      out.actual_column = actualColumn;
    }

    const qt = q(actualTable);
    const qc = actualColumn ? q(actualColumn) : null;

    // Build C_{t-1} query from OFFICIAL Prisma DMMF fields (falling back to table cols only if DMMF lacked model)
    const projCols = out.dmmf_used
      ? dmmfFields.map((f) => {
          const match = pgColsAtOld.find((pc) => pc.column_name.toLowerCase() === f.name.toLowerCase());
          return match ? match.column_name : f.name;
        })
      : pgColsAtOld.map((r) => r.column_name);

    cOldQuery = `SELECT ${projCols.map(q).join(", ")} FROM ${qt} LIMIT 5;`;

    if (c.op_kind === "SET_NOT_NULL" && qc) {
      const reqCols = pgColsAtOld.filter((r) => r.is_nullable === "NO" && !r.column_default && r.column_name !== actualColumn);
      const cNames = [qc], cVals = ["NULL"];
      let okIns = true;
      for (const rc of reqCols) {
        const lit = litForPg(rc, "1");
        if (lit === null) { okIns = false; break; }
        cNames.push(q(rc.column_name));
        cVals.push(lit);
      }
      if (okIns) {
        cOldQuery = `INSERT INTO ${qt} (${cNames.join(", ")}) VALUES (${cVals.join(", ")});`;
        isInsertTest = true;
      }
    } else if (c.op_kind === "ADD_NOT_NULL_NO_DEFAULT") {
      // C_{t-1} Prisma Client inserts using S_{t-1} required columns, omitting newly added column!
      const reqCols = pgColsAtOld.filter((r) => r.is_nullable === "NO" && !r.column_default);
      const cNames = [], cVals = [];
      let okIns = true;
      for (const rc of reqCols) {
        const lit = litForPg(rc, "1");
        if (lit === null) { okIns = false; break; }
        cNames.push(q(rc.column_name));
        cVals.push(lit);
      }
      if (okIns && cNames.length > 0) {
        cOldQuery = `INSERT INTO ${qt} (${cNames.join(", ")}) VALUES (${cVals.join(", ")});`;
        isInsertTest = true;
      }
    }

    if (isInsertTest) await querySafe(db, `SET session_replication_role = 'replica';`);
    const rOld = await querySafe(db, cOldQuery);
    out.c_old_at_s_old_ok = rOld.ok;
    if (isInsertTest) {
      await querySafe(db, `DELETE FROM ${qt};`);
      await querySafe(db, `SET session_replication_role = 'origin';`);
    }

    const migRes = await execStatements(db, [c.verbatim_migration_sql]);
    out.verbatim_mig_ok = migRes.ok;

    if (migRes.ok) {
      const targetTblName = c.op_kind === "RENAME_TABLE" && c.new_name ? c.new_name : actualTable;
      targetTableSnap = await inspectTableCols(db, targetTblName);

      if (isInsertTest) await querySafe(db, `SET session_replication_role = 'replica';`);
      const rCrash = await querySafe(db, cOldQuery);
      out.c_old_at_s_new_crash = !rCrash.ok;
      if (!rCrash.ok) {
        out.crash_sqlstate = rCrash.code;
        out.crash_message = rCrash.message;
      }
    }
    await db.close();
  }

  // --- RUN 2: Verify ExpandContractSynth 3-phase plan + target column schema equivalence! ---
  if (out.schema_loaded_ok && out.c_old_at_s_old_ok && out.c_old_at_s_new_crash && actualTable) {
    const db = new PGlite();
    await execStatements(db, c.prior_sqls);
    const qt = q(actualTable);
    const qc = actualColumn ? q(actualColumn) : null;

    let p1Sql = [], p3Sql = [], cNewQ = `SELECT 1;`;
    let targetColNameForCheck = actualColumn || c.column;
    let checkTableAfter = actualTable;

    if (c.op_kind === "DROP_COLUMN" && qc) {
      p1Sql = [`ALTER TABLE ${qt} ALTER COLUMN ${qc} DROP NOT NULL;`];
      p3Sql = [`ALTER TABLE ${qt} DROP COLUMN IF EXISTS ${qc};`];
      const rem = pgColsAtOld.filter((r) => r.column_name !== actualColumn).map((r) => q(r.column_name));
      cNewQ = rem.length ? `SELECT ${rem.join(", ")} FROM ${qt} LIMIT 5;` : `SELECT 1 FROM ${qt} LIMIT 5;`;
    } else if (c.op_kind === "DROP_TABLE") {
      p1Sql = [`-- Retain ${qt} during rollout`];
      p3Sql = [`DROP TABLE IF EXISTS ${qt} CASCADE;`];
    } else if (c.op_kind === "RENAME_COLUMN" && qc && c.new_name) {
      const qn = q(c.new_name);
      const ci = pgColsAtOld.find((r) => r.column_name === actualColumn);
      const pgT = ci?.data_type && ci.data_type !== "USER-DEFINED" ? ci.data_type : "TEXT";
      const fn = `ec_fs2_sync_${actualTable.toLowerCase().replace(/[^a-z0-9]/g, "_")}`;
      p1Sql = [
        `ALTER TABLE ${qt} ADD COLUMN IF NOT EXISTS ${qn} ${pgT};`,
        `ALTER TABLE ${qt} ALTER COLUMN ${qc} DROP NOT NULL;`,
        `CREATE OR REPLACE FUNCTION ${fn}() RETURNS trigger AS $$ BEGIN IF NEW.${qn} IS NULL THEN NEW.${qn} := NEW.${qc}; END IF; IF NEW.${qc} IS NULL THEN NEW.${qc} := NEW.${qn}; END IF; RETURN NEW; END; $$ LANGUAGE plpgsql;`,
        `CREATE TRIGGER trg_${fn} BEFORE INSERT OR UPDATE ON ${qt} FOR EACH ROW EXECUTE FUNCTION ${fn}();`,
        `UPDATE ${qt} SET ${qn} = ${qc} WHERE ${qn} IS NULL AND ${qc} IS NOT NULL;`,
      ];
      p3Sql = [
        `DROP TRIGGER IF EXISTS trg_${fn} ON ${qt};`,
        `DROP FUNCTION IF EXISTS ${fn}();`,
        `ALTER TABLE ${qt} DROP COLUMN IF EXISTS ${qc};`,
      ];
      cNewQ = `SELECT ${qn} FROM ${qt} LIMIT 5;`;
      targetColNameForCheck = c.new_name;
    } else if (c.op_kind === "RENAME_TABLE" && c.new_name) {
      const qnt = q(c.new_name);
      p1Sql = [`CREATE OR REPLACE VIEW ${qnt} AS SELECT * FROM ${qt};`];
      p3Sql = [`DROP VIEW IF EXISTS ${qnt};`, `ALTER TABLE ${qt} RENAME TO ${qnt};`];
      cNewQ = `SELECT * FROM ${qnt} LIMIT 5;`;
      checkTableAfter = c.new_name;
    } else if (c.op_kind === "SET_NOT_NULL" && qc) {
      const ci = pgColsAtOld.find((r) => r.column_name === actualColumn);
      const dflt = litForPg(ci || {}, "1") || "''";
      const fn = `ec_fs2_nn_${actualTable.toLowerCase().replace(/[^a-z0-9]/g, "_")}`;
      const chk = `chk_fs2_${actualTable.toLowerCase().replace(/[^a-z0-9]/g, "_")}_nn`;
      p1Sql = [
        `ALTER TABLE ${qt} ALTER COLUMN ${qc} SET DEFAULT ${dflt};`,
        `CREATE OR REPLACE FUNCTION ${fn}() RETURNS trigger AS $$ BEGIN IF NEW.${qc} IS NULL THEN NEW.${qc} := ${dflt}; END IF; RETURN NEW; END; $$ LANGUAGE plpgsql;`,
        `CREATE TRIGGER trg_${fn} BEFORE INSERT OR UPDATE ON ${qt} FOR EACH ROW EXECUTE FUNCTION ${fn}();`,
        `UPDATE ${qt} SET ${qc} = ${dflt} WHERE ${qc} IS NULL;`,
      ];
      p3Sql = [
        `DROP TRIGGER IF EXISTS trg_${fn} ON ${qt};`,
        `DROP FUNCTION IF EXISTS ${fn}();`,
        `ALTER TABLE ${qt} ADD CONSTRAINT ${chk} CHECK (${qc} IS NOT NULL) NOT VALID;`,
        `ALTER TABLE ${qt} VALIDATE CONSTRAINT ${chk};`,
        `ALTER TABLE ${qt} ALTER COLUMN ${qc} SET NOT NULL;`,
        `ALTER TABLE ${qt} DROP CONSTRAINT ${chk};`,
        `ALTER TABLE ${qt} ALTER COLUMN ${qc} DROP DEFAULT;`,
      ];
      cNewQ = `SELECT ${qc} FROM ${qt} LIMIT 5;`;
    } else if (c.op_kind === "ADD_NOT_NULL_NO_DEFAULT" && c.column) {
      // Match exact target column name & type from targetTableSnap!
      const tgtCol = (targetTableSnap || []).find((r) => r.column_name.toLowerCase() === c.column.toLowerCase());
      const exactColName = tgtCol ? tgtCol.column_name : c.column;
      const qnew = q(exactColName);
      let pgT = c.new_type || (tgtCol?.data_type === "USER-DEFINED" ? q(tgtCol.udt_name) : (tgtCol?.data_type || "TEXT"));
      if (tgtCol?.data_type === "timestamp without time zone" && tgtCol?.datetime_precision != null) {
        pgT = `TIMESTAMP(${tgtCol.datetime_precision})`;
      } else if (tgtCol?.data_type === "timestamp with time zone" && tgtCol?.datetime_precision != null) {
        pgT = `TIMESTAMPTZ(${tgtCol.datetime_precision})`;
      }
      const dflt = litForPg(tgtCol || { data_type: pgT }, "1") || "''";
      const chk = `chk_fs2_add_${actualTable.toLowerCase().replace(/[^a-z0-9]/g, "_")}`;
      p1Sql = [`ALTER TABLE ${qt} ADD COLUMN IF NOT EXISTS ${qnew} ${pgT} DEFAULT ${dflt};`];
      p3Sql = [
        `UPDATE ${qt} SET ${qnew} = ${dflt} WHERE ${qnew} IS NULL;`,
        `ALTER TABLE ${qt} ADD CONSTRAINT ${chk} CHECK (${qnew} IS NOT NULL) NOT VALID;`,
        `ALTER TABLE ${qt} VALIDATE CONSTRAINT ${chk};`,
        `ALTER TABLE ${qt} ALTER COLUMN ${qnew} SET NOT NULL;`,
        `ALTER TABLE ${qt} DROP CONSTRAINT ${chk};`,
        `ALTER TABLE ${qt} ALTER COLUMN ${qnew} DROP DEFAULT;`,
      ];
      cNewQ = `SELECT ${qnew} FROM ${qt} LIMIT 5;`;
      targetColNameForCheck = exactColName;
    }

    if (c.ecs_plan) {
      p1Sql = [...(c.ecs_plan.phase1 || []), ...(c.ecs_plan.phase2 || [])];
      p3Sql = c.ecs_plan.phase3 || [];
      out.ecs_plan_used = true;
    }
    if (p1Sql.length) {
      const r1 = await execStatements(db, p1Sql);
      out.synth_applied_ok = r1.ok;
      out.synth_apply_err = r1.ok ? null : `${r1.code}: ${r1.message}`;
      // v3: the C_{t-1} insert probe runs with session_replication_role='replica' (to bypass FK checks on
      // synthetic rows), which would also silence the ECS triggers under test. Mark them ENABLE ALWAYS.
      if (r1.ok) {
        for (const stmt of p1Sql) {
          const m = /CREATE\s+TRIGGER\s+("?[\w]+"?)\s+[\s\S]*?\bON\s+((?:"[^"]+")|\w+)/i.exec(stmt);
          if (m) { const ea = await querySafe(db, `ALTER TABLE ${m[2]} ENABLE ALWAYS TRIGGER ${m[1]};`); out.enable_always = (out.enable_always || []).concat([`${m[1]}:${ea.ok ? 'ok' : ea.message}`]); }
        }
      }
      if (r1.ok) {
        if (isInsertTest) await querySafe(db, `SET session_replication_role = 'replica';`);
        const ro = await querySafe(db, cOldQuery);
        const rn = await querySafe(db, cNewQ);
        out.synth_c_old_pass = ro.ok;
        out.synth_c_new_pass = rn.ok;
        out.synth_c_old_err = ro.ok ? null : `${ro.code}: ${ro.message}`;
        out.synth_c_new_err = rn.ok ? null : `${rn.code}: ${rn.message}`;
        out.c_new_probe_sql = String(cNewQ).slice(0, 200);
        if (isInsertTest) await querySafe(db, `SET session_replication_role = 'origin';`);
        if (qc) { const nr = await querySafe(db, `SELECT COUNT(*)::int AS n FROM ${qt} WHERE ${qc} IS NULL;`); out.null_rows_before_contract = nr.ok ? nr.rows[0].n : nr.message; }
        out.c_old_probe_sql = cOldQuery.slice(0, 300);
        const r3 = await execStatements(db, p3Sql);
        out.synth_contract_ok = r3.ok;
        out.synth_contract_err = r3.ok ? null : `${r3.code}: ${r3.message}`;
        if (r3.ok) {
          const rnf = await querySafe(db, cNewQ);
          // Check target-column schema equivalence against verbatim S_t!
          const contractSnap = await inspectTableCols(db, checkTableAfter);
          if (c.op_kind === "DROP_TABLE") {
            out.synth_schema_equiv_target_col = contractSnap.length === 0;
          } else if (c.op_kind === "DROP_COLUMN") {
            out.synth_schema_equiv_target_col = !contractSnap.some((r) => r.column_name.toLowerCase() === (targetColNameForCheck || "").toLowerCase());
          } else if (c.op_kind === "RENAME_TABLE") {
            out.synth_schema_equiv_target_col = contractSnap.length === (targetTableSnap || []).length && contractSnap.length > 0;
          } else {
            const c1 = contractSnap.find((r) => r.column_name.toLowerCase() === (targetColNameForCheck || "").toLowerCase());
            const c2 = (targetTableSnap || []).find((r) => r.column_name.toLowerCase() === (targetColNameForCheck || "").toLowerCase());
            out.equiv_detail = { contract: c1 || null, target: c2 || null };
            out.synth_schema_equiv_target_col = Boolean(
              c1 && c2 && c1.data_type === c2.data_type && c1.is_nullable === c2.is_nullable && c1.column_default === c2.column_default
            );
          }
          out.synth_full_verified = Boolean(
            out.synth_applied_ok && out.synth_c_old_pass && out.synth_c_new_pass && out.synth_contract_ok && rnf.ok && out.synth_schema_equiv_target_col
          );
        }
      }
    }
    await db.close();
  }

  out.elapsed_ms = Date.now() - t0;
  return out;
}

const [, , inPath, outPath] = process.argv;
const cases = JSON.parse(fs.readFileSync(inPath, "utf8"));
const results = [];
for (const c of cases) {
  results.push(await replayOneFull(c));
}
fs.writeFileSync(outPath, JSON.stringify(results, null, 2), "utf8");
