"""Free, keyless real-world data layer for HIVE: web search, news, page reading."""
import re, html, json, time, base64, threading, urllib.request, urllib.parse
from concurrent.futures import ThreadPoolExecutor, as_completed

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Safari/537.36"
_cache, _lock = {}, threading.Lock()


def get(url, timeout=15, accept="text/html", cache_ttl=300):
    """Fetch real web content, with a short five-minute cache to cut repeat latency."""
    with _lock:
        hit = _cache.get(url)
        if hit and time.time() - hit[0] < cache_ttl:
            return hit[1]
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept-Language": "en-CA,en;q=0.9", "Accept": accept})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        data = r.read(3_000_000).decode("utf8", "ignore")
    with _lock:
        _cache[url] = (time.time(), data)
    return data


def clean(s):
    return html.unescape(re.sub(r"<[^>]+>", "", s or "")).strip()


_brave_lock, _brave_last = threading.Lock(), [0.0]


def brave(q, n):
    with _brave_lock:  # Brave tolerates ~1 query per 1.5s per IP
        wait = 1.5 - (time.time() - _brave_last[0])
        if wait > 0:
            time.sleep(wait)
        _brave_last[0] = time.time()
    s = get("https://search.brave.com/search?q=" + urllib.parse.quote(q))
    out = []
    for blk in re.split(r'<div[^>]+data-type="web"', s)[1:]:
        m = re.search(r'<a[^>]+href="(https?://[^"]+)"', blk)
        t = re.search(r'class="[^"]*title[^"]*"[^>]*>(.*?)</div>', blk, re.S)
        d = re.search(r'class="[^"]*(?:snippet-description|content)[^"]*"[^>]*>(.*?)</div>', blk, re.S)
        if m and t:
            out.append({"title": clean(t.group(1)), "url": m.group(1), "snippet": clean(d.group(1) if d else "")[:300]})
        if len(out) >= n:
            break
    return out


def _bing_url(u):
    u = html.unescape(u)
    m = re.search(r"[?&]u=a1([^&]+)", u)
    if m:
        try:
            b = m.group(1); return base64.urlsafe_b64decode(b + "=" * (-len(b) % 4)).decode()
        except Exception:
            pass
    return u


def bing(q, n):
    s = get("https://www.bing.com/search?form=QBLH&setlang=en&q=" + urllib.parse.quote(q))
    out = []
    for blk in re.findall(r'<li class="b_algo"(.*?)</li>', s, re.S):
        m = re.search(r'<h2[^>]*>\s*<a[^>]+href="([^"]+)"[^>]*>(.*?)</a>', blk, re.S)
        if not m:
            continue
        p = re.search(r"<p[^>]*>(.*?)</p>", blk, re.S)
        out.append({"title": clean(m.group(2)), "url": _bing_url(m.group(1)), "snippet": clean(p.group(1) if p else "")[:300]})
        if len(out) >= n:
            break
    return out


def duckduckgo(q, n):
    req = urllib.request.Request("https://html.duckduckgo.com/html/", data=urllib.parse.urlencode({"q": q, "kl": "ca-en"}).encode(),
                                 headers={"User-Agent": UA, "Content-Type": "application/x-www-form-urlencoded"})
    with urllib.request.urlopen(req, timeout=15) as r:
        s = r.read().decode("utf8", "ignore")
    out = []
    for m in re.finditer(r'class="result__a"[^>]*href="([^"]+)"[^>]*>(.*?)</a>.*?class="result__snippet"[^>]*>(.*?)</a>', s, re.S):
        u = html.unescape(m.group(1))
        uu = re.search(r"uddg=([^&]+)", u)
        out.append({"title": clean(m.group(2)), "url": urllib.parse.unquote(uu.group(1)) if uu else u, "snippet": clean(m.group(3))[:300]})
        if len(out) >= n:
            break
    return out


def mwmbl(q, n):
    j = json.loads(get("https://api.mwmbl.org/api/v1/search/?s=" + urllib.parse.quote(q), accept="application/json"))
    txt = lambda parts: "".join(p["value"] for p in parts)
    return [{"title": txt(r["title"]), "url": r["url"], "snippet": txt(r.get("extract", []))[:300]} for r in j[:n]]


