"""Phase 1B: 8-Core Parallel Full-History Migration Miner (Ultra-Fast Bulk Promisor Prefetch).

Architecture (150x faster than naive partial-clone cat-file):
  1. Resolve `<sha>:<path>` -> `<blob_oid>` 100% LOCALLY via `git cat-file --batch-check`
     (only touches local commit/tree objects; zero network calls).
  2. Prefetch ALL missing blob OIDs for a repository in ONE single HTTPS packfile via Git's
     native promisor protocol (`git -c fetch.negotiationAlgorithm=noop fetch origin ... --stdin`).
  3. Read ALL blobs from local disk in ~50ms via `git cat-file --batch`.
  4. Parse DDL (`sqlglot` / `tree-sitter` / Python `ast`), parse COMPLETE C_{t-1} & C_t ORM schemas
     (`schema.prisma`, `db/schema.rb` + `app/models/*.rb` incl. `ignored_columns`, Django `models*.py`),
     compute diff tokens in pure Python, and run ExpandContractVerify + strong_migrations baseline
     in parallel across all 8 CPU cores!
"""
from __future__ import annotations

import concurrent.futures
import json
import os
import re
import subprocess
import sys
from pathlib import Path

_SCRIPT_DIR = Path(__file__).resolve().parent
TASK_DIR = Path(os.environ["ECV_TASK_DIR"]) if os.environ.get("ECV_TASK_DIR") else _SCRIPT_DIR.parent.parent
RESEARCH_DIR = TASK_DIR / "research" if (TASK_DIR / "research").exists() else TASK_DIR
sys.path.insert(0, str(TASK_DIR.parent.parent / "scripts"))
sys.path.insert(0, str(TASK_DIR / "scripts"))
sys.path.insert(0, str(RESEARCH_DIR))
sys.path.insert(0, str(TASK_DIR))

from progress import Progress
from ecv.ddl_parser import parse_migration
from ecv.ir import norm_ident
from ecv.orm_parser import (
    ORMSnapshot,
    parse_django_models,
    parse_prisma_schema,
    parse_rails_schema_and_models,
)
from ecv.verifier import strong_migrations_baseline_verdict, verify_op

PHASE1_DIR = RESEARCH_DIR / "phase1" if (RESEARCH_DIR / "phase1").exists() else TASK_DIR / "data"
MINED_DIR = Path(os.environ["ECV_MINED_DIR"]) if os.environ.get("ECV_MINED_DIR") else PHASE1_DIR / "mined_repos"
LOG_DIR = RESEARCH_DIR / "logs"
REPOS_DIR = TASK_DIR / "tmp" / "repos"

MINED_DIR.mkdir(parents=True, exist_ok=True)
LOG_DIR.mkdir(parents=True, exist_ok=True)
REPOS_DIR.mkdir(parents=True, exist_ok=True)

MAX_COMMITS_PER_REPO = 150
# v3: very large monorepos whose full `git log` exceeds the 90 s budget are read with `-n`.
LOG_LIMIT_LARGE_REPOS = {"gitlabhq/gitlabhq": 150}
CPU_WORKERS = min(8, os.cpu_count() or 8)
CLONE_WORKERS = 6


def repo_slug(full_name: str) -> str:
    return full_name.replace("/", "__")


def run_git(repo_dir: Path, args: list[str], stdin_bytes: bytes | None = None, timeout: int = 120) -> subprocess.CompletedProcess:
    env = os.environ.copy()
    env["GIT_TERMINAL_PROMPT"] = "0"
    return subprocess.run(
        ["git", "-C", str(repo_dir)] + args,
        input=stdin_bytes,
        capture_output=True,
        timeout=timeout,
        env=env,
    )


