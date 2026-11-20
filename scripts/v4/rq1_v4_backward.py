"""v4 RQ1 statistics for backward compatibility (C_{t-1} on S_t), computed from research/v4/added_only_pool_v4.json.

A violation is DUAL_VERSION_VIOLATION_BACKWARD or _BOTH. Forward-only violations break C_t on S_{t-1}
(rollout-before-migrate) and are counted as backward-safe here and reported separately.
Also: validity filter of the RQ4 full-history replay (v3 runs) against the v4 exclusion rules.
Writes research/v4/rq1_v4_backward.json
"""
from __future__ import annotations

import importlib.util
import json
import os
from collections import Counter, defaultdict
from pathlib import Path

import numpy as np
from scipy import stats

TASK = Path(os.environ["ECV_TASK_DIR"]).resolve() if "ECV_TASK_DIR" in os.environ else Path(__file__).resolve().parent.parent.parent
IS_PKG = (TASK / "data" / "v4").exists() and not (TASK / "research" / "v4").exists()
R = TASK / "data" if IS_PKG else TASK / "research"
V4 = R / "v4"
SCRIPTS_V4 = (TASK / "scripts" / "v4") if IS_PKG else V4
CORPUS_JSON = (R / "corpus_repos.json") if IS_PKG else (R / "phase1" / "corpus_repos.json")
FULL_SCHEMA_JSON = (R / "rq4" / "full_schema_v3_results.json") if IS_PKG else (R / "v3" / "full_schema_v3_results.json")
spec = importlib.util.spec_from_file_location("rq1", SCRIPTS_V4 / "rq1_v4_recompute.py")
rq1 = importlib.util.module_from_spec(spec)
spec.loader.exec_module(rq1)

BACK = ("DUAL_VERSION_VIOLATION_BACKWARD", "DUAL_VERSION_VIOLATION_BOTH")


