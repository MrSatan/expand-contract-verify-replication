"""V3 RQ1 recompute (adapted from v2 Phase 1A).

v3 changes: reads research/v3/mined_repos (re-mined with post-deploy migrations, GitLab ignore_column,
Sentry SafeRemoveField, DATA_DEPENDENT constraints); counts DATA_DEPENDENT and post-deploy ops separately;
adds a bootstrap CI for the K8s-vs-non-K8s repository-median difference. Writes to research/v3/.

Original v2 docstring:
1. Mine `gitlabhq/gitlabhq` using `git log -n 150` (solving the 90s timeout on full history) so all 110/110 corpus repos are mined.
2. Audit post-deployment migrations across the corpus:
   - Rails `db/post_migrate/*.rb` (GitLab, Mastodon)
   - Django `is_post_deployment = True` / `SafeRemoveField` / `SafeDeleteModel` (Sentry & any other Django repo).
3. Perform STRICT FILE-LEVEL Added-Only (`status == 'A'`) filtering across all 110 mined repositories,
   excluding edits to already-applied historical migration files from the primary RQ1 dataset.
4. Recompute ALL RQ1 pooled & repository-level statistics (Kruskal-Wallis, Mann-Whitney U, Cliff's delta,
   K8s/Helm stratified analysis, UNCERTAIN coverage rates, operator breakdown).
"""
from __future__ import annotations

import json
import math
import os
import re
import subprocess
import sys
from collections import Counter, defaultdict
from pathlib import Path

import numpy as np
from scipy import stats

TASK_DIR = Path(__file__).resolve().parent.parent.parent
RESEARCH_DIR = TASK_DIR / "research"
V2_DIR = RESEARCH_DIR / "v3"  # v3 output dir (name kept for minimal diff)
V2_DIR.mkdir(parents=True, exist_ok=True)
MINED_DIR = RESEARCH_DIR / "v3" / "mined_repos"
REPOS_DIR = TASK_DIR / "tmp" / "repos"

sys.path.insert(0, str(RESEARCH_DIR))
sys.path.insert(0, str(RESEARCH_DIR / "phase1"))
sys.path.insert(0, str(TASK_DIR.parent.parent / "scripts"))

from progress import Progress
from ecv.ddl_parser import parse_migration
from ecv.orm_parser import parse_rails_schema_and_models
from ecv.verifier import strong_migrations_baseline_verdict, verify_op
from mine_corpus import (
    diff_tokens_from_texts,
    ls_tree_oids,
    parse_created_tables,
    read_blobs_by_oid,
    repo_slug,
    run_git,
)


def wilson_ci(k: int, n: int, z: float = 1.96) -> list[float]:
    if n == 0:
        return [0.0, 0.0]
    p = k / n
    denom = 1.0 + z * z / n
    center = (p + z * z / (2 * n)) / denom
    margin = z * math.sqrt((p * (1 - p) + z * z / (4 * n)) / n) / denom
    return [round(max(0.0, center - margin) * 100, 1), round(min(1.0, center + margin) * 100, 1)]


def cliffs_delta(x, y):
    x, y = list(x), list(y)
    if not x or not y:
        return 0.0, "negligible"
    more = sum(1 for a in x for b in y if a > b)
    less = sum(1 for a in x for b in y if a < b)
    d = (more - less) / (len(x) * len(y))
    ad = abs(d)
    mag = "negligible" if ad < 0.147 else ("small" if ad < 0.33 else ("medium" if ad < 0.474 else "large"))
    return round(d, 4), mag


