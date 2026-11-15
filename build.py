"""
Adună știrile din sursele definite în sources.json și scrie data.json.
Opțional (dacă există secretul ANTHROPIC_API_KEY), generează o dată pe zi
un rezumat în română în briefing.json.

Rulează automat din GitHub Actions (.github/workflows/update.yml).
"""
import datetime as dt
import hashlib
import html
import json
import os
import re
import sys
import time
from urllib.parse import quote_plus
from zoneinfo import ZoneInfo

import feedparser
import requests

ROOT = os.path.dirname(os.path.abspath(__file__))
TZ = ZoneInfo("Europe/Bucharest")
UA = "Mozilla/5.0 (compatible; PsyPeopleDashboard/1.0; +https://github.com)"
KEEP_DAYS = 7            # cât de vechi pot fi știrile păstrate
PUBMED_DAYS = 14         # studiile apar mai rar, deci fereastră mai mare
MAX_PER_SOURCE = 40
NOW = dt.datetime.now(dt.timezone.utc)


def load_json(name, default=None):
    path = os.path.join(ROOT, name)
    if not os.path.exists(path):
        return default
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def save_json(name, data):
    with open(os.path.join(ROOT, name), "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)


def clean(text, limit=280):
    if not text:
        return ""
    text = html.unescape(text)
    text = re.sub(r"<[^>]+>", " ", text)
    text = html.unescape(text)
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) > limit:
        text = text[:limit].rsplit(" ", 1)[0] + "…"
    return text


def to_iso(struct):
    if not struct:
        return None
    try:
        d = dt.datetime(*struct[:6], tzinfo=dt.timezone.utc)
    except Exception:
        return None
    if d > NOW:
        d = NOW
    return d.isoformat()


def item_id(link, title):
    return hashlib.md5((link or title or "").encode("utf-8")).hexdigest()[:12]


def gnews_url(query, lang):
    q = quote_plus(f"{query} when:{KEEP_DAYS}d")
    if lang == "ro":
        return f"https://news.google.com/rss/search?q={q}&hl=ro&gl=RO&ceid=RO:ro"
    return f"https://news.google.com/rss/search?q={q}&hl=en-US&gl=US&ceid=US:en"


def fetch_feed(src):
    url = src["url"] if src["type"] == "rss" else gnews_url(src["query"], src.get("lang", "en"))
    r = requests.get(url, headers={"User-Agent": UA}, timeout=30)
    r.raise_for_status()
    feed = feedparser.parse(r.content)
    if feed.bozo and not feed.entries:
        raise ValueError(f"feed invalid: {feed.bozo_exception}")
    items = []
    for e in feed.entries[: MAX_PER_SOURCE * 2]:
        title = clean(e.get("title"), 300)
        link = e.get("link")
        if not title or not link:
            continue
        publisher = None
        if src["type"] == "gnews":
            # Google News pune „ - Publicație” la finalul titlului
            publisher = (e.get("source") or {}).get("title")
            if publisher and title.endswith(" - " + publisher):
                title = title[: -len(publisher) - 3]
        summary = "" if src["type"] == "gnews" else clean(e.get("summary") or e.get("description"))
        items.append({
            "title": title,
            "link": link,
            "date": to_iso(e.get("published_parsed") or e.get("updated_parsed")),
            "summary": summary,
            "author": clean(e.get("author"), 80) or None,
            "publisher": publisher,
        })
    return items


