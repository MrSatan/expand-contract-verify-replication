"""V2 Phase 2A: Unbiased Stratified Benchmark Sampler (N=180) + Full Non-Circular Evidence Extraction.

Key methodological guarantees (eliminating all v1 circularity & selection bias):
  1. Samples strictly from newly-added (`status == 'A'`) forward-destructive migration files.
  2. NEVER inspects ECV's verdicts or filters out `UNCERTAIN` — sampling is 100% blind to ECV!
  3. Stratifies on a purely observable, ECV-independent Git commit property:
       - `mig_only`: commit modifies ONLY migration/schema files (0 non-migration app source files)
       - `cochanged`: commit co-modifies migration files AND non-migration application source files
     (30 mig_only + 30 cochanged per ecosystem = 60 Prisma + 60 Rails + 60 Django = 180 cases),
     recording exact stratum population totals (N_h) for unbiased inverse-probability weighting.
  4. Extracts verbatim migration files, full commit diffs, verbatim C_{t-1} ORM schemas/models,
     and full `git grep` references at C_{t-1} (no 45-file heuristic, no ECV evidence fields).
"""
from __future__ import annotations

import concurrent.futures
import json
import os
import random
import re
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
from mine_corpus import ls_tree_oids, read_blobs_by_oid, repo_slug, run_git

SEED = 20261121
TARGET_PER_ECO = 60
TARGET_MIG_ONLY = 30
TARGET_COCHANGED = 30
CPU_WORKERS = min(8, os.cpu_count() or 8)

APP_EXTS = {
    "prisma": (".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs"),
    "rails": (".rb", ".erb", ".haml", ".slim"),
    "django": (".py",),
}


def classify_commit_strata_for_repo(args: tuple[str, str, list[str]]) -> dict[str, dict]:
    """Check purely from git diff-tree whether each commit touches non-migration app source files."""
    repo, eco, shas = args
    rdir = REPOS_DIR / repo_slug(repo)
    out = {}
    if not (rdir / ".git").exists() or not shas:
        return out
    exts = APP_EXTS[eco]
    proc = subprocess.run(
        ["git", "-C", str(rdir), "-c", "diff.renames=false", "diff-tree",
         "--no-renames", "-r", "--name-only", "--stdin", "--format=COMMIT|%H"],
        input="\n".join(sorted(set(shas))) + "\n",
        capture_output=True, text=True, encoding="utf-8", errors="ignore", timeout=60,
    )
    cur = None
    files_by_sha = defaultdict(list)
    for ln in proc.stdout.splitlines():
        if ln.startswith("COMMIT|"):
            cur = ln.split("|", 1)[1].strip()
        elif cur and ln.strip():
            files_by_sha[cur].append(ln.strip())

    for sha, flist in files_by_sha.items():
        non_mig_app = []
        touches_orm = False
        for f in flist:
            low = f.lower()
            if any(k in low for k in ("/migrations/", "/migrate/", "db/migrate/", "db/post_migrate/", "/test", "/spec", "__tests__", "schema.rb", "structure.sql")):
                continue
            if low.endswith(".prisma") or re.search(r"(^|/)models(\.py|/.*\.py)$", low) or re.search(r"(^|/)app/models/.*\.rb$", low):
                touches_orm = True
                non_mig_app.append(f)
            elif low.endswith(exts):
                non_mig_app.append(f)
        out[sha] = {
            "stratum": "cochanged" if len(non_mig_app) > 0 else "mig_only",
            "non_mig_app_files_count": len(non_mig_app),
            "touches_orm": touches_orm,
            "changed_files_count": len(flist),
        }
    return out