def mine_gitlab(corpus_entry: dict) -> dict:
    """Mine gitlabhq/gitlabhq with `-n 150` on `db/migrate/*.rb` (fast, <30s)."""
    full_name = "gitlabhq/gitlabhq"
    out_file = MINED_DIR / "gitlabhq__gitlabhq.json"
    if out_file.exists():
        d = json.loads(out_file.read_text(encoding="utf-8"))
        if d.get("status") == "ok":
            return d

    repo_dir = REPOS_DIR / repo_slug(full_name)
    proc = run_git(
        repo_dir,
        [
            "log", "-n", "150", "--no-merges", "--raw", "--no-abbrev",
            "--format=COMMIT|%H|%P|%aI|%s", "--", "db/migrate/*.rb",
        ],
        timeout=90,
    )
    if proc.returncode != 0:
        return {"repo": full_name, "ecosystem": "rails", "status": "git_log_failed"}

    commits = []
    cur = None
    for line in proc.stdout.decode("utf-8", errors="ignore").splitlines():
        if line.startswith("COMMIT|"):
            parts = line.split("|", 4)
            if len(parts) < 5:
                continue
            sha, parents_str, date_iso, subj = parts[1], parts[2], parts[3], parts[4]
            parents = parents_str.split()
            cur = {
                "sha": sha,
                "parent": parents[0] if parents else None,
                "date": date_iso,
                "subject": subj[:140],
                "mig_files": [],
            }
            commits.append(cur)
        elif cur is not None and line.startswith(":") and "\t" in line:
            meta, path_part = line.split("\t", 1)
            mparts = meta.split()
            if len(mparts) < 5:
                continue
            new_oid = mparts[3]
            status = mparts[4]
            fpath = path_part.split("\t")[-1]
            if status.startswith(("A", "M")) and len(new_oid) == 40 and new_oid != "0" * 40:
                cur["mig_files"].append((fpath, new_oid))

    commits = [c for c in commits if c["mig_files"] and c["parent"]][:150]
    all_mig_oids = [oid for c in commits for _, oid in c["mig_files"]]
    mig_blobs = read_blobs_by_oid(repo_dir, all_mig_oids, timeout=180)

    destructive_commits = []
    stats_d = {
        "total_migration_commits_in_history": len(commits),
        "migration_commits_analyzed": len(commits),
        "total_migration_files_parsed": len(mig_blobs),
        "commits_with_any_destructive": 0,
        "commits_with_forward_destructive": 0,
        "commits_with_rollback_only_destructive": 0,
        "total_ops_extracted": 0,
        "destructive_ops_up": 0,
        "destructive_ops_down": 0,
    }
    for c in commits:
        c_ops = []
        created_tbls: set[str] = set()
        for fp, oid in c["mig_files"]:
            content = mig_blobs.get(oid, "")
            if not content:
                continue
            ops = parse_migration("rails", content, file_path=fp)
            c_ops.extend(ops)
            created_tbls.update(parse_created_tables("rails", content, file_path=fp))
        stats_d["total_ops_extracted"] += len(c_ops)
        dest_up = [o for o in c_ops if o.is_destructive and o.phase_section == "up"]
        dest_down = [o for o in c_ops if o.is_destructive and o.phase_section == "down"]
        stats_d["destructive_ops_up"] += len(dest_up)
        stats_d["destructive_ops_down"] += len(dest_down)
        if dest_up or dest_down:
            stats_d["commits_with_any_destructive"] += 1
            if dest_up:
                stats_d["commits_with_forward_destructive"] += 1
            else:
                stats_d["commits_with_rollback_only_destructive"] += 1
            destructive_commits.append({
                "commit": c, "ops": c_ops, "created_tables": created_tbls, "raw_sql": "",
            })

    # Fetch db/structure.sql + matching models at parent & sha for each destructive commit
    needed_oids = []
    commit_blob_plan = {}
    for dc in destructive_commits:
        c = dc["commit"]
        sha, parent = c["sha"], c["parent"]
        target_tables = {o.table for o in dc["ops"] if o.table}

        def _pick_gitlab_orm(rev: str):
            # Targeted ls-tree only on db/structure.sql and app/models (much faster than full 75k-file tree!)
            p1 = run_git(repo_dir, ["ls-tree", "-r", rev, "db/structure.sql", "app/models"], timeout=30)
            picked = []
            if p1.returncode == 0:
                for ln in p1.stdout.decode("utf-8", errors="ignore").splitlines():
                    tab = ln.find("\t")
                    if tab == -1:
                        continue
                    meta = ln[:tab].split()
                    if len(meta) < 3 or meta[1] != "blob":
                        continue
                    fp, oid = ln[tab + 1:], meta[2]
                    if fp == "db/structure.sql":
                        picked.append((fp, oid))
                    elif fp.endswith(".rb"):
                        base = Path(fp).stem.lower()
                        if any((base in t or t.startswith(base) or base in t.replace("_", "")) for t in target_tables):
                            picked.append((fp, oid))
            return picked[:40]

        orm_old_list = _pick_gitlab_orm(parent)
        orm_new_list = _pick_gitlab_orm(sha)
        changed_code = []
        dp = run_git(repo_dir, ["diff-tree", "-r", "--raw", "--no-abbrev", "--no-commit-id", parent, sha, "--", "app/"], timeout=25)
        if dp.returncode == 0:
            for ln in dp.stdout.decode("utf-8", errors="ignore").splitlines():
                if not ln.startswith(":") or "\t" not in ln:
                    continue
                meta, ppart = ln.split("\t", 1)
                mp = meta.split()
                if len(mp) < 5:
                    continue
                fp = ppart.split("\t")[-1]
                if fp.endswith(".rb") and "/spec" not in fp:
                    changed_code.append((fp, mp[2], mp[3]))
                    if len(changed_code) >= 12:
                        break
        commit_blob_plan[sha] = {"orm_old": orm_old_list, "orm_new": orm_new_list, "code": changed_code}
        for _, oid in orm_old_list:
            needed_oids.append(oid)
        for _, oid in orm_new_list:
            needed_oids.append(oid)
        for _, oo, no in changed_code:
            needed_oids.extend([oo, no])

    all_blobs = read_blobs_by_oid(repo_dir, needed_oids, timeout=240)
    commit_records = []
    verdict_counts = {}
    for dc in destructive_commits:
        c = dc["commit"]
        sha, parent = c["sha"], c["parent"]
        plan = commit_blob_plan[sha]
        old_map = {fp: all_blobs[oid] for fp, oid in plan["orm_old"] if oid in all_blobs}
        new_map = {fp: all_blobs[oid] for fp, oid in plan["orm_new"] if oid in all_blobs}
        st_old = old_map.get("db/structure.sql")
        st_new = new_map.get("db/structure.sql")
        m_old = {k: v for k, v in old_map.items() if k != "db/structure.sql"}
        m_new = {k: v for k, v in new_map.items() if k != "db/structure.sql"}
        orm_old = parse_rails_schema_and_models(None, m_old, structure_sql=st_old)
        orm_new = parse_rails_schema_and_models(None, m_new, structure_sql=st_new)

        rem_toks, add_toks = set(), set()
        app_files = []
        for fp, oo, no in plan["code"]:
            app_files.append(fp)
            rt, at = diff_tokens_from_texts(all_blobs.get(oo, ""), all_blobs.get(no, ""))
            rem_toks.update(rt)
            add_toks.update(at)

        verdicts_out = []
        for op in dc["ops"]:
            v = verify_op(
                op=op, orm_old=orm_old, orm_new=orm_new,
                removed_code_tokens=rem_toks, added_code_tokens=add_toks,
                created_tables_in_commit=dc["created_tables"],
            )
            if v.op.is_destructive or v.classification == "ROLLBACK_ONLY_FALSE_ALARM":
                vd = v.to_dict()
                vd["strong_migrations_baseline"] = strong_migrations_baseline_verdict(v.op)
                verdicts_out.append(vd)
                verdict_counts[v.classification] = verdict_counts.get(v.classification, 0) + 1
        if verdicts_out:
            commit_records.append({
                "sha": sha, "parent": parent, "date": c["date"], "subject": c["subject"],
                "mig_files": [fp for fp, _ in c["mig_files"]],
                "app_files_touched_in_commit": app_files,
                "orm_models_parsed_old": len(orm_old.models_by_table),
                "raw_sql": "",
                "verdicts": verdicts_out,
            })

    result = {
        "repo": full_name, "ecosystem": "rails", "stars": corpus_entry.get("stars", 0),
        "has_k8s_helm": corpus_entry.get("has_k8s_helm", False),
        "status": "ok", "stats": stats_d, "verdict_counts": verdict_counts,
        "destructive_commits": commit_records,
    }
    out_file.write_text(json.dumps(result, indent=2), encoding="utf-8")
    return result


