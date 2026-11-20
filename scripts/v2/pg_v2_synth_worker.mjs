/**
 * V2 Phase 4 (RQ4): ExpandContractSynth Execution + EXACT SCHEMA EQUIVALENCE Verifier
 * on PostgreSQL 18.7 (PGlite).
 *
 * Fixes ALL v1 RQ4 reviewer critiques:
 *   1. Verifies BOTH concurrent dual-version execution (C_{t-1}@S_expand & C_t@S_expand)
 *      AND exact final schema equivalence (S_contract == S_target via information_schema.columns:
 *      column_name, data_type, is_nullable, column_default)!
 *   2. Preserves exact PostgreSQL types (e.g. TIMESTAMP(3), VARCHAR, BIGINT) and verifies
 *      temporary Phase-1 DEFAULTs are dropped in Phase 3.
 *   3. Explicitly verifies Listing 1 (`papermark/papermark@51a67827` multi-op migration)
 *      for exact S_contract == S_target equivalence.
 *
 * Usage: node pg_v2_synth_worker.mjs <in_cases.json> <out_results.json>
 */
import fs from "node:fs";
import path from "node:path";

const __here = path.dirname(new URL(import.meta.url).pathname).replace(/^\/([A-Za-z]:)/, "$1");
const toolsDir = process.env.ECV_TOOLS_DIR
  ? path.resolve(process.env.ECV_TOOLS_DIR)
  : fs.existsSync(path.resolve(__here, "../tools/node_modules/@electric-sql/pglite"))
    ? path.resolve(__here, "../tools")
    : path.resolve(__here, "../..");
const { PGlite } = await import(
  "file:///" + path.join(toolsDir, "node_modules/@electric-sql/pglite/dist/index.js").replace(/\\/g, "/")
);

const q = (id) => `"${String(id).replace(/"/g, '""')}"`;

function pgType(raw) {
  if (!raw) return "TEXT";
  const r = String(raw).trim();
  const s = r.toLowerCase();
  const exactPrefixes = [
    "timestamp", "timestamptz", "varchar", "character varying", "bigint", "smallint",
    "integer", "numeric", "decimal", "double precision", "real", "boolean", "jsonb", "uuid", "text",
  ];
  if ((s === "date" || exactPrefixes.some((p) => s.startsWith(p))) && !s.includes("field")) return r.toUpperCase();
  if (s.includes("bigint")) return "BIGINT";
  if (s.includes("int") || s.includes("serial")) return "INTEGER";
  if (s.includes("bool")) return "BOOLEAN";
  if (s.includes("float") || s.includes("double") || s.includes("dec") || s.includes("num")) return "DOUBLE PRECISION";
  if (s.includes("json")) return "JSONB";
  if (s.includes("datetime")) return "TIMESTAMP(3)";
  if (s.includes("time") || s.includes("date")) return "TIMESTAMPTZ";
  if (s.includes("uuid")) return "UUID";
  return "TEXT";
}

function sampleVal(pgT, tag = "v1") {
  const t = pgT.toUpperCase();
  if (t.includes("INT")) return tag === "v2" ? "42" : "7";
  if (t.includes("DOUBLE") || t.includes("NUMERIC") || t.includes("DECIMAL") || t.includes("REAL")) return tag === "v2" ? "9.5" : "3.14";
  if (t.includes("BOOL")) return tag === "v2" ? "FALSE" : "TRUE";
  if (t.includes("JSON")) return tag === "v2" ? `'{"v":2}'::jsonb` : `'{"v":1}'::jsonb`;
  if (t.includes("TIME") || t.includes("DATE")) return "NOW()";
  if (t.includes("UUID")) return tag === "v2" ? "'22222222-2222-2222-2222-222222222222'::uuid" : "'11111111-1111-1111-1111-111111111111'::uuid";
  return `'val_${tag}'`;
}

async function execSafe(db, sqlList) {
  let executed = 0;
  for (const sql of sqlList) {
    const trimmed = (sql || "").trim();
    const nonComment = trimmed.split("\n").map((l) => l.trim()).filter((l) => l && !l.startsWith("--")).join("\n");
    if (!nonComment) continue;
    try {
      await db.exec(trimmed);
      executed += 1;
    } catch (err) {
      return { ok: false, executed, code: err.code || "PG_ERR", message: String(err.message || err).slice(0, 200) };
    }
  }
  return { ok: true, executed, code: null, message: null };
}

