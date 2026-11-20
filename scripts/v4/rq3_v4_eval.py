"""v4 RQ3/RQ4 evaluation on the fixed benchmark.

* labels: v3 ground truth + v4 adjudications (label_overrides_v4.py);
* cases whose migration file is excluded from the v4 population (squashed, rewritten history, copied/moved,
  re-added) are removed from the benchmark;
* ECV-Fast/Deep and the ablations use v4 verdicts and evidence; baselines reuse the executed v3 outputs;
* metrics on all cases and on resolved cases; exact McNemar tests;
* post-stratified IPW (cells = ecosystem x commit stratum x operator, because the sample was operator-balanced)
  with stratified bootstrap intervals; label-based (tool-independent) prevalence;
* RQ4 minimal-schema replay re-run for the v4 VIOL set (v2 worker).
Outputs research/v4/rq3_v4_metrics.json, research/v4/ground_truth_v4.json, log research/v4/rq3_v4.log
"""
from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from collections import Counter, defaultdict
from pathlib import Path

import numpy as np

TASK = Path(os.environ["ECV_TASK_DIR"]).resolve() if "ECV_TASK_DIR" in os.environ else Path(__file__).resolve().parent.parent.parent
IS_PKG = (TASK / "data" / "v4").exists() and not (TASK / "research" / "v4").exists()
R = TASK if IS_PKG else TASK / "research"
V2 = (TASK / "data" / "benchmark") if IS_PKG else (R / "v2")
V3 = (TASK / "data" / "benchmark") if IS_PKG else (R / "v3")
V4 = (TASK / "data" / "v4") if IS_PKG else (R / "v4")
SCRIPTS_V2 = (TASK / "scripts" / "v2") if IS_PKG else (R / "v2")
SCRIPTS_V3 = (TASK / "scripts" / "v3") if IS_PKG else (R / "v3")
SCRIPTS_V4 = (TASK / "scripts" / "v4") if IS_PKG else (R / "v4")
MINED_DIR = (TASK / "data" / "rq1" / "mined_repos") if IS_PKG else (V4 / "mined_repos")
REPOS = TASK / "tmp" / "repos"
RUNTIME = TASK / "tmp" / "v2_runtime_bench"
sys.path.insert(0, str(R))
sys.path.insert(0, str(SCRIPTS_V4))
for _p in (TASK / "scripts", TASK.parent.parent / "scripts"):
    if _p.exists():
        sys.path.insert(0, str(_p))
from ecv.deep_scanner import scan_file_ast_for_symbol  # noqa: E402
from ecv.ir import DDLOp  # noqa: E402
from ecv.synthesizer import synthesize_expand_contract  # noqa: E402
from label_overrides_v4 import OVERRIDES  # noqa: E402
from progress import Progress  # noqa: E402

_spec = importlib.util.spec_from_file_location("fin3", SCRIPTS_V3 / "finalize_rq3_v3.py")
fin3 = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(fin3)
compute_metrics, mcnemar_exact = fin3.compute_metrics, fin3.mcnemar_exact
_spec2 = importlib.util.spec_from_file_location("rq1v4", SCRIPTS_V4 / "rq1_v4_recompute.py")
rq1 = importlib.util.module_from_spec(_spec2)
_spec2.loader.exec_module(rq1)

FLAG = ("DUAL_VERSION_VIOLATION_BACKWARD", "DUAL_VERSION_VIOLATION_BOTH", "DATA_DEPENDENT")  # backward compatibility (C_{t-1} on S_t)
# Verified DDL-free Django operations (proxy-model DeleteModel and same-PG-type field subclass rename):
# Django emits no SQL for these operations, so SQL-based linters (django-migration-linter and translated squawk)
# receive empty SQL rather than synthetic DDL.
DDL_FREE_DJANGO_NOOPS = {"django_124", "django_139", "django_153"}
B = 2000


def key(sha, op):
    return (sha, op.get("file_path") or "", op.get("kind"), (op.get("table") or "").lower(), (op.get("column") or "").lower())


