"""v3 RQ4 (B): full-schema Prisma replay that executes the *real* ExpandContractSynth plan.

Same candidate selection and DMMF-driven C_{t-1} queries as v2, but (1) candidates come from the v3
added-only pool, and (2) each candidate carries the Python synthesizer's plan (with original-case
identifiers), which the v3 worker executes verbatim. Outputs research/v3/full_schema_v3_results.json
and research/v3/rq4_full_schema_v3_summary.json. Progress: research/v3/v3_full_schema.log.
"""
from __future__ import annotations
import concurrent.futures, json, subprocess, sys
from collections import Counter, defaultdict
from pathlib import Path

TASK = Path(__file__).resolve().parent.parent.parent
R = TASK / "research"; V3 = R / "v3"
sys.path[:0] = [str(R), str(R / "phase1"), str(R / "v2"), str(TASK.parent.parent / "scripts")]
from progress import Progress  # noqa: E402
from ecv.ddl_parser import parse_migration  # noqa: E402
from ecv.synthesizer import synthesize_expand_contract  # noqa: E402
from ecv.orm_parser import parse_prisma_schema  # noqa: E402

PRISMA_PG = {"int": "INTEGER", "bigint": "BIGINT", "string": "TEXT", "boolean": "BOOLEAN", "datetime": "TIMESTAMP(3)",
             "float": "DOUBLE PRECISION", "decimal": "NUMERIC", "json": "JSONB", "bytes": "BYTEA"}
from run_v2_synth_and_full_schema import fetch_full_schema_repo, TARGET_FS_KINDS, CPU_WORKERS  # noqa: E402


def old_type_from_schema(cand: dict) -> str:
    """C_{t-1} column type as ECV reads it from schema.prisma (what the tool would know in CI)."""
    snap = parse_prisma_schema(cand.get("prisma_schemas_old") or {})
    m = snap.find_model(cand["table"])
    f = m.fields.get(cand["column"]) if (m and cand.get("column")) else None
    if not f:
        return "TEXT"
    return PRISMA_PG.get((f.field_type or "").lower().rstrip("?[]"), f.field_type or "TEXT")


def ecs_plan_for(cand: dict) -> dict | None:
    old_t = old_type_from_schema(cand)
    for o in parse_migration("prisma", cand["verbatim_migration_sql"], cand["mig_file"]):
        if (o.is_destructive and o.kind == cand["op_kind"] and (o.table or "") == (cand["table"] or "")
                and (o.column or "") == (cand["column"] or "")):
            p = synthesize_expand_contract(o, old_pg_type=old_t, new_pg_type=o.new_type)
            # The harness probes C_t with these names, so give it the original-case SQL identifiers too.
            cand["column"] = o.extra.get("column_sql") or cand["column"]
            cand["new_name"] = o.extra.get("new_name_sql") or cand["new_name"]
            return {"phase1": p.phase1_expand_sql, "phase2": p.phase2_backfill_sql, "phase3": p.phase3_contract_sql}
    return None