def _query_terms(q):
    stop = {"find", "every", "ways", "using", "with", "from", "that", "this", "what", "when", "where", "could", "would", "should", "about", "into", "make", "starting", "current", "latest", "realistic", "2026", "2025", "2024"}
    return list(dict.fromkeys(w for w in re.findall(r"[a-z0-9]+", q.lower()) if (len(w) >= 3 or w in ("ai", "fx")) and w not in stop))[:10]


def relevance_score(q, result):
    """Small transparent lexical query-overlap heuristic; it is not an AI fact check."""
    words = _query_terms(q)
    tokens = set(re.findall(r"[a-z0-9]+", (result.get("title", "") + " " + result.get("snippet", "")).lower()))
    matched = sum(1 for w in words if w in tokens)
    query = " ".join(words)
    text = (result.get("title", "") + " " + result.get("snippet", "")).lower()
    phrase_bonus = .5 if any(p in query and p in text for p in ("small business", "artificial intelligence", "ai automation", "foreign exchange")) else 0
    return min(1.0, (matched + phrase_bonus) / max(1, len(words)))


def _match_count(q, result):
    terms = _query_terms(q)
    tokens = set(re.findall(r"[a-z0-9]+", (result.get("title", "") + " " + result.get("snippet", "")).lower()))
    return sum(1 for w in terms if w in tokens)


def wikipedia(q, n=3):
    j = json.loads(get("https://en.wikipedia.org/w/api.php?action=query&list=search&format=json&srlimit=%d&srsearch=%s" % (n, urllib.parse.quote(q)), accept="application/json"))
    return [{"title": r["title"], "url": "https://en.wikipedia.org/wiki/" + urllib.parse.quote(r["title"].replace(" ", "_")),
             "snippet": clean(r["snippet"])} for r in j["query"]["search"]]


def bing_news(q, n=6):
    s = get("https://www.bing.com/news/search?format=rss&q=" + urllib.parse.quote(q), accept="application/rss+xml")
    out = []
    for it in re.findall(r"<item>(.*?)</item>", s, re.S)[:n]:
        g = lambda tag: clean((re.search(rf"<{tag}>(.*?)</{tag}>", it, re.S) or [None, ""])[1])
        link = html.unescape(g("link"))
        m = re.search(r"[?&]url=([^&]+)", link)
        out.append({"title": g("title"), "url": urllib.parse.unquote(m.group(1)) if m else link,
                    "snippet": (g("description")[:250] + " · Published " + g("pubDate")).strip(" ·")})
    return out


def news(q, n=6):
    try:
        r = bing_news(q, n)
        if r:
            for x in r: x["engine"] = "Bing News RSS"
            return r
    except Exception:
        pass
    r = google_news(q, n)
    for x in r: x["engine"] = "Google News RSS"
    return r


def google_news(q, n=6):
    s = get("https://news.google.com/rss/search?hl=en-CA&gl=CA&ceid=CA:en&q=" + urllib.parse.quote(q), accept="application/rss+xml")
    out = []
    for it in re.findall(r"<item>(.*?)</item>", s, re.S)[:n]:
        g = lambda tag: clean((re.search(rf"<{tag}>(.*?)</{tag}>", it, re.S) or [None, ""])[1])
        sm = re.search(r'<source[^>]*url="([^"]+)"[^>]*>(.*?)</source>', it, re.S)
        publisher_url = html.unescape(sm.group(1)) if sm else ""
        publisher = clean(sm.group(2)) if sm else "Google News"
        out.append({"title": g("title"), "url": g("link"), "publisher": publisher,
                    "publisher_url": publisher_url, "snippet": "Published " + g("pubDate")})
    return out


def hackernews(q, n=5):
    j = json.loads(get("https://hn.algolia.com/api/v1/search?hitsPerPage=%d&query=%s" % (n, urllib.parse.quote(q)), accept="application/json"))
    return [{"title": h.get("title") or "", "url": h.get("url") or f"https://news.ycombinator.com/item?id={h['objectID']}",
             "snippet": f"{h.get('points',0)} points, {h.get('num_comments',0)} comments, {h.get('created_at','')[:10]}"} for h in j["hits"] if h.get("title")]