def exclusion_reason(case: dict, mined_cls: str | None, cache: dict) -> str | None:
    if mined_cls == "SQUASHED_REPLAY":
        return "squashed_migration"
    name, eco, sha, parent, fp = case["repo"], case["ecosystem"], case["sha"], case["parent"], case["file_path"]
    rdir = REPOS / name.replace("/", "__")
    if (name, sha) not in cache:
        fm = rq1.file_status_and_oids(rdir, [sha]).get(sha, {})
        cache[(name, sha)] = fm
    fm = cache[(name, sha)]
    st, oid = fm.get(fp, ("?", ""))
    if st != "A":
        return "edited_existing_file"
    from pathlib import Path as _P
    del_dirs = {str(_P(p).parent) for p, (s, _) in fm.items() if s == "D" and rq1.is_mig_path(eco, p)}
    if str(_P(fp).parent) in del_dirs:
        return "rewritten_history"
    if oid and oid in rq1.parent_oids(rdir, parent):
        return "copied_or_moved_file"
    dels = rq1.deleted_paths(rdir, eco)
    t = rq1.commit_time(rdir, [sha]).get(sha, 1 << 40)
    if any(x < t for x in dels.get(fp, [])):
        return "readded_file"
    return None


def ipw(rows, pred, w):
    tp = fp = fn = tn = 0.0
    for r in rows:
        p, y, wt = bool(pred(r)), bool(r["gt_violation"]), w[r["id"]]
        if p and y:
            tp += wt
        elif p:
            fp += wt
        elif y:
            fn += wt
        else:
            tn += wt
    prec = tp / (tp + fp) if tp + fp else 0
    rec = tp / (tp + fn) if tp + fn else 0
    fpr = fp / (fp + tn) if fp + tn else 0
    den = ((tp + fp) * (tp + fn) * (tn + fp) * (tn + fn)) ** 0.5
    return {"precision": prec, "recall": rec, "f1": 2 * prec * rec / (prec + rec) if prec + rec else 0,
            "fpr": fpr, "mcc": (tp * tn - fp * fn) / den if den else 0, "prevalence": (tp + fn) / (tp + fp + fn + tn)}


