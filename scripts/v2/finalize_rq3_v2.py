"""V2 Phase 2D: Apply Verified Manual Adjudications for the 22 Edge Cases, Archive Complete
180-Case Auditable Ground-Truth Table (`benchmark_audit_180.md`), and Compute ALL
Non-Circular RQ3 Metrics (Precision, Recall, F1, FPR, Specificity, Balanced Accuracy, MCC,
Coverage, 95% Wilson CIs, Exact Two-Sided McNemar Tests, and IPW Population Estimates).
"""
from __future__ import annotations

import json
import math
from collections import Counter, defaultdict
from pathlib import Path

from scipy import stats

TASK_DIR = Path(__file__).resolve().parent.parent.parent
V2_DIR = TASK_DIR / "research" / "v2"
OUT_DIR = TASK_DIR / "output"

# Explicit forensic adjudication overrides for the 22 REVIEW cases inspected directly against
# verbatim migration files and git diffs (ZERO ECV verdicts consulted!).
MANUAL_OVERRIDES: dict[str, tuple[bool, str]] = {
    "django_122": (True,  "VIOL (verified): swaps integer FK/PK `next_log` to `UUIDField`; C_{t-1} integer queries crash (22P02)."),
    "django_125": (False, "SAFE (verified): tutorial project commit adding entire Django app at once; no live C_{t-1} deployment."),
    "django_148": (False, "SAFE (verified): tutorial project commit adding entire Django app at once; no live C_{t-1} deployment."),
    "django_153": (False, "SAFE (verified): Python subclass rename `CoreFormulaModelField -> FormulaField`; both map to PG TEXT."),
    "django_160": (True,  "VIOL (verified): narrows `TextField` to `FileField` (varchar(100)) & changes value format on active `brands_brand`."),
    "django_167": (True,  "VIOL (verified): converts `PositiveIntegerField` (`type`) to `ForeignKey` (`type_id`) while C_{t-1} reads `type`."),
    "django_174": (True,  "VIOL (verified): converts `GenericIPAddressField` (`mx`) to `ForeignKey` (`mx_id`) on active model."),
    "prisma_003": (True,  "VIOL (verified): drops/rebuilds PG ENUM `Theme` and lowercases values while C_{t-1} uses old enum/casing."),
    "prisma_011": (True,  "VIOL (verified): removes `BreakTheMonolith` value from PG ENUM `EnumUserActionType` in place."),
    "prisma_017": (False, "SAFE (verified): `Decimal -> DECIMAL(18,2)` currency precision normalization; compatible with standard C_{t-1} values."),
    "prisma_024": (False, "SAFE (verified): `Decimal -> DECIMAL(18,2)` currency precision normalization; compatible with standard C_{t-1} values."),
    "prisma_033": (False, "SAFE (verified): `TIMESTAMPTZ(6) -> TIMESTAMP(3)`; PG accepts Prisma ISO datetime strings without crash."),
    "prisma_042": (False, "SAFE (verified): `TIMESTAMP(3) -> TIMESTAMPTZ(3)`; PG accepts Prisma ISO datetime strings without crash."),
    "prisma_046": (True,  "VIOL (verified): bulk table/FK rename migration in active `amplication` schema without dual-version view."),
    "prisma_060": (False, "SAFE (verified): `TIMESTAMP(3) -> TIMESTAMPTZ(3)`; PG accepts Prisma ISO datetime strings without crash."),
    "rails_062":  (True,  "VIOL (verified): narrows `bigint -> integer` on active `tag_versions.previous_version_id` (overflow risk 22003)."),
    "rails_068":  (False, "SAFE (verified): same `:string` type; only updates PostgreSQL collation (`natural_sort_order`)."),
    "rails_074":  (True,  "VIOL (verified): changes `checklists.sorted_item_ids` to PostgreSQL `varchar[]` array type."),
    "rails_075":  (False, "SAFE (verified): explicit cleanup commit ('Remove unused columns from spree_promotion_rules'); 0 C_{t-1} refs."),
    "rails_080":  (False, "SAFE (verified): value-compatible `:string -> :text` widening on `flipper_gates.value`."),
    "rails_101":  (False, "SAFE (verified): PostgreSQL `text -> character varying` (unbounded); binary-coercible identical value domain."),
    "rails_109":  (False, "SAFE (verified): `integer -> bigint` PK widening on `stories.id` (lock hazard only, no C_{t-1} value crash)."),
}