TRUSTED = (".gc.ca", ".gov", ".edu", "ontario.ca", "canada.ca", "toronto.ca", "statcan", "bankofcanada", "wikipedia.org",
           "reuters.com", "cbc.ca", "bbc.", "theglobeandmail", "forbes.com", "bloomberg", "nerdwallet", "shopify.com",
           "upwork.com", "fiverr.com", "etsy.com", "investopedia", "hbr.org", "techcrunch", "statista", "mckinsey", "ycombinator")
WEAK = ("pinterest.", "quora.com", "facebook.com", "tiktok.com", "instagram.com", "scribd", "slideshare")


def credibility(url):
    u = url.lower()
    if any(t in u for t in TRUSTED):
        return 2
    if any(w in u for w in WEAK):
        return -1
    return 0


def search(q, n=6):
    """Free public web search with per-result query overlap and engine fallback."""
    best = ([], "none")
    for engine in (brave, duckduckgo, bing, mwmbl):
        try:
            raw = engine(q, n)
            scored = []
            for row in raw:
                row = dict(row)
                row["relevance"] = relevance_score(q, row)
                scored.append(row)
            qlen = len(_query_terms(q))
            needed = 1 if qlen <= 1 else (2 if qlen <= 4 else 3)
            good = [r for r in scored if _match_count(q, r) >= needed and r["relevance"] >= 0.30]
            good.sort(key=lambda x: (x["relevance"], credibility(x["url"])), reverse=True)
            if len(good) > len(best[0]):
                best = (good, engine.__name__)
            if len(good) >= 2:
                return good[:n], engine.__name__
        except Exception:
            pass
    if best[0]:
        return best[0][:n], best[1]
    # General encyclopedic fallback only for general-definition questions; never pad a market/current-data
    # search with vaguely related Wikipedia pages.
    if re.search(r"\b(what is|define|overview of|history of)\b", q, re.I):
        try:
            rows = wikipedia(q, n)
            for row in rows: row["relevance"] = relevance_score(q, row)
            rows.sort(key=lambda x: x["relevance"], reverse=True)
            required = 1 if len(_query_terms(q)) <= 1 else 2
            return [r for r in rows if _match_count(q, r) >= required and r["relevance"] >= 0.30], "wikipedia"
        except Exception:
            pass
    return [], "none"


def read(url, limit=6000):
    """Read a web page as clean text (Jina reader, falling back to direct fetch)."""
    try:
        t = get("https://r.jina.ai/" + url, timeout=25, accept="text/plain")
        if len(t) > 200:
            return t[:limit]
    except Exception:
        pass
    try:
        s = get(url, timeout=15)
        s = re.sub(r"(?is)<(script|style|nav|footer|header|svg)[^>]*>.*?</\1>", " ", s)
        return re.sub(r"\s+", " ", clean(s))[:limit]
    except Exception as e:
        return f"(could not read {url}: {e})"


