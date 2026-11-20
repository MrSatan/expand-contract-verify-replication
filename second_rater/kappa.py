"""Cohen's kappa between the human second rater(s) and the benchmark ground truth.

Usage:
  py -3.13 kappa.py second_rater_sheet_60.csv [second_rater_sheet_60_rater2.csv]

UNSURE labels are reported separately and excluded from kappa. With two filled sheets, the script
also reports rater-vs-rater kappa.
"""
from __future__ import annotations

import csv
import json
import sys
from pathlib import Path

_HERE = Path(__file__).resolve()
# v4: final labels (167 cases; 13 sampled cases fall into excluded files and are skipped)
GT_PATH = next(p for p in (_HERE.parents[1] / "data" / "benchmark" / "ground_truth_v4.json",
                           _HERE.parents[2] / "v4" / "ground_truth_v4.json",
                           _HERE.parents[1] / "data" / "benchmark" / "ground_truth_180_verified.json") if p.exists())


def kappa(a: list[bool], b: list[bool]) -> float:
    n = len(a)
    if n == 0:
        return float("nan")
    po = sum(x == y for x, y in zip(a, b)) / n
    pa, pb = sum(a) / n, sum(b) / n
    pe = pa * pb + (1 - pa) * (1 - pb)
    return (po - pe) / (1 - pe) if pe < 1 else 1.0


def load(path: str) -> dict[str, str]:
    with open(path, encoding="utf-8-sig") as f:
        return {r["case_id"]: r["rater_label"].strip().upper() for r in csv.DictReader(f)}


def main() -> None:
    gt = {r["id"]: r["gt_violation"] for r in json.loads(GT_PATH.read_text(encoding="utf-8"))}
    r1 = load(sys.argv[1])
    lab = {k: v for k, v in r1.items() if v in ("VIOL", "SAFE")}
    unsure = [k for k, v in r1.items() if v == "UNSURE"]
    empty = [k for k, v in r1.items() if not v]
    skipped = sorted(k for k in lab if k not in gt)
    ids = sorted(k for k in lab if k in gt)
    a = [lab[i] == "VIOL" for i in ids]
    b = [gt[i] for i in ids]
    agree = sum(x == y for x, y in zip(a, b))
    print(f"rater1 vs GT: n={len(ids)} agree={agree} ({agree / max(1, len(ids)):.1%}) kappa={kappa(a, b):.3f}; UNSURE={len(unsure)} empty={len(empty)} skipped(excluded)={len(skipped)}")
    print("disagreements:", [i for i, x, y in zip(ids, a, b) if x != y])
    # PABAK and a bootstrap interval for kappa
    import random
    rnd = random.Random(1)
    ks = []
    for _ in range(2000):
        s_ = [rnd.randrange(len(ids)) for _ in ids]
        ks.append(kappa([a[i] for i in s_], [b[i] for i in s_]))
    ks.sort()
    po = agree / max(1, len(ids))
    print(f"PABAK={2 * po - 1:.3f}; kappa 95% bootstrap CI=[{ks[50]:.3f}, {ks[1949]:.3f}]")
    if len(sys.argv) > 2:
        r2 = load(sys.argv[2])
        both = sorted(k for k in lab if r2.get(k) in ("VIOL", "SAFE"))
        x = [lab[k] == "VIOL" for k in both]
        y = [r2[k] == "VIOL" for k in both]
        print(f"rater1 vs rater2: n={len(both)} kappa={kappa(x, y):.3f}")


if __name__ == "__main__":
    main()