async function querySafe(db, sql) {
  try {
    const res = await db.query(sql);
    return { ok: true, rows: res.rows ?? [], code: null, message: null };
  } catch (err) {
    return { ok: false, rows: [], code: err.code || "PG_ERR", message: String(err.message || err).slice(0, 200) };
  }
}

async function runWorkload(db, stmts) {
  for (const s of stmts) {
    const r = await querySafe(db, s);
    if (!r.ok) return r;
  }
  return { ok: true, code: null, message: null };
}

async function snapshotSchema(db, schemaName) {
  const res = await querySafe(
    db,
    `SELECT table_name, column_name, data_type, udt_name, datetime_precision, is_nullable, column_default
     FROM information_schema.columns
     WHERE table_schema = '${schemaName}'
     ORDER BY table_name, column_name;`
  );
  return (res.rows || []).map((r) => ({
    table: r.table_name,
    col: r.column_name,
    type: r.data_type,
    udt: r.udt_name,
    dt_prec: r.datetime_precision,
    nullable: r.is_nullable,
    // Normalize serial sequence schema prefix if present
    default: r.column_default ? String(r.column_default).replace(new RegExp(`"${schemaName}"\\.`, "g"), "").replace(new RegExp(`${schemaName}\\.`, "g"), "") : null,
  }));
}

async function withSchema(db, name, fn) {
  await db.exec(`DROP SCHEMA IF EXISTS "${name}" CASCADE; CREATE SCHEMA "${name}"; SET search_path TO "${name}";`);
  try {
    return await fn();
  } finally {
    await db.exec(`SET search_path TO public; DROP SCHEMA IF EXISTS "${name}" CASCADE;`).catch(() => {});
  }
}