def fetch_pubmed(src):
    base = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/"
    params = {"db": "pubmed", "term": src["query"], "reldate": PUBMED_DAYS, "datetype": "edat",
              "retmax": MAX_PER_SOURCE, "sort": "date", "retmode": "json", "tool": "psy-dashboard"}
    r = requests.get(base + "esearch.fcgi", params=params, headers={"User-Agent": UA}, timeout=30)
    r.raise_for_status()
    ids = r.json()["esearchresult"].get("idlist", [])
    time.sleep(0.4)  # limită NCBI: max 3 cereri/secundă fără cheie
    if not ids:
        return []
    r = requests.get(base + "esummary.fcgi",
                     params={"db": "pubmed", "id": ",".join(ids), "retmode": "json", "tool": "psy-dashboard"},
                     headers={"User-Agent": UA}, timeout=30)
    r.raise_for_status()
    res = r.json().get("result", {})
    time.sleep(0.4)
    items = []
    for pid in ids:
        doc = res.get(pid)
        if not doc:
            continue
        added = None
        for h in doc.get("history", []):
            if h.get("pubstatus") in ("entrez", "pubmed") and h.get("date"):
                try:
                    added = dt.datetime.strptime(h["date"][:16], "%Y/%m/%d %H:%M").replace(tzinfo=dt.timezone.utc)
                except ValueError:
                    pass
                break
        authors = [a.get("name") for a in doc.get("authors", []) if a.get("name")]
        author = (authors[0] + (" et al." if len(authors) > 1 else "")) if authors else None
        pubtypes = [p for p in doc.get("pubtype", []) if p != "Journal Article"]
        items.append({
            "title": clean(doc.get("title"), 300),
            "link": f"https://pubmed.ncbi.nlm.nih.gov/{pid}/",
            "date": (min(added, NOW) if added else NOW).isoformat(),
            "summary": " · ".join(x for x in [doc.get("fulljournalname") or doc.get("source"),
                                               ", ".join(pubtypes[:2])] if x),
            "author": author,
            "publisher": doc.get("source"),
        })
    return items


def build_data():
    cfg = load_json("sources.json")
    cutoff = NOW - dt.timedelta(days=KEEP_DAYS)
    all_items, sources_out, seen = [], [], set()

    for i, src in enumerate(cfg["sources"]):
        sid = f"s{i}"
        status = {"id": sid, "name": src["name"], "category": src["category"],
                  "home": src.get("home"), "type": src["type"], "ok": True, "error": None, "count": 0}
        try:
            raw = fetch_pubmed(src) if src["type"] == "pubmed" else fetch_feed(src)
            raw.sort(key=lambda x: x["date"] or "", reverse=True)
            kept = 0
            for it in raw:
                if src["type"] != "pubmed" and it["date"] and it["date"] < cutoff.isoformat():
                    continue
                tkey = re.sub(r"\W+", "", it["title"].lower())[:80]
                if tkey in seen or it["link"] in seen:
                    continue
                seen.update((tkey, it["link"]))
                it.update({"id": item_id(it["link"], it["title"]), "source": sid,
                           "category": src["category"], "date": it["date"] or NOW.isoformat()})
                all_items.append(it)
                kept += 1
                if kept >= MAX_PER_SOURCE:
                    break
            status["count"] = kept
            print(f"OK   {src['name']}: {kept}")
        except Exception as ex:  # o sursă căzută nu oprește restul
            status.update(ok=False, error=str(ex)[:200])
            print(f"FAIL {src['name']}: {ex}", file=sys.stderr)
        sources_out.append(status)

    all_items.sort(key=lambda x: x["date"], reverse=True)
    data = {"generated_at": NOW.isoformat(), "categories": cfg["categories"],
            "sources": sources_out, "items": all_items}
    save_json("data.json", data)
    return data


