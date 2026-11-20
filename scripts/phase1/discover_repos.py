"""Phase 1A: Discover and validate 90-120 cloud-native production repositories
across Prisma (TypeScript), Rails ActiveRecord (Ruby), and Django (Python).

Uses GitHub REST API (via `gh` token) with controlled concurrency and full
progress reporting via `scripts/progress.py`.
"""
import concurrent.futures
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from progress import Progress

ROOT = Path(os.environ.get("ECV_TASK_DIR", ".")) / "research"
OUT_DIR = ROOT / "phase1"
LOG_DIR = ROOT / "logs"
OUT_DIR.mkdir(parents=True, exist_ok=True)
LOG_DIR.mkdir(parents=True, exist_ok=True)

TARGET_PER_ECO = 40  # Aim for 40 verified repos per ecosystem (120 total)
MIN_STARS = 300
MIN_MIGRATIONS = 15

# Curated seed + search queries per ecosystem so we get both flagship cloud-native
# apps and unbiased top-starred GitHub search results.
SEARCH_QUERIES = {
    "prisma": [
        "prisma migrations stars:>500 archived:false fork:false",
        "prisma postgresql docker stars:>300 archived:false fork:false",
        "topic:prisma stars:>500 archived:false fork:false",
        "topic:prisma language:TypeScript stars:>300 archived:false fork:false",
    ],
    "rails": [
        "topic:rails language:Ruby stars:>800 archived:false fork:false",
        "topic:ruby-on-rails language:Ruby stars:>500 archived:false fork:false",
        "rails docker postgresql language:Ruby stars:>400 archived:false fork:false",
    ],
    "django": [
        "topic:django language:Python stars:>1000 archived:false fork:false",
        "django docker postgresql language:Python stars:>500 archived:false fork:false",
        "django kubernetes OR helm language:Python stars:>300 archived:false fork:false",
    ],
}