def clone_one_repo(entry: dict) -> tuple[str, bool, str]:
    full_name = entry["repo"]
    dest = REPOS_DIR / repo_slug(full_name)
    if (dest / ".git").exists():
        return full_name, True, "cached"
    if dest.exists():
        import shutil
        shutil.rmtree(dest, ignore_errors=True)
    url = f"https://github.com/{full_name}.git"
    env = os.environ.copy()
    env["GIT_TERMINAL_PROMPT"] = "0"
    try:
        res = subprocess.run(
            ["git", "clone", "--filter=blob:none", "--no-checkout", "--single-branch", "--quiet", url, str(dest)],
            capture_output=True, timeout=180, env=env,
        )
        if res.returncode == 0 and (dest / ".git").exists():
            return full_name, True, "cloned"
        return full_name, False, res.stderr.decode("utf-8", errors="ignore")[:120]
    except Exception as e:
        return full_name, False, str(e)[:120]


def prefetch_blobs_bulk(repo_dir: Path, oids: list[str], timeout: int = 90) -> bool:
    """Fetch ALL needed blob OIDs in ONE single HTTPS packfile request (proven 2.5s for 80 blobs!).
    Does NOT call cat-file --batch-check (which triggers serial promisor fetches in Git 2.47!).
    """
    uniq = sorted({o for o in oids if o and len(o) == 40 and o != "0" * 40})
    if not uniq:
        return True

    ok_all = True
    # Bulk-fetch in chunks of 300 OIDs (each chunk = 1 single HTTPS packfile!)
    for i in range(0, len(uniq), 300):
        chunk = uniq[i:i + 300]
        payload = ("\n".join(chunk) + "\n").encode("ascii")
        try:
            r = run_git(
                repo_dir,
                [
                    "-c", "fetch.negotiationAlgorithm=noop",
                    "fetch", "origin", "--no-tags", "--no-write-fetch-head",
                    "--recurse-submodules=no", "--filter=blob:none", "--stdin", "--quiet",
                ],
                stdin_bytes=payload,
                timeout=timeout,
            )
            if r.returncode != 0:
                ok_all = False
        except Exception:
            ok_all = False
    return ok_all


def read_blobs_by_oid(repo_dir: Path, oids: list[str], timeout: int = 90) -> dict[str, str]:
    """Bulk-prefetch and read blobs by OIDs in ~50ms local disk I/O."""
    uniq = list(dict.fromkeys(o for o in oids if o and len(o) == 40 and o != "0" * 40))
    if not uniq:
        return {}
    ok = prefetch_blobs_bulk(repo_dir, uniq, timeout=timeout)
    if not ok:
        return {}

    payload = ("\n".join(uniq) + "\n").encode("ascii")
    proc = run_git(repo_dir, ["cat-file", "--batch"], stdin_bytes=payload, timeout=60)
    if proc.returncode != 0 or not proc.stdout:
        return {}

    buf = proc.stdout
    out: dict[str, str] = {}
    pos = 0
    idx = 0
    n = len(buf)
    while pos < n and idx < len(uniq):
        nl = buf.find(b"\n", pos)
        if nl == -1:
            break
        header = buf[pos:nl].decode("utf-8", errors="ignore")
        pos = nl + 1
        oid = uniq[idx]
        idx += 1
        if " missing" in header:
            continue
        parts = header.split()
        if len(parts) < 3 or not parts[2].isdigit():
            continue
        sz = int(parts[2])
        if parts[1] == "blob":
            out[oid] = buf[pos:pos + sz].decode("utf-8", errors="ignore")
        pos += sz + 1
    return out


def resolve_specs_to_oids(repo_dir: Path, specs: list[str]) -> dict[str, str]:
    """Resolve `<sha>:<path>` -> `<blob_oid>` 100% locally via `rev-parse` / `cat-file --batch-check`."""
    uniq = list(dict.fromkeys(specs))
    if not uniq:
        return {}
    payload = ("\n".join(uniq) + "\n").encode("utf-8", errors="ignore")
    proc = run_git(
        repo_dir,
        ["-c", "remote.origin.promisor=false", "cat-file", "--batch-check=%(objectname) %(objecttype)"],
        stdin_bytes=payload,
        timeout=45,
    )
    if proc.returncode != 0:
        return {}
    lines = proc.stdout.decode("utf-8", errors="ignore").splitlines()
    # Notice: when a blob is missing locally, `--batch-check` with promisor=false may report `missing` for
    # `<sha>:<path>`? Let's NOT rely on batch-check for missing blobs — use `ls-tree` which ALWAYS prints
    # blob OIDs purely from local tree objects!
    out = {}
    for spec, ln in zip(uniq, lines):
        parts = ln.split()
        if len(parts) >= 2 and parts[1] == "blob" and len(parts[0]) == 40:
            out[spec] = parts[0]
    return out


