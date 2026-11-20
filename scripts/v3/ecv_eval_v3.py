"""v3: ECV-Fast / ECV-Deep verdicts for the fixed 180-case benchmark using the v3 re-mined corpus.

- ECV-Fast verdict = v3 mined classification of the same operation (matched by sha, file, kind, table, column).
  A case is *flagged* when the verdict is DUAL_VERSION_VIOLATION_* or DATA_DEPENDENT (a CI warning).
- ECV-Deep = ECV-Fast, plus the AST call-site scan of the archived C_{t-1} app files when ECV-Fast does not flag.
- strong_migrations predictions come from the v3 run that feeds real C_{t-1} column metadata.
Writes research/v3/baselines_and_oracles_v3.json (v2 file with `ecv_eval` and `strong_migrations` replaced).
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

TASK = Path(__file__).resolve().parent.parent.parent
V2 = TASK / "research" / "v2"
V3 = TASK / "research" / "v3"
RUNTIME = TASK / "tmp" / "v2_runtime_bench"
sys.path.insert(0, str(TASK / "research"))
from ecv.deep_scanner import scan_file_ast_for_symbol  # noqa: E402


def key(sha, op):
    return (sha, op.get("file_path") or "", op.get("kind"), (op.get("table") or "").lower(), (op.get("column") or "").lower())


def main() -> None:
    raw = json.loads((V2 / "benchmark_v2_raw.json").read_text(encoding="utf-8"))
    bo = json.loads((V2 / "baselines_and_oracles_v2.json").read_text(encoding="utf-8"))
    v3_index: dict[tuple, str] = {}
    mined_cache: dict[str, dict] = {}
    for c in raw:
        slug = c["repo"].replace("/", "__")
        if slug not in mined_cache:
            mined_cache[slug] = json.loads((V3 / "mined_repos" / f"{slug}.json").read_text(encoding="utf-8"))
            for dc in mined_cache[slug].get("destructive_commits", []):
                for v in dc["verdicts"]:
                    if v["op"].get("phase_section") == "up":
                        v3_index.setdefault(key(dc["sha"], v["op"]), v["classification"])
    ecv_eval, missing, changed = {}, [], []
    for c in raw:
        cid, eco = c["id"], c["ecosystem"]
        cls = v3_index.get(key(c["sha"], c["op"]))
        if cls is None:
            missing.append(cid)
            cls = c["ecv_classification"]
        if cls != c["ecv_classification"]:
            changed.append((cid, c["ecv_classification"], cls))
        fast = cls.startswith("DUAL_VERSION_VIOLATION") or cls == "DATA_DEPENDENT"
        hits = []
        if not fast:
            app_dir = RUNTIME / cid / "app_old"
            for ap in sorted(app_dir.iterdir()) if app_dir.exists() else []:
                res = scan_file_ast_for_symbol(eco, ap.name, ap.read_text(encoding="utf-8", errors="ignore"),
                                               c["op"].get("table"), c["op"].get("column"))
                if res["hit"]:
                    hits.append({"file": ap.name, "kind": res["kind"]})
                    break
        old = bo["ecv_eval"][cid]
        ecv_eval[cid] = {**old, "ecv_classification": cls, "is_uncertain": cls == "UNCERTAIN",
                         "ecv_fast_pred": fast, "ecv_deep_pred": fast or bool(hits), "ecv_deep_ast_hits": hits}
    sm3 = {r["id"]: r for r in json.loads((V3 / "strong_migrations_60_out_v3.json").read_text(encoding="utf-8"))}
    bo["ecv_eval"] = ecv_eval
    bo["strong_migrations"] = sm3
    bo["runtime_summary"]["strong_migrations_real_rails"].update({
        "executed_ok": sum(r["executed_ok"] for r in sm3.values()), "flagged": sum(r["flagged"] for r in sm3.values())})
    bo["v3_notes"] = {"verdict_changes_vs_v2": changed, "unmatched_cases_kept_v2_verdict": missing}
    (V3 / "baselines_and_oracles_v3.json").write_text(json.dumps(bo, indent=2), encoding="utf-8")
    print(f"matched {len(raw) - len(missing)}/{len(raw)}; verdict changes vs v2: {len(changed)}")
    for x in changed:
        print("  ", x)
    if missing:
        print("unmatched:", missing)


if __name__ == "__main__":
    main()
