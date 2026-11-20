"""V2 Phase 4: Run ExpandContractSynth + Schema Equivalence Verification on:
  (A) All 117 verified breaking cases from the v2 non-circular benchmark + Listing 1 (`papermark@51a67827`)
  (B) Expanded full-schema multi-table Prisma production database histories (with Official Prisma DMMF C_{t-1} queries,
      including ADD_NOT_NULL_NO_DEFAULT & SET_NOT_NULL, plus exact target-column schema equivalence checks!)
  (C) Recompute RQ2 `squawk v2.73.0` on all 400 added-only (`status == 'A'`) Prisma forward-destructive commits
      using our consistent strict 10-rule `SQUAWK_STRICT_COMPAT_RULES`.
"""
from __future__ import annotations

import concurrent.futures
import json
import os
import subprocess
import sys
from collections import Counter, defaultdict
from pathlib import Path

TASK_DIR = Path(__file__).resolve().parent.parent.parent
RESEARCH_DIR = TASK_DIR / "research"
V2_DIR = RESEARCH_DIR / "v2"
REPOS_DIR = TASK_DIR / "tmp" / "repos"

sys.path.insert(0, str(RESEARCH_DIR))
sys.path.insert(0, str(RESEARCH_DIR / "phase1"))
sys.path.insert(0, str(TASK_DIR.parent.parent / "scripts"))

from progress import Progress
from ecv.ir import DDLOp
from ecv.synthesizer import synthesize_expand_contract
from mine_corpus import ls_tree_oids, read_blobs_by_oid, repo_slug

CPU_WORKERS = min(8, os.cpu_count() or 8)
TARGET_FS_KINDS = {"DROP_COLUMN", "DROP_TABLE", "RENAME_COLUMN", "RENAME_TABLE", "SET_NOT_NULL", "ADD_NOT_NULL_NO_DEFAULT"}

SQUAWK_STRICT_COMPAT_RULES = {
    "ban-drop-column", "ban-drop-table", "ban-drop-type",
    "renaming-column", "renaming-table",
    "adding-not-nullable-field", "adding-required-field",
    "changing-column-type", "ban-drop-not-null", "ban-drop-database",
}


def fetch_full_schema_repo(args: tuple[str, list[dict]]) -> list[dict]:
    repo_name, cands = args
    rdir = REPOS_DIR / repo_slug(repo_name)
    if not (rdir / ".git").exists():
        return []
    needed = []
    plans = []
    for cand in cands:
        parent = cand["parent"]
        mig_root = str(Path(cand["mig_file"]).parent.parent).replace("\\", "/")
        tree_old = ls_tree_oids(rdir, parent)
        prior_migs = sorted(
            [(p, oid) for p, oid in tree_old if p.startswith(mig_root + "/") and p.endswith("migration.sql")],
            key=lambda x: x[0],
        )
        prisma_old = [(p, oid) for p, oid in tree_old if p.endswith(".prisma")]
        if not prior_migs or len(prior_migs) > 220:
            continue
        for _, oid in prior_migs:
            needed.append(oid)
        for _, oid in prisma_old:
            needed.append(oid)
        plans.append((cand, prior_migs, prisma_old))
    if not plans:
        return []
    blobs = read_blobs_by_oid(rdir, needed, timeout=180)
    out = []
    for cand, prior_migs, prisma_old in plans:
        prior_sqls = [blobs[oid] for _, oid in prior_migs if oid in blobs]
        if len(prior_sqls) != len(prior_migs):
            continue
        schemas = {p: blobs[oid][:120000] for p, oid in prisma_old if oid in blobs}
        out.append({**cand, "prior_sqls": prior_sqls, "prisma_schemas_old": schemas})
    return out


