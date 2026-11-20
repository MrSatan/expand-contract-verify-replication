"""V2 Phase 3B (RQ2): Curate High-Precision, Verified Practitioner Failure & Linter Reports
from Mature Open-Source Repositories (stars >= 500, full issue bodies verified).

Focuses on two strictly verified evidence classes:
  (A) Real runtime/deployment schema-skew crashes (PG::UndefinedColumn, UndefinedTable,
      not-null constraint violations during rolling upgrades or zero-downtime deploys).
  (B) Practitioner bug/pain reports on industrial single-file migration linters
      (`squawk`, `strong_migrations`, `django-migration-linter`) documenting false positives
      on valid Expand-Contract phases or unsafe bypasses.
"""
from __future__ import annotations

import json
import re
import subprocess
import time
from pathlib import Path

import requests

TASK_DIR = Path(__file__).resolve().parent.parent.parent
V2_DIR = TASK_DIR / "research" / "v2"

# Targeted GitHub searches across mature projects & the 3 industrial linters themselves
SEARCHES = [
    ("linter", 'repo:sbdchd/squawk "false positive" OR "expand" OR "drop column" is:issue'),
    ("linter", 'repo:ankane/strong_migrations "ignored_columns" OR "safety_assured" OR "false positive" is:issue'),
    ("linter", 'repo:3YOURMIND/django-migration-linter "SeparateDatabaseAndState" OR "false positive" is:issue'),
    ("crash",  '"UndefinedColumn" "ignored_columns" is:issue'),
    ("crash",  '"UndefinedColumn" "rolling" OR "zero downtime" OR "deploy" is:issue'),
    ("crash",  '"column" "does not exist" "rolling deploy" OR "rolling update" is:issue'),
    ("crash",  '"violates not-null constraint" "rolling" OR "zero downtime" OR "old pods" is:issue'),
    ("crash",  '"SeparateDatabaseAndState" "does not exist" OR "downtime" OR "deploy" is:issue'),
    ("crash",  '"P2022" "prisma" "migrate" OR "deploy" is:issue'),
]


def get_token() -> str:
    return subprocess.run(["gh", "auth", "token"], capture_output=True, text=True, check=True).stdout.strip()


def main():
    token = get_token()
    sess = requests.Session()
    sess.headers.update({
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "User-Agent": "expand-contract-rq2-v2",
    })

    seen = set()
    candidates = []
    stars_cache = {}

    for kind, q in SEARCHES:
        r = sess.get("https://api.github.com/search/issues", params={"q": q, "sort": "comments", "order": "desc", "per_page": 20}, timeout=25)
        if r.status_code == 200:
            for it in r.json().get("items", []):
                url = it.get("html_url", "")
                if not url or "/pull/" in url or url in seen:
                    continue
                seen.add(url)
                repo = "/".join(url.split("/")[3:5])
                if repo not in stars_cache:
                    rr = sess.get(f"https://api.github.com/repos/{repo}", timeout=15)
                    if rr.status_code == 200:
                        rd = rr.json()
                        stars_cache[repo] = (rd.get("stargazers_count", 0), rd.get("fork", False))
                    else:
                        stars_cache[repo] = (0, False)
                    time.sleep(0.15)
                st, is_fork = stars_cache[repo]
                if is_fork or st < 500:
                    continue
                candidates.append({
                    "kind": kind,
                    "repo": repo,
                    "stars": st,
                    "number": it.get("number"),
                    "url": url,
                    "state": it.get("state"),
                    "comments": it.get("comments", 0),
                    "created_at": (it.get("created_at") or "")[:10],
                    "title": it.get("title") or "",
                    "body_excerpt": (it.get("body") or "")[:700].replace("\r", "").replace("\n", " "),
                })
        time.sleep(2.2)

    candidates.sort(key=lambda x: (x["kind"], -x["comments"], -x["stars"]))
    (V2_DIR / "rq2_mature_candidates.json").write_text(json.dumps(candidates, indent=2), encoding="utf-8")
    print(f"Found {len(candidates)} mature-repo (stars>=500) candidates:")
    for c in candidates[:35]:
        print(f"  [{c['kind']:6s}] {c['repo']} ({c['stars']}*) #{c['number']} ({c['created_at']}, {c['comments']}c): {c['title'][:90]}")
        print(f"           BODY: {c['body_excerpt'][:160]}")


if __name__ == "__main__":
    main()