def audit_post_deploy_migrations() -> dict:
    """Count newly-added post-deploy migrations in Rails (`db/post_migrate/`) and check Django `is_post_deployment`."""
    rails_post = {}
    for repo in ("gitlabhq/gitlabhq", "mastodon/mastodon"):
        rdir = REPOS_DIR / repo_slug(repo)
        if not (rdir / ".git").exists():
            continue
        p = run_git(rdir, ["log", "-n", "150", "--no-merges", "--diff-filter=A", "--name-only", "--format=COMMIT|%H", "--", "db/post_migrate/*.rb"], timeout=60)
        commits = 0
        files = 0
        for ln in p.stdout.decode("utf-8", errors="ignore").splitlines():
            if ln.startswith("COMMIT|"):
                commits += 1
            elif ln.strip().endswith(".rb"):
                files += 1
        rails_post[repo] = {"added_post_migrate_commits_in_last_150": commits, "added_post_migrate_files": files}

    # Check Django is_post_deployment across Sentry's mined destructive commits
    sentry_post_ops = 0
    sentry_checked = 0
    sf = MINED_DIR / "getsentry__sentry.json"
    if sf.exists():
        sd = json.loads(sf.read_text(encoding="utf-8"))
        rdir = REPOS_DIR / "getsentry__sentry"
        for c in sd.get("destructive_commits", []):
            for fp in c.get("mig_files", []):
                sentry_checked += 1
                pr = run_git(rdir, ["show", f"{c['sha']}:{fp}"], timeout=15)
                if pr.returncode == 0 and "is_post_deployment = True" in pr.stdout.decode("utf-8", errors="ignore"):
                    sentry_post_ops += 1
    return {"rails_db_post_migrate": rails_post, "django_sentry_is_post_deployment_true_in_mined": sentry_post_ops, "django_sentry_files_checked": sentry_checked}


