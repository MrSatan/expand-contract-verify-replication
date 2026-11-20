"""v4 Figure Generator: Publication-quality vector PDF figures (+ PNG previews) for the JSS paper.
Reads 100% directly from v4 verified JSON artifacts (`rq1_v4_backward.json`, `rq1_v4_summary.json`,
`rq3_v4_metrics.json`).
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np

TASK_DIR = Path(os.environ["ECV_TASK_DIR"]).resolve() if "ECV_TASK_DIR" in os.environ else Path(__file__).resolve().parent.parent.parent
IS_PKG = (TASK_DIR / "data" / "v4").exists() and not (TASK_DIR / "research" / "v4").exists()
V2_DIR = (TASK_DIR / "data" / "v4") if IS_PKG else (TASK_DIR / "research" / "v4")
FIG_OUT = (TASK_DIR / "figures") if IS_PKG else (TASK_DIR / "output" / "paper" / "figures")
FIG_PNG = TASK_DIR / "tmp" / "fig_previews_v4"
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
    rq1 = json.loads((V2_DIR / "rq1_v4_backward.json").read_text(encoding="utf-8"))
    summ = json.loads((V2_DIR / "rq1_v4_summary.json").read_text(encoding="utf-8"))["pooled_by_ecosystem"]
    rq3 = json.loads((V2_DIR / "rq3_v4_metrics.json").read_text(encoding="utf-8"))

    # ------------------------------------------------------------------
    # Figure 1: RQ1 Newly-Added Migration Prevalence & Coverage (110 Repos)
    # ------------------------------------------------------------------
    ecos = ["prisma", "rails", "django"]
    labels = ["Prisma\n(30 repos)", "Rails\n(40 repos)", "Django\n(40 repos)"]
    by_eco = rq1["by_ecosystem"]

    viol_pcts = [by_eco[e]["rate_pct"] for e in ecos]
    ci_lo = [by_eco[e]["rate_pct"] - by_eco[e]["cluster_ci95_pct"][0] for e in ecos]
    ci_hi = [by_eco[e]["cluster_ci95_pct"][1] - by_eco[e]["rate_pct"] for e in ecos]
    safe_pcts = [100.0 - v for v in viol_pcts]

    fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(9.4, 3.65), gridspec_kw={"width_ratios": [1.25, 1.05]})
    x = np.arange(len(ecos))
    w = 0.36
    b1 = ax1.bar(x - w / 2, viol_pcts, w, label="Breaks previous release", color=C_RED, alpha=0.92,
                 yerr=[ci_lo, ci_hi], capsize=3.5, error_kw={"elinewidth": 1.1, "ecolor": "#222"})
    b2 = ax1.bar(x + w / 2, safe_pcts, w, label="Backward-safe", color=C_GREEN, alpha=0.88)
    for bi, bar in enumerate(b1):
        h = bar.get_height()
        ax1.text(bar.get_x() + bar.get_width() / 2, h + ci_hi[bi] + 1.2, f"{h:.1f}%", ha="center", va="bottom", fontsize=8.2, fontweight="bold")
    for bar in b2:
        h = bar.get_height()
        ax1.text(bar.get_x() + bar.get_width() / 2, h + 1.5, f"{h:.1f}%", ha="center", va="bottom", fontsize=8.2)
    ax1.set_xticks(x)
    ax1.set_xticklabels(labels)
    ax1.set_ylim(0, 112)
    ax1.set_ylabel("Share of Resolved Forward DDL Ops (%)")
    ax1.set_title("(a) Resolved operations, 95% cluster-bootstrap CI")
    ax1.legend(frameon=False, loc="upper left")
    ax1.grid(axis="y", linestyle="--", alpha=0.3)

    # Panel (b): Verdict composition including UNCERTAIN and Rollback-Only
    v_c = np.array([by_eco[e]["viol"] for e in ecos])
    s_c = np.array([by_eco[e]["safe"] for e in ecos])
    u_c = np.array([by_eco[e]["uncert"] for e in ecos])
    rb_c = np.array([summ[e]["down"] for e in ecos])
    sa_c = np.array([summ[e]["safe_add"] + summ[e]["data_dependent"] for e in ecos])
    ax2.bar(x, v_c, 0.52, label="Violation", color=C_RED)
    ax2.bar(x, s_c, 0.52, bottom=v_c, label="Backward-safe", color=C_GREEN)
    ax2.bar(x, u_c, 0.52, bottom=v_c + s_c, label="Uncertain", color=C_GRAY, alpha=0.8)
    ax2.bar(x, rb_c, 0.52, bottom=v_c + s_c + u_c, label="Rollback-Only (down)", color=C_ORANGE)
    ax2.bar(x, sa_c, 0.52, bottom=v_c + s_c + u_c + rb_c, label="Safe additive / data-dep.", color=C_BLUE, alpha=0.55)
    totals = v_c + s_c + u_c + rb_c + sa_c
    for i, tot in enumerate(totals):
        ax2.text(x[i], tot + 90, f"{int(tot):,}", ha="center", va="bottom", fontsize=8.3, fontweight="bold")
    ax2.set_xticks(x)
    ax2.set_xticklabels(["Prisma", "Rails", "Django"])
    ax2.set_ylim(0, max(totals) * 1.32)
    ax2.set_ylabel("Newly Added Destructive DDL Ops")
    ax2.set_title("(b) All Destructive Ops in Added Files by Verdict")
    ax2.legend(frameon=False, loc="upper left", fontsize=7.6)
    ax2.grid(axis="y", linestyle="--", alpha=0.3)

    fig.tight_layout()
    save_fig(fig, "fig_ecosystem_prevalence")

    # ------------------------------------------------------------------
    # Figure 2: RQ1 Breakdown by Forward-Only DDL Operator Kind (Added-Only)
    # ------------------------------------------------------------------
    op_map = rq1["by_operator"]
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
    ax.barh(y, v_op, color=C_RED, label="Breaks previous release", height=0.62)
    ax.barh(y, s_op, left=v_op, color=C_GREEN, label="Backward-safe", height=0.62)
    for i, (vv, ss) in enumerate(zip(v_op, s_op)):
        tot = vv + ss
        pct = 100.0 * vv / tot if tot else 0.0
        ax.text(tot + 35, y[i], f"{tot:,} ({pct:.0f}% viol.)", va="center", ha="left", fontsize=8.1)
    ax.set_yticks(y)
    ax.set_yticklabels([pretty.get(k, k) for k in ordered])
    ax.set_xlim(0, max(v + s for v, s in zip(v_op, s_op)) * 1.25)
    ax.set_xlabel("Resolved Newly-Added Forward-Only Destructive DDL Ops (110 repositories)")
    ax.set_title("Backward-Incompatible vs. Backward-Safe Operations by DDL Operator Kind")
    ax.legend(frameon=False, loc="lower right")
    ax.grid(axis="x", linestyle="--", alpha=0.3)
    fig.tight_layout()
    save_fig(fig, "fig_operator_breakdown")

    # ------------------------------------------------------------------
    # Figure 3: RQ2 Benchmark Comparison (F1/FPR + MCC/BalAcc)
    # ------------------------------------------------------------------
    all_m = rq3["metrics"]["all"]
    prisma_m = rq3["metrics"]["prisma"]
    rails_m = rq3["metrics"]["rails_exec"]
    django_m = rq3["metrics"]["django"]

    rows = [
        ("ECV-Fast\n(n=167)", all_m["ecv_fast"]),
        ("ECV-Deep\n(n=167)", all_m["ecv_deep"]),
        ("ECV\nSchema", all_m["ecv_schema_only"]),
        ("ECV\nDiff", all_m["ecv_diff_only"]),
        ("squawk\n(compat)", all_m["squawk_compat"]),
        ("Always\nFlag", all_m["always_flag"]),
        ("ECV-Fast\n(Pr. 60)", prisma_m["ecv_fast"]),
        ("squawk\n(Pr. 60)", prisma_m["squawk_compat_native"]),
        ("ECV-Fast\n(Rails 50)", rails_m["ecv_fast"]),
        ("strong_mig\n(Rails 50)", rails_m["strong_migrations"]),
        ("ECV-Fast\n(Dj. 49)", django_m["ecv_fast"]),
        ("dml\n(Dj. 49)", django_m["dml"]),
    ]
    m_labels = [lbl for lbl, _ in rows]
    f1s = [d["f1_pct"] for _, d in rows]
    fprs = [d["fpr_pct"] for _, d in rows]
    bal = [d["balanced_acc_pct"] for _, d in rows]
    mccs = [d["mcc"] for _, d in rows]

    fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(13.4, 4.5), gridspec_kw={"width_ratios": [1.72, 1.0]})

    xm = np.arange(len(rows))
    w = 0.25
    ax1.bar(xm - w, f1s, w, label="F1-Score (%)", color=C_PURPLE)
    ax1.bar(xm, bal, w, label="Balanced Acc. (%)", color=C_BLUE)
    ax1.bar(xm + w, fprs, w, label="FPR (%, lower=better)", color=C_RED, alpha=0.86)
    for sep in (5.5, 7.5, 9.5):
        ax1.axvline(sep, color="#555", linewidth=0.9, linestyle=":")
    for i in range(len(rows)):
        f1_x = xm[i] - w - (0.04 if f1s[i] < bal[i] - 3 else 0.0)
        ax1.text(f1_x, f1s[i] + 1.5, f"{f1s[i]:.1f}", ha="center", va="bottom", fontsize=5.6, fontweight="bold")
        fpr_int = int(fprs[i] + 0.5)
        fpr_x = xm[i] + w + (0.06 if fprs[i] < bal[i] - 3 else 0.0)
        ax1.text(fpr_x, fprs[i] + 1.5, f"{fpr_int}%", ha="center", va="bottom", fontsize=5.4, color=C_RED)
    ax1.set_xticks(xm)
    ax1.set_xticklabels(m_labels, fontsize=6.8)
    ax1.set_ylim(0, 114)
    ax1.set_ylabel("Percentage (%)")
    ax1.set_title("(a) F1, Balanced Accuracy, & FPR (dotted lines separate scopes)", pad=20)
    ax1.legend(frameon=False, ncol=3, loc="upper center", bbox_to_anchor=(0.5, 1.09), fontsize=7.8)
    ax1.grid(axis="y", linestyle="--", alpha=0.3)

    # Panel (b): Matthews Correlation Coefficient (MCC) across all real tools & trivial baseline
    ym = np.arange(len(rows))
    colors = [C_GREEN if v > 0.4 else (C_ORANGE if v > 0.05 else C_RED) for v in mccs]
    ax2.barh(ym, mccs, color=colors, height=0.58)
    ax2.axvline(0.0, color="#333", linewidth=0.9, linestyle="-")
    for sep in (5.5, 7.5, 9.5):
        ax2.axhline(sep, color="#555", linewidth=0.8, linestyle=":")
    for i, v in enumerate(mccs):
        xpos = v + 0.025 if v >= 0 else v - 0.025
        ha = "left" if v >= 0 else "right"
        ax2.text(xpos, ym[i], f"{v:+.3f}", va="center", ha=ha, fontsize=7.6, fontweight="bold")
    ax2.set_yticks(ym)
    ax2.set_yticklabels([lbl.replace("\n", " ") for lbl, _ in rows], fontsize=7.6)
    ax2.invert_yaxis()
    ax2.set_xlim(-0.32, 1.05)
    ax2.set_xlabel("Matthews Correlation Coefficient (MCC)")
    ax2.set_title("(b) Imbalance-Robust Discrimination (MCC)")
    ax2.grid(axis="x", linestyle="--", alpha=0.3)

    fig.tight_layout()
    save_fig(fig, "fig_baseline_ablation")
    print("Generated 3 v4 vector PDF figures in", FIG_OUT)


if __name__ == "__main__":
    main()
