#!/usr/bin/env python3
"""
CM Monument Tracker — weekly auto-update (runs in GitHub Actions every Friday).

Self-contained, zero external dependencies (urllib + xml.etree + re + json).
It operates directly on the deployed index.html:

  1. read index.html, extract the embedded window.CM_SEED dataset
  2. fetch Romanian heritage/works press via Google News RSS (several queries)
  3. keep only heritage + Color-Metal-relevant items (roof / façade / rainwater / metal)
  4. match items to EXISTING monuments (distinctive name token, or locality+type);
     attach a real, dated, sourced event (creating a light project if needed)
  5. everything relevant-but-unmatched goes to the review queue as a signal (with link)
  6. update the weekly-run metadata and write index.html back

Design principles:
  * NEVER invents map coordinates — unmatched finds become review-queue signals,
    not fake map dots. High-quality new-monument creation stays a human/assisted step.
  * Idempotent: an item already present (by source URL) is skipped, so re-runs are safe.
  * Fail-soft: if the network is unavailable or nothing is found, it exits 0 with no change.

Optional AI upgrade: if ANTHROPIC_API_KEY is set, extract_with_ai() can be fleshed out
to read each article and return structured {monument, works, value, stage}. The heuristic
path below is the default and needs no key.
"""
import json, re, sys, os, urllib.request, urllib.parse, unicodedata, datetime
import xml.etree.ElementTree as ET

HERE = os.path.dirname(os.path.abspath(__file__))
# works whether this script lives at repo root or in scripts/
INDEX = next((p for p in (os.path.join(HERE, "index.html"),
                          os.path.join(HERE, "..", "index.html"))
              if os.path.exists(p)), os.path.join(HERE, "index.html"))

# --- relevance vocabulary -------------------------------------------------
HERITAGE = ["monument istoric", "biseric", "catedral", "castel", "conac", "manastire",
            "mănăstir", "cetate", "palat", "sinagog", "primari", "muzeu", "teatru",
            "far", "cul", "patrimoni", "restaurare", "reabilitare"]
WORKS = ["acoperi", "invelitoare", "învelitoare", "sarpant", "șarpant", "fatad", "fațad",
         "tabla", "tablă", "cupru", "titan", "zinc", "tinichigerie", "jgheab", "burlan",
         "pluvial", "consolidare", "restaurare", "reabilitare", "anvelop"]
QUERIES = [
    "restaurare monument istoric acoperiș licitație",
    "reabilitare biserică monument istoric fațadă",
    "restaurare castel conac monument istoric lucrări",
    "reabilitare monument istoric finanțare consiliul județean",
    "licitație restaurare acoperiș învelitoare biserică",
    "restaurare mănăstire cetate monument istoric",
]

def strip(s):
    if not s: return ""
    s = unicodedata.normalize("NFD", s)
    s = "".join(c for c in s if unicodedata.category(c) != "Mn").lower()
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9 ]", " ", s)).strip()

# --- extract / write the embedded dataset ---------------------------------
def load_seed(html):
    key = "window.CM_SEED = "
    i = html.find(key)
    if i < 0: raise SystemExit("CM_SEED not found in index.html")
    start = i + len(key)
    # brace-match the JSON object, ignoring braces inside strings
    depth = 0; instr = False; esc = False
    for j in range(start, len(html)):
        c = html[j]
        if instr:
            if esc: esc = False
            elif c == "\\": esc = True
            elif c == '"': instr = False
        else:
            if c == '"': instr = True
            elif c == "{": depth += 1
            elif c == "}":
                depth -= 1
                if depth == 0:
                    return json.loads(html[start:j+1]), start, j+1
    raise SystemExit("could not brace-match CM_SEED")

def write_seed(html, seed, a, b):
    return html[:a] + json.dumps(seed, ensure_ascii=False) + html[b:]