# Known flagship cloud-native repos (to ensure major systems aren't missed by topic tags)
SEED_REPOS = {
    "prisma": [
        "calcom/cal.com", "formbricks/formbricks", "langfuse/langfuse",
        "umami-software/umami", "hoppscotch/hoppscotch", "dubinc/dub",
        "documenso/documenso", "twentyhq/twenty", "triggerdotdev/trigger.dev",
        "typebot-io/typebot.io", "heyform/heyform", "activepieces/activepieces",
        "plausible/analytics", "papermark/papermark", "mfts/papermark",
        "Rallly/rallly", "lukevella/rallly", "novuhq/novu",
        "AmruthPillai/Reactive-Resume", "lobehub/lobe-chat",
        "maybe-finance/maybe", "boxyhq/saas-starter-kit",
        "openstatusHQ/openstatus", "screenpipe/screenpipe",
        "twentyhq/twenty", "PostHog/posthog", "outline/outline",
        "usememos/memos", "docmost/docmost", "capsoftware/cap",
        "antiwork/gumroad", "midday-ai/midday", "unkeyed/unkey",
        "infisical/infisical", "highlight/highlight", "medusajs/medusa",
        "payloadcms/payload", "strapi/strapi", "n8n-io/n8n",
        "twentyhq/twenty", "kestra-io/kestra", "FlowiseAI/Flowise",
        "danswer-ai/danswer", "onyx-dot-app/onyx", "langgenius/dify",
        "logto-io/logto", "supertokens/supertokens-core",
        "hcengineering/platform", "plane/plane", "makeplane/plane",
        "twentyhq/twenty", "shelf-nu/shelf.nu", "teableio/teable",
        "actualbudget/actual", "immich-app/immich", "twentyhq/twenty",
        "twentyhq/twenty", "windmill-labs/windmill", "hasura/graphql-engine",
        "open-metadata/OpenMetadata", "saleor/saleor",
    ],
    "rails": [
        "mastodon/mastodon", "chatwoot/chatwoot", "discourse/discourse",
        "gitlabhq/gitlabhq", "forem/forem", "opf/openproject",
        "maybe-finance/maybe", "huginn/huginn", "redmine/redmine",
        "postalserver/postal", "diaspora/diaspora", "canvas-lms/canvas-lms",
        "instructure/canvas-lms", "decidim/decidim", "spree/spree",
        "solidusio/solidus", "lobsters/lobsters", "rubygems/rubygems.org",
        "zammad/zammad", "exercism/exercism", "theodinproject/theodinproject",
        "antiwork/gumroad", "usefathom/fathom", "feedbin/feedbin",
        "basecamp/once-campfire", "docusealco/docuseal", "codetriage/codetriage",
        "errbit/errbit", "consuldemocracy/consuldemocracy", "growstuff/growstuff",
        "rapidpro/rapidpro", "sharetribe/sharetribe", "alchemy_cms/alchemy_cms",
        "AlchemyCMS/alchemy_cms", "refinery/refinerycms", "fat_free_crm/fat_free_crm",
        "TracksApp/tracks", "publify/publify", "overseerr/overseerr",
        "danbooru/danbooru", "pixelfed/pixelfed", "inaturalist/inaturalist",
        "openstreetmap/openstreetmap-website", "rubygems/rubygems.org",
        "code-dot-org/code-dot-org", "manageiq/manageiq", "theforeman/foreman",
        "rapid7/metasploit-framework", "gobierto/gobierto", "helpyio/helpy",
        "alaveteli/alaveteli", "mysociety/alaveteli",
    ],
    "django": [
        "getsentry/sentry", "PostHog/posthog", "makeplane/plane",
        "saleor/saleor", "netbox-community/netbox", "paperless-ngx/paperless-ngx",
        "goauthentik/authentik", "glitchtip/glitchtip-backend", "weblateorg/weblate",
        "WeblateOrg/weblate", "zulip/zulip", "healthchecks/healthchecks",
        "wagtail/wagtail", "awx/awx", "ansible/awx", "DefectDojo/django-DefectDojo",
        "inventree/InTree", "inventree/InvenTree", "readthedocs/readthedocs.org",
        "mayan-edms/Mayan-EDMS", "shuup/shuup", "oscar/django-oscar",
        "django-oscar/django-oscar", "taigaio/taiga-back", "kobotoolbox/kpi",
        "modoboa/modoboa", "nautobot/nautobot", "tandoorrecipes/recipes",
        "vabene1111/recipes", "openwisp/openwisp-controller", "jazzband/django-silk",
        "labelstudio/label-studio", "HumanSignal/label-studio",
        "openedx/edx-platform", "pretix/pretix", "pretalx/pretalx",
        "hyperkitty/hyperkitty", "mailman/hyperkitty", "ralph/ralph",
        "allegro/ralph", "funkwhale/funkwhale", "cvat-ai/cvat",
        "baserow/baserow", "immich/immich", "peering-manager/peering-manager",
        "ralph/ralph", "djangopackages/djangopackages", "cookiecutter/cookiecutter-django",
        "mozilla/addons-server", "mozilla/kitsune", "archlinux/archweb",
        "fedora-infra/bodhi", "pulp/pulpcore",
    ],
}


def get_gh_token() -> str:
    res = subprocess.run(["gh", "auth", "token"], capture_output=True, text=True, check=True)
    return res.stdout.strip()


def make_session(token: str) -> requests.Session:
    s = requests.Session()
    s.headers.update({
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "expand-contract-research-miner",
    })
    return s


def gh_get(session: requests.Session, url: str, params=None, retries=3):
    for attempt in range(retries):
        resp = session.get(url, params=params, timeout=25)
        if resp.status_code == 200:
            return resp.json()
        if resp.status_code in (403, 429):
            reset = int(resp.headers.get("X-RateLimit-Reset", 0))
            wait = max(2, min(65, reset - int(time.time()) + 2)) if reset else 10 * (attempt + 1)
            time.sleep(wait)
            continue
        if resp.status_code in (404, 409, 422, 451):
            return None
        time.sleep(2 * (attempt + 1))
    return None


