"""Union Legolas's committed article caches (data/learnings.json history) into one replay corpus.
Scratch tool - not committed. Output: corpus.json (articles) + posts.json (old-design posts)."""
import json, subprocess, sys
repo, since, out = sys.argv[1], sys.argv[2], sys.argv[3]
shas = subprocess.check_output(["git","-C",repo,"log","--since",since,"--reverse","--format=%H %cI","--","data/learnings.json"],text=True).split("\n")
arts, posts, runs = {}, {}, []
for line in filter(None, shas):
    sha, ts = line.split()
    try:
        d = json.loads(subprocess.check_output(["git","-C",repo,"show",f"{sha}:data/learnings.json"]))
    except Exception as e:
        continue
    runs.append(ts)
    for a in d.get("article_cache", []):
        if a["guid"] not in arts:
            arts[a["guid"]] = {**a, "first_cached_at": ts}
    for p in d.get("recently_posted", []):
        posts[(p["headline"], p["posted_at"])] = p
json.dump({"articles": sorted(arts.values(), key=lambda a: a["published_dt"]),
           "posts": sorted(posts.values(), key=lambda p: p["posted_at"]),
           "runs": runs}, open(out, "w"))
print(len(arts), "articles", len(posts), "posts", len(runs), "runs")