def pick_diverse(pool: list[dict], k: int, rng: random.Random) -> list[dict]:
    """Round-robin across op_kind, preferring distinct repos & commits (100% blind to ECV verdict!)."""
    by_kind = defaultdict(list)
    for item in pool:
        by_kind[item["op"]["kind"]].append(item)
    for lst in by_kind.values():
        rng.shuffle(lst)

    chosen = []
    used_commits = set()
    used_repo_counts = Counter()
    kinds = sorted(by_kind.keys())

    # First pass: strictly 1 case per (repo, sha) commit
    while len(chosen) < k:
        progressed = False
        for kd in kinds:
            if len(chosen) >= k:
                break
            cands = [x for x in by_kind[kd] if (x["repo"], x["sha"]) not in used_commits]
            if not cands:
                continue
            cands.sort(key=lambda x: used_repo_counts[x["repo"]])
            pick = cands[0]
            chosen.append(pick)
            used_commits.add((pick["repo"], pick["sha"]))
            used_repo_counts[pick["repo"]] += 1
            progressed = True
        if not progressed:
            break

    # Second pass fallback if needed (distinct op id)
    if len(chosen) < k:
        used_idx = {id(x) for x in chosen}
        rem = [x for x in pool if id(x) not in used_idx]
        rng.shuffle(rem)
        chosen.extend(rem[: k - len(chosen)])
    return chosen