def main():
    log_path = V2_DIR / "v3_rq1_recompute.log"
    corpus = json.loads((RESEARCH_DIR / "phase1/corpus_repos.json").read_text(encoding="utf-8"))
    corpus_by_name = {r["repo"]: r for r in corpus}

    with open(log_path, "w", encoding="utf-8") as logf:
        # v3: GitLab is mined by mine_corpus.py itself (git log -n 150); post-deploy ops are counted below.
        post_audit = {"post_deploy_ops_added": Counter(), "data_dependent_added": Counter()}

        # Step 3: Strict FILE-LEVEL Added-Only ('A') filtering across all 110 mined repositories
        prog = Progress(total=len(corpus), label="v3-rq1-file-level-added-only", stream=logf)

        added_records_by_eco = defaultdict(list)  # for downstream unbiased benchmark sampling!
        eco_totals = {e: Counter() for e in ("prisma", "rails", "django")}
        op_totals_added = defaultdict(Counter)
        repo_level_added = []
        mod_only_excluded = Counter()
        added_commit_counts = Counter()
        total_commits_analyzed = Counter()

        for r in corpus:
            rname = r["repo"]
            eco = r["ecosystem"]
            mp = MINED_DIR / f"{repo_slug(rname)}.json"
            if not mp.exists():
                prog.step(note=f"MISS {rname}")
                continue
            d = json.loads(mp.read_text(encoding="utf-8"))
            if d.get("status") != "ok":
                prog.step(note=f"ERR {rname}")
                continue

            total_commits_analyzed[eco] += int(d.get("stats", {}).get("migration_commits_analyzed", 0))
            rdir = REPOS_DIR / repo_slug(rname)
            commits = d.get("destructive_commits", [])

            # Get exact file-level status ('A', 'M', ...) for EVERY commit in one fast local git diff-tree --stdin call
            file_status_map: dict[str, dict[str, str]] = {}
            if commits and (rdir / ".git").exists():
                shas = [c["sha"] for c in commits]
                proc = subprocess.run(
                    ["git", "-C", str(rdir), "-c", "diff.renames=false", "diff-tree",
                     "--no-renames", "-r", "--raw", "--stdin", "--format=COMMIT|%H"],
                    input="\n".join(shas) + "\n",
                    capture_output=True, text=True, encoding="utf-8", errors="ignore", timeout=60,
                )
                cur = None
                for ln in proc.stdout.splitlines():
                    if ln.startswith("COMMIT|"):
                        cur = ln.split("|", 1)[1].strip()
                        file_status_map[cur] = {}
                    elif cur and ln.startswith(":") and "\t" in ln:
                        meta, fp = ln.split("\t", 1)
                        st = meta.split()[-1][:1]
                        file_status_map[cur][fp.strip()] = st

            r_viol = r_safe = r_uncert = r_down = r_safe_add = 0
            r_viol_b = r_viol_both = r_viol_f = 0
            r_safety_assured_viol = 0
            r_ignored_safe = 0

            for c in commits:
                sha = c["sha"]
                fmap = file_status_map.get(sha, {})
                commit_has_added_fwd = False
                for v in c.get("verdicts", []):
                    op = v["op"]
                    fp = op.get("file_path", "")
                    cls = v["classification"]
                    st = fmap.get(fp)
                    # Fallback if file_path wasn't populated (rare): check if any mig_file in commit is 'A'
                    if st is None and c.get("mig_files"):
                        st = "A" if any(fmap.get(mf) == "A" for mf in c["mig_files"]) else "M"

                    if st != "A":
                        mod_only_excluded["ops_total"] += 1
                        mod_only_excluded[cls] += 1
                        continue

                    # Newly added migration file!
                    if "post_deploy" in (v["evidence"].get("notes") or []):
                        post_audit["post_deploy_ops_added"][f"{rname}|{cls}"] += 1
                    if cls == "DATA_DEPENDENT":
                        post_audit["data_dependent_added"][eco] += 1
                        op_totals_added[op["kind"]]["data_dependent"] += 1
                        continue
                    if cls == "ROLLBACK_ONLY_FALSE_ALARM":
                        r_down += 1
                        op_totals_added[op["kind"]]["rollback_only"] += 1
                    elif cls == "SAFE_ADDITIVE":
                        r_safe_add += 1
                    elif cls == "UNCERTAIN":
                        r_uncert += 1
                        op_totals_added[op["kind"]]["uncert"] += 1
                        commit_has_added_fwd = True
                        added_records_by_eco[eco].append({
                            "repo": rname, "ecosystem": eco, "sha": sha, "parent": c["parent"],
                            "date": c["date"], "subject": c["subject"],
                            "file_path": fp, "op": op, "ecv_classification": cls,
                            "evidence": v["evidence"], "raw_sql": c.get("raw_sql", ""),
                        })
                    elif cls == "SAFE_CONTRACT":
                        r_safe += 1
                        op_totals_added[op["kind"]]["safe"] += 1
                        commit_has_added_fwd = True
                        if eco == "rails" and v["evidence"].get("rails_ignored_old"):
                            r_ignored_safe += 1
                        added_records_by_eco[eco].append({
                            "repo": rname, "ecosystem": eco, "sha": sha, "parent": c["parent"],
                            "date": c["date"], "subject": c["subject"],
                            "file_path": fp, "op": op, "ecv_classification": cls,
                            "evidence": v["evidence"], "raw_sql": c.get("raw_sql", ""),
                        })
                    elif cls.startswith("DUAL_VERSION_VIOLATION"):
                        r_viol += 1
                        op_totals_added[op["kind"]]["viol"] += 1
                        commit_has_added_fwd = True
                        if cls.endswith("BACKWARD"):
                            r_viol_b += 1
                        elif cls.endswith("BOTH"):
                            r_viol_both += 1
                        elif cls.endswith("FORWARD"):
                            r_viol_f += 1
                        if eco == "rails" and op.get("has_safety_bypass"):
                            r_safety_assured_viol += 1
                        added_records_by_eco[eco].append({
                            "repo": rname, "ecosystem": eco, "sha": sha, "parent": c["parent"],
                            "date": c["date"], "subject": c["subject"],
                            "file_path": fp, "op": op, "ecv_classification": cls,
                            "evidence": v["evidence"], "raw_sql": c.get("raw_sql", ""),
                        })
                if commit_has_added_fwd:
                    added_commit_counts[eco] += 1

            eco_totals[eco]["repos_ok"] += 1
            eco_totals[eco]["viol"] += r_viol
            eco_totals[eco]["viol_b"] += r_viol_b
            eco_totals[eco]["viol_both"] += r_viol_both
            eco_totals[eco]["viol_f"] += r_viol_f
            eco_totals[eco]["safe"] += r_safe
            eco_totals[eco]["uncert"] += r_uncert
            eco_totals[eco]["down"] += r_down
            eco_totals[eco]["safe_add"] += r_safe_add
            eco_totals[eco]["safety_assured_viol"] += r_safety_assured_viol
            eco_totals[eco]["ignored_safe"] += r_ignored_safe
            if r_viol > 0:
                eco_totals[eco]["repos_with_viol"] += 1

            resolved = r_viol + r_safe
            repo_level_added.append({
                "repo": rname,
                "ecosystem": eco,
                "has_k8s_helm": bool(r.get("has_k8s_helm", False)),
                "stars": r.get("stars", 0),
                "viol": r_viol,
                "safe": r_safe,
                "uncert": r_uncert,
                "down": r_down,
                "safe_add": r_safe_add,
                "resolved": resolved,
                "viol_rate": (r_viol / resolved) if resolved > 0 else None,
            })
            prog.step(note=f"{rname} v={r_viol} s={r_safe} u={r_uncert}")
        prog.done()

        # Compute repository-level non-parametric stats on clean added-only dataset
        active = [x for x in repo_level_added if x["resolved"] > 0]
        by_eco_rates = {e: sorted([x["viol_rate"] for x in active if x["ecosystem"] == e]) for e in ("prisma", "rails", "django")}
        eco_repo_stats = {}
        for e in ("prisma", "rails", "django"):
            arr = by_eco_rates[e]
            q25, med, q75 = np.percentile(arr, [25, 50, 75])
            eco_repo_stats[e] = {
                "n_repos_total": eco_totals[e]["repos_ok"],
                "n_repos_active": len(arr),
                "median_pct": round(float(med) * 100, 1),
                "iqr_pct": [round(float(q25) * 100, 1), round(float(q75) * 100, 1)],
                "mean_pct": round(float(np.mean(arr)) * 100, 1),
            }

        kw_h, kw_p = stats.kruskal(by_eco_rates["prisma"], by_eco_rates["rails"], by_eco_rates["django"])
        pairwise = {}
        for a, b in [("prisma", "rails"), ("django", "rails"), ("prisma", "django")]:
            u, p = stats.mannwhitneyu(by_eco_rates[a], by_eco_rates[b], alternative="two-sided")
            cd, mag = cliffs_delta(by_eco_rates[a], by_eco_rates[b])
            pairwise[f"{a}_vs_{b}"] = {"u": float(u), "p": float(p), "cliffs_delta": cd, "magnitude": mag}

        # K8s/Helm verified analysis on added-only dataset
        k8s_active = [x for x in active if x["has_k8s_helm"]]
        non_k8s_active = [x for x in active if not x["has_k8s_helm"]]
        k8s_v = sum(x["viol"] for x in k8s_active)
        k8s_s = sum(x["safe"] for x in k8s_active)
        nk_v = sum(x["viol"] for x in non_k8s_active)
        nk_s = sum(x["safe"] for x in non_k8s_active)
        _, k8s_op_p = stats.fisher_exact([[k8s_v, k8s_s], [nk_v, nk_s]])
        k8s_rates = [x["viol_rate"] for x in k8s_active]
        nk_rates = [x["viol_rate"] for x in non_k8s_active]
        k8s_u, k8s_repo_p = stats.mannwhitneyu(k8s_rates, nk_rates, alternative="two-sided")
        k8s_cd, k8s_mag = cliffs_delta(k8s_rates, nk_rates)
        _rng = np.random.default_rng(20261121)
        _diffs = [float(np.median(_rng.choice(k8s_rates, len(k8s_rates))) - np.median(_rng.choice(nk_rates, len(nk_rates)))) for _ in range(10000)]
        k8s_median_diff_ci = [round(100 * float(x), 1) for x in np.percentile(_diffs, [2.5, 97.5])]

        # Stratified K8s
        k8s_strat = {}
        for e in ("prisma", "rails", "django"):
            ka = [x for x in active if x["ecosystem"] == e and x["has_k8s_helm"]]
            na = [x for x in active if x["ecosystem"] == e and not x["has_k8s_helm"]]
            kv, ks = sum(x["viol"] for x in ka), sum(x["safe"] for x in ka)
            nv, ns = sum(x["viol"] for x in na), sum(x["safe"] for x in na)
            kr = [x["viol_rate"] for x in ka]
            nr = [x["viol_rate"] for x in na]
            k8s_strat[e] = {
                "k8s_active_n": len(ka), "non_k8s_active_n": len(na),
                "k8s_ops_rate_pct": round(100 * kv / (kv + ks), 1) if (kv + ks) else None,
                "non_k8s_ops_rate_pct": round(100 * nv / (nv + ns), 1) if (nv + ns) else None,
                "k8s_repo_median_pct": round(float(np.median(kr)) * 100, 1) if kr else None,
                "non_k8s_repo_median_pct": round(float(np.median(nr)) * 100, 1) if nr else None,
                "repo_mwu_p": float(stats.mannwhitneyu(kr, nr, alternative="two-sided")[1]) if (kr and nr) else None,
            }

        # Pooled summary
        pooled_by_eco = {}
        tot_v = tot_s = tot_u = tot_d = tot_sa = 0
        for e in ("prisma", "rails", "django"):
            c = eco_totals[e]
            v, s, u, dn, sa = c["viol"], c["safe"], c["uncert"], c["down"], c["safe_add"]
            tot_v += v; tot_s += s; tot_u += u; tot_d += dn; tot_sa += sa
            res = v + s
            fwd_all = v + s + u
            pooled_by_eco[e] = {
                "repos_ok": c["repos_ok"],
                "repos_with_viol": c["repos_with_viol"],
                "commits_analyzed": total_commits_analyzed[e],
                "added_fwd_destructive_commits": added_commit_counts[e],
                "viol": v, "viol_b": c["viol_b"], "viol_both": c["viol_both"], "viol_f": c["viol_f"],
                "safe": s, "resolved": res, "uncert": u, "down": dn, "safe_add": sa,
                "viol_rate_pct": round(100 * v / res, 1) if res else 0.0,
                "viol_ci95_pct": wilson_ci(v, res),
                "resolution_coverage_pct": round(100 * res / fwd_all, 1) if fwd_all else 0.0,
                "safety_assured_viol": c["safety_assured_viol"],
                "ignored_safe": c["ignored_safe"],
            }

        tot_res = tot_v + tot_s
        tot_fwd = tot_res + tot_u
        cont_eco = [[pooled_by_eco[e]["viol"], pooled_by_eco[e]["safe"]] for e in ("prisma", "rails", "django")]
        chi2_e, p_e, dof_e, _ = stats.chi2_contingency(cont_eco)

        summary = {
            "repos_mined_total": sum(eco_totals[e]["repos_ok"] for e in ("prisma", "rails", "django")),
            "commits_analyzed_total": sum(total_commits_analyzed.values()),
            "added_fwd_destructive_commits_total": sum(added_commit_counts.values()),
            "modified_only_ops_excluded": dict(mod_only_excluded),
            "post_deploy_audit": {k: dict(v) for k, v in post_audit.items()},
            "pooled_total": {
                "viol": tot_v, "safe": tot_s, "resolved": tot_res, "uncert": tot_u,
                "down": tot_d, "safe_add": tot_sa,
                "total_extracted_added": tot_v + tot_s + tot_u + tot_d + tot_sa,
                "viol_rate_pct": round(100 * tot_v / tot_res, 1),
                "viol_ci95_pct": wilson_ci(tot_v, tot_res),
                "resolution_coverage_pct": round(100 * tot_res / tot_fwd, 1),
                "repos_with_viol": sum(eco_totals[e]["repos_with_viol"] for e in ("prisma", "rails", "django")),
            },
            "pooled_by_ecosystem": pooled_by_eco,
            "chi2_ecosystems": {"chi2": round(float(chi2_e), 2), "p": float(p_e), "dof": int(dof_e)},
            "repo_level_ecosystem": eco_repo_stats,
            "kruskal_wallis": {"H": round(float(kw_h), 2), "p": float(kw_p)},
            "pairwise_mwu": pairwise,
            "k8s_vs_non_k8s_added_only": {
                "k8s_active_n": len(k8s_active), "non_k8s_active_n": len(non_k8s_active),
                "k8s_ops": {"viol": k8s_v, "safe": k8s_s, "rate_pct": round(100 * k8s_v / (k8s_v + k8s_s), 1)},
                "non_k8s_ops": {"viol": nk_v, "safe": nk_s, "rate_pct": round(100 * nk_v / (nk_v + nk_s), 1)},
                "op_fisher_p": float(k8s_op_p),
                "k8s_repo_median_pct": round(float(np.median(k8s_rates)) * 100, 1),
                "k8s_repo_iqr_pct": [round(float(x) * 100, 1) for x in np.percentile(k8s_rates, [25, 75])],
                "non_k8s_repo_median_pct": round(float(np.median(nk_rates)) * 100, 1),
                "non_k8s_repo_iqr_pct": [round(float(x) * 100, 1) for x in np.percentile(nk_rates, [25, 75])],
                "repo_mwu_u": float(k8s_u), "repo_mwu_p": float(k8s_repo_p),
                "cliffs_delta": k8s_cd, "magnitude": k8s_mag,
                "median_diff_pct_bootstrap_ci95": k8s_median_diff_ci,
                "stratified": k8s_strat,
            },
            "by_operator_added_only": {
                k: {
                    "viol": v["viol"], "safe": v["safe"], "uncert": v["uncert"], "rollback_only": v["rollback_only"],
                    "data_dependent": v["data_dependent"],
                    "viol_rate_pct": round(100 * v["viol"] / (v["viol"] + v["safe"]), 1) if (v["viol"] + v["safe"]) else 0.0,
                }
                for k, v in sorted(op_totals_added.items(), key=lambda kv: -(kv[1]["viol"] + kv[1]["safe"]))
            },
            "gitlab_summary": next((x for x in repo_level_added if x["repo"] == "gitlabhq/gitlabhq"), None),
        }
        (V2_DIR / "rq1_added_only_summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
        (V2_DIR / "added_only_pool.json").write_text(json.dumps(added_records_by_eco, indent=2), encoding="utf-8")
        print(json.dumps(summary, indent=2), file=logf, flush=True)
        print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