async function verifyOneSynth(db, c, idx) {
  const tbl = c.table || "items";
  const col = c.column || "target_col";
  const newName = c.new_name || `${col}_v2`;
  const kind = c.op_kind;
  const oldT = pgType(c.old_type);
  const newT = pgType(c.new_type || "INTEGER");
  const qt = q(tbl);
  const qc = q(col);
  const qn = q(newName);
  const plan = c.synth_plan || {};

  let initSql = [];
  let targetMigSql = [];
  let cOldW = [];
  let cNewW = [];

  if (kind === "DROP_COLUMN") {
    initSql = [
      `CREATE TABLE ${qt} (id SERIAL PRIMARY KEY, payload TEXT NOT NULL, ${qc} ${oldT});`,
      `INSERT INTO ${qt} (payload, ${qc}) VALUES ('seed', ${sampleVal(oldT, "v1")});`,
    ];
    targetMigSql = [`ALTER TABLE ${qt} DROP COLUMN ${qc};`];
    cOldW = [`INSERT INTO ${qt} (payload, ${qc}) VALUES ('old', ${sampleVal(oldT, "v1")});`, `SELECT id, ${qc} FROM ${qt} LIMIT 5;`];
    cNewW = [`INSERT INTO ${qt} (payload) VALUES ('new');`, `SELECT id, payload FROM ${qt} LIMIT 5;`];
  } else if (kind === "DROP_TABLE") {
    initSql = [
      `CREATE TABLE ${qt} (id SERIAL PRIMARY KEY, payload TEXT NOT NULL);`,
      `INSERT INTO ${qt} (payload) VALUES ('seed');`,
      `CREATE TABLE "ec_keep" (id SERIAL PRIMARY KEY, name TEXT);`,
    ];
    targetMigSql = [`DROP TABLE ${qt};`];
    cOldW = [`INSERT INTO ${qt} (payload) VALUES ('old');`, `SELECT id FROM ${qt} LIMIT 5;`];
    cNewW = [`SELECT id FROM "ec_keep" LIMIT 1;`];
  } else if (kind === "RENAME_COLUMN") {
    initSql = [
      `CREATE TABLE ${qt} (id SERIAL PRIMARY KEY, payload TEXT NOT NULL, ${qc} ${oldT});`,
      `INSERT INTO ${qt} (payload, ${qc}) VALUES ('seed', ${sampleVal(oldT, "v1")});`,
    ];
    targetMigSql = [`ALTER TABLE ${qt} RENAME COLUMN ${qc} TO ${qn};`];
    cOldW = [`INSERT INTO ${qt} (payload, ${qc}) VALUES ('old', ${sampleVal(oldT, "v1")});`, `SELECT id, ${qc} FROM ${qt} LIMIT 5;`];
    cNewW = [`INSERT INTO ${qt} (payload, ${qn}) VALUES ('new', ${sampleVal(oldT, "v2")});`, `SELECT id, ${qn} FROM ${qt} LIMIT 5;`];
  } else if (kind === "RENAME_TABLE") {
    const qnt = q(newName);
    initSql = [`CREATE TABLE ${qt} (id SERIAL PRIMARY KEY, payload TEXT NOT NULL);`, `INSERT INTO ${qt} (payload) VALUES ('seed');`];
    targetMigSql = [`ALTER TABLE ${qt} RENAME TO ${qnt};`];
    cOldW = [`INSERT INTO ${qt} (payload) VALUES ('old');`, `SELECT id FROM ${qt} LIMIT 5;`];
    cNewW = [`INSERT INTO ${qnt} (payload) VALUES ('new');`, `SELECT id FROM ${qnt} LIMIT 5;`];
  } else if (kind === "ADD_NOT_NULL_NO_DEFAULT") {
    initSql = [`CREATE TABLE ${qt} (id SERIAL PRIMARY KEY, payload TEXT NOT NULL);`, `INSERT INTO ${qt} (payload) VALUES ('seed');`];
    targetMigSql = [
      `ALTER TABLE ${qt} ADD COLUMN ${qc} ${newT} DEFAULT ${sampleVal(newT, "v1")} NOT NULL;`,
      `ALTER TABLE ${qt} ALTER COLUMN ${qc} DROP DEFAULT;`,
    ];
    cOldW = [`INSERT INTO ${qt} (payload) VALUES ('old');`];
    cNewW = [`INSERT INTO ${qt} (payload, ${qc}) VALUES ('new', ${sampleVal(newT, "v2")});`, `SELECT id, ${qc} FROM ${qt} LIMIT 5;`];
  } else if (kind === "SET_NOT_NULL") {
    initSql = [
      `CREATE TABLE ${qt} (id SERIAL PRIMARY KEY, payload TEXT NOT NULL, ${qc} ${oldT});`,
      `INSERT INTO ${qt} (payload, ${qc}) VALUES ('seed', ${sampleVal(oldT, "v1")});`,
    ];
    targetMigSql = [`ALTER TABLE ${qt} ALTER COLUMN ${qc} SET NOT NULL;`];
    cOldW = [`INSERT INTO ${qt} (payload, ${qc}) VALUES ('old', NULL);`, `SELECT id, ${qc} FROM ${qt} LIMIT 5;`];
    cNewW = [`INSERT INTO ${qt} (payload, ${qc}) VALUES ('new', ${sampleVal(oldT, "v2")});`, `SELECT id, ${qc} FROM ${qt} LIMIT 5;`];
  } else if (kind === "ALTER_COLUMN_TYPE") {
    const dstT = newT === "TEXT" ? "INTEGER" : newT;
    initSql = [
      `CREATE TABLE ${qt} (id SERIAL PRIMARY KEY, payload TEXT NOT NULL, ${qc} TEXT);`,
      `INSERT INTO ${qt} (payload, ${qc}) VALUES ('seed', '100');`,
    ];
    targetMigSql = [`ALTER TABLE ${qt} ALTER COLUMN ${qc} TYPE ${dstT} USING ${qc}::${dstT};`];
    cOldW = [`INSERT INTO ${qt} (payload, ${qc}) VALUES ('old', '200');`];
    cNewW = [`INSERT INTO ${qt} (payload, ${qc}) VALUES ('new', ${sampleVal(dstT, "v2")});`, `SELECT id, ${qc} FROM ${qt} LIMIT 5;`];
  } else if (kind === "RENAME_TYPE_ENUM") {
    const qeOld = q(tbl);
    initSql = [
      `CREATE TYPE ${qeOld} AS ENUM ('A', 'B');`,
      `CREATE TABLE "ec_enum_tbl" (id SERIAL PRIMARY KEY, status ${qeOld} NOT NULL);`,
      `INSERT INTO "ec_enum_tbl" (status) VALUES ('A'::${qeOld});`,
    ];
    targetMigSql = [`ALTER TYPE ${qeOld} ADD VALUE IF NOT EXISTS 'C';`];
    cOldW = [`INSERT INTO "ec_enum_tbl" (status) VALUES ('B'::${qeOld});`];
    cNewW = [`INSERT INTO "ec_enum_tbl" (status) VALUES ('A'::${qeOld});`];
  } else if (kind === "ADD_UNIQUE_OR_CHECK") {
    initSql = [
      `CREATE TABLE ${qt} (id SERIAL PRIMARY KEY, payload TEXT NOT NULL, val INTEGER);`,
      `INSERT INTO ${qt} (payload, val) VALUES ('seed', 10);`,
    ];
    targetMigSql = [`ALTER TABLE ${qt} ADD CONSTRAINT "ec_chk_pos" CHECK (val > 0);`];
    cOldW = [`INSERT INTO ${qt} (payload, val) VALUES ('old', -1);`];
    cNewW = [`INSERT INTO ${qt} (payload, val) VALUES ('new', 20);`];
  }

  const out = {
    id: c.id,
    op_kind: kind,
    advisory_only: plan.executable_sql === false,
    executed_stmts: 0,
    phase1_ok: false,
    phase1_err: null,
    c_old_expand_ok: false,
    c_new_expand_ok: false,
    dual_write_consistent: false,
    phase3_ok: false,
    phase3_err: null,
    c_new_contract_ok: false,
    schema_equiv_to_target: false,
    schema_diff_note: null,
    verified_pass: false,
  };
  if (!plan.supported || !initSql.length || plan.executable_sql === false) return out;

  // 1. Build reference target schema S_t
  let targetSnap = [];
  await withSchema(db, `v2_ref_${idx}`, async () => {
    await execSafe(db, initSql);
    await execSafe(db, targetMigSql);
    targetSnap = await snapshotSchema(db, `v2_ref_${idx}`);
  });

  // 2. Execute 3-phase ExpandContractSynth plan & compare final S_contract against S_t!
  await withSchema(db, `v2_syn_${idx}`, async () => {
    await execSafe(db, initSql);
    const p1 = await execSafe(db, [...(plan.phase1_expand_sql || []), ...(plan.phase2_backfill_sql || [])]);
    out.phase1_ok = p1.ok;
    out.phase1_err = p1.ok ? null : `${p1.code}: ${p1.message}`;
    out.executed_stmts += p1.executed;
    if (p1.ok) {
      const ro = await runWorkload(db, cOldW);
      out.c_old_expand_ok = ro.ok;
      let cNewExp = cNewW;
      if (kind === "ALTER_COLUMN_TYPE") {
        const sh = q(`${col}__ec_v2`);
        const dstT = newT === "TEXT" ? "INTEGER" : newT;
        cNewExp = [`INSERT INTO ${qt} (payload, ${sh}) VALUES ('new', ${sampleVal(dstT, "v2")});`, `SELECT id, ${sh} FROM ${qt} LIMIT 5;`];
      }
      const rn = await runWorkload(db, cNewExp);
      out.c_new_expand_ok = rn.ok;

      if (kind === "RENAME_COLUMN" && ro.ok && rn.ok) {
        const chk = await querySafe(db, `SELECT COUNT(*)::int AS mm FROM ${qt} WHERE ${qc} IS DISTINCT FROM ${qn};`);
        out.dual_write_consistent = chk.ok && Number(chk.rows?.[0]?.mm) === 0;
      } else {
        out.dual_write_consistent = ro.ok && rn.ok;
      }

      const p3 = await execSafe(db, plan.phase3_contract_sql || []);
      out.phase3_ok = p3.ok;
      out.phase3_err = p3.ok ? null : `${p3.code}: ${p3.message}`;
      out.executed_stmts += p3.executed;
      if (p3.ok) {
        const rnf = await runWorkload(db, cNewW);
        out.c_new_contract_ok = rnf.ok;
        const contractSnap = await snapshotSchema(db, `v2_syn_${idx}`);
        const eq = JSON.stringify(contractSnap) === JSON.stringify(targetSnap);
        out.schema_equiv_to_target = eq;
        if (!eq) {
          out.schema_diff_note = `contract=${JSON.stringify(contractSnap).slice(0, 140)} vs target=${JSON.stringify(targetSnap).slice(0, 140)}`;
        }
      }
    }
  });

  out.verified_pass = Boolean(
    !out.advisory_only && out.executed_stmts > 0 && out.phase1_ok && out.c_old_expand_ok && out.c_new_expand_ok && out.dual_write_consistent && out.phase3_ok && out.c_new_contract_ok && out.schema_equiv_to_target
  );
  return out;
}