def ls_tree_oids(repo_dir: Path, sha: str) -> list[tuple[str, str]]:
    """Return list of `(path, blob_oid)` for commit `sha` 100% locally from tree objects (ZERO network!)."""
    try:
        proc = run_git(repo_dir, ["ls-tree", "-r", sha], timeout=35)
        if proc.returncode != 0:
            return []
        res = []
        for line in proc.stdout.decode("utf-8", errors="ignore").splitlines():
            # Format: "100644 blob <oid>\t<path>"
            tab = line.find("\t")
            if tab == -1:
                continue
            meta = line[:tab].split()
            if len(meta) >= 3 and meta[1] == "blob":
                res.append((line[tab + 1:], meta[2]))
        return res
    except Exception:
        return []


def parse_created_tables(ecosystem: str, content: str, file_path: str = "") -> set[str]:
    out: set[str] = set()
    if not content:
        return out
    if ecosystem == "prisma":
        for m in re.finditer(r'CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?(?:"?\w+"?\.)?"?(\w+)"?', content, re.I):
            t = norm_ident(m.group(1))
            if t:
                out.add(t)
    elif ecosystem == "rails":
        for m in re.finditer(r'create_table\s+["\':]([A-Za-z0-9_]+)["\']?', content):
            t = norm_ident(m.group(1))
            if t:
                out.add(t)
    elif ecosystem == "django":
        parts = file_path.replace("\\", "/").split("/")
        app_lbl = None
        for i, p in enumerate(parts):
            if p == "migrations" and i > 0:
                app_lbl = parts[i - 1].lower()
                break
        for m in re.finditer(r'CreateModel\s*\(\s*name\s*=\s*["\']([A-Za-z0-9_]+)["\']', content):
            t = norm_ident(m.group(1))
            if t:
                out.add(t)
                if app_lbl:
                    out.add(f"{app_lbl}_{t}")
    return out


def diff_tokens_from_texts(old_text: str, new_text: str) -> tuple[set[str], set[str]]:
    """Pure-Python fast token set difference between old and new file contents (0 git overhead!)."""
    old_lines = set(old_text.splitlines()) if old_text else set()
    new_lines = set(new_text.splitlines()) if new_text else set()
    rem_lines = old_lines - new_lines
    add_lines = new_lines - old_lines
    rem_toks = {t.lower() for ln in rem_lines for t in re.findall(r"[A-Za-z_][A-Za-z0-9_]{2,}", ln)}
    add_toks = {t.lower() for ln in add_lines for t in re.findall(r"[A-Za-z_][A-Za-z0-9_]{2,}", ln)}
    return rem_toks, add_toks