def main() -> None:
    pool = json.loads((V4 / "added_only_pool_v4.json").read_text(encoding="utf-8"))
    corpus = json.loads(CORPUS_JSON.read_text(encoding="utf-8"))
    k8s = {c["repo"]: bool(c.get("has_k8s_helm")) for c in corpus}
    eco_of = {c["repo"]: c["ecosystem"] for c in corpus}
    rows = defaultdict(Counter)
    ops = defaultdict(Counter)
    for eco, L in pool.items():
        for x in L:
            c = x["ecv_classification"]
            k = "viol" if c in BACK else "fwd_only" if c == "DUAL_VERSION_VIOLATION_FORWARD" else \
                "safe" if c == "SAFE_CONTRACT" else "uncert" if c == "UNCERTAIN" else "dd" if c == "DATA_DEPENDENT" else None
            if k is None:
                continue
            rows[x["repo"]][k] += 1
            ops[x["op"]["kind"]][k] += 1
            if c == "DUAL_VERSION_VIOLATION_BOTH":
                rows[x["repo"]]["both"] += 1
    repo_rows = []
    for repo in eco_of:
        c = rows.get(repo, Counter())
        safe = c["safe"] + c["fwd_only"]
        res = c["viol"] + safe
        repo_rows.append({"repo": repo, "ecosystem": eco_of[repo], "k8s": k8s[repo], "viol": c["viol"], "safe": safe,
                          "safe_contract": c["safe"], "fwd_only": c["fwd_only"], "both": c["both"], "uncert": c["uncert"],
                          "resolved": res, "rate": c["viol"] / res if res else None})
    rng = np.random.default_rng(20261121)

    def agg(sub):
        v = sum(r["viol"] for r in sub)
        s = sum(r["safe"] for r in sub)
        u = sum(r["uncert"] for r in sub)
        res = v + s
        return {"viol": v, "safe": s, "safe_contract": sum(r["safe_contract"] for r in sub),
                "fwd_only": sum(r["fwd_only"] for r in sub), "both": sum(r["both"] for r in sub), "uncert": u,
                "resolved": res, "rate_pct": round(100 * v / res, 1), "wilson_ci95_pct": rq1.wilson(v, res),
                "cluster_ci95_pct": rq1.cluster_boot(sub, rng), "coverage_pct": round(100 * res / (res + u), 1),
                "bound_uncert_safe_pct": round(100 * v / (res + u), 1), "bound_uncert_viol_pct": round(100 * (v + u) / (res + u), 1),
                "reverse_order_pct": round(100 * (sum(r["both"] for r in sub) + sum(r["fwd_only"] for r in sub)) / res, 1),
                "repos_with_viol": sum(1 for r in sub if r["viol"])}
    out = {"total": agg(repo_rows), "by_ecosystem": {e: agg([r for r in repo_rows if r["ecosystem"] == e]) for e in ("prisma", "rails", "django")}}
    active = [r for r in repo_rows if r["resolved"]]
    by = {e: [r["rate"] for r in active if r["ecosystem"] == e] for e in ("prisma", "rails", "django")}
    out["repo_level"] = {e: {"n": len(v), "median_pct": round(100 * float(np.median(v)), 1),
                             "iqr_pct": [round(100 * float(q), 1) for q in np.percentile(v, [25, 75])]} for e, v in by.items()}
    out["n_active_repos"] = len(active)
    kw = stats.kruskal(*by.values())
    out["kruskal_wallis"] = {"H": round(float(kw.statistic), 2), "p": float(kw.pvalue)}
    out["pairwise"] = {}
    pw_raw = []
    for a, b in [("django", "rails"), ("django", "prisma"), ("prisma", "rails")]:
        u = stats.mannwhitneyu(by[a], by[b], alternative="two-sided")
        pw_raw.append((f"{a}_vs_{b}", float(u.pvalue), rq1.cliffs(by[a], by[b])))
    # Holm-Bonferroni step-down correction (monotone) over the 3 ecosystem pairwise comparisons
    running_max = 0.0
    for rank, (k_pw, pv, cd) in enumerate(sorted(pw_raw, key=lambda x: x[1])):
        running_max = max(running_max, min(1.0, pv * (len(pw_raw) - rank)))
        out["pairwise"][k_pw] = {"p": pv, "p_holm": running_max, "cliffs_delta": cd}
    k8 = [r for r in active if r["k8s"]]
    nk = [r for r in active if not r["k8s"]]
    kr, nr = [r["rate"] for r in k8], [r["rate"] for r in nk]
    pd = []
    for _ in range(5000):
        a = [k8[i] for i in rng.integers(0, len(k8), len(k8))]
        b = [nk[i] for i in rng.integers(0, len(nk), len(nk))]
        pd.append(sum(x["viol"] for x in a) / max(1, sum(x["resolved"] for x in a)) - sum(x["viol"] for x in b) / max(1, sum(x["resolved"] for x in b)))
    md = [float(np.median(rng.choice(kr, len(kr))) - np.median(rng.choice(nr, len(nr)))) for _ in range(5000)]
    out["k8s"] = {"n_k8s": len(k8), "n_other": len(nk),
                  "pooled_k8s_pct": round(100 * sum(r["viol"] for r in k8) / sum(r["resolved"] for r in k8), 1),
                  "pooled_other_pct": round(100 * sum(r["viol"] for r in nk) / sum(r["resolved"] for r in nk), 1),
                  "pooled_diff_cluster_ci95_pp": [round(100 * float(q), 1) for q in np.percentile(pd, [2.5, 97.5])],
                  "median_k8s_pct": round(100 * float(np.median(kr)), 1), "median_other_pct": round(100 * float(np.median(nr)), 1),
                  "median_diff_ci95_pp": [round(100 * float(q), 1) for q in np.percentile(md, [2.5, 97.5])],
                  "mwu_p": float(stats.mannwhitneyu(kr, nr, alternative="two-sided").pvalue), "cliffs": rq1.cliffs(kr, nr),
                  "by_ecosystem_repo_medians": {e: {"k8s_n": sum(1 for r in k8 if r["ecosystem"] == e),
                                                    "k8s_median_pct": round(100 * float(np.median([r["rate"] for r in k8 if r["ecosystem"] == e])), 1) if any(r["ecosystem"] == e for r in k8) else None,
                                                    "other_median_pct": round(100 * float(np.median([r["rate"] for r in nk if r["ecosystem"] == e])), 1)}
                                                for e in ("prisma", "rails", "django")}}
    out["by_operator"] = {k: {"viol": v["viol"], "safe": v["safe"] + v["fwd_only"], "uncert": v["uncert"], "dd": v["dd"],
                              "rate_pct": round(100 * v["viol"] / (v["viol"] + v["safe"] + v["fwd_only"]), 1) if (v["viol"] + v["safe"] + v["fwd_only"]) else None}
                          for k, v in sorted(ops.items(), key=lambda kv: -(kv[1]["viol"] + kv[1]["safe"]))}
    # sensitivity: rewritten-history ops kept (from the RQ1 recompute summary)
    s = json.loads((V4 / "rq1_v4_summary.json").read_text(encoding="utf-8"))
    rw = s["excluded_by_verdict"]["rewritten_history"]
    rv = rw.get("DUAL_VERSION_VIOLATION_BACKWARD", 0) + rw.get("DUAL_VERSION_VIOLATION_BOTH", 0)
    rs = rw.get("SAFE_CONTRACT", 0) + rw.get("DUAL_VERSION_VIOLATION_FORWARD", 0)
    t = out["total"]
    out["sensitivity_keep_rewritten_history_pct"] = round(100 * (t["viol"] + rv) / (t["resolved"] + rv + rs), 1)
    # Sensitivity: excluding ADD_NOT_NULL_NO_DEFAULT (and excluding Django ADD_NOT_NULL_NO_DEFAULT)
    no_ann_v = sum(v["viol"] for k, v in ops.items() if k != "ADD_NOT_NULL_NO_DEFAULT")
    no_ann_s = sum(v["safe"] + v["fwd_only"] for k, v in ops.items() if k != "ADD_NOT_NULL_NO_DEFAULT")
    out["sensitivity_without_add_not_null_pct"] = round(100 * no_ann_v / max(1, no_ann_v + no_ann_s), 1)
    dj_ann_v = sum(1 for x in pool.get("django", []) if x["op"]["kind"] == "ADD_NOT_NULL_NO_DEFAULT" and x["ecv_classification"] in BACK)
    dj_ann_s = sum(1 for x in pool.get("django", []) if x["op"]["kind"] == "ADD_NOT_NULL_NO_DEFAULT" and x["ecv_classification"] in ("SAFE_CONTRACT", "DUAL_VERSION_VIOLATION_FORWARD"))
    out["django_add_not_null_viol"] = dj_ann_v
    out["sensitivity_without_django_add_not_null_pct"] = round(100 * (t["viol"] - dj_ann_v) / max(1, t["resolved"] - dj_ann_v - dj_ann_s), 1)
    total_frame = t["resolved"] + t["uncert"] + sum(v["dd"] for v in ops.values())
    out["population_frame_ops"] = total_frame
    out["abstention_rate_population_pct"] = round(100 * t["uncert"] / max(1, total_frame), 1)
    out["excluded_ops"] = s["excluded_ops"]
    out["rollback_only"] = sum(s["pooled_by_ecosystem"][e]["down"] for e in ("prisma", "rails", "django"))
    out["safe_additive"] = sum(s["pooled_by_ecosystem"][e]["safe_add"] for e in ("prisma", "rails", "django"))
    out["post_deploy_ops"] = s["post_deploy_ops"]

    # RQ4 full-history replay (v3 runs): validity under v4 rules
    fs = json.loads(FULL_SCHEMA_JSON.read_text(encoding="utf-8"))
    loaded = [r for r in fs if r.get("schema_loaded_ok")]
    valid, notes = [], Counter()
    for r in loaded:
        if not r["c_old_at_s_old_ok"]:
            notes["c_old_fails_on_s_old"] += 1
            continue
        if not r["verbatim_mig_ok"]:
            notes["verbatim_migration_not_applicable_on_replayed_history"] += 1
            continue
        if not r["c_old_at_s_new_crash"]:
            notes["no_crash"] += 1
            continue
        valid.append(r)
    excl = Counter()
    keep = []
    for r in valid:
        rdir = rq1.REPOS / r["repo"].replace("/", "__")
        fm = rq1.file_status_and_oids(rdir, [r["sha"]]).get(r["sha"], {})
        dels = {str(Path(p).parent) for p, (st, _) in fm.items() if st == "D" and rq1.is_mig_path("prisma", p)}
        added = [p for p, (st, _) in fm.items() if st == "A" and rq1.is_mig_path("prisma", p)]
        if any(str(Path(p).parent) in dels for p in added):
            excl["rewritten_history"] += 1
            continue
        keep.append(r)
    out["rq4_full_history"] = {
        "candidates": len(fs), "loaded": len(loaded), "not_reproduced": dict(notes), "excluded_v4": dict(excl),
        "valid_reproductions": len(keep), "repos": len({r["repo"] for r in keep}),
        "pass": sum(bool(r["synth_full_verified"]) for r in keep),
        "by_op": {k: {"n": sum(1 for r in keep if r["op_kind"] == k), "pass": sum(1 for r in keep if r["op_kind"] == k and r["synth_full_verified"])}
                  for k in sorted({r["op_kind"] for r in keep})},
        "sqlstates": dict(Counter(r["crash_sqlstate"] for r in keep)),
        "avg_tables": round(float(np.mean([r["schema_stats"]["tables"] for r in keep])), 1),
        "avg_fks": round(float(np.mean([r["schema_stats"]["foreign_keys"] for r in keep])), 1),
        "max_tables": max(r["schema_stats"]["tables"] for r in keep), "max_fks": max(r["schema_stats"]["foreign_keys"] for r in keep),
        "failures": [{"id": r["id"], "repo": r["repo"], "op": r["op_kind"], "apply_err": (r.get("synth_apply_err") or "")[:120],
                      "contract_err": (r.get("synth_contract_err") or "")[:120], "equiv": r.get("synth_schema_equiv_target_col")}
                     for r in keep if not r["synth_full_verified"]],
    }
    (V4 / "rq1_v4_backward.json").write_text(json.dumps(out, indent=2), encoding="utf-8")
    print(json.dumps(out, indent=1))


if __name__ == "__main__":
    main()