def research(queries, read_top=2, with_news=True, on_event=None, owner_i=-1):
    """Search in parallel and open selected pages. Optional callback streams provenance events."""
    queries = [q.strip() for q in queries if q and q.strip()][:4]
    sources, blocks = [], []

    def emit(payload):
        if on_event:
            try:
                on_event({**payload, "owner_i": owner_i})
            except Exception:
                pass

    # Stream each query's start and completion, rather than waiting for the whole research batch.
    for q in queries:
        emit({"type": "search", "stage": "searching", "query": q})
    tasks = {}
    with ThreadPoolExecutor(max(1, min(8, len(queries) + (1 if with_news and queries else 0)))) as ex:
        for q in queries:
            tasks[ex.submit(search, q)] = ("web", q)
        if with_news and queries:
            tasks[ex.submit(news, queries[0], 4)] = ("news", queries[0])
        for fut in as_completed(tasks):
            kind, q = tasks[fut]
            try:
                result = fut.result()
                if kind == "web":
                    res, eng = result
                    emit({"type": "search", "stage": "complete", "query": q, "engine": eng, "count": len(res)})
                    for r in res[:5]:
                        row = {**r, "query": q, "engine": eng, "kind": "web"}
                        exrow = next((s for s in sources if s["url"] == row["url"]), None)
                        if exrow:
                            if q not in exrow["queries"]: exrow["queries"].append(q)
                        else:
                            sources.append(row)
                else:
                    for r in result or []:
                        row = {**r, "query": q, "engine": r.get("engine", "News RSS"), "kind": "news", "relevance": relevance_score(q, r)}
                        if row["url"] and not any(s["url"] == row["url"] for s in sources):
                            sources.append(row)
                    emit({"type": "search", "stage": "news_complete", "query": q,
                          "engine": "Bing / Google News RSS", "count": len(result or [])})
            except Exception as e:
                emit({"type": "search", "stage": "error", "query": q, "engine": kind, "error": str(e)[:140]})

    for i, s in enumerate(sources, 1):
        s["id"] = i
        s["queries"] = s.get("queries", [s.get("query", "")])
        s["snippet"] = (s.get("snippet") or "")[:600]
        s["text"] = s["title"] + " " + s["snippet"]
        s["trust"] = credibility(s["url"])
        s["page_read"] = False
        s["observed_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
        emit({"type": "source", "stage": "found", "source": {k: s.get(k) for k in
              ("id", "title", "url", "snippet", "trust", "query", "queries", "engine", "kind", "relevance", "publisher", "publisher_url", "observed_at", "page_read")}})
        blocks.append(f"[{i}] {s['title']} — {s['url']}\n    Search: {s.get('query','')} · Engine: {s.get('engine','web')}\n    {s['snippet']}")

    ranked = sorted([s for s in sources if s.get("kind") != "news"], key=lambda s: (s.get("relevance", 0), credibility(s["url"])), reverse=True)
    to_read = ranked[:max(0, int(read_top))]
    for s in to_read:
        emit({"type": "source", "stage": "reading", "source": {k: s.get(k) for k in
              ("id", "title", "url", "snippet", "trust", "query", "queries", "engine", "kind", "relevance", "publisher", "publisher_url", "observed_at", "page_read")}})
    with ThreadPoolExecutor(max(1, min(4, len(to_read)))) as ex:
        futures = {ex.submit(read, s["url"], 3500): s for s in to_read}
        for fut in as_completed(futures):
            s = futures[fut]
            try: txt = fut.result()
            except Exception as e: txt = f"(could not read {s['url']}: {e})"
            s["text"] += " " + txt
            s["page_read"] = bool(txt and not txt.startswith("(could not read"))
            s["read_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
            emit({"type": "source", "stage": "read" if s["page_read"] else "read_error",
                  "source": {**{k: s.get(k) for k in ("id", "title", "url", "snippet", "trust", "query", "queries", "engine", "kind", "observed_at", "page_read", "read_at")},
                             "excerpt": txt[:900]}})
            blocks.append(f"\n--- Full text of [{s['id']}] {s['url']} ---\n{txt}")
    return "\n".join(blocks), sources

if __name__ == "__main__":
    import sys
    t = time.time()
    ctx, src = research([" ".join(sys.argv[1:]) or "toronto mobile car detailing prices"])
    print(ctx[:2500]); print("\n", len(src), "sources in", round(time.time() - t, 1), "s")


# ----------------------------------------------------------------------------- official open data (no keys)
def fx_rates():
    """Latest Bank of Canada FX rates vs CAD."""
    try:
        j = json.loads(get("https://www.bankofcanada.ca/valet/observations/FXUSDCAD,FXEURCAD,FXGBPCAD,FXCNYCAD,FXINRCAD/json?recent=1", accept="application/json"))
        o = j["observations"][-1]
        pick = {k: v["v"] for k, v in o.items() if k.startswith("FX") and k[2:5] in ("USD", "EUR", "GBP", "CNY", "INR")}
        return f"Bank of Canada FX ({o['d']}): " + ", ".join(f"1 {k[2:5]} = {v} CAD" for k, v in pick.items())
    except Exception:
        return ""