def wilson_ci(k: int, n: int, z: float = 1.96) -> list[float]:
    if n == 0:
        return [0.0, 0.0]
    p = k / n
    denom = 1.0 + z * z / n
    center = (p + z * z / (2 * n)) / denom
    margin = z * math.sqrt((p * (1 - p) + z * z / (4 * n)) / n) / denom
    return [round(max(0.0, center - margin) * 100, 1), round(min(1.0, center + margin) * 100, 1)]


def compute_metrics(tp: int, fp: int, fn: int, tn: int, coverage_n: int | None = None, total_n: int | None = None) -> dict:
    prec = tp / (tp + fp) if (tp + fp) else 0.0
    rec = tp / (tp + fn) if (tp + fn) else 0.0
    f1 = (2 * prec * rec / (prec + rec)) if (prec + rec) else 0.0
    fpr = fp / (fp + tn) if (fp + tn) else 0.0
    spec = tn / (fp + tn) if (fp + tn) else 0.0
    bal_acc = 0.5 * (rec + spec)
    acc = (tp + tn) / (tp + fp + fn + tn) if (tp + fp + fn + tn) else 0.0
    denom_mcc = math.sqrt((tp + fp) * (tp + fn) * (tn + fp) * (tn + fn))
    mcc = ((tp * tn) - (fp * fn)) / denom_mcc if denom_mcc > 0 else 0.0
    out = {
        "tp": tp, "fp": fp, "fn": fn, "tn": tn,
        "precision_pct": round(prec * 100, 1),
        "recall_pct": round(rec * 100, 1),
        "f1_pct": round(f1 * 100, 1),
        "fpr_pct": round(fpr * 100, 1),
        "specificity_pct": round(spec * 100, 1),
        "balanced_acc_pct": round(bal_acc * 100, 1),
        "accuracy_pct": round(acc * 100, 1),
        "mcc": round(mcc, 3),
        "precision_ci95_pct": wilson_ci(tp, tp + fp),
        "recall_ci95_pct": wilson_ci(tp, tp + fn),
        "fpr_ci95_pct": wilson_ci(fp, fp + tn),
    }
    if coverage_n is not None and total_n is not None and total_n > 0:
        out["coverage_pct"] = round(100 * coverage_n / total_n, 1)
    return out


def mcnemar_exact(preds_a: list[bool], preds_b: list[bool], truth: list[bool], subset_neg_only: bool = False) -> dict:
    """Exact two-sided McNemar test on paired binary predictions (overall accuracy or FPR on negatives)."""
    b = c = 0
    for pa, pb, y in zip(preds_a, preds_b, truth):
        if subset_neg_only and y:
            continue
        ok_a = (pa == y)
        ok_b = (pb == y)
        if ok_a and not ok_b:
            b += 1
        elif (not ok_a) and ok_b:
            c += 1
    n = b + c
    p = float(stats.binomtest(min(b, c), n, 0.5, alternative="two-sided").pvalue) if n > 0 else 1.0
    return {"b_a_right_b_wrong": b, "c_b_right_a_wrong": c, "p_value": p}