def main():
    log_path = V2_DIR / "v2_synth_and_full_schema.log"
    gt_rows = json.loads((V2_DIR / "ground_truth_180_verified.json").read_text(encoding="utf-8"))
    raw_by_id = {c["id"]: c for c in json.loads((V2_DIR / "benchmark_v2_raw.json").read_text(encoding="utf-8"))}

    # --- PART A: Verify ExpandContractSynth + Schema Equivalence on all 117 verified breaking benchmark cases ---
    synth_cases = []
    for r in gt_rows:
        if not r["gt_violation"]:
            continue
        c = raw_by_id[r["id"]]
        op = c["op"]
        ddl = DDLOp(
            ecosystem=r["ecosystem"], kind=op["kind"], table=op.get("table"), column=op.get("column"),
            new_name=op.get("new_name"), old_type=r.get("old_type_observed") or op.get("old_type"),
            new_type=op.get("new_type"), phase_section="up", file_path=r["file_path"],
            raw_snippet=op.get("raw_snippet", ""),
        )
        old_t = r.get("old_type_observed") or op.get("old_type") or "TEXT"
        new_t = op.get("new_type") or ("INTEGER" if op["kind"] == "ALTER_COLUMN_TYPE" else None)
        plan = synthesize_expand_contract(ddl, old_pg_type=old_t, new_pg_type=new_t)
        synth_cases.append({
            "id": r["id"], "op_kind": op["kind"], "table": op.get("table"), "column": op.get("column"),
            "new_name": op.get("new_name"), "old_type": old_t, "new_type": new_t,
            "synth_plan": plan.to_dict(),
        })

    in_syn = V2_DIR / "synth_v2_in.json"
    out_syn = V2_DIR / "synth_v2_out.json"
    in_syn.write_text(json.dumps(synth_cases, indent=2), encoding="utf-8")
    subprocess.run(["node", str(V2_DIR / "pg_v2_synth_worker.mjs"), str(in_syn), str(out_syn)], check=True, timeout=180)
    syn_data = json.loads(out_syn.read_text(encoding="utf-8"))
    syn_rows = syn_data["results"]
    by_op = defaultdict(Counter)
    for row in syn_rows:
        k = row["op_kind"]
        by_op["ALL"]["attempted"] += 1
        by_op[k]["attempted"] += 1
        if row["verified_pass"]:
            by_op["ALL"]["verified_pass"] += 1
            by_op[k]["verified_pass"] += 1
        if row["schema_equiv_to_target"]:
            by_op["ALL"]["schema_equiv"] += 1
            by_op[k]["schema_equiv"] += 1

    # --- PART B: Expanded Full-Schema Multi-Table Prisma Verification (with Official DMMF + ADD_NOT_NULL / SET_NOT_NULL!) ---
    pool = json.loads((V2_DIR / "added_only_pool.json").read_text(encoding="utf-8"))
    prisma_added = pool["prisma"]
    by_repo_fs: dict[str, list[dict]] = {}
    fid = 0
    # Prioritize diverse op kinds including ADD_NOT_NULL_NO_DEFAULT & SET_NOT_NULL!
    by_repo_pool = defaultdict(list)
    for r in prisma_added:
        if r["ecv_classification"].startswith("DUAL_VERSION_VIOLATION") and r["op"]["kind"] in TARGET_FS_KINDS and r.get("raw_sql"):
            by_repo_pool[r["repo"]].append(r)

    for repo, recs in sorted(by_repo_pool.items()):
        picked = []
        used_kinds = set()
        # First prefer ADD_NOT_NULL_NO_DEFAULT / SET_NOT_NULL / RENAME_COLUMN / RENAME_TABLE
        recs_sorted = sorted(
            recs,
            key=lambda x: 0 if x["op"]["kind"] in ("ADD_NOT_NULL_NO_DEFAULT", "SET_NOT_NULL", "RENAME_COLUMN", "RENAME_TABLE") else 1,
        )
        for r in recs_sorted:
            k = r["op"]["kind"]
            if k in used_kinds and len(picked) >= 2:
                continue
            fid += 1
            used_kinds.add(k)
            picked.append({
                "id": f"fs2_{fid}", "repo": repo, "sha": r["sha"], "parent": r["parent"],
                "mig_file": r["file_path"], "op_kind": k,
                "table": r["op"].get("table"), "column": r["op"].get("column"),
                "new_name": r["op"].get("new_name"), "new_type": r["op"].get("new_type"),
                "verbatim_migration_sql": r["raw_sql"],
            })
            if len(picked) >= 4:
                break
        if picked:
            by_repo_fs[repo] = picked

    with open(log_path, "w", encoding="utf-8") as logf:
        p1 = Progress(total=len(by_repo_fs), label="v2-fetch-full-schema-prisma", stream=logf)
        fs_all = []
        with concurrent.futures.ProcessPoolExecutor(max_workers=CPU_WORKERS) as ex:
            futs = {ex.submit(fetch_full_schema_repo, item): item[0] for item in by_repo_fs.items()}
            for fut in concurrent.futures.as_completed(futs):
                repo = futs[fut]
                try:
                    fs_all.extend(fut.result())
                except Exception as e:
                    print(f"ERR {repo}: {e}", file=logf, flush=True)
                p1.step(note=f"{repo} built={len(fs_all)}")
        p1.done()

        n_shards = min(CPU_WORKERS, max(1, len(fs_all)))
        shards = [fs_all[i::n_shards] for i in range(n_shards)]
        sh_dir = V2_DIR / "fs_shards"
        sh_dir.mkdir(exist_ok=True)
        procs = []
        for idx, sh in enumerate(shards):
            inp = sh_dir / f"in_{idx}.json"
            outp = sh_dir / f"out_{idx}.json"
            inp.write_text(json.dumps(sh), encoding="utf-8")
            procs.append((idx, subprocess.Popen(["node", str(V2_DIR / "pg_v2_full_schema_worker.mjs"), str(inp), str(outp)]), outp))
        fs_results = []
        for idx, proc, outp in procs:
            rc = proc.wait(timeout=300)
            if rc == 0 and outp.exists():
                fs_results.extend(json.loads(outp.read_text(encoding="utf-8")))

    # --- PART C: Clean Added-Only Prisma Corpus Squawk Evaluation (strict 10-rule compat!) ---
    # Deduplicate added-only Prisma commits
    prisma_commits = {}
    for r in prisma_added:
        key = f"{r['repo'].replace('/','__')}__{r['sha'][:10]}"
        if key not in prisma_commits:
            prisma_commits[key] = {"repo": r["repo"], "sha": r["sha"], "raw_sql": r.get("raw_sql") or "", "classes": []}
        prisma_commits[key]["classes"].append(r["ecv_classification"])

    sq_corpus_dir = TASK_DIR / "tmp" / "v2_squawk_prisma_added"
    sq_corpus_dir.mkdir(parents=True, exist_ok=True)
    for k, d in prisma_commits.items():
        (sq_corpus_dir / f"{k}.sql").write_text(d["raw_sql"], encoding="utf-8")

    sq_bin = os.environ.get("SQUAWK_BIN", str(RESEARCH_DIR / "tools" / "squawk-linux-x64"))
    sql_paths = sorted(str(p) for p in sq_corpus_dir.glob("*.sql"))
    sq_cmd = (
        ["wsl", "bash", "-lc", f"{sq_bin} --reporter json {sq_corpus_dir.as_posix()}/*.sql"]
        if os.name == "nt" and os.environ.get("SQUAWK_USE_WSL", "1") == "1"
        else [sq_bin, "--reporter", "json"] + sql_paths
    )
    sq_c_proc = subprocess.run(sq_cmd, capture_output=True, text=True, timeout=120)
    sq_c_diags = json.loads(sq_c_proc.stdout) if sq_c_proc.stdout.strip().startswith("[") else []
    c_rules = defaultdict(set)
    for it in sq_c_diags:
        if isinstance(it, dict):
            stem = Path(it.get("file", "")).stem
            rn = it.get("rule_name", "")
            if stem in prisma_commits and rn:
                c_rules[stem].add(rn)

    safe_only_tot = safe_only_sq_def = safe_only_sq_cmp = 0
    viol_tot = viol_sq_cmp_miss = 0
    for k, d in prisma_commits.items():
        has_v = any(cl.startswith("DUAL_VERSION_VIOLATION") for cl in d["classes"])
        only_s = (not has_v) and any(cl == "SAFE_CONTRACT" for cl in d["classes"])
        rs = c_rules[k]
        def_f = len(rs) > 0
        cmp_f = bool(rs & SQUAWK_STRICT_COMPAT_RULES)
        if only_s:
            safe_only_tot += 1
            if def_f: safe_only_sq_def += 1
            if cmp_f: safe_only_sq_cmp += 1
        elif has_v:
            viol_tot += 1
            if not cmp_f: viol_sq_cmp_miss += 1

    # Summarize Full-Schema
    loaded = [r for r in fs_results if r["schema_loaded_ok"]]
    repro = [r for r in loaded if r["c_old_at_s_old_ok"] and r["verbatim_mig_ok"]]
    crashed = [r for r in repro if r["c_old_at_s_new_crash"]]
    fs_ok = [r for r in crashed if r["synth_full_verified"]]

    summary = {
        "rq4_synth_minimal_with_schema_equiv": {k: dict(v) for k, v in sorted(by_op.items())},
        "listing1_papermark_exact_equiv": syn_data["listing1_papermark_check"],
        "rq4_full_schema_dmmf": {
            "candidates_attempted": len(fs_results),
            "histories_loaded": len(loaded),
            "reproducible": len(repro),
            "crashes_reproduced": len(crashed),
            "dmmf_driven_crashes": sum(1 for r in crashed if r["dmmf_used"]),
            "distinct_repos_crashed": len({r["repo"] for r in crashed}),
            "synth_verified_with_schema_equiv": len(fs_ok),
            "pass_rate_pct": round(100 * len(fs_ok) / len(crashed), 1) if crashed else 0.0,
            "avg_tables": round(sum(r["schema_stats"]["tables"] for r in crashed) / max(1, len(crashed)), 1),
            "avg_fks": round(sum(r["schema_stats"]["foreign_keys"] for r in crashed) / max(1, len(crashed)), 1),
            "max_tables": max((r["schema_stats"]["tables"] for r in crashed), default=0),
            "max_fks": max((r["schema_stats"]["foreign_keys"] for r in crashed), default=0),
            "by_op_crashed": dict(Counter(r["op_kind"] for r in crashed)),
            "by_op_verified": dict(Counter(r["op_kind"] for r in fs_ok)),
            "sqlstates": dict(Counter(r["crash_sqlstate"] for r in crashed)),
            "examples": [
                {
                    "repo": r["repo"], "sha": r["sha"][:8], "op_kind": r["op_kind"],
                    "table": r["actual_table"], "col": r["actual_column"], "dmmf": r["dmmf_used"],
                    "tables": r["schema_stats"]["tables"], "fks": r["schema_stats"]["foreign_keys"],
                    "enums": r["schema_stats"]["enums"], "prior_migs": r["prior_migrations_count"],
                    "sqlstate": r["crash_sqlstate"], "equiv": r["synth_schema_equiv_target_col"],
                    "verified": r["synth_full_verified"], "ms": r["elapsed_ms"],
                }
                for r in fs_ok[:15]
            ],
        },
        "rq2_squawk_prisma_added_only": {
            "commits_evaluated": len(prisma_commits),
            "ecv_safe_contract_only_commits": safe_only_tot,
            "flagged_by_squawk_default": safe_only_sq_def,
            "flagged_by_squawk_strict_compat": safe_only_sq_cmp,
            "ecv_violation_commits": viol_tot,
            "missed_by_squawk_strict_compat": viol_sq_cmp_miss,
        },
    }
    (V2_DIR / "rq4_and_rq2_squawk_v2.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    (V2_DIR / "full_schema_v2_results.json").write_text(json.dumps(fs_results, indent=2), encoding="utf-8")
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