// Explicit end-to-end verification of FIXED Listing 1 (`papermark@51a67827`) on PostgreSQL 18.7!
async function verifyListing1Papermark(db) {
  const initChat = [
    `CREATE TABLE "Chat" ("id" TEXT PRIMARY KEY, "threadId" TEXT NOT NULL);`,
    `INSERT INTO "Chat" ("id", "threadId") VALUES ('c1', 'th_1');`,
  ];
  const verbatimTarget = [
    `ALTER TABLE "Chat" DROP COLUMN "threadId", ADD COLUMN "teamId" TEXT NOT NULL DEFAULT 'tmp', ADD COLUMN "updatedAt" TIMESTAMP(3) NOT NULL DEFAULT NOW();`,
    `ALTER TABLE "Chat" ALTER COLUMN "teamId" DROP DEFAULT, ALTER COLUMN "updatedAt" DROP DEFAULT;`,
  ];
  const synthPhase12 = [
    `ALTER TABLE "Chat" ALTER COLUMN "threadId" DROP NOT NULL;`,
    `ALTER TABLE "Chat" ADD COLUMN IF NOT EXISTS "teamId" TEXT DEFAULT '';`,
    `ALTER TABLE "Chat" ADD COLUMN IF NOT EXISTS "updatedAt" TIMESTAMP(3) DEFAULT NOW();`,
    `UPDATE "Chat" SET "teamId" = '' WHERE "teamId" IS NULL;`,
    `UPDATE "Chat" SET "updatedAt" = NOW() WHERE "updatedAt" IS NULL;`,
  ];
  const synthPhase3 = [
    `ALTER TABLE "Chat" ADD CONSTRAINT chk_chat_teamid_nn CHECK ("teamId" IS NOT NULL) NOT VALID;`,
    `ALTER TABLE "Chat" VALIDATE CONSTRAINT chk_chat_teamid_nn;`,
    `ALTER TABLE "Chat" ALTER COLUMN "teamId" SET NOT NULL;`,
    `ALTER TABLE "Chat" DROP CONSTRAINT chk_chat_teamid_nn;`,
    `ALTER TABLE "Chat" ALTER COLUMN "teamId" DROP DEFAULT;`,
    `ALTER TABLE "Chat" ADD CONSTRAINT chk_chat_updatedat_nn CHECK ("updatedAt" IS NOT NULL) NOT VALID;`,
    `ALTER TABLE "Chat" VALIDATE CONSTRAINT chk_chat_updatedat_nn;`,
    `ALTER TABLE "Chat" ALTER COLUMN "updatedAt" SET NOT NULL;`,
    `ALTER TABLE "Chat" DROP CONSTRAINT chk_chat_updatedat_nn;`,
    `ALTER TABLE "Chat" ALTER COLUMN "updatedAt" DROP DEFAULT;`,
    `ALTER TABLE "Chat" DROP COLUMN IF EXISTS "threadId";`,
  ];
  let refSnap = [], synSnap = [], cOldOk = false, cNewOk = false, cNewFinalOk = false;
  await withSchema(db, "l1_ref", async () => {
    await execSafe(db, initChat);
    await execSafe(db, verbatimTarget);
    refSnap = await snapshotSchema(db, "l1_ref");
  });
  await withSchema(db, "l1_syn", async () => {
    await execSafe(db, initChat);
    const p1 = await execSafe(db, synthPhase12);
    if (p1.ok) {
      cOldOk = (await runWorkload(db, [`INSERT INTO "Chat" ("id", "threadId") VALUES ('c2', 'th_2');`, `SELECT "id", "threadId" FROM "Chat";`])).ok;
      cNewOk = (await runWorkload(db, [`INSERT INTO "Chat" ("id", "teamId", "updatedAt") VALUES ('c3', 't1', NOW());`, `SELECT "id", "teamId", "updatedAt" FROM "Chat";`])).ok;
      const p3 = await execSafe(db, synthPhase3);
      if (p3.ok) {
        cNewFinalOk = (await runWorkload(db, [`INSERT INTO "Chat" ("id", "teamId", "updatedAt") VALUES ('c4', 't2', NOW());`])).ok;
        synSnap = await snapshotSchema(db, "l1_syn");
      }
    }
  });
  return {
    c_old_expand_ok: cOldOk,
    c_new_expand_ok: cNewOk,
    c_new_contract_ok: cNewFinalOk,
    schema_exact_match_S_t: JSON.stringify(refSnap) === JSON.stringify(synSnap),
    ref_schema: refSnap,
    contract_schema: synSnap,
  };
}

const [, , inPath, outPath] = process.argv;
const cases = JSON.parse(fs.readFileSync(inPath, "utf8"));
const db = new PGlite();
const results = [];
for (let i = 0; i < cases.length; i++) {
  results.push(await verifyOneSynth(db, cases[i], i));
}
const listing1 = await verifyListing1Papermark(db);
await db.close();
fs.writeFileSync(outPath, JSON.stringify({ results, listing1_papermark_check: listing1 }, null, 2), "utf8");