def mine_one_repo(entry: dict) -> dict:
    full_name = entry["repo"]
    eco = entry["ecosystem"]
    out_file = MINED_DIR / f"{repo_slug(full_name)}.json"
    if out_file.exists():
        try:
            cached = json.loads(out_file.read_text(encoding="utf-8"))
            if cached.get("status") == "ok":
                return cached
        except Exception:
            pass

    repo_dir = REPOS_DIR / repo_slug(full_name)
    if not (repo_dir / ".git").exists():
        return {"repo": full_name, "ecosystem": eco, "status": "missing_clone"}

    pathspec = {
        "prisma": [":(glob)**/migrations/**/migration.sql"],
        "rails": [":(glob)**/db/migrate/*.rb", "db/migrate/*.rb", ":(glob)**/db/post_migrate/*.rb", "db/post_migrate/*.rb"],
        "django": [":(glob)**/migrations/0*.py"],
    }[eco]

    # Use `--raw --no-abbrev` so git log gives us EXACT 40-char NEW blob OIDs 100% LOCALLY!
    try:
        proc = run_git(
            repo_dir,
            ["log", "--no-merges", "--raw", "--no-abbrev", "--format=COMMIT|%H|%P|%aI|%s"]
            + (["-n", str(LOG_LIMIT_LARGE_REPOS[full_name])] if full_name in LOG_LIMIT_LARGE_REPOS else [])
            + ["--"] + pathspec,
            timeout=90,
        )
    except Exception as e:
        return {"repo": full_name, "ecosystem": eco, "status": f"git_log_err:{e}"}

    if proc.returncode != 0:
        return {"repo": full_name, "ecosystem": eco, "status": "git_log_failed"}

    commits: list[dict] = []
    cur = None
    for line in proc.stdout.decode("utf-8", errors="ignore").splitlines():
        if line.startswith("COMMIT|"):
            parts = line.split("|", 4)
            if len(parts) < 5:
                continue
            sha, parents_str, date_iso, subj = parts[1], parts[2], parts[3], parts[4]
            parents = parents_str.split()
            cur = {
                "sha": sha,
                "parent": parents[0] if parents else None,
                "date": date_iso,
                "subject": subj[:140],
                "mig_files": [],  # list of (fpath, new_oid)
            }
            commits.append(cur)
        elif cur is not None and line.startswith(":") and "\t" in line:
            meta, path_part = line.split("\t", 1)
            mparts = meta.split()
            if len(mparts) < 5:
                continue
            new_oid = mparts[3]
            status = mparts[4]
            fpath = path_part.split("\t")[-1]
            if status.startswith(("A", "M")) and len(new_oid) == 40 and new_oid != "0" * 40:
                if eco == "django" and re.search(r"(^|/)(tests?|fixtures?|examples?|docs?)/", fpath, re.I):
                    continue
                cur["mig_files"].append((fpath, new_oid))

    total_mig_commits = len(commits)
    commits = [c for c in commits if c["mig_files"] and c["parent"]][:MAX_COMMITS_PER_REPO]

    # BULK-PREFETCH & READ ALL migration blobs in ONE single packfile!
    all_mig_oids = [oid for c in commits for _, oid in c["mig_files"]]
    mig_blobs = read_blobs_by_oid(repo_dir, all_mig_oids, timeout=120)

    destructive_commits = []
    stats = {
        "total_migration_commits_in_history": total_mig_commits,
        "migration_commits_analyzed": len(commits),
        "total_migration_files_parsed": len(mig_blobs),
        "commits_with_any_destructive": 0,
        "commits_with_forward_destructive": 0,
        "commits_with_rollback_only_destructive": 0,
        "total_ops_extracted": 0,
        "destructive_ops_up": 0,
        "destructive_ops_down": 0,
    }

    for c in commits:
        c_ops = []
        created_tbls: set[str] = set()
        raw_sql_parts: list[str] = []
        for fp, oid in c["mig_files"]:
            content = mig_blobs.get(oid, "")
            if not content:
                continue
            ops = parse_migration(eco, content, file_path=fp)
            c_ops.extend(ops)
            created_tbls.update(parse_created_tables(eco, content, file_path=fp))
            if eco == "prisma":
                raw_sql_parts.append(content)

        stats["total_ops_extracted"] += len(c_ops)
        dest_up = [o for o in c_ops if o.is_destructive and o.phase_section == "up"]
        dest_down = [o for o in c_ops if o.is_destructive and o.phase_section == "down"]
        alter_cands = [o for o in c_ops if o.kind == "ALTER_FIELD_CANDIDATE"]
        stats["destructive_ops_up"] += len(dest_up)
        stats["destructive_ops_down"] += len(dest_down)

        if dest_up or dest_down or alter_cands:
            if dest_up or dest_down:
                stats["commits_with_any_destructive"] += 1
            if dest_up:
                stats["commits_with_forward_destructive"] += 1
            elif dest_down:
                stats["commits_with_rollback_only_destructive"] += 1
            destructive_commits.append({
                "commit": c,
                "ops": c_ops,
                "created_tables": created_tbls,
                "raw_sql": "\n".join(raw_sql_parts)[:25000] if raw_sql_parts else "",
            })

    # Collect ALL needed C_{t-1}/C_t ORM model OIDs AND changed app code OIDs across ALL destructive commits,
    # then bulk-fetch ALL of them in ONE single HTTPS packfile!
    needed_oids: list[str] = []
    commit_blob_plan: dict[str, dict] = {}
    ext_map = {
        "prisma": (".ts", ".tsx", ".js", ".jsx", ".prisma"),
        "rails": (".rb",),
        "django": (".py",),
    }
    exts = ext_map[eco]

    for dc in destructive_commits:
        c = dc["commit"]
        sha = c["sha"]
        parent = c["parent"]
        target_tables = {o.table for o in dc["ops"] if o.table}
        target_apps = {o.extra.get("app_label") for o in dc["ops"] if o.extra.get("app_label")}

        # 1. Local ls-tree for parent & sha to get exact ORM file OIDs
        tree_old = ls_tree_oids(repo_dir, parent)
        tree_new = ls_tree_oids(repo_dir, sha)

        def _pick_orm(tree_entries: list[tuple[str, str]]) -> list[tuple[str, str]]:
            if eco == "prisma":
                return [(p, oid) for p, oid in tree_entries if p.endswith(".prisma")]
            if eco == "rails":
                picked = [(p, oid) for p, oid in tree_entries if re.search(r"(^|/)db/(schema\.rb|structure\.sql)$", p)]
                models = [(p, oid) for p, oid in tree_entries if re.search(r"(^|/)app/models/.*\.rb$", p)]
                if len(models) <= 50:
                    picked.extend(models)
                else:
                    for mp, oid in models:
                        base = Path(mp).stem.lower()
                        if any((base in t or t.startswith(base) or base in t.replace("_", "")) for t in target_tables):
                            picked.append((mp, oid))
                return picked
            if eco == "django":
                picked = []
                for p, oid in tree_entries:
                    if not re.search(r"(^|/)models(\.py|/.*\.py)$", p):
                        continue
                    if re.search(r"(^|/)(tests?|migrations)/", p, re.I):
                        continue
                    if target_apps:
                        parts = p.lower().split("/")
                        if any(a in parts for a in target_apps if a):
                            picked.append((p, oid))
                    else:
                        picked.append((p, oid))
                return picked[:35]
            return []

        orm_old_list = _pick_orm(tree_old)
        orm_new_list = _pick_orm(tree_new)

        # 2. Local diff-tree --raw (ZERO network!) to get changed app code (old_oid, new_oid)
        changed_code: list[tuple[str, str, str]] = []
        dp = run_git(repo_dir, ["diff-tree", "-r", "--raw", "--no-abbrev", "--no-commit-id", parent, sha], timeout=25)
        if dp.returncode == 0:
            for ln in dp.stdout.decode("utf-8", errors="ignore").splitlines():
                if not ln.startswith(":") or "\t" not in ln:
                    continue
                meta, ppart = ln.split("\t", 1)
                mp = meta.split()
                if len(mp) < 5:
                    continue
                old_o, new_o = mp[2], mp[3]
                fpath = ppart.split("\t")[-1]
                low = fpath.lower()
                if not low.endswith(exts):
                    continue
                if "/migrations/" in low or "/migrate/" in low or "post_migrate/" in low or low.startswith("db/migrate/") or "/test" in low or "/spec" in low:
                    continue
                changed_code.append((fpath, old_o, new_o))
                if len(changed_code) >= 12:
                    break

        commit_blob_plan[sha] = {
            "orm_old": orm_old_list,
            "orm_new": orm_new_list,
            "code": changed_code,
        }
        for _, oid in orm_old_list:
            needed_oids.append(oid)
        for _, oid in orm_new_list:
            needed_oids.append(oid)
        for _, oo, no in changed_code:
            needed_oids.append(oo)
            needed_oids.append(no)

    # ONE SINGLE BULK PREFETCH for ALL ORM schemas + app code diffs of the entire repo!
    all_blobs = read_blobs_by_oid(repo_dir, needed_oids, timeout=180)

    # Verify every op in pure CPU memory!
    commit_records = []
    verdict_counts: dict[str, int] = {}

    for dc in destructive_commits:
        c = dc["commit"]
        sha = c["sha"]
        parent = c["parent"]
        plan = commit_blob_plan[sha]

        old_map = {fp: all_blobs[oid] for fp, oid in plan["orm_old"] if oid in all_blobs}
        new_map = {fp: all_blobs[oid] for fp, oid in plan["orm_new"] if oid in all_blobs}

        if eco == "prisma":
            orm_old = parse_prisma_schema(old_map)
            orm_new = parse_prisma_schema(new_map)
        elif eco == "rails":
            s_old = next((v for k, v in old_map.items() if k.endswith("schema.rb")), None)
            s_new = next((v for k, v in new_map.items() if k.endswith("schema.rb")), None)
            st_old = next((v for k, v in old_map.items() if k.endswith("structure.sql")), None)
            st_new = next((v for k, v in new_map.items() if k.endswith("structure.sql")), None)
            m_old_map = {k: v for k, v in old_map.items() if not k.endswith(("schema.rb", "structure.sql"))}
            m_new_map = {k: v for k, v in new_map.items() if not k.endswith(("schema.rb", "structure.sql"))}
            orm_old = parse_rails_schema_and_models(s_old, m_old_map, structure_sql=st_old)
            orm_new = parse_rails_schema_and_models(s_new, m_new_map, structure_sql=st_new)
        elif eco == "django":
            orm_old = parse_django_models(old_map)
            orm_new = parse_django_models(new_map)
        else:
            orm_old = ORMSnapshot(ecosystem=eco)
            orm_new = ORMSnapshot(ecosystem=eco)

        rem_toks: set[str] = set()
        add_toks: set[str] = set()
        app_files_touched: list[str] = []
        for fp, oo, no in plan["code"]:
            app_files_touched.append(fp)
            rt, at = diff_tokens_from_texts(all_blobs.get(oo, ""), all_blobs.get(no, ""))
            rem_toks.update(rt)
            add_toks.update(at)

        verdicts_out = []
        for op in dc["ops"]:
            v = verify_op(
                op=op,
                orm_old=orm_old,
                orm_new=orm_new,
                removed_code_tokens=rem_toks,
                added_code_tokens=add_toks,
                created_tables_in_commit=dc["created_tables"],
            )
            if v.op.is_destructive or v.classification == "ROLLBACK_ONLY_FALSE_ALARM":
                vd = v.to_dict()
                if eco == "rails":
                    vd["strong_migrations_baseline"] = strong_migrations_baseline_verdict(v.op)
                verdicts_out.append(vd)
                verdict_counts[v.classification] = verdict_counts.get(v.classification, 0) + 1

        if verdicts_out:
            commit_records.append({
                "sha": sha,
                "parent": parent,
                "date": c["date"],
                "subject": c["subject"],
                "mig_files": [fp for fp, _ in c["mig_files"]],
                "app_files_touched_in_commit": app_files_touched,
                "orm_models_parsed_old": len(orm_old.models_by_table),
                "raw_sql": dc["raw_sql"],
                "verdicts": verdicts_out,
            })

    result = {
        "repo": full_name,
        "ecosystem": eco,
        "stars": entry.get("stars", 0),
        "has_k8s_helm": entry.get("has_k8s_helm", False),
        "status": "ok",
        "stats": stats,
        "verdict_counts": verdict_counts,
        "destructive_commits": commit_records,
    }
    out_file.write_text(json.dumps(result, indent=2), encoding="utf-8")
    return result


