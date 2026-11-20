"""v3: Fetch full text of every RQ2 candidate issue (title, body, first comments) for manual verification."""
import json, subprocess
from pathlib import Path
V3 = Path(__file__).resolve().parent
ISSUES = [("fatkodima/online_migrations",84),("getsentry/sentry",98147),("David-Wobrock/django-migration-linter",294),
          ("ankane/strong_migrations",208),("ankane/strong_migrations",66),("ankane/strong_migrations",211),
          ("sbdchd/squawk",1370),("sbdchd/squawk",115),("sbdchd/squawk",149),("ankane/strong_migrations",95),
          ("netbox-community/netbox",17401),("Dispatcharr/Dispatcharr",987),("mastodon/mastodon",26246),("chatwoot/chatwoot",12288)]
out = []
for repo, n in ISSUES:
    p = subprocess.run(["gh","issue","view",str(n),"-R",repo,"--json","title,body,state,createdAt,url,comments"],capture_output=True,text=True,encoding="utf-8",errors="ignore",timeout=60)
    if p.returncode != 0:
        out.append({"repo":repo,"number":n,"error":p.stderr[:200]}); continue
    d = json.loads(p.stdout)
    out.append({"repo":repo,"number":n,"url":d["url"],"title":d["title"],"state":d["state"],"created":d["createdAt"],
                "body":(d.get("body") or "")[:4000],
                "comments":[(c.get("author",{}).get("login"),(c.get("body") or "")[:1200]) for c in d.get("comments",[])[:6]]})
(V3/"rq2_issue_texts.json").write_text(json.dumps(out,indent=1,ensure_ascii=False),encoding="utf-8")
for x in out:
    print("=====",x["repo"],x["number"],x.get("title"),x.get("created","")[:10],x.get("error",""))
    print((x.get("body") or "")[:900].replace("\r",""))
    for a,b in x.get("comments",[])[:2]: print("  >>",a,":",b[:300].replace("\n"," "))