BRIEFING_PROMPT = """Ești editorul unui newsletter zilnic pentru o Head of Commercial Support dintr-o companie de leasing operațional din România, care coordonează ~15 oameni și studiază psihologie. O interesează: stresul organizațional și burnout-ul, motivația, echitatea percepută, wellbeing-ul angajaților, leadership-ul, cultura organizațională, People & Culture și cercetarea nouă din psihologie. Îi pasă de gândire critică: diferența dintre ce pare adevărat și ce e susținut de dovezi.

Mai jos sunt știrile din ultimele ore, numerotate. Alege cele mai relevante {n} subiecte (poți grupa mai multe știri despre același subiect). Evită subiectele pur clinice sau medicale fără legătură cu munca sau comportamentul, cu excepția celor foarte importante.

Pentru fiecare subiect scrie, în română corectă, cu diacritice, direct, fără limbaj corporatist:
- "tag": una dintre: CERCETARE, WELLBEING, PEOPLE & CULTURE, LEADERSHIP, ROMÂNIA, SĂNĂTATE
- "title": titlu scurt în română (max. 12 cuvinte)
- "summary": 2–3 propoziții: ce s-a întâmplat sau ce s-a găsit
- "so_what": o propoziție: de ce contează pentru un manager de echipă sau ce ar putea testa practic
- "evidence": cât de solidă e dovada, în maxim 10 cuvinte (de ex. „meta-analiză, solid”, „un singur studiu transversal, corelațional”, „sondaj al unei firme de consultanță”, „opinie / editorial”, „știre, fără date”)
- "refs": lista numerelor știrilor folosite (ex. [3, 17])

Răspunde DOAR cu JSON valid, în forma: {{"items": [ ... ]}}

ȘTIRI:
{news}"""


def build_briefing(data):
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        print("Fără ANTHROPIC_API_KEY: sar peste rezumatul AI.")
        return
    today = dt.datetime.now(TZ).date().isoformat()
    old = load_json("briefing.json", {})
    if old.get("date") == today and not os.environ.get("FORCE_BRIEFING"):
        print("Rezumatul de azi există deja.")
        return

    src_names = {s["id"]: s["name"] for s in data["sources"]}
    pool = []
    for hours in (36, 72, 24 * 7):
        lim = (NOW - dt.timedelta(hours=hours)).isoformat()
        pool = [it for it in data["items"] if it["date"] >= lim]
        if len(pool) >= 40:
            break
    # echilibrăm categoriile: max. 35 de știri din fiecare
    per_cat, picked = {}, []
    for it in pool:
        c = it["category"]
        if per_cat.get(c, 0) < 35:
            per_cat[c] = per_cat.get(c, 0) + 1
            picked.append(it)
    if not picked:
        return
    lines = []
    for n, it in enumerate(picked, 1):
        line = f"[{n}] ({src_names.get(it['source'], '')}) {it['title']}"
        if it.get("summary"):
            line += f" — {it['summary'][:200]}"
        lines.append(line)

    prompt = BRIEFING_PROMPT.format(n=6, news="\n".join(lines))
    r = requests.post("https://api.anthropic.com/v1/messages", timeout=180, headers={
        "x-api-key": key, "anthropic-version": "2023-06-01", "content-type": "application/json"},
        json={"model": os.environ.get("BRIEFING_MODEL") or "claude-sonnet-5-5",
              "max_tokens": 4000, "messages": [{"role": "user", "content": prompt}]})
    if r.status_code != 200:
        print(f"Eroare API Anthropic {r.status_code}: {r.text[:300]}", file=sys.stderr)
        return
    text = "".join(b.get("text", "") for b in r.json().get("content", []))
    try:
        parsed = json.loads(text[text.index("{"): text.rindex("}") + 1])
    except Exception as ex:
        print(f"Nu am putut citi JSON-ul rezumatului: {ex}", file=sys.stderr)
        return
    out = []
    for b in parsed.get("items", []):
        links = []
        for ref in b.get("refs", []):
            if isinstance(ref, int) and 1 <= ref <= len(picked):
                it = picked[ref - 1]
                links.append({"title": it["title"], "link": it["link"],
                              "source": src_names.get(it["source"], "")})
        out.append({k: b.get(k, "") for k in ("tag", "title", "summary", "so_what", "evidence")} | {"links": links[:4]})
    save_json("briefing.json", {"date": today, "generated_at": NOW.isoformat(), "items": out})
    print(f"Rezumat generat: {len(out)} subiecte.")


if __name__ == "__main__":
    d = build_data()
    print(f"Total: {len(d['items'])} știri din {sum(s['ok'] for s in d['sources'])}/{len(d['sources'])} surse.")
    build_briefing(d)