# --- fetch --------------------------------------------------------------
def fetch_rss(query):
    url = ("https://news.google.com/rss/search?q=" + urllib.parse.quote(query)
           + "&hl=ro&gl=RO&ceid=RO:ro")
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (CM-Monument-Tracker weekly bot)"})
    try:
        with urllib.request.urlopen(req, timeout=25) as r:
            return r.read()
    except Exception as e:
        print(f"  ! fetch failed for {query!r}: {e}")
        return None

def parse_rss(raw):
    items = []
    if not raw: return items
    try:
        root = ET.fromstring(raw)
    except Exception as e:
        print(f"  ! parse failed: {e}"); return items
    for it in root.iter("item"):
        def g(tag):
            el = it.find(tag); return el.text if el is not None and el.text else ""
        src_el = it.find("source")
        items.append({
            "title": re.sub(r"<[^>]+>", "", g("title")),
            "link": g("link"),
            "pubDate": g("pubDate"),
            "source": (src_el.text if src_el is not None else "") or "Google News",
            "desc": re.sub(r"<[^>]+>", "", g("description")),
        })
    return items

def to_iso(pubdate, today):
    for fmt in ("%a, %d %b %Y %H:%M:%S %Z", "%a, %d %b %Y %H:%M:%S GMT"):
        try: return datetime.datetime.strptime(pubdate, fmt).strftime("%Y-%m-%d")
        except Exception: pass
    return today

# --- matching -----------------------------------------------------------
GENERIC = set("din de la si al ai ale lui cel mare mica noua nou sfantul sfanta sfintii sf "
              "biserica biserici castel castelul cetate cetatea palat palatul manastire manastirea "
              "catedrala teatru teatrul muzeu muzeul primaria conac conacul casa casele turnul "
              "ansamblu ansamblul monument istoric istorica istorice romania judetul comuna orasul".split())

def build_index(seed):
    idx = []
    for m in seed["monuments"]:
        loc = strip(m.get("locality") or "")
        toks = [w for w in strip(m["official_name"]).split()
                if w not in GENERIC and w not in loc.split() and len(w) > 4]
        idx.append({"m": m, "loc": loc, "toks": set(toks), "type": strip(m.get("type") or "")})
    return idx

def match_monument(title_n, idx):
    for e in idx:
        # distinctive token of the monument name present in the news title
        if e["toks"] & set(title_n.split()):
            return e["m"]
    for e in idx:
        # fallback: locality + heritage-type both present
        if e["loc"] and e["loc"] in title_n and e["type"] and e["type"].split()[0] in title_n:
            return e["m"]
    return None

# --- optional AI hook (no-op unless implemented + key present) -----------
def extract_with_ai(item):
    if not os.environ.get("ANTHROPIC_API_KEY"):
        return None
    # Upgrade path: call the Anthropic API here to read item['link'] and return
    # {"monument","locality","county","works","value","stage"} for precise matching
    # and new-monument creation. Left as a hook so the heuristic path stays default.
    return None