def main():
    raw_cases = {c["id"]: c for c in json.loads((V2_DIR / "benchmark_v2_raw.json").read_text(encoding="utf-8"))}
    gt_rows = json.loads((V2_DIR / "gt_v2_initial.json").read_text(encoding="utf-8"))
    bo = json.loads((V2_DIR / "baselines_and_oracles_v2.json").read_text(encoding="utf-8"))
    strata = json.loads((V2_DIR / "benchmark_v2_strata.json").read_text(encoding="utf-8"))

    # Apply manual overrides cleanly
    for r in gt_rows:
        if r["id"] in MANUAL_OVERRIDES:
            lbl, rat = MANUAL_OVERRIDES[r["id"]]
            r["gt_violation"] = lbl
            r["rationale"] = rat
            r["confidence"] = "VERIFIED_MANUAL"

    (V2_DIR / "ground_truth_180_verified.json").write_text(json.dumps(gt_rows, indent=2), encoding="utf-8")

    # Verify all 180 SHAs are valid 40-char hex strings!
    bad_shas = [r["id"] for r in gt_rows if len(r["sha"]) != 40 or not all(ch in "0123456789abcdef" for ch in r["sha"].lower())]
    assert not bad_shas, f"Corrupted SHAs found: {bad_shas}"

    # Collect paired prediction arrays across all 180 cases
    ecv_ev = bo["ecv_eval"]
    sq_ev = bo["squawk"]
    sm_ev = bo["strong_migrations"]
    dml_ev = bo["django_migration_linter"]
    dmmf_ev = bo["prisma_dmmf_oracle"]

    # Check Prisma DMMF execution oracle agreement with verified GT (on cases where DMMF ran PG query)
    dmmf_agree = dmmf_total = 0
    for r in gt_rows:
        if r["ecosystem"] == "prisma":
            dm = dmmf_ev[r["id"]]
            if dm["pg_c_old_at_s_new_crash"] is not None:
                dmmf_total += 1
                if bool(dm["pg_c_old_at_s_new_crash"]) == bool(r["gt_violation"]):
                    dmmf_agree += 1

    # Helper to evaluate any classifier on any subset of rows
    def eval_subset(subset_rows: list[dict], pred_fn, resolved_fn=None) -> dict:
        tp = fp = fn = tn = 0
        cov = 0
        for r in subset_rows:
            if resolved_fn is not None:
                if resolved_fn(r):
                    cov += 1
            pred = bool(pred_fn(r))
            y = bool(r["gt_violation"])
            if pred and y:
                tp += 1
            elif pred and not y:
                fp += 1
            elif (not pred) and y:
                fn += 1
            else:
                tn += 1
        return compute_metrics(tp, fp, fn, tn, cov if resolved_fn else None, len(subset_rows) if resolved_fn else None)

    # Prediction functions
    def p_ecv_fast(r): return ecv_ev[r["id"]]["ecv_fast_pred"]
    def p_ecv_deep(r): return ecv_ev[r["id"]]["ecv_deep_pred"]
    def p_ecv_resolved(r): return not ecv_ev[r["id"]]["is_uncertain"]
    def p_diff_only(r): return bool((raw_cases[r["id"]].get("evidence") or {}).get("removed_in_commit_diff"))
    def p_schema_only(r): return bool((raw_cases[r["id"]].get("evidence") or {}).get("orm_active_old"))
    def p_sq_def(r): return sq_ev[r["id"]]["default_flagged"]
    def p_sq_compat(r): return sq_ev[r["id"]]["strict_compat_flagged"]
    def p_always_flag(r): return True
    def p_sm_real(r): return bool(sm_ev[r["id"]]["flagged"])
    def p_dml_real(r): return bool(dml_ev[r["id"]]["flagged_err"])

    prisma_rows = [r for r in gt_rows if r["ecosystem"] == "prisma"]
    rails_rows = [r for r in gt_rows if r["ecosystem"] == "rails"]
    rails_exec_rows = [r for r in rails_rows if sm_ev[r["id"]]["executed_ok"]]
    django_rows = [r for r in gt_rows if r["ecosystem"] == "django"]

    resolved_all = [r for r in gt_rows if p_ecv_resolved(r)]
    resolved_prisma = [r for r in prisma_rows if p_ecv_resolved(r)]
    resolved_rails = [r for r in rails_rows if p_ecv_resolved(r)]
    resolved_django = [r for r in django_rows if p_ecv_resolved(r)]

    metrics = {
        "all_180": {
            "ecv_deep": eval_subset(gt_rows, p_ecv_deep, lambda r: (not ecv_ev[r["id"]]["is_uncertain"]) or ecv_ev[r["id"]]["ecv_deep_pred"]),
            "ecv_fast": eval_subset(gt_rows, p_ecv_fast, p_ecv_resolved),
            "ecv_fast_resolved_only": eval_subset(resolved_all, p_ecv_fast, p_ecv_resolved),
            "ecv_deep_resolved_only": eval_subset(resolved_all, p_ecv_deep),
            "ablation_schema_only": eval_subset(gt_rows, p_schema_only),
            "ablation_diff_only": eval_subset(gt_rows, p_diff_only),
            "squawk_strict_compat": eval_subset(gt_rows, p_sq_compat),
            "squawk_default": eval_subset(gt_rows, p_sq_def),
            "always_flag_trivial": eval_subset(gt_rows, p_always_flag),
        },
        "prisma_60": {
            "ecv_deep": eval_subset(prisma_rows, p_ecv_deep),
            "ecv_fast": eval_subset(prisma_rows, p_ecv_fast, p_ecv_resolved),
            "ecv_fast_resolved_only": eval_subset(resolved_prisma, p_ecv_fast),
            "squawk_strict_compat_native": eval_subset(prisma_rows, p_sq_compat),
            "squawk_default_native": eval_subset(prisma_rows, p_sq_def),
            "always_flag_trivial": eval_subset(prisma_rows, p_always_flag),
        },
        "rails_60": {
            "ecv_deep": eval_subset(rails_rows, p_ecv_deep),
            "ecv_fast": eval_subset(rails_rows, p_ecv_fast, p_ecv_resolved),
            "ecv_fast_resolved_only": eval_subset(resolved_rails, p_ecv_fast),
            "strong_migrations_real_all60": eval_subset(rails_rows, p_sm_real),
            "strong_migrations_real_exec52": eval_subset(rails_exec_rows, p_sm_real),
            "ecv_fast_on_sm_exec52": eval_subset(rails_exec_rows, p_ecv_fast, p_ecv_resolved),
            "ecv_deep_on_sm_exec52": eval_subset(rails_exec_rows, p_ecv_deep),
            "squawk_strict_compat_sql_transl": eval_subset(rails_rows, p_sq_compat),
            "always_flag_trivial": eval_subset(rails_rows, p_always_flag),
        },
        "django_60": {
            "ecv_deep": eval_subset(django_rows, p_ecv_deep),
            "ecv_fast": eval_subset(django_rows, p_ecv_fast, p_ecv_resolved),
            "ecv_fast_resolved_only": eval_subset(resolved_django, p_ecv_fast),
            "django_migration_linter_real": eval_subset(django_rows, p_dml_real),
            "squawk_strict_compat_sql_transl": eval_subset(django_rows, p_sq_compat),
            "always_flag_trivial": eval_subset(django_rows, p_always_flag),
        },
    }

    # Exact paired two-sided McNemar tests (both overall accuracy AND FPR on negative ground-truth cases!)
    truth_all = [r["gt_violation"] for r in gt_rows]
    truth_p = [r["gt_violation"] for r in prisma_rows]
    truth_r52 = [r["gt_violation"] for r in rails_exec_rows]
    truth_d = [r["gt_violation"] for r in django_rows]

    mcnemar_tests = {
        "all_180_ecv_fast_vs_squawk_compat_overall": mcnemar_exact([p_ecv_fast(r) for r in gt_rows], [p_sq_compat(r) for r in gt_rows], truth_all, False),
        "all_180_ecv_fast_vs_squawk_compat_fpr": mcnemar_exact([p_ecv_fast(r) for r in gt_rows], [p_sq_compat(r) for r in gt_rows], truth_all, True),
        "all_180_ecv_deep_vs_squawk_compat_overall": mcnemar_exact([p_ecv_deep(r) for r in gt_rows], [p_sq_compat(r) for r in gt_rows], truth_all, False),
        "all_180_ecv_deep_vs_squawk_compat_fpr": mcnemar_exact([p_ecv_deep(r) for r in gt_rows], [p_sq_compat(r) for r in gt_rows], truth_all, True),
        "all_180_ecv_fast_vs_ecv_deep_overall": mcnemar_exact([p_ecv_fast(r) for r in gt_rows], [p_ecv_deep(r) for r in gt_rows], truth_all, False),
        "all_180_ecv_fast_vs_always_flag_overall": mcnemar_exact([p_ecv_fast(r) for r in gt_rows], [True]*len(gt_rows), truth_all, False),
        "all_180_ecv_fast_vs_always_flag_fpr": mcnemar_exact([p_ecv_fast(r) for r in gt_rows], [True]*len(gt_rows), truth_all, True),
        "prisma_60_ecv_fast_vs_squawk_native_overall": mcnemar_exact([p_ecv_fast(r) for r in prisma_rows], [p_sq_compat(r) for r in prisma_rows], truth_p, False),
        "prisma_60_ecv_fast_vs_squawk_native_fpr": mcnemar_exact([p_ecv_fast(r) for r in prisma_rows], [p_sq_compat(r) for r in prisma_rows], truth_p, True),
        "rails_52_ecv_fast_vs_strong_migrations_overall": mcnemar_exact([p_ecv_fast(r) for r in rails_exec_rows], [p_sm_real(r) for r in rails_exec_rows], truth_r52, False),
        "rails_52_ecv_fast_vs_strong_migrations_fpr": mcnemar_exact([p_ecv_fast(r) for r in rails_exec_rows], [p_sm_real(r) for r in rails_exec_rows], truth_r52, True),
        "rails_52_ecv_deep_vs_strong_migrations_overall": mcnemar_exact([p_ecv_deep(r) for r in rails_exec_rows], [p_sm_real(r) for r in rails_exec_rows], truth_r52, False),
        "rails_52_ecv_deep_vs_strong_migrations_fpr": mcnemar_exact([p_ecv_deep(r) for r in rails_exec_rows], [p_sm_real(r) for r in rails_exec_rows], truth_r52, True),
        "django_60_ecv_fast_vs_dml_overall": mcnemar_exact([p_ecv_fast(r) for r in django_rows], [p_dml_real(r) for r in django_rows], truth_d, False),
        "django_60_ecv_fast_vs_dml_fpr": mcnemar_exact([p_ecv_fast(r) for r in django_rows], [p_dml_real(r) for r in django_rows], truth_d, True),
        "django_60_ecv_deep_vs_dml_overall": mcnemar_exact([p_ecv_deep(r) for r in django_rows], [p_dml_real(r) for r in django_rows], truth_d, False),
        "django_60_ecv_deep_vs_dml_fpr": mcnemar_exact([p_ecv_deep(r) for r in django_rows], [p_dml_real(r) for r in django_rows], truth_d, True),
    }

    # Horvitz-Thompson Inverse-Probability-Weighted (IPW) population estimates accounting for (mig_only / cochanged) strata!
    ipw_results = {}
    for name, fn in [("ecv_fast", p_ecv_fast), ("ecv_deep", p_ecv_deep), ("squawk_strict_compat", p_sq_compat), ("always_flag", p_always_flag)]:
        wtp = wfp = wfn = wtn = 0.0
        for r in gt_rows:
            eco = r["ecosystem"]
            st = r["git_stratum"]
            n_pop = strata[eco][f"{st}_total"]
            n_samp = strata[eco][f"{st}_sampled"]
            w = n_pop / max(1, n_samp)
            pred = bool(fn(r))
            y = bool(r["gt_violation"])
            if pred and y: wtp += w
            elif pred and not y: wfp += w
            elif (not pred) and y: wfn += w
            else: wtn += w
        prec = wtp / (wtp + wfp) if (wtp + wfp) else 0
        rec = wtp / (wtp + wfn) if (wtp + wfn) else 0
        f1 = 2 * prec * rec / (prec + rec) if (prec + rec) else 0
        fpr = wfp / (wfp + wtn) if (wfp + wtn) else 0
        spec = wtn / (wfp + wtn) if (wfp + wtn) else 0
        bal = 0.5 * (rec + spec)
        den = math.sqrt((wtp + wfp) * (wtp + wfn) * (wtn + wfp) * (wtn + wfn))
        mcc = ((wtp * wtn) - (wfp * wfn)) / den if den > 0 else 0
        ipw_results[name] = {
            "precision_pct": round(prec * 100, 1),
            "recall_pct": round(rec * 100, 1),
            "f1_pct": round(f1 * 100, 1),
            "fpr_pct": round(fpr * 100, 1),
            "balanced_acc_pct": round(bal * 100, 1),
            "mcc": round(mcc, 3),
        }

    # Error analysis: list every ECV-Fast & ECV-Deep disagreement with verified GT
    fast_errs = []
    deep_errs = []
    for r in gt_rows:
        cid = r["id"]
        y = r["gt_violation"]
        pf = p_ecv_fast(r)
        pd = p_ecv_deep(r)
        if pf != y:
            fast_errs.append({"id": cid, "eco": r["ecosystem"], "op": r["op_kind"], "ecv": ecv_ev[cid]["ecv_classification"], "gt": y, "kind": "FP" if pf else "FN", "rationale": r["rationale"]})
        if pd != y:
            deep_errs.append({"id": cid, "eco": r["ecosystem"], "op": r["op_kind"], "ecv": ecv_ev[cid]["ecv_classification"], "gt": y, "kind": "FP" if pd else "FN", "rationale": r["rationale"]})

    final_rq3 = {
        "benchmark_size": len(gt_rows),
        "gt_overall": {"viol": sum(1 for r in gt_rows if r["gt_violation"]), "safe": sum(1 for r in gt_rows if not r["gt_violation"])},
        "gt_by_ecosystem": {
            e: {
                "viol": sum(1 for r in gt_rows if r["ecosystem"] == e and r["gt_violation"]),
                "safe": sum(1 for r in gt_rows if r["ecosystem"] == e and not r["gt_violation"]),
                "uncertain_in_ecv": sum(1 for r in gt_rows if r["ecosystem"] == e and ecv_ev[r["id"]]["is_uncertain"]),
            }
            for e in ("prisma", "rails", "django")
        },
        "prisma_dmmf_pg_oracle_agreement": {"agree": dmmf_agree, "total": dmmf_total},
        "metrics": metrics,
        "ipw_population_estimates": ipw_results,
        "mcnemar_exact_tests": mcnemar_tests,
        "runtime_summary": bo["runtime_summary"],
        "ecv_fast_errors_summary": {f"{k[0]}_{k[1]}": v for k, v in Counter((e["eco"], e["kind"]) for e in fast_errs).items()},
        "ecv_deep_errors_summary": {f"{k[0]}_{k[1]}": v for k, v in Counter((e["eco"], e["kind"]) for e in deep_errs).items()},
        "ecv_fast_errors": fast_errs,
        "ecv_deep_errors": deep_errs,
    }
    (V2_DIR / "rq3_v2_final_metrics.json").write_text(json.dumps(final_rq3, indent=2), encoding="utf-8")

    # Write COMPLETE auditable 180-case markdown audit sheet with verified 40-char SHAs!
    md = [
        "# Complete 180-Case Non-Circular Benchmark Ground-Truth Audit (V2)",
        "",
        "- **Sampling Protocol:** Stratified strictly by ecosystem (60 Prisma, 60 Rails, 60 Django) and observable Git commit stratum (`30 mig_only` + `30 cochanged` per ecosystem), drawn exclusively from newly added migration files (`git diff-tree status == 'A'`), **100% blind to ECV verdicts** (including all naturally occurring `UNCERTAIN` cases).",
        f"- **Ground-Truth Distribution:** **{final_rq3['gt_overall']['viol']} VIOL** / **{final_rq3['gt_overall']['safe']} SAFE** (Prisma: {final_rq3['gt_by_ecosystem']['prisma']['viol']}/{final_rq3['gt_by_ecosystem']['prisma']['safe']}, Rails: {final_rq3['gt_by_ecosystem']['rails']['viol']}/{final_rq3['gt_by_ecosystem']['rails']['safe']}, Django: {final_rq3['gt_by_ecosystem']['django']['viol']}/{final_rq3['gt_by_ecosystem']['django']['safe']}).",
        f"- **Official Prisma 7.10.1 DMMF Execution Agreement:** {dmmf_agree}/{dmmf_total} on executable Prisma cases.",
        "- **SHA Integrity Check:** 180/180 commit SHAs verified as exact 40-character hexadecimal hashes.",
        "",
        "| # | ID | Eco | Stratum | Repo & Commit | Op & Target | GT | ECV-Fast | ECV-Deep | Native Baseline | Rationale |",
        "| ---: | --- | --- | --- | --- | --- | :---: | :---: | :---: | :---: | --- |",
    ]
    for idx, r in enumerate(gt_rows, 1):
        cid = r["id"]
        eco = r["ecosystem"]
        url = f"https://github.com/{r['repo']}/commit/{r['sha']}"
        tgt = f"`{r['table']}.{r['column']}`" if r["column"] else f"`{r['table']}`"
        gt_s = "**VIOL**" if r["gt_violation"] else "SAFE"
        ef_s = "UNCERT" if ecv_ev[cid]["is_uncertain"] else ("VIOL" if ecv_ev[cid]["ecv_fast_pred"] else "SAFE")
        ed_s = "VIOL" if ecv_ev[cid]["ecv_deep_pred"] else "SAFE"
        if eco == "prisma":
            nb_s = "sq:FLG" if sq_ev[cid]["strict_compat_flagged"] else "sq:OK"
        elif eco == "rails":
            smr = sm_ev[cid]
            nb_s = ("sm:FLG" if smr["flagged"] else "sm:OK") if smr["executed_ok"] else "sm:ERR"
        else:
            nb_s = "dml:FLG" if dml_ev[cid]["flagged_err"] else "dml:OK"
        rat = r["rationale"].replace("|", "/")
        md.append(f"| {idx} | `{cid}` | {eco} | {r['git_stratum']} | [{r['repo']}@{r['sha'][:8]}]({url}) | `{r['op_kind']}` {tgt} | {gt_s} | {ef_s} | {ed_s} | {nb_s} | {rat} |")

    (OUT_DIR / "benchmark_audit_180.md").write_text("\n".join(md) + "\n", encoding="utf-8")

    # Print clean summary table to stdout!
    print("=== V2 NON-CIRCULAR RQ3 SUMMARY ===")
    print("GT overall:", final_rq3["gt_overall"], "by eco:", final_rq3["gt_by_ecosystem"])
    print("Prisma DMMF PG oracle agreement:", final_rq3["prisma_dmmf_pg_oracle_agreement"])
    for scope, block in metrics.items():
        print(f"\n--- {scope} ---")
        for mname, m in block.items():
            cov = f" cov={m['coverage_pct']}%" if "coverage_pct" in m else ""
            print(f"  {mname:34s} | TP={m['tp']:3d} FP={m['fp']:2d} FN={m['fn']:2d} TN={m['tn']:2d} | P={m['precision_pct']:5.1f}% R={m['recall_pct']:5.1f}% F1={m['f1_pct']:5.1f}% FPR={m['fpr_pct']:5.1f}% BalAcc={m['balanced_acc_pct']:5.1f}% MCC={m['mcc']:+.3f}{cov}")
    print("\n--- IPW Population Estimates ---")
    for k, v in ipw_results.items():
        print(f"  {k:24s} | P={v['precision_pct']:5.1f}% R={v['recall_pct']:5.1f}% F1={v['f1_pct']:5.1f}% FPR={v['fpr_pct']:5.1f}% BalAcc={v['balanced_acc_pct']:5.1f}% MCC={v['mcc']:+.3f}")
    print("\n--- Exact Two-Sided McNemar p-values ---")
    for k, v in mcnemar_tests.items():
        print(f"  {k:50s} | b={v['b_a_right_b_wrong']:2d} c={v['c_b_right_a_wrong']:2d} p={v['p_value']:.3e}")


if __name__ == "__main__":
    main()