def main() -> None:
    log = open(V4 / "rq3_v4.log", "w", encoding="utf-8")
    raw = {c["id"]: c for c in json.loads((V2 / "benchmark_v2_raw.json").read_text(encoding="utf-8"))}
    gt = json.loads((V3 / "ground_truth_180_verified.json").read_text(encoding="utf-8"))
    bo = json.loads((V3 / "baselines_and_oracles_v3.json").read_text(encoding="utf-8"))
    pool = json.loads((V4 / "added_only_pool_v4.json").read_text(encoding="utf-8"))
    pool_idx = {key(x["sha"], x["op"]): x for e in pool.values() for x in e}

    mined_idx = {}
    for slugf in {c["repo"].replace("/", "__") for c in raw.values()}:
        d = json.loads((MINED_DIR / f"{slugf}.json").read_text(encoding="utf-8"))
        for dc in d.get("destructive_commits", []):
            for v in dc["verdicts"]:
                if v["op"].get("phase_section") == "up":
                    mined_idx.setdefault(key(dc["sha"], v["op"]), v)

    # labels
    changed = []
    for r in gt:
        if r["id"] in OVERRIDES:
            lbl, rat = OVERRIDES[r["id"]]
            if lbl != r["gt_violation"]:
                changed.append((r["id"], r["gt_violation"], lbl))
            r["gt_violation"], r["rationale"], r["confidence"] = lbl, rat, "ADJUDICATED_V4"

    prog = Progress(total=len(gt), label="v4-rq3-benchmark", stream=log)
    rows, excluded, unmatched = [], {}, []
    cache = {}
    for r in gt:
        c = raw[r["id"]]
        k = key(c["sha"], c["op"])
        rec = pool_idx.get(k)
        mv = mined_idx.get(k)
        cls = rec["ecv_classification"] if rec else (mv["classification"] if mv else None)
        reason = exclusion_reason(c, cls, cache)
        if reason:
            excluded[r["id"]] = {"reason": reason, "label": r["gt_violation"], "v4_cls": cls}
            prog.step(note=f"{r['id']} excluded {reason}")
            continue
        if cls is None:
            unmatched.append(r["id"])
            cls = c["ecv_classification"]
        ev = (rec or mv or {}).get("evidence") or c.get("evidence") or {}
        fast = cls.startswith(FLAG)
        hits = []
        if not fast:
            app_dir = RUNTIME / r["id"] / "app_old"
            for ap in sorted(app_dir.iterdir()) if app_dir.exists() else []:
                res = scan_file_ast_for_symbol(r["ecosystem"], ap.name, ap.read_text(encoding="utf-8", errors="ignore"),
                                               c["op"].get("table"), c["op"].get("column"))
                if res["hit"]:
                    hits.append(ap.name)
                    break
        rows.append({**r, "v4_cls": cls, "fast": fast, "deep": fast or bool(hits), "resolved": cls != "UNCERTAIN",
                     "schema_only": bool(fast and ev.get("orm_active_old")), "diff_only": bool(ev.get("removed_in_commit_diff")),
                     "git_stratum": c["git_stratum"], "kind": c["op"]["kind"]})
        prog.step(note=f"{r['id']} {cls}")
    prog.done()
    (V4 / "ground_truth_v4.json").write_text(json.dumps([{k: v for k, v in r.items()} for r in rows], indent=2), encoding="utf-8")

    sq, sm, dml = bo["squawk"], bo["strong_migrations"], bo["django_migration_linter"]
    preds = {
        "ecv_fast": lambda r: r["fast"], "ecv_deep": lambda r: r["deep"],
        "ecv_schema_only": lambda r: r["schema_only"], "ecv_diff_only": lambda r: r["diff_only"],
        "squawk_compat": lambda r: False if r["id"] in DDL_FREE_DJANGO_NOOPS else sq[r["id"]]["strict_compat_flagged"],
        "squawk_default": lambda r: False if r["id"] in DDL_FREE_DJANGO_NOOPS else sq[r["id"]]["default_flagged"],
        "always_flag": lambda r: True,
    }
    dml_pred = lambda r: False if r["id"] in DDL_FREE_DJANGO_NOOPS else bool(dml[r["id"]]["flagged_err"])

    def ev_rows(sub, fn, cov=False):
        tp = sum(1 for r in sub if fn(r) and r["gt_violation"])
        fp = sum(1 for r in sub if fn(r) and not r["gt_violation"])
        fnn = sum(1 for r in sub if not fn(r) and r["gt_violation"])
        tn = sum(1 for r in sub if not fn(r) and not r["gt_violation"])
        return compute_metrics(tp, fp, fnn, tn, sum(r["resolved"] for r in sub) if cov else None, len(sub) if cov else None)

    out = {"n_cases": len(rows), "excluded": excluded, "unmatched_kept_previous_verdict": unmatched,
           "label_changes_v4": changed,
           "labels": {"viol": sum(r["gt_violation"] for r in rows), "safe": sum(not r["gt_violation"] for r in rows)}}
    m = {"all": {k: ev_rows(rows, f, k.startswith("ecv_fast")) for k, f in preds.items()}}
    res_rows = [r for r in rows if r["resolved"]]
    m["resolved_only"] = {"ecv_fast": ev_rows(res_rows, preds["ecv_fast"]), "ecv_deep": ev_rows(res_rows, preds["ecv_deep"]),
                          "n": len(res_rows)}
    excl_constraints = [r for r in rows if r["kind"] != "ADD_UNIQUE_OR_CHECK"]
    m["all_excl_constraints"] = {"ecv_fast": ev_rows(excl_constraints, preds["ecv_fast"])}
    by_eco = {e: [r for r in rows if r["ecosystem"] == e] for e in ("prisma", "rails", "django")}
    for e, sub in by_eco.items():
        m[e] = {"ecv_fast": ev_rows(sub, preds["ecv_fast"], True), "ecv_fast_resolved": ev_rows([r for r in sub if r["resolved"]], preds["ecv_fast"])}
    m["prisma"]["squawk_compat_native"] = ev_rows(by_eco["prisma"], preds["squawk_compat"])
    r_exec = [r for r in by_eco["rails"] if sm.get(r["id"], {}).get("executed_ok")]
    m["rails_exec"] = {"n": len(r_exec), "ecv_fast": ev_rows(r_exec, preds["ecv_fast"]),
                       "strong_migrations": ev_rows(r_exec, lambda r: bool(sm[r["id"]]["flagged"]))}
    m["rails"]["strong_migrations_all"] = ev_rows(by_eco["rails"], lambda r: bool(sm.get(r["id"], {}).get("flagged")))
    m["django"]["dml"] = ev_rows(by_eco["django"], dml_pred)
    out["metrics"] = m

    def mc(sub, a, b, neg=False):
        return mcnemar_exact([a(r) for r in sub], [b(r) for r in sub], [r["gt_violation"] for r in sub], neg)
    out["mcnemar"] = {
        "fast_vs_always_overall": mc(rows, preds["ecv_fast"], preds["always_flag"]),
        "fast_vs_squawk_compat_overall": mc(rows, preds["ecv_fast"], preds["squawk_compat"]),
        "fast_vs_squawk_compat_safe": mc(rows, preds["ecv_fast"], preds["squawk_compat"], True),
        "fast_vs_deep_overall": mc(rows, preds["ecv_fast"], preds["ecv_deep"]),
        "prisma_fast_vs_squawk_overall": mc(by_eco["prisma"], preds["ecv_fast"], preds["squawk_compat"]),
        "prisma_fast_vs_squawk_safe": mc(by_eco["prisma"], preds["ecv_fast"], preds["squawk_compat"], True),
        "rails_exec_fast_vs_sm_overall": mc(r_exec, preds["ecv_fast"], lambda r: bool(sm[r["id"]]["flagged"])),
        "rails_exec_fast_vs_sm_safe": mc(r_exec, preds["ecv_fast"], lambda r: bool(sm[r["id"]]["flagged"]), True),
        "django_fast_vs_dml_overall": mc(by_eco["django"], preds["ecv_fast"], dml_pred),
        "django_fast_vs_dml_safe": mc(by_eco["django"], preds["ecv_fast"], dml_pred, True),
    }

    # post-stratified IPW over the v4 frame (viol / safe / uncertain / data-dependent ops in added files)
    popN = Counter((e, x.get("git_stratum", "cochanged"), x["op"]["kind"]) for e, L in pool.items() for x in L)
    sampN = Counter((r["ecosystem"], r["git_stratum"], r["kind"]) for r in rows)
    strat_pop = Counter((e, s) for (e, s, _), n in popN.items() for _ in range(n))
    covered = Counter()
    for (e, s, k), n in popN.items():
        if sampN[(e, s, k)]:
            covered[(e, s)] += n
    w = {}
    for r in rows:
        cell = (r["ecosystem"], r["git_stratum"], r["kind"])
        es = (r["ecosystem"], r["git_stratum"])
        base = popN[cell] / sampN[cell] if popN[cell] else 0.0
        w[r["id"]] = base * (strat_pop[es] / covered[es] if covered[es] else 0.0)
    uncovered = {f"{e}|{s}|{k}": n for (e, s, k), n in popN.items() if not sampN[(e, s, k)]}
    rng = np.random.default_rng(20261121)
    cells = defaultdict(list)
    for r in rows:
        cells[(r["ecosystem"], r["git_stratum"], r["kind"])].append(r)
    ipw_out = {}
    for name in ("ecv_fast", "ecv_deep", "squawk_compat", "always_flag"):
        point = ipw(rows, preds[name], w)
        boots = []
        for _ in range(B):
            bs = []
            for cl in cells.values():
                bs.extend(cl[i] for i in rng.integers(0, len(cl), len(cl)))
            # weights stay attached to the original case ids
            boots.append(ipw(bs, preds[name], w))
        ipw_out[name] = {k: {"point": round(100 * v, 1) if k != "mcc" else round(v, 3),
                             "ci95": [round((100 if k != "mcc" else 1) * float(q), 1 if k != "mcc" else 3)
                                      for q in np.percentile([b[k] for b in boots], [2.5, 97.5])]}
                         for k, v in point.items()}
    out["ipw"] = {"estimates": ipw_out, "population_frame_ops": sum(popN.values()),
                  "uncovered_cells_ops": sum(uncovered.values()), "uncovered_cells": uncovered}

    # Pre-adjudication sensitivities:
    # (a) true initial rule-based labels (gt_v2_initial.json, before any manual adjudication)
    v2_path = V2 / "gt_v2_initial.json"
    if v2_path.exists():
        v2_lbl_map = {x["id"]: bool(x["gt_violation"]) for x in json.loads(v2_path.read_text(encoding="utf-8"))}
        i_tp = sum(1 for r in rows if r["fast"] and v2_lbl_map[r["id"]])
        i_fp = sum(1 for r in rows if r["fast"] and not v2_lbl_map[r["id"]])
        i_fn = sum(1 for r in rows if not r["fast"] and v2_lbl_map[r["id"]])
        i_tn = sum(1 for r in rows if not r["fast"] and not v2_lbl_map[r["id"]])
        init_changes = [r for r in rows if bool(r["gt_violation"]) != v2_lbl_map[r["id"]]]
        out["pre_initial_rule_based"] = {
            "n_label_changes_in_167": len(init_changes),
            "moved_toward_ecv_fast": sum(1 for r in init_changes if bool(r["gt_violation"]) == bool(r["fast"])),
            "moved_away_from_ecv_fast": sum(1 for r in init_changes if bool(r["gt_violation"]) != bool(r["fast"])),
            "ecv_fast_metrics": compute_metrics(i_tp, i_fp, i_fn, i_tn),
        }

    # (b) Pre-v4-adjudication sensitivity (v3 intermediate labels on the 167 kept cases)
    v3_lbl_map = {x["id"]: bool(x["gt_violation"]) for x in json.loads((V3 / "ground_truth_180_verified.json").read_text(encoding="utf-8"))}
    pre_tp = sum(1 for r in rows if r["fast"] and v3_lbl_map[r["id"]])
    pre_fp = sum(1 for r in rows if r["fast"] and not v3_lbl_map[r["id"]])
    pre_fn = sum(1 for r in rows if not r["fast"] and v3_lbl_map[r["id"]])
    pre_tn = sum(1 for r in rows if not r["fast"] and not v3_lbl_map[r["id"]])
    kept_changes = [r for r in rows if bool(r["gt_violation"]) != v3_lbl_map[r["id"]]]
    out["pre_v4_adjudication"] = {
        "n_label_changes_in_167": len(kept_changes),
        "moved_toward_ecv_fast": sum(1 for r in kept_changes if bool(r["gt_violation"]) == bool(r["fast"])),
        "moved_away_from_ecv_fast": sum(1 for r in kept_changes if bool(r["gt_violation"]) != bool(r["fast"])),
        "ecv_fast_metrics": compute_metrics(pre_tp, pre_fp, pre_fn, pre_tn),
    }

    # DMMF oracle agreement (initial v3 labels vs final v4 labels)
    dm = bo["prisma_dmmf_oracle"]
    v3_lbl = v3_lbl_map
    ex = [r for r in rows if r["ecosystem"] == "prisma" and dm[r["id"]]["pg_c_old_at_s_new_crash"] is not None]
    out["dmmf_oracle"] = {"executable": len(ex),
                          "agree_v3_labels": sum(bool(dm[r["id"]]["pg_c_old_at_s_new_crash"]) == bool(v3_lbl[r["id"]]) for r in ex),
                          "agree_v4_labels": sum(bool(dm[r["id"]]["pg_c_old_at_s_new_crash"]) == bool(r["gt_violation"]) for r in ex)}

    # error listing
    out["ecv_fast_errors"] = [{"id": r["id"], "kind": "FN" if r["gt_violation"] else "FP", "op": r["kind"], "v4_cls": r["v4_cls"],
                               "rationale": r["rationale"][:200]} for r in rows if r["fast"] != r["gt_violation"]]

    # RQ4 minimal-schema replay on the v4 VIOL set
    synth_cases = []
    for r in rows:
        if not r["gt_violation"]:
            continue
        op = raw[r["id"]]["op"]
        ddl = DDLOp(ecosystem=r["ecosystem"], kind=op["kind"], table=op.get("table"), column=op.get("column"),
                    new_name=op.get("new_name"), old_type=r.get("old_type_observed") or op.get("old_type"),
                    new_type=op.get("new_type"), phase_section="up", file_path=r["file_path"], raw_snippet=op.get("raw_snippet", ""))
        old_t = r.get("old_type_observed") or op.get("old_type") or "TEXT"
        new_t = op.get("new_type") or ("INTEGER" if op["kind"] == "ALTER_COLUMN_TYPE" else None)
        plan = synthesize_expand_contract(ddl, old_pg_type=old_t, new_pg_type=new_t)
        synth_cases.append({"id": r["id"], "op_kind": op["kind"], "table": op.get("table"), "column": op.get("column"),
                            "new_name": op.get("new_name"), "old_type": old_t, "new_type": new_t, "synth_plan": plan.to_dict()})
    sin, sout = V4 / "synth_v4_in.json", V4 / "synth_v4_out.json"
    sin.write_text(json.dumps(synth_cases, indent=2), encoding="utf-8")
    subprocess.run(["node", str(SCRIPTS_V2 / "pg_v2_synth_worker.mjs"), str(sin), str(sout)], check=True, timeout=600, cwd=str(TASK if IS_PKG else TASK.parent.parent))
    syn = json.loads(sout.read_text(encoding="utf-8"))["results"]
    by_op = defaultdict(Counter)
    for s in syn:
        by_op[s["op_kind"]]["attempted"] += 1
        by_op[s["op_kind"]]["advisory_only"] += int(bool(s.get("advisory_only")))
        if not s.get("advisory_only"):
            by_op[s["op_kind"]]["executable_attempted"] += 1
            by_op[s["op_kind"]]["pass"] += int(bool(s["verified_pass"]))
    exec_syn = [s for s in syn if not s.get("advisory_only")]
    adv_syn = [s for s in syn if s.get("advisory_only")]
    out["rq4_minimal"] = {
        "total_violations": len(syn),
        "executable_attempted": len(exec_syn),
        "executable_pass": sum(bool(s["verified_pass"]) for s in exec_syn),
        "advisory_only": len(adv_syn),
        "attempted": len(exec_syn),
        "pass": sum(bool(s["verified_pass"]) for s in exec_syn),
        "by_op": {k: dict(v) for k, v in by_op.items()},
        "failures": [{"id": s["id"], "op": s["op_kind"], "phase1_ok": s["phase1_ok"],
                      "phase1_err": s.get("phase1_err"), "phase3_err": s.get("phase3_err"),
                      "schema_equiv": s["schema_equiv_to_target"]} for s in exec_syn if not s["verified_pass"]],
    }
    (V4 / "rq3_v4_metrics.json").write_text(json.dumps(out, indent=2), encoding="utf-8")
    brief = {k: out[k] for k in ("n_cases", "labels", "excluded", "unmatched_kept_previous_verdict")}
    print(json.dumps(brief, indent=2), file=log, flush=True)
    print(json.dumps(brief, indent=2))


if __name__ == "__main__":
    main()