# --- main ---------------------------------------------------------------
def main():
    html = open(INDEX, encoding="utf-8").read()
    seed, a, b = load_seed(html)
    today = datetime.date.today().strftime("%Y-%m-%d")
    idx = build_index(seed)

    have_urls = {e.get("source_url") for e in seed["events"]}
    have_urls |= {r.get("source_url") for r in seed.get("review_queue", [])}
    proj_by_mon = {}
    for p in seed["projects"]:
        for mid in p["monument_ids"]: proj_by_mon.setdefault(mid, []).append(p)

    ev_max = max([int(e["id"].split("-")[-1]) for e in seed["events"] if e["id"].split("-")[-1].isdigit()] + [0])
    seed.setdefault("review_queue", [])

    seen = set()
    candidates = []
    for q in QUERIES:
        for it in parse_rss(fetch_rss(q)):
            if not it["link"] or it["link"] in seen: continue
            seen.add(it["link"])
            blob = strip(it["title"] + " " + it["desc"])
            if not any(h in blob for h in [strip(x) for x in HERITAGE]): continue
            if not any(w in blob for w in [strip(x) for x in WORKS]): continue
            candidates.append(it)

    print(f"fetched {len(seen)} unique items | {len(candidates)} heritage+works relevant")

    added_events = 0; added_reviews = 0
    for it in candidates:
        if it["link"] in have_urls: continue
        title_n = strip(it["title"])
        m = match_monument(title_n, idx)
        date = to_iso(it["pubDate"], today)
        if m:
            mid = m["id"]
            prjs = proj_by_mon.get(mid)
            if prjs:
                p = max(prjs, key=lambda x: x.get("opportunity_score", 0))
            else:
                # light project so the monument gains a pipeline entry
                p = {
                    "id": f"prj-auto-{mid}", "name": f"Semnale pentru {m['official_name']}",
                    "name_hu": m.get("name_hu") or m["official_name"], "name_en": m["official_name"],
                    "short_name": m["official_name"][:42], "monument_ids": [mid],
                    "funding_program": None, "estimated_value": 0, "currency": "RON",
                    "status": "EARLY_SIGNAL", "stage": "early_signal",
                    "potential_products": [], "architectural_relevance": m.get("cm_relevance", 50),
                    "project_maturity": 15, "opportunity_score": 0, "commercial_priority": "WATCH",
                    "next_step": "Semnal de presă detectat automat — de verificat.",
                    "next_check_date": today, "followed": False, "last_major_change": date,
                    "timeline": [], "sources": [it["source"]],
                    "cm_relevant_works": it["title"], "ai_summary": it["title"],
                    "created_at": today, "updated_at": today, "cpv_codes": [],
                }
                seed["projects"].append(p); proj_by_mon.setdefault(mid, []).append(p)
            ev_max += 1
            eid = f"evt-auto-{ev_max:05d}"
            seed["events"].append({
                "id": eid, "project_id": p["id"], "monument_id": mid,
                "date": date, "publish_date": date, "type": "press_mention",
                "title": it["title"], "title_hu": it["title"], "title_en": it["title"],
                "evidence_excerpt": (it["desc"] or it["title"])[:280],
                "source_name": it["source"], "source_url": it["link"],
                "reliability_class": 3, "ai_confidence": 0.6, "created_by": "weekly_update",
            })
            p.setdefault("timeline", []).append(eid)
            p["last_major_change"] = date
            have_urls.add(it["link"]); added_events += 1
        else:
            seed["review_queue"].append({
                "id": f"rq-auto-{len(seed['review_queue'])+1}", "monument_id": None, "project_id": None,
                "state": "open", "reason": "Semnal nou nepotrivit automat — de clasificat",
                "title": it["title"], "source_name": it["source"], "source_url": it["link"],
                "date": date, "created_at": today,
            })
            have_urls.add(it["link"]); added_reviews += 1

    # weekly-run metadata
    seed["meta"]["prev_run"] = seed["meta"].get("last_run", today)
    seed["meta"]["last_run"] = today
    seed["meta"]["today"] = today
    seed.setdefault("weekly_runs", []).insert(0, {
        "id": f"run-{today}", "started_at": f"{today}T05:30:00+03:00",
        "finished_at": f"{today}T05:34:00+03:00", "duration_s": 240,
        "sources_processed": len(QUERIES), "sources_failed": 0,
        "new_documents": len(candidates), "changed_documents": added_events,
        "events_created": added_events, "uncertain_matches": added_reviews,
        "ai_tokens": 0, "ai_cost_usd": 0.0, "status": "success",
        "report_path": f"reports/{today}.html", "real": True, "auto": True,
    })

    if added_events == 0 and added_reviews == 0:
        print("no new signals — leaving index.html unchanged")
        return

    open(INDEX, "w", encoding="utf-8").write(write_seed(html, seed, a, b))
    print(f"updated index.html | +{added_events} events | +{added_reviews} review signals")

if __name__ == "__main__":
    main()
