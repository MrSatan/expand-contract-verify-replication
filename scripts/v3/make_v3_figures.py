"""V2 Figure Generator: Publication-quality vector PDF figures (+ PNG previews) for the revised JSS paper.
Reads 100% directly from v2 verified JSON artifacts:
  - `research/v2/rq1_added_only_summary.json`
  - `research/v2/rq3_v3_final_metrics.json`
"""
from __future__ import annotations

import json
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np

TASK_DIR = Path(__file__).resolve().parent.parent.parent
V2_DIR = TASK_DIR / "research" / "v3"  # v3 inputs
FIG_OUT = TASK_DIR / "output" / "paper" / "figures"
FIG_PNG = TASK_DIR / "tmp" / "fig_previews_v3"
FIG_OUT.mkdir(parents=True, exist_ok=True)
FIG_PNG.mkdir(parents=True, exist_ok=True)

plt.rcParams.update({
    "font.family": "DejaVu Sans",
    "font.size": 9.2,
    "axes.titlesize": 10.2,
    "axes.labelsize": 9.2,
    "xtick.labelsize": 8.5,
    "ytick.labelsize": 8.5,
    "legend.fontsize": 8.2,
    "axes.spines.top": False,
    "axes.spines.right": False,
    "figure.dpi": 300,
})

C_RED = "#D55E00"
C_BLUE = "#0072B2"
C_GREEN = "#009E73"
C_ORANGE = "#E69F00"
C_PURPLE = "#CC79A7"
C_GRAY = "#7F7F7F"


def save_fig(fig, name: str):
    fig.savefig(FIG_OUT / f"{name}.pdf", bbox_inches="tight")
    fig.savefig(FIG_PNG / f"{name}.png", dpi=220, bbox_inches="tight")
    plt.close(fig)


