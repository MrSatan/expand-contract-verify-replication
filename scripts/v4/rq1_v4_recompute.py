"""v4 RQ1 recomputation over the v4 re-mined corpus (research/v4/mined_repos).

Changes vs v3:
  * newly added files only, but additionally excluding
      - squashed Django migrations (verdict SQUASHED_REPLAY, `replaces = [...]`),
      - copied or moved files (the added blob already exists in the parent tree),
      - re-added files (the same path was deleted by an earlier commit, e.g. reverts);
  * cluster (repository) bootstrap intervals next to Wilson intervals;
  * bounds for UNCERTAIN operations; backward / both / forward breakdown;
  * K8s comparison: pooled rates reported descriptively, inference at repository level;
  * mig_only / cochanged commit stratum stored for every pool record (IPW frame for RQ3).
Outputs: research/v4/rq1_v4_summary.json, research/v4/added_only_pool_v4.json, log research/v4/rq1_v4.log
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

TASK = Path(os.environ["ECV_TASK_DIR"]).resolve() if "ECV_TASK_DIR" in os.environ else Path(__file__).resolve().parent.parent.parent
IS_PKG = (TASK / "data" / "v4").exists() and not (TASK / "research" / "v4").exists()
RESEARCH = TASK / "data" if IS_PKG else TASK / "research"
V4 = RESEARCH / "v4"
MINED = (RESEARCH / "rq1" / "mined_repos") if IS_PKG else (V4 / "mined_repos")
CORPUS_JSON = (RESEARCH / "corpus_repos.json") if IS_PKG else (RESEARCH / "phase1" / "corpus_repos.json")
REPOS = TASK / "tmp" / "repos"
for _p in (TASK / "scripts", TASK.parent.parent / "scripts"):
    if _p.exists():
        sys.path.insert(0, str(_p))
from progress import Progress  # noqa: E402

PATHSPEC = {
    "prisma": [":(glob)**/migrations/**/migration.sql"],
    "rails": [":(glob)**/db/migrate/*.rb", ":(glob)**/db/post_migrate/*.rb"],
    "django": [":(glob)**/migrations/0*.py"],
}
CODE_EXT = {"prisma": (".ts", ".tsx", ".js", ".jsx", ".prisma"), "rails": (".rb",), "django": (".py",)}
RNG_SEED = 20261121
B = 5000


def is_mig_path(eco: str, p: str) -> bool:
    q = "/" + p.replace("\\", "/")
    if eco == "prisma":
        return "/migrations/" in q and q.endswith("/migration.sql")
    if eco == "rails":
        return ("/db/migrate/" in q or "/db/post_migrate/" in q) and q.endswith(".rb")
    return "/migrations/" in q and Path(q).name.startswith("0") and q.endswith(".py")


def slug(n: str) -> str:
    return n.replace("/", "__")


def git(rdir: Path, args: list[str], stdin: str | None = None, timeout: int = 120) -> str:
    p = subprocess.run(["git", "-C", str(rdir)] + args, input=stdin, capture_output=True, text=True,
                       encoding="utf-8", errors="ignore", timeout=timeout)
    return p.stdout


def wilson(k: int, n: int, z: float = 1.96) -> list[float]:
    if n == 0:
        return [0.0, 0.0]
    p = k / n
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    m = z * math.sqrt((p * (1 - p) + z * z / (4 * n)) / n) / d
    return [round(100 * max(0, c - m), 1), round(100 * min(1, c + m), 1)]


def cliffs(x, y):
    if not x or not y:
        return 0.0, "negligible"
    more = sum(1 for a in x for b in y if a > b)
    less = sum(1 for a in x for b in y if a < b)
    d = (more - less) / (len(x) * len(y))
    a = abs(d)
    return round(d, 3), ("negligible" if a < 0.147 else "small" if a < 0.33 else "medium" if a < 0.474 else "large")


def cluster_boot(rows: list[dict], rng, b: int = B) -> list[float]:
    """Repository-level bootstrap for the pooled rate sum(viol)/sum(viol+safe)."""
    v = np.array([r["viol"] for r in rows], float)
    n = np.array([r["viol"] + r["safe"] for r in rows], float)
    if n.sum() == 0:
        return [0.0, 0.0]
    idx = rng.integers(0, len(rows), size=(b, len(rows)))
    rates = v[idx].sum(1) / np.maximum(n[idx].sum(1), 1)
    lo, hi = np.percentile(rates, [2.5, 97.5])
    return [round(100 * lo, 1), round(100 * hi, 1)]


def file_status_and_oids(rdir: Path, shas: list[str]) -> dict[str, dict[str, tuple[str, str]]]:
    out: dict[str, dict[str, tuple[str, str]]] = {}
    txt = git(rdir, ["-c", "diff.renames=false", "diff-tree", "--no-renames", "-r", "--raw", "--no-abbrev",
                     "--stdin", "--format=COMMIT|%H"], stdin="\n".join(shas) + "\n", timeout=180)
    cur = None
    for ln in txt.splitlines():
        if ln.startswith("COMMIT|"):
            cur = ln.split("|", 1)[1].strip()
            out[cur] = {}
        elif cur and ln.startswith(":") and "\t" in ln:
            meta, fp = ln.split("\t", 1)
            mp = meta.split()
            out[cur][fp.strip()] = (mp[-1][:1], mp[3])
    return out


def parent_oids(rdir: Path, parent: str) -> set[str]:
    txt = git(rdir, ["ls-tree", "-r", parent], timeout=120)
    return {ln.split()[2] for ln in txt.splitlines() if len(ln.split()) >= 3}


def deleted_paths(rdir: Path, eco: str) -> dict[str, list[int]]:
    txt = git(rdir, ["log", "--no-merges", "--diff-filter=D", "--no-renames", "--format=COMMIT|%ct", "--name-only", "--"]
              + PATHSPEC[eco], timeout=180)
    out: dict[str, list[int]] = defaultdict(list)
    t = None
    for ln in txt.splitlines():
        if ln.startswith("COMMIT|"):
            t = int(ln.split("|")[1])
        elif ln.strip() and t is not None:
            out[ln.strip()].append(t)
    return out


def commit_time(rdir: Path, shas: list[str]) -> dict[str, int]:
    txt = git(rdir, ["show", "-s", "--format=%H %ct"] + shas, timeout=120) if shas else ""
    return {a: int(b) for a, b in (ln.split() for ln in txt.splitlines() if len(ln.split()) == 2)}


def commit_strata(rdir: Path, eco: str, shas: list[str]) -> dict[str, str]:
    txt = git(rdir, ["-c", "diff.renames=false", "diff-tree", "--no-renames", "-r", "--name-only", "--stdin",
                     "--format=COMMIT|%H"], stdin="\n".join(shas) + "\n", timeout=180)
    files = defaultdict(list)
    cur = None
    for ln in txt.splitlines():
        if ln.startswith("COMMIT|"):
            cur = ln.split("|", 1)[1].strip()
        elif cur and ln.strip():
            files[cur].append(ln.strip())
    out = {}
    for sha in shas:
        non_mig = 0
        for f in files.get(sha, []):
            low = f.lower()
            if any(k in low for k in ("/migrations/", "/migrate/", "db/migrate/", "db/post_migrate/", "/test", "/spec",
                                      "__tests__", "schema.rb", "structure.sql")):
                continue
            if low.endswith(CODE_EXT[eco]):
                non_mig += 1
        out[sha] = "cochanged" if non_mig else "mig_only"
    return out


def main() -> None:
    corpus = json.loads(CORPUS_JSON.read_text(encoding="utf-8"))
    log = open(V4 / "rq1_v4.log", "w", encoding="utf-8")
    prog = Progress(total=len(corpus), label="v4-rq1-recompute", stream=log)

    pool = defaultdict(list)
    excl = Counter()
    excl_by_cls = defaultdict(Counter)
    excl_examples = defaultdict(list)
    eco_tot = {e: Counter() for e in PATHSPEC}
    op_tot = defaultdict(Counter)
    repo_rows = []
    commits_analyzed = Counter()
    added_commits = Counter()
    post_deploy = Counter()
    n_added_mig_per_commit = []

    for r in corpus:
        name, eco = r["repo"], r["ecosystem"]
        mp = MINED / f"{slug(name)}.json"
        if not mp.exists():
            prog.step(note=f"MISS {name}")
            continue
        d = json.loads(mp.read_text(encoding="utf-8"))
        if d.get("status") != "ok":
            prog.step(note=f"ERR {name}")
            continue
        commits_analyzed[eco] += int(d["stats"]["migration_commits_analyzed"])
        rdir = REPOS / slug(name)
        cs = d.get("destructive_commits", [])
        shas = [c["sha"] for c in cs]
        fmap = file_status_and_oids(rdir, shas) if cs else {}
        dels = deleted_paths(rdir, eco) if cs else {}
        ctime = commit_time(rdir, shas) if cs else {}
        strata = commit_strata(rdir, eco, shas) if cs else {}
        poid_cache: dict[str, set[str]] = {}

        rv = Counter()
        for c in cs:
            sha, parent = c["sha"], c["parent"]
            fm = fmap.get(sha, {})
            added_mig = [fp for fp in c.get("mig_files", []) if fm.get(fp, ("?",))[0] == "A"]
            n_added_mig_per_commit.append(len(added_mig))
            # v4: migration files deleted in the same commit (manual squash, renamed or rewritten history)
            del_dirs = {str(Path(p).parent) for p, (s, _) in fm.items() if s == "D" and is_mig_path(eco, p)}
            has_fwd = False
            for v in c["verdicts"]:
                op, cls = v["op"], v["classification"]
                fp = op.get("file_path") or ""
                st, oid = fm.get(fp, ("?", ""))
                if st != "A":
                    excl["edited_existing_file"] += 1
                    excl_by_cls["edited_existing_file"][cls] += 1
                    continue
                reason = None
                if cls == "SQUASHED_REPLAY":
                    reason = "squashed_migration"
                elif str(Path(fp).parent) in del_dirs:
                    reason = "rewritten_history"
                else:
                    if oid:
                        if parent not in poid_cache:
                            poid_cache[parent] = parent_oids(rdir, parent)
                        if oid in poid_cache[parent]:
                            reason = "copied_or_moved_file"
                    if reason is None and any(t < ctime.get(sha, 1 << 40) for t in dels.get(fp, [])):
                        reason = "readded_file"
                if reason:
                    excl[reason] += 1
                    excl_by_cls[reason][cls] += 1
                    if len(excl_examples[reason]) < 8:
                        excl_examples[reason].append(f"{name}@{sha[:8]} {fp} ({c['subject'][:60]})")
                    continue
                if "post_deploy" in (v["evidence"].get("notes") or []):
                    post_deploy[f"{name}|{cls}"] += 1
                kind = op["kind"]
                rec = {"repo": name, "ecosystem": eco, "sha": sha, "parent": parent, "date": c["date"],
                       "subject": c["subject"], "file_path": fp, "op": op, "ecv_classification": cls,
                       "evidence": v["evidence"], "git_stratum": strata.get(sha, "cochanged")}
                if cls == "DATA_DEPENDENT":
                    rv["dd"] += 1
                    op_tot[kind]["data_dependent"] += 1
                    pool[eco].append(rec)
                    has_fwd = True
                elif cls == "ROLLBACK_ONLY_FALSE_ALARM":
                    rv["down"] += 1
                    op_tot[kind]["rollback_only"] += 1
                elif cls == "SAFE_ADDITIVE":
                    rv["safe_add"] += 1
                elif cls == "UNCERTAIN":
                    rv["uncert"] += 1
                    op_tot[kind]["uncert"] += 1
                    pool[eco].append(rec)
                    has_fwd = True
                elif cls == "SAFE_CONTRACT":
                    rv["safe"] += 1
                    op_tot[kind]["safe"] += 1
                    if eco == "rails" and v["evidence"].get("rails_ignored_old"):
                        rv["ignored_safe"] += 1
                    pool[eco].append(rec)
                    has_fwd = True
                elif cls.startswith("DUAL_VERSION_VIOLATION"):
                    rv["viol"] += 1
                    op_tot[kind]["viol"] += 1
                    rv["viol_" + cls.rsplit("_", 1)[1].lower()] += 1
                    if eco == "rails" and op.get("has_safety_bypass"):
                        rv["safety_assured_viol"] += 1
                    pool[eco].append(rec)
                    has_fwd = True
            if has_fwd:
                added_commits[eco] += 1
        for k, val in rv.items():
            eco_tot[eco][k] += val
        eco_tot[eco]["repos_ok"] += 1
        if rv["viol"]:
            eco_tot[eco]["repos_with_viol"] += 1
        res = rv["viol"] + rv["safe"]
        repo_rows.append({"repo": name, "ecosystem": eco, "has_k8s_helm": bool(r.get("has_k8s_helm")),
                          "viol": rv["viol"], "safe": rv["safe"], "uncert": rv["uncert"], "resolved": res,
                          "viol_rate": rv["viol"] / res if res else None})
        prog.step(note=f"{name} v={rv['viol']} s={rv['safe']} u={rv['uncert']}")
    prog.done()

    rng = np.random.default_rng(RNG_SEED)
    pooled = {}
    T = Counter()
    for e in PATHSPEC:
        c = eco_tot[e]
        for k, val in c.items():
            T[k] += val
        res = c["viol"] + c["safe"]
        fwd = res + c["uncert"]
        rows_e = [x for x in repo_rows if x["ecosystem"] == e]
        pooled[e] = {
            "repos_ok": c["repos_ok"], "repos_with_viol": c["repos_with_viol"],
            "commits_analyzed": commits_analyzed[e], "added_fwd_destructive_commits": added_commits[e],
            "viol": c["viol"], "viol_backward": c["viol_backward"], "viol_both": c["viol_both"], "viol_forward": c["viol_forward"],
            "safe": c["safe"], "resolved": res, "uncert": c["uncert"], "down": c["down"], "safe_add": c["safe_add"],
            "data_dependent": c["dd"],
            "viol_rate_pct": round(100 * c["viol"] / res, 1) if res else None,
            "wilson_ci95_pct": wilson(c["viol"], res),
            "cluster_boot_ci95_pct": cluster_boot(rows_e, rng),
            "coverage_pct": round(100 * res / fwd, 1) if fwd else None,
            "bounds_if_uncertain_all_safe_pct": round(100 * c["viol"] / fwd, 1) if fwd else None,
            "bounds_if_uncertain_all_viol_pct": round(100 * (c["viol"] + c["uncert"]) / fwd, 1) if fwd else None,
            "reverse_order_rate_pct": round(100 * (c["viol_both"] + c["viol_forward"]) / res, 1) if res else None,
            "safety_assured_viol": c["safety_assured_viol"], "ignored_safe": c["ignored_safe"],
        }
    res_t = T["viol"] + T["safe"]
    fwd_t = res_t + T["uncert"]
    total = {
        "viol": T["viol"], "viol_backward": T["viol_backward"], "viol_both": T["viol_both"], "viol_forward": T["viol_forward"],
        "safe": T["safe"], "resolved": res_t, "uncert": T["uncert"], "down": T["down"], "safe_add": T["safe_add"],
        "data_dependent": T["dd"],
        "destructive_ops_in_added_files": res_t + T["uncert"] + T["down"] + T["safe_add"] + T["dd"],
        "viol_rate_pct": round(100 * T["viol"] / res_t, 1), "wilson_ci95_pct": wilson(T["viol"], res_t),
        "cluster_boot_ci95_pct": cluster_boot(repo_rows, rng),
        "coverage_pct": round(100 * res_t / fwd_t, 1),
        "bounds_if_uncertain_all_safe_pct": round(100 * T["viol"] / fwd_t, 1),
        "bounds_if_uncertain_all_viol_pct": round(100 * (T["viol"] + T["uncert"]) / fwd_t, 1),
        "reverse_order_rate_pct": round(100 * (T["viol_both"] + T["viol_forward"]) / res_t, 1),
        "repos_with_viol": sum(eco_tot[e]["repos_with_viol"] for e in PATHSPEC),
        "safety_assured_viol": T["safety_assured_viol"], "ignored_safe": T["ignored_safe"],
    }

    active = [x for x in repo_rows if x["resolved"] > 0]
    by_eco = {e: [x["viol_rate"] for x in active if x["ecosystem"] == e] for e in PATHSPEC}
    repo_stats = {e: {"n_active": len(v), "median_pct": round(100 * float(np.median(v)), 1),
                      "iqr_pct": [round(100 * float(q), 1) for q in np.percentile(v, [25, 75])]} for e, v in by_eco.items()}
    kw = stats.kruskal(*by_eco.values())
    pair = {}
    for a, b in [("django", "rails"), ("django", "prisma"), ("prisma", "rails")]:
        u = stats.mannwhitneyu(by_eco[a], by_eco[b], alternative="two-sided")
        cd, mag = cliffs(by_eco[a], by_eco[b])
        pair[f"{a}_vs_{b}"] = {"p": float(u.pvalue), "cliffs_delta": cd, "magnitude": mag}

    k8 = [x for x in active if x["has_k8s_helm"]]
    nk = [x for x in active if not x["has_k8s_helm"]]
    kr, nr = [x["viol_rate"] for x in k8], [x["viol_rate"] for x in nk]
    mw = stats.mannwhitneyu(kr, nr, alternative="two-sided")
    kcd, kmag = cliffs(kr, nr)
    diffs = [float(np.median(rng.choice(kr, len(kr))) - np.median(rng.choice(nr, len(nr)))) for _ in range(B)]
    pdiffs = []
    for _ in range(B):
        a = [k8[i] for i in rng.integers(0, len(k8), len(k8))]
        b = [nk[i] for i in rng.integers(0, len(nk), len(nk))]
        pa = sum(x["viol"] for x in a) / max(1, sum(x["resolved"] for x in a))
        pb = sum(x["viol"] for x in b) / max(1, sum(x["resolved"] for x in b))
        pdiffs.append(pa - pb)
    strat = {}
    for e in PATHSPEC:
        ka = [x for x in active if x["ecosystem"] == e and x["has_k8s_helm"]]
        na = [x for x in active if x["ecosystem"] == e and not x["has_k8s_helm"]]
        strat[e] = {
            "k8s_n": len(ka), "non_k8s_n": len(na),
            "k8s_pooled_pct": round(100 * sum(x["viol"] for x in ka) / max(1, sum(x["resolved"] for x in ka)), 1) if ka else None,
            "non_k8s_pooled_pct": round(100 * sum(x["viol"] for x in na) / max(1, sum(x["resolved"] for x in na)), 1) if na else None,
            "k8s_repo_median_pct": round(100 * float(np.median([x["viol_rate"] for x in ka])), 1) if ka else None,
            "non_k8s_repo_median_pct": round(100 * float(np.median([x["viol_rate"] for x in na])), 1) if na else None,
            "repo_mwu_p": float(stats.mannwhitneyu([x["viol_rate"] for x in ka], [x["viol_rate"] for x in na],
                                                   alternative="two-sided").pvalue) if (ka and na) else None,
        }
    k8s = {
        "k8s_n": len(k8), "non_k8s_n": len(nk),
        "k8s_pooled_pct": round(100 * sum(x["viol"] for x in k8) / sum(x["resolved"] for x in k8), 1),
        "non_k8s_pooled_pct": round(100 * sum(x["viol"] for x in nk) / sum(x["resolved"] for x in nk), 1),
        "pooled_diff_cluster_boot_ci95_pp": [round(100 * float(q), 1) for q in np.percentile(pdiffs, [2.5, 97.5])],
        "k8s_repo_median_pct": round(100 * float(np.median(kr)), 1), "non_k8s_repo_median_pct": round(100 * float(np.median(nr)), 1),
        "repo_mwu_p": float(mw.pvalue), "cliffs_delta": kcd, "magnitude": kmag,
        "median_diff_boot_ci95_pp": [round(100 * float(q), 1) for q in np.percentile(diffs, [2.5, 97.5])],
        "stratified": strat,
    }
    hist = Counter(min(n, 20) for n in n_added_mig_per_commit)
    summary = {
        "repos_mined": sum(eco_tot[e]["repos_ok"] for e in PATHSPEC),
        "repos_with_resolved_ops": len(active),
        "commits_analyzed": sum(commits_analyzed.values()),
        "added_fwd_destructive_commits": sum(added_commits.values()),
        "excluded_ops": dict(excl),
        "excluded_by_verdict": {k: dict(v) for k, v in excl_by_cls.items()},
        "excluded_examples": dict(excl_examples),
        "post_deploy_ops": dict(post_deploy),
        "pooled_total": total,
        "pooled_by_ecosystem": pooled,
        "repo_level": repo_stats,
        "kruskal_wallis": {"H": round(float(kw.statistic), 2), "p": float(kw.pvalue)},
        "pairwise_mwu": pair,
        "k8s": k8s,
        "by_operator": {k: {**dict(v), "viol_rate_pct": round(100 * v["viol"] / (v["viol"] + v["safe"]), 1) if (v["viol"] + v["safe"]) else None}
                        for k, v in sorted(op_tot.items(), key=lambda kv: -(kv[1]["viol"] + kv[1]["safe"]))},
        "added_migration_files_per_destructive_commit_hist_capped20": dict(sorted(hist.items())),
        "repo_rows": repo_rows,
    }
    (V4 / "rq1_v4_summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    (V4 / "added_only_pool_v4.json").write_text(json.dumps(pool), encoding="utf-8")
    out = {k: v for k, v in summary.items() if k != "repo_rows"}
    print(json.dumps(out, indent=2), file=log, flush=True)
    print(json.dumps(out, indent=2))


if __name__ == "__main__":
    main()
