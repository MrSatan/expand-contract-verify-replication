"""One-command reproduction of all paper tables (Tables 2-7) from `data/v4/`.

Usage:
    python reproduce_tables.py
"""
from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent
V4 = ROOT / "data" / "v4"


def main() -> None:
    s = json.loads((V4 / "rq1_v4_summary.json").read_text(encoding="utf-8"))
    b = json.loads((V4 / "rq1_v4_backward.json").read_text(encoding="utf-8"))
    m = json.loads((V4 / "rq3_v4_metrics.json").read_text(encoding="utf-8"))

    print("=== TABLE 2: Corpus Funnel ===")
    t = b["total"]
    excl = b["excluded_ops"]
    print(f"Repos mined: {s['repos_mined']}, commits analyzed: {s['commits_analyzed']}")
    print(f"Edited files excluded: {excl['edited_existing_file']}")
    excl_added = excl["squashed_migration"] + excl["rewritten_history"] + excl["copied_or_moved_file"] + excl["readded_file"]
    print(f"Excluded added files: {excl_added} (squashed={excl['squashed_migration']}, rewritten={excl['rewritten_history']}, copied={excl['copied_or_moved_file']}, readded={excl['readded_file']})")
    dd = sum(v["dd"] for v in b["by_operator"].values())
    total_added = t["resolved"] + t["uncert"] + dd + b["rollback_only"] + b["safe_additive"]
    print(f"Destructive ops in newly added files: {total_added} (resolved={t['resolved']}, uncert={t['uncert']}, dd={dd}, rollback={b['rollback_only']}, safe_add={b['safe_additive']})")
    print(f"Population frame N = {t['resolved'] + t['uncert'] + dd}, population abstention = {b.get('abstention_rate_population_pct')}%")

    print("\n=== TABLE 3: RQ1 by Ecosystem ===")
    for eco in ("prisma", "rails", "django"):
        e = b["by_ecosystem"][eco]
        commits = s["pooled_by_ecosystem"][eco]["added_fwd_destructive_commits"]
        print(f"  {eco:7s} commits={commits:4d} viol={e['viol']:4d} (B={e['viol']-e['both']}, Both={e['both']}) safe_c={e['safe_contract']:3d} F={e['fwd_only']:2d} rate={e['rate_pct']:5.1f}% CI={e['cluster_ci95_pct']} cov={e['coverage_pct']:5.1f}% uncert={e['uncert']:3d} rev={e['reverse_order_pct']:4.1f}%")
    print(f"  TOTAL   commits={s['added_fwd_destructive_commits']:4d} viol={t['viol']:4d} (B={t['viol']-t['both']}, Both={t['both']}) safe_c={t['safe_contract']:3d} F={t['fwd_only']:2d} rate={t['rate_pct']:5.1f}% CI={t['cluster_ci95_pct']} cov={t['coverage_pct']:5.1f}% uncert={t['uncert']:3d} rev={t['reverse_order_pct']:4.1f}%")
    print(f"  Sensitivity: without ADD_NOT_NULL_NO_DEFAULT = {b.get('sensitivity_without_add_not_null_pct')}%, keep rewritten = {b.get('sensitivity_keep_rewritten_history_pct')}%")
    print(f"  Kruskal-Wallis: H={b['kruskal_wallis']['H']}, p={b['kruskal_wallis']['p']:.4g}; pairwise={b['pairwise']}")
    print(f"  K8s/Helm: {b['k8s']}")

    print("\n=== TABLE 4: RQ1 by Operator ===")
    for op, v in b["by_operator"].items():
        rb = s["by_operator"].get(op, {}).get("rollback_only", 0)
        print(f"  {op:26s} viol={v['viol']:4d} safe={v['safe']:3d} dd={v['dd']:2d} rate={str(v['rate_pct']):>5s}% uncert={v['uncert']:3d} rollback={rb:3d}")

    print("\n=== TABLE 5: RQ2 Benchmark (N=" + str(m["n_cases"]) + ") ===")
    def show(lbl, d):
        print(f"  {lbl:32s} TP={d['tp']:3d} FP={d['fp']:2d} FN={d['fn']:2d} TN={d['tn']:2d} P={d['precision_pct']:5.1f}% R={d['recall_pct']:5.1f}% F1={d['f1_pct']:5.1f}% FPR={d['fpr_pct']:5.1f}% MCC={d['mcc']:+.3f}")
    show("ECV-Fast (All 167)", m["metrics"]["all"]["ecv_fast"])
    show("ECV-Fast (Excl. constraints)", m["metrics"]["all_excl_constraints"]["ecv_fast"])
    show("ECV-Fast (Resolved)", m["metrics"]["resolved_only"]["ecv_fast"])
    ipw_f = m["ipw"]["estimates"]["ecv_fast"]
    print(f"  {'ECV-Fast (IPW)':32s} P={ipw_f['precision']['point']:5.1f}% R={ipw_f['recall']['point']:5.1f}% F1={ipw_f['f1']['point']:5.1f}% FPR={ipw_f['fpr']['point']:5.1f}% {ipw_f['fpr']['ci95']} MCC={ipw_f['mcc']['point']:+.3f} Prev={ipw_f['prevalence']['point']:5.1f}% {ipw_f['prevalence']['ci95']}")
    show("ECV-Deep (All 167)", m["metrics"]["all"]["ecv_deep"])
    show("ECV-SchemaOnly", m["metrics"]["all"]["ecv_schema_only"])
    show("ECV-DiffOnly", m["metrics"]["all"]["ecv_diff_only"])
    show("squawk compat (All)", m["metrics"]["all"]["squawk_compat"])
    show("squawk default (All)", m["metrics"]["all"]["squawk_default"])
    show("always flag (All)", m["metrics"]["all"]["always_flag"])
    show("ECV-Fast (Prisma 60)", m["metrics"]["prisma"]["ecv_fast"])
    show("squawk compat (Prisma 60)", m["metrics"]["prisma"]["squawk_compat_native"])
    show("ECV-Fast (Rails 50 exec)", m["metrics"]["rails_exec"]["ecv_fast"])
    show("strong_migrations (Rails 50)", m["metrics"]["rails_exec"]["strong_migrations"])
    show("ECV-Fast (Rails all 58)", m["metrics"]["rails"]["ecv_fast"])
    show("strong_migrations (Rails 58)", m["metrics"]["rails"]["strong_migrations_all"])
    show("ECV-Fast (Django 49)", m["metrics"]["django"]["ecv_fast"])
    show("django-migration-linter (49)", m["metrics"]["django"]["dml"])
    if "pre_initial_rule_based" in m:
        pi = m["pre_initial_rule_based"]
        im = pi["ecv_fast_metrics"]
        print(f"  Initial rule-based (changes={pi['n_label_changes_in_167']}, toward={pi['moved_toward_ecv_fast']}, away={pi['moved_away_from_ecv_fast']}): F1={im['f1_pct']}%, FPR={im['fpr_pct']}%, MCC={im['mcc']}")
    if "pre_v4_adjudication" in m:
        pa = m["pre_v4_adjudication"]
        pm = pa["ecv_fast_metrics"]
        print(f"  Pre-v4-adjudication (changes={pa['n_label_changes_in_167']}, toward={pa['moved_toward_ecv_fast']}, away={pa['moved_away_from_ecv_fast']}): F1={pm['f1_pct']}%, FPR={pm['fpr_pct']}%, MCC={pm['mcc']}")

    print("\n=== TABLE 7: RQ3 Plan Generation ===")
    rm = m["rq4_minimal"]
    fh = b["rq4_full_history"]
    print(f"  Minimal schemas (executable): {rm['executable_pass']}/{rm['executable_attempted']} (advisory-only: {rm['advisory_only']}, total violations: {rm['total_violations']})")
    for op, v in rm["by_op"].items():
        print(f"    {op:26s} {v}")
    print(f"  Full Prisma histories: {fh['pass']}/{fh['valid_reproductions']} across {fh['repos']} repos")


if __name__ == "__main__":
    main()