def main():
    rq1 = json.loads((V2_DIR / "rq1_added_only_summary.json").read_text(encoding="utf-8"))
    rq3 = json.loads((V2_DIR / "rq3_v3_final_metrics.json").read_text(encoding="utf-8"))

    # ------------------------------------------------------------------
    # Figure 1: RQ1 Newly-Added Migration Prevalence & Coverage (110 Repos)
    # ------------------------------------------------------------------
    ecos = ["prisma", "rails", "django"]
    labels = ["Prisma\n(30 repos)", "Rails\n(40 repos)", "Django\n(40 repos)"]
    by_eco = rq1["pooled_by_ecosystem"]

    viol_pcts = [by_eco[e]["viol_rate_pct"] for e in ecos]
    ci_lo = [by_eco[e]["viol_rate_pct"] - by_eco[e]["viol_ci95_pct"][0] for e in ecos]
    ci_hi = [by_eco[e]["viol_ci95_pct"][1] - by_eco[e]["viol_rate_pct"] for e in ecos]
    safe_pcts = [100.0 - v for v in viol_pcts]

    fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(9.4, 3.65), gridspec_kw={"width_ratios": [1.25, 1.05]})
    x = np.arange(len(ecos))
    w = 0.36
    b1 = ax1.bar(x - w / 2, viol_pcts, w, label="Dual-Version Violation", color=C_RED, alpha=0.92,
                 yerr=[ci_lo, ci_hi], capsize=3.5, error_kw={"elinewidth": 1.1, "ecolor": "#222"})
    b2 = ax1.bar(x + w / 2, safe_pcts, w, label="Safe Contract Phase", color=C_GREEN, alpha=0.88)
    for bar in b1:
        h = bar.get_height()
        ax1.text(bar.get_x() + bar.get_width() / 2, h + 2.6, f"{h:.1f}%", ha="center", va="bottom", fontsize=8.2, fontweight="bold")
    for bar in b2:
        h = bar.get_height()
        ax1.text(bar.get_x() + bar.get_width() / 2, h + 1.5, f"{h:.1f}%", ha="center", va="bottom", fontsize=8.2)
    ax1.set_xticks(x)
    ax1.set_xticklabels(labels)
    ax1.set_ylim(0, 106)
    ax1.set_ylabel("Share of Resolved Forward DDL Ops (%)")
    ax1.set_title("(a) Added-Only Violation vs. Safe Contract (95% CI)")
    ax1.legend(frameon=False, loc="upper left")
    ax1.grid(axis="y", linestyle="--", alpha=0.3)

    # Panel (b): Verdict composition including UNCERTAIN and Rollback-Only
    v_c = np.array([by_eco[e]["viol"] for e in ecos])
    s_c = np.array([by_eco[e]["safe"] for e in ecos])
    u_c = np.array([by_eco[e]["uncert"] for e in ecos])
    rb_c = np.array([by_eco[e]["down"] for e in ecos])
    ax2.bar(x, v_c, 0.52, label="Violation", color=C_RED)
    ax2.bar(x, s_c, 0.52, bottom=v_c, label="Safe Contract", color=C_GREEN)
    ax2.bar(x, u_c, 0.52, bottom=v_c + s_c, label="Uncertain", color=C_GRAY, alpha=0.8)
    ax2.bar(x, rb_c, 0.52, bottom=v_c + s_c + u_c, label="Rollback-Only (down)", color=C_ORANGE)
    totals = v_c + s_c + u_c + rb_c
    for i, tot in enumerate(totals):
        ax2.text(x[i], tot + 90, f"{int(tot):,}", ha="center", va="bottom", fontsize=8.3, fontweight="bold")
    ax2.set_xticks(x)
    ax2.set_xticklabels(["Prisma", "Rails", "Django"])
    ax2.set_ylim(0, max(totals) * 1.18)
    ax2.set_ylabel("Newly Added Destructive DDL Ops")
    ax2.set_title("(b) Volume, Coverage (Uncertain), & Rollback Blocks")
    ax2.legend(frameon=False, loc="upper left", fontsize=7.8)
    ax2.grid(axis="y", linestyle="--", alpha=0.3)

    fig.tight_layout()
    save_fig(fig, "fig_ecosystem_prevalence")

    # ------------------------------------------------------------------
    # Figure 2: RQ1 Breakdown by Forward-Only DDL Operator Kind (Added-Only)
    # ------------------------------------------------------------------
    op_map = rq1["by_operator_added_only"]
    pretty = {
        "DROP_COLUMN": "DROP COLUMN",
        "ADD_NOT_NULL_NO_DEFAULT": "ADD NOT NULL (no DB default)",
        "DROP_TABLE": "DROP TABLE",
        "ALTER_COLUMN_TYPE": "ALTER COLUMN TYPE",
        "RENAME_COLUMN": "RENAME COLUMN",
        "SET_NOT_NULL": "SET NOT NULL",
        "RENAME_TABLE": "RENAME TABLE",
        "RENAME_TYPE_ENUM": "RENAME/DROP ENUM TYPE",
        "ADD_UNIQUE_OR_CHECK": "ADD CHECK / UNIQUE",
    }
    ordered = sorted([k for k in op_map if op_map[k]["viol"] + op_map[k]["safe"] > 0], key=lambda k: op_map[k]["viol"] + op_map[k]["safe"])
    y = np.arange(len(ordered))
    v_op = [op_map[k]["viol"] for k in ordered]
    s_op = [op_map[k]["safe"] for k in ordered]

    fig, ax = plt.subplots(figsize=(8.6, 4.0))
    ax.barh(y, v_op, color=C_RED, label="Dual-Version Violation", height=0.62)
    ax.barh(y, s_op, left=v_op, color=C_GREEN, label="Safe Contract Phase", height=0.62)
    for i, (vv, ss) in enumerate(zip(v_op, s_op)):
        tot = vv + ss
        pct = 100.0 * vv / tot if tot else 0.0
        ax.text(tot + 35, y[i], f"{tot:,} ({pct:.0f}% viol.)", va="center", ha="left", fontsize=8.1)
    ax.set_yticks(y)
    ax.set_yticklabels([pretty.get(k, k) for k in ordered])
    ax.set_xlim(0, max(v + s for v, s in zip(v_op, s_op)) * 1.25)
    ax.set_xlabel("Resolved Newly-Added Forward-Only Destructive DDL Ops (110 Repositories)")
    ax.set_title("Dual-Version Violations vs. Safe Contract Phases by DDL Operator Kind")
    ax.legend(frameon=False, loc="lower right")
    ax.grid(axis="x", linestyle="--", alpha=0.3)
    fig.tight_layout()
    save_fig(fig, "fig_operator_breakdown")

    # ------------------------------------------------------------------
    # Figure 3: RQ3 Non-Circular Benchmark Comparison (F1/FPR + MCC/BalAcc)
    # ------------------------------------------------------------------
    all_m = rq3["metrics"]["all_180"]
    rails_m = rq3["metrics"]["rails_60"]
    django_m = rq3["metrics"]["django_60"]

    rows = [
        ("ECV-Fast\n(Ours)", all_m["ecv_fast"]),
        ("ECV-Deep\n(Ours)", all_m["ecv_deep"]),
        ("ECV\nSchema", all_m["ablation_schema_only"]),
        ("ECV\nDiff", all_m["ablation_diff_only"]),
        ("squawk\n(compat)", all_m["squawk_strict_compat"]),
        ("Always\nFlag", all_m["always_flag_trivial"]),
        ("strong_mig\n(Rails)", rails_m["strong_migrations_real_exec52"]),
        ("dml\n(Django)", django_m["django_migration_linter_real"]),
    ]
    m_labels = [lbl for lbl, _ in rows]
    f1s = [d["f1_pct"] for _, d in rows]
    fprs = [d["fpr_pct"] for _, d in rows]
    bal = [d["balanced_acc_pct"] for _, d in rows]
    mccs = [d["mcc"] for _, d in rows]

    fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(11.4, 4.1), gridspec_kw={"width_ratios": [1.45, 1.0]})

    xm = np.arange(len(rows))
    w = 0.26
    ax1.bar(xm - w, f1s, w, label="F1-Score (%)", color=C_PURPLE)
    ax1.bar(xm, bal, w, label="Balanced Acc. (%)", color=C_BLUE)
    ax1.bar(xm + w, fprs, w, label="FPR (%, lower=better)", color=C_RED, alpha=0.86)
    for i in range(len(rows)):
        ax1.text(xm[i] - w, f1s[i] + 1.4, f"{f1s[i]:.1f}", ha="center", va="bottom", fontsize=6.9, fontweight="bold")
        ax1.text(xm[i] + w, fprs[i] + 1.4, f"{fprs[i]:.0f}%", ha="center", va="bottom", fontsize=6.8, color=C_RED)
    ax1.set_xticks(xm)
    ax1.set_xticklabels(m_labels, fontsize=7.9)
    ax1.set_ylim(0, 114)
    ax1.set_ylabel("Percentage (%)")
    ax1.set_title("(a) F1, Balanced Accuracy, & FPR (180-case benchmark)", pad=20)
    ax1.legend(frameon=False, ncol=3, loc="upper center", bbox_to_anchor=(0.5, 1.09), fontsize=7.8)
    ax1.grid(axis="y", linestyle="--", alpha=0.3)

    # Panel (b): Matthews Correlation Coefficient (MCC) across all real tools & trivial baseline
    ym = np.arange(len(rows))
    colors = [C_GREEN if v > 0.4 else (C_ORANGE if v > 0.05 else C_RED) for v in mccs]
    ax2.barh(ym, mccs, color=colors, height=0.58)
    ax2.axvline(0.0, color="#333", linewidth=0.9, linestyle="-")
    for i, v in enumerate(mccs):
        xpos = v + 0.025 if v >= 0 else v - 0.025
        ha = "left" if v >= 0 else "right"
        ax2.text(xpos, ym[i], f"{v:+.3f}", va="center", ha=ha, fontsize=8.0, fontweight="bold")
    ax2.set_yticks(ym)
    ax2.set_yticklabels([lbl.replace("\n", " ") for lbl, _ in rows], fontsize=8.0)
    ax2.invert_yaxis()
    ax2.set_xlim(-0.42, 0.78)
    ax2.set_xlabel("Matthews Correlation Coefficient (MCC)")
    ax2.set_title("(b) Imbalance-Robust Discrimination (MCC)")
    ax2.grid(axis="x", linestyle="--", alpha=0.3)

    fig.tight_layout()
    save_fig(fig, "fig_baseline_ablation")
    print("Generated 3 v3 vector PDF figures in", FIG_OUT)


if __name__ == "__main__":
    main()