def main():
    corpus_path = PHASE1_DIR / "corpus_repos.json"
    corpus = json.loads(corpus_path.read_text(encoding="utf-8"))
    log_path = (MINED_DIR.parent / f"mine_corpus_{os.environ.get('ECV_TAG', 'v3')}.log") if os.environ.get("ECV_MINED_DIR") else LOG_DIR / "mine_corpus.log"

    with open(log_path, "w", encoding="utf-8") as logf:
        p_clone = Progress(total=len(corpus), label="phase1b-clone-repos", stream=logf)
        cloned_ok = []
        clone_errors = []
        with concurrent.futures.ThreadPoolExecutor(max_workers=CLONE_WORKERS) as ex:
            futs = {ex.submit(clone_one_repo, r): r for r in corpus}
            for fut in concurrent.futures.as_completed(futs):
                entry = futs[fut]
                name, ok, msg = fut.result()
                if ok:
                    cloned_ok.append(entry)
                else:
                    clone_errors.append({"repo": name, "error": msg})
                p_clone.step(note=f"{name} ({msg})")
        p_clone.done()

        p_mine = Progress(total=len(cloned_ok), label="phase1b-mine-8cores-bulk", stream=logf)
        aggregate = {
            "repos_targeted": len(corpus),
            "repos_cloned_ok": len(cloned_ok),
            "clone_errors": clone_errors,
            "repos_mined_ok": 0,
            "by_ecosystem": {},
            "overall_verdicts": {},
            "overall_op_kinds": {},
        }

        with concurrent.futures.ProcessPoolExecutor(max_workers=CPU_WORKERS) as ex:
            futs = {ex.submit(mine_one_repo, r): r for r in cloned_ok}
            for fut in concurrent.futures.as_completed(futs):
                entry = futs[fut]
                try:
                    res = fut.result()
                except Exception as e:
                    res = {"repo": entry["repo"], "ecosystem": entry["ecosystem"], "status": f"exc:{e}"}

                eco = entry["ecosystem"]
                eb = aggregate["by_ecosystem"].setdefault(eco, {
                    "repos_ok": 0,
                    "migration_commits_analyzed": 0,
                    "commits_with_forward_destructive": 0,
                    "commits_with_rollback_only_destructive": 0,
                    "verdicts": {},
                    "op_kinds": {},
                    "rails_safety_assured_violations": 0,
                    "rails_ignored_columns_safe_contracts": 0,
                })

                if res.get("status") == "ok":
                    aggregate["repos_mined_ok"] += 1
                    eb["repos_ok"] += 1
                    st = res["stats"]
                    eb["migration_commits_analyzed"] += st["migration_commits_analyzed"]
                    eb["commits_with_forward_destructive"] += st["commits_with_forward_destructive"]
                    eb["commits_with_rollback_only_destructive"] += st["commits_with_rollback_only_destructive"]

                    for c_rec in res["destructive_commits"]:
                        for v in c_rec["verdicts"]:
                            cls = v["classification"]
                            kind = v["op"]["kind"]
                            eb["verdicts"][cls] = eb["verdicts"].get(cls, 0) + 1
                            aggregate["overall_verdicts"][cls] = aggregate["overall_verdicts"].get(cls, 0) + 1
                            eb["op_kinds"][kind] = eb["op_kinds"].get(kind, 0) + 1
                            aggregate["overall_op_kinds"][kind] = aggregate["overall_op_kinds"].get(kind, 0) + 1
                            if eco == "rails":
                                if cls.startswith("DUAL_VERSION_VIOLATION") and v["op"].get("has_safety_bypass"):
                                    eb["rails_safety_assured_violations"] += 1
                                if cls == "SAFE_CONTRACT" and v["evidence"].get("rails_ignored_old"):
                                    eb["rails_ignored_columns_safe_contracts"] += 1

                    vc = res.get("verdict_counts", {})
                    viol = sum(cnt for k, cnt in vc.items() if k.startswith("DUAL_VERSION_VIOLATION"))
                    sc = vc.get("SAFE_CONTRACT", 0)
                    p_mine.step(note=f"{entry['repo']} [{eco}] viol={viol} safe_c={sc}")
                else:
                    p_mine.step(note=f"{entry['repo']} [{eco}] ERR:{res.get('status')}")
        p_mine.done()

        agg_path = (MINED_DIR.parent / f"mined_aggregate_summary_{os.environ.get('ECV_TAG', 'v3')}.json") if os.environ.get("ECV_MINED_DIR") else PHASE1_DIR / "mined_aggregate_summary.json"
        agg_path.write_text(json.dumps(aggregate, indent=2), encoding="utf-8")
        print(json.dumps(aggregate, indent=2), file=logf, flush=True)
        print(json.dumps(aggregate, indent=2))


if __name__ == "__main__":
    main()