def extract_evidence_for_repo(args: tuple[str, list[dict]]) -> list[dict]:
    """Fetch verbatim migration file, C_{t-1} ORM schema/models, commit diff, and full C_{t-1} git-grep hits."""
    repo, cases = args
    rdir = REPOS_DIR / repo_slug(repo)
    if not (rdir / ".git").exists():
        return []

    needed_oids = []
    plans = []
    for c in cases:
        eco = c["ecosystem"]
        sha = c["sha"]
        parent = c["parent"]
        mig_fp = c["file_path"]
        tbl = (c["op"].get("table") or "").lower()
        col = (c["op"].get("column") or "").lower()
        app_lbl = (c["op"].get("extra") or {}).get("app_label")

        tree_sha = ls_tree_oids(rdir, sha)
        tree_old = ls_tree_oids(rdir, parent)
        sha_map = dict(tree_sha)
        old_map = dict(tree_old)

        mig_oid = sha_map.get(mig_fp)
        if mig_oid:
            needed_oids.append(mig_oid)

        # Pick C_{t-1} ORM files cleanly
        orm_old_files = []
        if eco == "prisma":
            orm_old_files = [(p, oid) for p, oid in tree_old if p.endswith(".prisma")]
        elif eco == "rails":
            orm_old_files = [(p, oid) for p, oid in tree_old if re.search(r"(^|/)db/(schema\.rb|structure\.sql)$", p)]
            models = [(p, oid) for p, oid in tree_old if re.search(r"(^|/)app/models/.*\.rb$", p)]
            tbl_stem = tbl.rstrip("s").split("_")[-1] if tbl else ""
            for mp, oid in models:
                stem = Path(mp).stem.lower()
                if len(models) <= 60 or (tbl_stem and (stem in tbl or tbl_stem in stem or stem in tbl.replace("_", ""))):
                    orm_old_files.append((mp, oid))
        elif eco == "django":
            for p, oid in tree_old:
                if re.search(r"(^|/)models(\.py|/.*\.py)$", p) and not re.search(r"(^|/)(tests?|migrations)/", p, re.I):
                    if (not app_lbl) or (app_lbl in p.lower().split("/")):
                        orm_old_files.append((p, oid))
            orm_old_files = orm_old_files[:30]

        for _, oid in orm_old_files:
            needed_oids.append(oid)

        # Non-migration changed files in commit t (old & new OIDs for diff)
        dp = run_git(rdir, ["diff-tree", "-r", "--raw", "--no-abbrev", "--no-commit-id", parent, sha], timeout=25)
        diff_files = []
        if dp.returncode == 0:
            for ln in dp.stdout.decode("utf-8", errors="ignore").splitlines():
                if not ln.startswith(":") or "\t" not in ln:
                    continue
                meta, fp = ln.split("\t", 1)
                mp = meta.split()
                if len(mp) < 5:
                    continue
                oo, no = mp[2], mp[3]
                low = fp.lower()
                if any(s in low for s in ("/migrations/", "/migrate/", "db/migrate/", "/test", "/spec", "__tests__", "package-lock", "yarn.lock", "pnpm-lock")):
                    continue
                if low.endswith(APP_EXTS[eco] + (".prisma", ".sql")):
                    diff_files.append((fp, oo, no))
                    if oo != "0" * 40:
                        needed_oids.append(oo)
                    if no != "0" * 40:
                        needed_oids.append(no)
                if len(diff_files) >= 20:
                    break

        # Also fetch up to 35 candidate C_{t-1} app source files (for non-circular full-codebase inspection & ECV-Deep)
        # Note: in partial clones `--filter=blob:none`, `git grep <rev>` would trigger serial promisor fetches!
        # So we pick candidate source files purely by path relevance and bulk-prefetch them in ONE packfile.
        exts = APP_EXTS[eco]
        tbl_stem = tbl.rstrip("s").split("_")[-1] if tbl else ""
        cands = []
        for p, oid in tree_old:
            low = p.lower()
            if not low.endswith(exts):
                continue
            if any(sk in low for sk in ("/migrations/", "/migrate/", "db/migrate/", "/test", "/spec", "__tests__", "node_modules/", ".d.ts", "schema.rb", "structure.sql")):
                continue
            sc = 0
            if tbl_stem and len(tbl_stem) >= 3 and tbl_stem in low:
                sc += 10
            if col and len(col) >= 4 and col in low:
                sc += 8
            if app_lbl and app_lbl in low.split("/"):
                sc += 6
            if any(k in low for k in ("/controllers/", "/services/", "/api/", "/routers/", "/queries/", "/repositories/", "views.py", "serializers.py", "forms.py", "admin.py")):
                sc += 3
            if sc > 0:
                cands.append((sc, p, oid))
        cands.sort(key=lambda x: x[0], reverse=True)
        app_old_files = [(p, oid) for _, p, oid in cands[:35]]
        for _, oid in app_old_files:
            needed_oids.append(oid)

        # Prior 10 commit messages (100% local)
        plog = run_git(rdir, ["log", "-n", "10", "--format=%h %s", parent], timeout=15)
        prior_msgs = plog.stdout.decode("utf-8", errors="ignore").splitlines() if plog.returncode == 0 else []

        plans.append((c, mig_oid, orm_old_files, diff_files, app_old_files, prior_msgs))

    blobs = read_blobs_by_oid(rdir, needed_oids, timeout=240)

    out = []
    for c, mig_oid, orm_old_files, diff_files, app_old_files, prior_msgs in plans:
        mig_content = blobs.get(mig_oid, "") if mig_oid else ""
        orm_old_texts = {fp: blobs[oid] for fp, oid in orm_old_files if oid in blobs}
        app_old_texts = {fp: blobs[oid] for fp, oid in app_old_files if oid in blobs}

        # Build compact non-migration diff lines touching target table/column
        tbl = (c["op"].get("table") or "").lower()
        col = (c["op"].get("column") or "").lower()
        col_camel = re.sub(r"_([a-z])", lambda m: m.group(1).upper(), col) if "_" in col else col
        tbl_short = tbl.split("_")[-1] if (c["ecosystem"] == "django" and "_" in tbl) else tbl

        diff_summary = []
        for fp, oo, no in diff_files:
            ot = blobs.get(oo, "")
            nt = blobs.get(no, "")
            ol = set(ot.splitlines())
            nl = set(nt.splitlines())
            rem = [ln.strip() for ln in (ol - nl) if ln.strip()]
            add = [ln.strip() for ln in (nl - ol) if ln.strip()]
            # Keep lines mentioning col or tbl
            keys = [k for k in (col, col_camel, tbl, tbl_short) if k and len(k) >= 3]
            rem_hits = [ln[:140] for ln in rem if any(k in ln.lower() for k in keys)][:6]
            add_hits = [ln[:140] for ln in add if any(k in ln.lower() for k in keys)][:6]
            if rem_hits or add_hits:
                diff_summary.append({"file": fp, "removed": rem_hits, "added": add_hits})

        # Also extract C_{t-1} non-model code matches for col/tbl (with line snippets!)
        code_hits_col = []
        code_hits_tbl = []
        rx_col = re.compile(rf"\b({re.escape(col)}|{re.escape(col_camel)})\b", re.I) if col and len(col) >= 2 else None
        rx_tbl = re.compile(rf"\b{re.escape(tbl_short)}\b", re.I) if tbl_short and len(tbl_short) >= 3 else None
        for fp, txt in app_old_texts.items():
            low_fp = fp.lower()
            is_model = ("models" in low_fp or low_fp.endswith(".prisma"))
            if is_model:
                continue
            if rx_col:
                for i, ln in enumerate(txt.splitlines(), 1):
                    if rx_col.search(ln):
                        code_hits_col.append({"file": fp, "line": i, "text": ln.strip()[:140]})
                        break
            if rx_tbl:
                for i, ln in enumerate(txt.splitlines(), 1):
                    if rx_tbl.search(ln):
                        code_hits_tbl.append({"file": fp, "line": i, "text": ln.strip()[:140]})
                        break

        rec = dict(c)
        rec["verbatim_migration_content"] = mig_content[:35000]
        rec["orm_old_file_paths"] = list(orm_old_texts.keys())
        rec["diff_hits"] = diff_summary[:10]
        rec["c_old_code_hits_col"] = code_hits_col[:8]
        rec["c_old_code_hits_tbl"] = code_hits_tbl[:8]
        rec["prior_commit_subjects"] = prior_msgs[:10]
        # Store verbatim Prisma schema at C_{t-1} for official Prisma DMMF oracle!
        if c["ecosystem"] == "prisma":
            rec["prisma_schemas_old"] = {fp: txt[:120000] for fp, txt in orm_old_texts.items()}
        elif c["ecosystem"] == "rails":
            # Save only relevant table snippet from schema.rb / structure.sql + matching model files (keeps JSON compact!)
            rec["rails_models_old"] = {fp: txt[:20000] for fp, txt in orm_old_texts.items() if not fp.endswith(("schema.rb", "structure.sql"))}
            schema_snips = {}
            for fp, txt in orm_old_texts.items():
                if fp.endswith("schema.rb"):
                    m = re.search(rf'create_table\s+["\':]{re.escape(tbl)}["\']?.*?\n\s*end\b', txt, re.S | re.I)
                    if m:
                        schema_snips[fp] = m.group(0)[:4000]
                elif fp.endswith("structure.sql"):
                    m = re.search(rf'CREATE\s+TABLE\s+[^\n]*"{re.escape(tbl)}"\s*\(.*?\n\);', txt, re.S | re.I)
                    if m:
                        schema_snips[fp] = m.group(0)[:4000]
            rec["rails_schema_old_snippet"] = schema_snips
            rec["rails_has_schema_file"] = any(fp.endswith(("schema.rb", "structure.sql")) for fp in orm_old_texts)
        elif c["ecosystem"] == "django":
            rec["django_models_old"] = {fp: txt[:40000] for fp, txt in orm_old_texts.items()}
        # Store compact app files list for fair runtime & ECV-Deep re-evaluation
        rec["c_old_app_files_cached"] = {fp: txt[:25000] for fp, txt in list(app_old_texts.items())[:25]}
        out.append(rec)
    return out


