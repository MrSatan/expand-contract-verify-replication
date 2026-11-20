"""v3: Blind second-rater sheet for the 180-case benchmark ground truth.

Draws a stratified random sample (20 per ecosystem, 10 mig_only + 10 cochanged) and writes a CSV
that shows only what a human needs to judge C_{t-1} |= S_t: repository, commit link, migration file,
operator, target, and the forward DDL snippet. Neither the ground-truth label nor any tool verdict
is included. Fill in `rater_label` with VIOL / SAFE / UNSURE and run `kappa.py`.
"""
from __future__ import annotations

import csv
import json
import random
from pathlib import Path

TASK = Path(__file__).resolve().parent.parent.parent
V2 = TASK / "research" / "v2"
OUT = TASK / "research" / "v3" / "second_rater"
SEED = 20261121


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    gt = json.loads((V2 / "ground_truth_180_verified.json").read_text(encoding="utf-8"))
    rng = random.Random(SEED)
    picked = []
    for eco in ("prisma", "rails", "django"):
        for stratum in ("mig_only", "cochanged"):
            pool = [r for r in gt if r["ecosystem"] == eco and r["git_stratum"] == stratum]
            picked += rng.sample(pool, 10)
    rng.shuffle(picked)
    rows = []
    for i, r in enumerate(picked, 1):
        rows.append({
            "n": i,
            "case_id": r["id"],
            "ecosystem": r["ecosystem"],
            "commit_url": f"https://github.com/{r['repo']}/commit/{r['sha']}",
            "parent_tree_url": f"https://github.com/{r['repo']}/tree/{r['sha']}~1",
            "migration_file": r["file_path"],
            "operator": r["op_kind"],
            "table": r["table"],
            "column": r["column"] or "",
            "new_name": r.get("new_name") or "",
            "ddl_snippet": (r.get("raw_snippet") or "").replace("\n", " ")[:300],
            "rater_label": "",
            "rater_confidence_1to3": "",
            "rater_note": "",
        })
    with open(OUT / "second_rater_sheet_60.csv", "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)
    print(f"wrote {len(rows)} blind cases to {OUT / 'second_rater_sheet_60.csv'}")


if __name__ == "__main__":
    main()