def classify_cloud_native(paths: list[str]) -> dict:
    has_dockerfile = any(
        re.search(r"(^|/)dockerfile(\.|$)", p, re.I) for p in paths
    )
    has_compose = any(
        re.search(r"(^|/)(docker-)?compose.*\.(ya?ml)$", p, re.I) for p in paths
    )
    has_k8s_helm = any(
        re.search(r"(^|/)(k8s|kubernetes|helm|charts|kustomize|manifests)/", p, re.I)
        or re.search(r"(^|/)Chart\.ya?ml$", p)
        for p in paths
    )
    has_paas_ci = any(
        re.search(r"(^|/)(fly\.toml|render\.yaml|railway\.json|Procfile)$", p)
        or p.startswith(".github/workflows/")
        for p in paths
    )
    return {
        "has_dockerfile": has_dockerfile,
        "has_compose": has_compose,
        "has_k8s_helm": has_k8s_helm,
        "has_paas_ci": has_paas_ci,
        "is_cloud_native": has_dockerfile or has_compose or has_k8s_helm,
    }


def inspect_repo(token: str, full_name: str, expected_eco: str) -> dict | None:
    session = make_session(token)
    meta = gh_get(session, f"https://api.github.com/repos/{full_name}")
    if not meta:
        return None
    if meta.get("fork") or meta.get("archived") or meta.get("disabled"):
        return None
    stars = int(meta.get("stargazers_count", 0))
    if stars < MIN_STARS:
        return None

    canonical = meta["full_name"]
    branch = meta.get("default_branch", "main")
    tree_data = gh_get(
        session,
        f"https://api.github.com/repos/{canonical}/git/trees/{branch}",
        params={"recursive": "1"},
    )
    if not tree_data or "tree" not in tree_data:
        return None

    paths = [item["path"] for item in tree_data["tree"] if item.get("type") == "blob"]
    truncated = bool(tree_data.get("truncated", False))

    cn = classify_cloud_native(paths)
    if not cn["is_cloud_native"]:
        return None

    # Ecosystem-specific migration detection
    if expected_eco == "prisma":
        mig_files = [p for p in paths if re.search(r"migrations/[^/]+/migration\.sql$", p)]
        schema_files = [p for p in paths if p.endswith("schema.prisma") or re.search(r"prisma/.*\.prisma$", p)]
        if len(mig_files) < MIN_MIGRATIONS or not schema_files:
            return None
        mig_roots = sorted({re.sub(r"/[^/]+/migration\.sql$", "", p) for p in mig_files})
        primary_root = max(mig_roots, key=lambda r: sum(1 for p in mig_files if p.startswith(r + "/")))
    elif expected_eco == "rails":
        mig_files = [p for p in paths if re.search(r"(^|/)db/migrate/[^/]+\.rb$", p)]
        schema_files = [p for p in paths if re.search(r"(^|/)db/(schema\.rb|structure\.sql)$", p)]
        if len(mig_files) < MIN_MIGRATIONS:
            return None
        mig_roots = sorted({re.sub(r"/[^/]+\.rb$", "", p) for p in mig_files})
        primary_root = max(mig_roots, key=lambda r: sum(1 for p in mig_files if p.startswith(r + "/")))
    elif expected_eco == "django":
        mig_files = [
            p for p in paths
            if re.search(r"(^|/)migrations/\d{4}_[^/]+\.py$", p)
            and not re.search(r"(^|/)(tests?|fixtures?|examples?|docs?)/", p, re.I)
        ]
        schema_files = [
            p for p in paths
            if re.search(r"(^|/)models(\.py|/[^/]+\.py)$", p)
            and not re.search(r"(^|/)(tests?|migrations)/", p, re.I)
        ]
        if len(mig_files) < MIN_MIGRATIONS or not schema_files:
            return None
        mig_roots = sorted({re.sub(r"/\d{4}_[^/]+\.py$", "", p) for p in mig_files})
        primary_root = mig_roots[0]
    else:
        return None

    return {
        "repo": canonical,
        "ecosystem": expected_eco,
        "stars": stars,
        "default_branch": branch,
        "language": meta.get("language"),
        "pushed_at": meta.get("pushed_at"),
        "size_kb": meta.get("size"),
        "license": (meta.get("license") or {}).get("spdx_id"),
        "tree_truncated": truncated,
        "migration_count": len(mig_files),
        "primary_migration_root": primary_root,
        "migration_roots_count": len(mig_roots),
        "schema_files_sample": schema_files[:5],
        **cn,
    }