def main() -> None:
    pool = json.loads((V3 / "added_only_pool.json").read_text(encoding="utf-8"))["prisma"]
    by_repo_pool = defaultdict(list)
    for r in pool:
        if r["ecv_classification"].startswith("DUAL_VERSION_VIOLATION") and r["op"]["kind"] in TARGET_FS_KINDS and r.get("raw_sql"):
            by_repo_pool[r["repo"]].append(r)
    by_repo_fs, fid = {}, 0
    for repo, recs in sorted(by_repo_pool.items()):
        picked, used = [], set()
        for r in sorted(recs, key=lambda x: 0 if x["op"]["kind"] in ("ADD_NOT_NULL_NO_DEFAULT", "SET_NOT_NULL", "RENAME_COLUMN", "RENAME_TABLE") else 1):
            k = r["op"]["kind"]
            if k in used and len(picked) >= 2:
                continue
            fid += 1; used.add(k)
            cand = {"id": f"fs3_{fid}", "repo": repo, "sha": r["sha"], "parent": r["parent"], "mig_file": r["file_path"],
                    "op_kind": k, "table": r["op"].get("table"), "column": r["op"].get("column"),
                    "new_name": r["op"].get("new_name"), "new_type": r["op"].get("new_type"), "verbatim_migration_sql": r["raw_sql"]}
            picked.append(cand)
            if len(picked) >= 4:
                break
        if picked:
            by_repo_fs[repo] = picked
    log = open(V3 / "v3_full_schema.log", "w", encoding="utf-8")
    p = Progress(total=len(by_repo_fs), label="v3-fetch-full-schema-prisma", stream=log)
    fs_all = []
    with concurrent.futures.ProcessPoolExecutor(max_workers=CPU_WORKERS) as ex:
        futs = {ex.submit(fetch_full_schema_repo, it): it[0] for it in by_repo_fs.items()}
        for fut in concurrent.futures.as_completed(futs):
            fs_all.extend(fut.result()); p.step(note=f"{futs[fut]} built={len(fs_all)}")
    p.done()
    for cand in fs_all:
        cand["ecs_plan"] = ecs_plan_for(cand)
    n = min(CPU_WORKERS, max(1, len(fs_all)))
    sh = V3 / "fs_shards"; sh.mkdir(exist_ok=True)
    cwd = TASK.parent.parent  # worker resolves tools/ relative to the workspace root
    procs = []
    for i in range(n):
        (sh / f"in_{i}.json").write_text(json.dumps(fs_all[i::n]), encoding="utf-8")
        procs.append(subprocess.Popen(["node", str(V3 / "pg_v3_full_schema_worker.mjs"), str(sh / f"in_{i}.json"), str(sh / f"out_{i}.json")], cwd=cwd))
    p2 = Progress(total=n, label="v3-full-schema-replay", stream=log)
    res = []
    for i, pr in enumerate(procs):
        pr.wait(timeout=600)
        if (sh / f"out_{i}.json").exists():
            res.extend(json.loads((sh / f"out_{i}.json").read_text(encoding="utf-8")))
        p2.step(note=f"shard {i}")
    p2.done()
    crashed = [r for r in res if r.get("c_old_at_s_new_crash")]
    ok = [r for r in crashed if r.get("synth_full_verified")]
    summ = {
        "candidates_attempted": len(fs_all), "histories_loaded": sum(bool(r.get("schema_loaded_ok")) for r in res),
        "crashes_reproduced": len(crashed), "distinct_repos_crashed": len({r["repo"] for r in crashed}),
        "ecs_plan_missing": sum(1 for r in crashed if not r.get("ecs_plan_used")),
        "synth_verified_with_schema_equiv": len(ok),
        "pass_rate_pct": round(100 * len(ok) / len(crashed), 1) if crashed else None,
        "by_op_crashed": dict(Counter(r["op_kind"] for r in crashed)), "by_op_verified": dict(Counter(r["op_kind"] for r in ok)),
        "sqlstates": dict(Counter(r.get("crash_sqlstate") for r in crashed)),
        "avg_tables": round(sum((r.get("schema_stats") or {}).get("tables", 0) for r in crashed) / max(1, len(crashed)), 1),
        "avg_fks": round(sum((r.get("schema_stats") or {}).get("foreign_keys", 0) for r in crashed) / max(1, len(crashed)), 1),
        "max_tables": max([(r.get("schema_stats") or {}).get("tables", 0) for r in crashed] or [0]),
        "max_fks": max([(r.get("schema_stats") or {}).get("foreign_keys", 0) for r in crashed] or [0]),
        "failures": [{"repo": r["repo"], "sha": r["sha"][:8], "op": r["op_kind"], "table": r.get("actual_table"), "column": r.get("actual_column"),
                      "apply_err": r.get("synth_apply_err"), "contract_err": r.get("synth_contract_err"),
                      "c_old_pass": r.get("synth_c_old_pass"), "c_new_pass": r.get("synth_c_new_pass"), "c_old_err": r.get("synth_c_old_err"), "c_new_err": r.get("synth_c_new_err"), "c_new_probe": r.get("c_new_probe_sql"), "plan": r.get("ecs_plan_used"), "equiv_detail": r.get("equiv_detail"), "schema_equiv": r.get("synth_schema_equiv_target_col")}
                     for r in crashed if not r.get("synth_full_verified")],
    }
    (V3 / "full_schema_v3_results.json").write_text(json.dumps(res, indent=2), encoding="utf-8")
    (V3 / "rq4_full_schema_v3_summary.json").write_text(json.dumps(summ, indent=2), encoding="utf-8")
    print(json.dumps({k: v for k, v in summ.items() if k != "failures"}, indent=1))
    for f in summ["failures"]:
        print("FAIL", f)


if __name__ == "__main__":
    main()