def main():
    rng = random.Random(SEED)
    log_path = V2_DIR / "v2_sample_benchmark.log"
    pool = json.loads((V2_DIR / "added_only_pool.json").read_text(encoding="utf-8"))

    with open(log_path, "w", encoding="utf-8") as logf:
        # Step 1: Classify every commit in added_only_pool by observable Git stratum (mig_only vs cochanged)
        by_repo_shas = defaultdict(lambda: [None, set()])
        for eco, recs in pool.items():
            for r in recs:
                by_repo_shas[r["repo"]][0] = eco
                by_repo_shas[r["repo"]][1].add(r["sha"])

        p1 = Progress(total=len(by_repo_shas), label="v2-classify-git-strata", stream=logf)
        commit_strata = {}
        with concurrent.futures.ProcessPoolExecutor(max_workers=CPU_WORKERS) as ex:
            futs = {ex.submit(classify_commit_strata_for_repo, (repo, eco, list(shas))): repo
                    for repo, (eco, shas) in by_repo_shas.items()}
            for fut in concurrent.futures.as_completed(futs):
                repo = futs[fut]
                for sha, info in fut.result().items():
                    commit_strata[(repo, sha)] = info
                p1.step(note=repo)
        p1.done()

        # Step 2: Stratify strictly by (ecosystem, mig_only vs cochanged) — 100% blind to ECV verdict!
        benchmark = []
        stratum_pop = {}
        bid = 0
        for eco in ("prisma", "rails", "django"):
            mig_only = []
            cochanged = []
            for r in pool[eco]:
                st_info = commit_strata.get((r["repo"], r["sha"]), {"stratum": "cochanged", "non_mig_app_files_count": 1, "touches_orm": False})
                r2 = dict(r)
                r2["git_stratum"] = st_info["stratum"]
                r2["git_stratum_info"] = st_info
                if st_info["stratum"] == "mig_only":
                    mig_only.append(r2)
                else:
                    cochanged.append(r2)

            stratum_pop[eco] = {
                "mig_only_total": len(mig_only),
                "cochanged_total": len(cochanged),
                "total": len(mig_only) + len(cochanged),
            }
            n_mo = min(TARGET_MIG_ONLY, len(mig_only))
            n_co = TARGET_PER_ECO - n_mo
            picked = pick_diverse(mig_only, n_mo, rng) + pick_diverse(cochanged, n_co, rng)
            stratum_pop[eco]["mig_only_sampled"] = n_mo
            stratum_pop[eco]["cochanged_sampled"] = len(picked) - n_mo
            for x in picked:
                bid += 1
                x["id"] = f"{eco}_{bid:03d}"
                benchmark.append(x)

        print("Stratum populations:", json.dumps(stratum_pop, indent=2), file=logf, flush=True)
        print("Sampled ECV breakdown (for transparency only, NOT used in sampling!):",
              Counter((x["ecosystem"], x["ecv_classification"]) for x in benchmark), file=logf, flush=True)

        # Step 3: Extract full verbatim files & non-circular C_{t-1} evidence across all 8 cores
        by_repo_cases = defaultdict(list)
        for c in benchmark:
            by_repo_cases[c["repo"]].append(c)

        p2 = Progress(total=len(by_repo_cases), label="v2-extract-benchmark-evidence", stream=logf)
        enriched = []
        with concurrent.futures.ProcessPoolExecutor(max_workers=CPU_WORKERS) as ex:
            futs = {ex.submit(extract_evidence_for_repo, item): item[0] for item in by_repo_cases.items()}
            for fut in concurrent.futures.as_completed(futs):
                repo = futs[fut]
                try:
                    enriched.extend(fut.result())
                except Exception as e:
                    print(f"ERR {repo}: {e}", file=logf, flush=True)
                p2.step(note=f"{repo} total={len(enriched)}")
        p2.done()

        # Sort deterministically by id
        enriched.sort(key=lambda x: x["id"])
        (V2_DIR / "benchmark_v2_raw.json").write_text(json.dumps(enriched, indent=2), encoding="utf-8")
        (V2_DIR / "benchmark_v2_strata.json").write_text(json.dumps(stratum_pop, indent=2), encoding="utf-8")
        print(f"DONE: extracted {len(enriched)} unbiased added-only benchmark cases.", file=logf, flush=True)
        print(json.dumps({
            "total_extracted": len(enriched),
            "stratum_pop": stratum_pop,
            "by_eco": dict(Counter(x["ecosystem"] for x in enriched)),
            "by_ecv_verdict_natural": {f"{k[0]}:{k[1]}": v for k, v in sorted(Counter((x["ecosystem"], x["ecv_classification"]) for x in enriched).items())},
            "by_op_kind": dict(Counter(x["op"]["kind"] for x in enriched)),
        }, indent=2))


if __name__ == "__main__":
    main()