def main():
    log_path = LOG_DIR / "discover_repos.log"
    token = get_gh_token()
    session = make_session(token)

    with open(log_path, "w", encoding="utf-8") as logf:
        # Step 1: Collect candidate repo names per ecosystem
        candidates: dict[str, list[str]] = {eco: [] for eco in ("prisma", "rails", "django")}
        seen_global: set[str] = set()

        total_queries = sum(len(v) for v in SEARCH_QUERIES.values())
        pq = Progress(total=total_queries, label="phase1a-search-candidates", stream=logf)
        for eco, queries in SEARCH_QUERIES.items():
            # Add curated seeds first
            for s in SEED_REPOS[eco]:
                key = s.lower()
                if key not in seen_global:
                    seen_global.add(key)
                    candidates[eco].append(s)
            for q in queries:
                data = gh_get(
                    session,
                    "https://api.github.com/search/repositories",
                    params={"q": q, "sort": "stars", "order": "desc", "per_page": 100},
                )
                if data and "items" in data:
                    for it in data["items"]:
                        fn = it["full_name"]
                        if fn.lower() not in seen_global:
                            seen_global.add(fn.lower())
                            candidates[eco].append(fn)
                pq.step(note=f"{eco}: {q[:38]} -> {len(candidates[eco])} cands")
                time.sleep(2.2)  # Stay safely under 30 search req/min
        pq.done()

        # Cap candidates per ecosystem to inspect (top 140 per eco)
        tasks = []
        for eco in ("prisma", "rails", "django"):
            for fn in candidates[eco][:140]:
                tasks.append((eco, fn))

        p = Progress(total=len(tasks), label="phase1a-inspect-trees", stream=logf)
        verified: dict[str, list[dict]] = {eco: [] for eco in ("prisma", "rails", "django")}

        # Controlled concurrency (6 workers) against GitHub Core REST API (5000/hr)
        with concurrent.futures.ThreadPoolExecutor(max_workers=6) as ex:
            future_map = {
                ex.submit(inspect_repo, token, fn, eco): (eco, fn)
                for eco, fn in tasks
            }
            for fut in concurrent.futures.as_completed(future_map):
                eco, fn = future_map[fut]
                try:
                    res = fut.result()
                except Exception as e:
                    res = None
                if res:
                    # Avoid duplicate canonical names (e.g. redirects)
                    if not any(x["repo"].lower() == res["repo"].lower() for x in verified[eco]):
                        verified[eco].append(res)
                p.step(
                    note=f"{eco}:{fn} -> counts P={len(verified['prisma'])} R={len(verified['rails'])} D={len(verified['django'])}"
                )
        p.done()

        # Sort each ecosystem by stars descending, take top TARGET_PER_ECO
        selected = []
        summary = {}
        for eco in ("prisma", "rails", "django"):
            verified[eco].sort(key=lambda r: (r["has_k8s_helm"], r["stars"]), reverse=True)
            # Actually sort purely by stars descending (unbiased pre-registered criterion!)
            verified[eco].sort(key=lambda r: r["stars"], reverse=True)
            chosen = verified[eco][:TARGET_PER_ECO]
            selected.extend(chosen)
            summary[eco] = {
                "total_verified": len(verified[eco]),
                "selected": len(chosen),
                "min_stars": min((r["stars"] for r in chosen), default=0),
                "max_stars": max((r["stars"] for r in chosen), default=0),
                "k8s_helm_count": sum(1 for r in chosen if r["has_k8s_helm"]),
                "total_migrations": sum(r["migration_count"] for r in chosen),
            }

        out_all = OUT_DIR / "verified_repos_all.json"
        out_sel = OUT_DIR / "corpus_repos.json"
        out_sum = OUT_DIR / "corpus_summary.json"
        out_all.write_text(json.dumps(verified, indent=2), encoding="utf-8")
        out_sel.write_text(json.dumps(selected, indent=2), encoding="utf-8")
        out_sum.write_text(json.dumps(summary, indent=2), encoding="utf-8")

        print(json.dumps(summary, indent=2), file=logf, flush=True)
        print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
