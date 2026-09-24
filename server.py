#!/usr/bin/env python3
"""HIVE v3 — free, keyless multi-agent AI swarm with live web research,
recursive sub-swarms, fact-checking, a calculator tool, memory and a Commander chat.

Run:   python3 server.py        then open  http://localhost:8080
Needs: Python 3.9+ only. No pip installs. No API keys.
"""
import ast, json, math, operator, os, re, sys, time, uuid, random, threading, urllib.request, urllib.error
from concurrent.futures import ThreadPoolExecutor
from http.server import ThreadingHTTPServer, SimpleHTTPRequestHandler
import websearch

PORT = int(os.environ.get("PORT", sys.argv[1] if len(sys.argv) > 1 else 8080))
ROOT = os.path.dirname(os.path.abspath(__file__))
MEM_DIR = os.path.join(ROOT, "missions")
os.makedirs(MEM_DIR, exist_ok=True)
TODAY = lambda: time.strftime("%Y-%m-%d")

# ============================================================================ free AI engine
PRESETS = {
    "pollinations": {"label": "Pollinations (keyless)", "base": "https://text.pollinations.ai/openai", "model": "openai", "lanes": 1, "full_url": True},
    "groq": {"label": "Groq", "base": "https://api.groq.com/openai/v1", "model": "llama-3.3-70b-versatile", "lanes": 4},
    "gemini": {"label": "Google Gemini", "base": "https://generativelanguage.googleapis.com/v1beta/openai", "model": "gemini-2.5-flash", "lanes": 4},
    "openrouter": {"label": "OpenRouter", "base": "https://openrouter.ai/api/v1", "model": "openrouter/auto", "lanes": 3},
    "cerebras": {"label": "Cerebras", "base": "https://api.cerebras.ai/v1", "model": "llama-3.3-70b", "lanes": 4},
}


class Provider:
    def __init__(self, pid, key="", model=None):
        p = PRESETS[pid]
        self.id, self.label, self.key = pid, p["label"], key
        self.url = p["base"] if p.get("full_url") else p["base"].rstrip("/") + "/chat/completions"
        self.model = model or p["model"]
        self.lanes = threading.Semaphore(p["lanes"])
        self.nlanes = p["lanes"]
        self.cool_until, self.fails = 0.0, 0

    def call(self, messages, temperature, max_tokens):
        body = {"model": self.model, "messages": messages, "temperature": temperature}
        # Respect the same output budget for every provider; the anonymous endpoint
        # is rate-limited, so bounding outputs also reduces latency and waste.
        body["max_tokens"] = max_tokens
        if self.id == "pollinations":
            body["seed"] = random.randint(1, 10**9)
        headers = {"Content-Type": "application/json", "User-Agent": "hive-swarm"}
        if self.key:
            headers["Authorization"] = "Bearer " + self.key
        req = urllib.request.Request(self.url, data=json.dumps(body).encode(), headers=headers, method="POST")
        with urllib.request.urlopen(req, timeout=180) as r:
            j = json.loads(r.read())
        return (j["choices"][0]["message"].get("content") or "").strip()


# One shared keyless provider for the whole server (the free tier limits per IP).
SHARED = [Provider("pollinations")]
for _pid in PRESETS:  # optional: keys from environment, e.g. GROQ_API_KEY
    _k = os.environ.get(_pid.upper() + "_API_KEY")
    if _k and _pid != "pollinations":
        SHARED.append(Provider(_pid, _k))


def chat_llm(messages, temperature=0.6, max_tokens=1800, who="agent", stop=None, log=None):
    last_err, attempt = "no providers", 0
    while attempt < 15:
        if stop and stop.is_set():
            raise RuntimeError("stopped")
        now = time.time()
        ready = [p for p in SHARED if p.cool_until <= now]
        if not ready:
            time.sleep(min(1.0, max(0.1, min(p.cool_until for p in SHARED) - now)))
            continue
        ready.sort(key=lambda p: (p.id == "pollinations", p.fails, random.random()))
        prov = next((p for p in ready if p.lanes.acquire(blocking=False)), None)
        if not prov:
            # Another mission holds the only anonymous lane. Wait without burning retries.
            time.sleep(0.1)
            continue
        # A caller may have queued while the last request hit 429; re-check the cooldown
        # after acquiring the lane instead of immediately hammering the same endpoint.
        if prov.cool_until > time.time():
            wait = prov.cool_until - time.time()
            prov.lanes.release()
            time.sleep(min(1.0, max(0.1, wait)))
            continue
        attempt += 1
        try:
            out = prov.call(messages, temperature, max_tokens)
            if out:
                prov.fails = max(0, prov.fails - 1)
                return out
            last_err = "empty reply"
        except urllib.error.HTTPError as e:
            last_err = f"{prov.label} HTTP {e.code}"
            if e.code in (401, 403) and prov.key:
                prov.cool_until = time.time() + 3600
            elif e.code == 429 or e.code >= 500:
                prov.cool_until = time.time() + min(3 * attempt, 25)
            else:
                prov.fails += 1
                prov.cool_until = time.time() + 8
        except Exception as e:
            last_err = f"{prov.label}: {e}"
            prov.fails += 1
            prov.cool_until = time.time() + 4
        finally:
            prov.lanes.release()
        if log and attempt in (5, 10):
            log(f"{who}: free AI server busy — backing off, then retrying…", "a")
    raise RuntimeError(last_err)


# ============================================================================ tools
_OPS = {ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul, ast.Div: operator.truediv,
        ast.Pow: operator.pow, ast.Mod: operator.mod, ast.FloorDiv: operator.floordiv, ast.USub: operator.neg, ast.UAdd: operator.pos}
_FUN = {"round": round, "min": min, "max": max, "abs": abs, "sqrt": math.sqrt, "log": math.log, "ceil": math.ceil, "floor": math.floor}


def safe_eval(expr):
    expr = expr.replace(",", "").replace("$", "").replace("%", "/100").replace("×", "*").replace("^", "**")

    def ev(n):
        if isinstance(n, ast.Expression):
            return ev(n.body)
        if isinstance(n, ast.Constant) and isinstance(n.value, (int, float)):
            return n.value
        if isinstance(n, ast.BinOp) and type(n.op) in _OPS:
            a, b = ev(n.left), ev(n.right)
            if isinstance(n.op, ast.Pow) and abs(b) > 12:
                raise ValueError("too big")
            return _OPS[type(n.op)](a, b)
        if isinstance(n, ast.UnaryOp) and type(n.op) in _OPS:
            return _OPS[type(n.op)](ev(n.operand))
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id in _FUN:
            return _FUN[n.func.id](*[ev(a) for a in n.args])
        raise ValueError("unsupported")
    v = ev(ast.parse(expr.strip(), mode="eval"))
    return f"{v:,.2f}".rstrip("0").rstrip(".") if isinstance(v, float) else f"{v:,}"


def run_calcs(text):
    """Replace [[calc: expr]] with the computed value (the AI never does arithmetic itself)."""
    n = [0]

    def rep(m):
        try:
            n[0] += 1
            return f"**{safe_eval(m.group(1))}**"
        except Exception:
            return m.group(1)
    return re.sub(r"\[\[\s*calc\s*:\s*([^\]]+?)\s*\]\]", rep, text), n[0]


NUM_RE = re.compile(r"(?<![\w.])\$?\d[\d,]*(?:\.\d+)?\s?(?:%|[kKmMbB])?")


def norm_nums(s):
    """Normalize numeric tokens (including $1.2k -> 1200) for conservative source matching."""
    from decimal import Decimal, InvalidOperation
    out = set()
    for raw in NUM_RE.findall(s):
        token = raw.strip().replace("$", "").replace(",", "")
        suffix = token[-1:].lower() if token[-1:].lower() in ("k", "m", "b") else ""
        token = token[:-1].strip() if suffix else token.rstrip("%").strip()
        try:
            value = Decimal(token) * {"": 1, "k": 1000, "m": 1000000, "b": 1000000000}[suffix]
        except (InvalidOperation, KeyError):
            continue
        if value == value.to_integral_value():
            d = str(int(value))
        else:
            d = format(value.normalize(), "f")
        if re.fullmatch(r"(19|20)\d\d", d):  # years aren't numeric evidence claims
            continue
        out.add(d)
    return out


# ============================================================================ swarm brain
MODES = {
    "money": ("an AI MONEY-MAKING task force. Cover every angle of earning with AI: automation services for businesses, "
              "micro-SaaS & tools, digital products, content & media, e-commerce, freelancing with AI leverage, lead-gen, "
              "arbitrage, affiliate, grants & free funding. Include market researchers, demand analysts, competitor scouts, "
              "pricing analysts, builders (what exactly to build & with which free tools), sales/outreach specialists, "
              "legal/tax checkers (Canada/Ontario) and finance modelers"),
    "team": "a diverse team of top domain experts, each covering a distinct angle",
    "research": "a research lab of analysts, fact-checkers, data specialists and forecasters, each on a distinct sub-question",
    "debate": "thinkers with deliberately different perspectives who argue their case",
    "code": "a software squad: architect, backend, frontend, QA, security, DevOps — producing real, complete code",
    "osint": "a lawful public-source intelligence team: analysts trace open records, official releases, company filings, public news and public technical documentation; cite provenance and uncertainty",
    "security": "a defensive cybersecurity team: authorized asset inventory, secure configuration, threat modeling, patching, incident response and safe lab-only validation",
}
STYLES = {
    "detailed": "a thorough, well-structured report with headings",
    "actionable": "a concrete step-by-step action plan with priorities, costs, timelines and first actions for TODAY",
    "concise": "a tight, concise answer — no fluff",
}
DEPTH = {  # follow-up research loops, sub-swarm budget, pages read per agent
    "fast": {"loops": 0, "subswarms": 0, "read": 1, "verify_fetch": 6},
    "accurate": {"loops": 1, "subswarms": 2, "read": 2, "verify_fetch": 14},
    "max": {"loops": 2, "subswarms": 4, "read": 3, "verify_fetch": 24},
}


def rules(web):
    r = ("NEVER-QUIT RULES: Never say something is impossible, that you can't help, or tell the user to ask someone else. "
         "If a path is blocked, find a workaround — an alternative method, free tool, partial solution or creative route — and deliver it. "
         "Only legal, ethical methods (no scams, spam, fake reviews or ToS violations) — within that, be relentlessly resourceful.\n"
         "SAFETY / AUTHORIZATION: Use public, lawfully accessible information only. Do not break into systems, bypass logins/paywalls, steal credentials, deploy malware, evade detection, dox or surveil private people, or claim access to classified CIA/FBI/SpaceX/internal data. For cybersecurity, limit work to defensive guidance or systems the user is authorized to test; for intelligence, use traceable public sources and label uncertainty. Never initiate account access, payments, trades or money movement. If asked for an unsafe action, offer a concrete lawful alternative.\n"
         "TOOLS you can use inside your answer:\n"
         "- For ANY arithmetic write [[calc: expression]] e.g. [[calc: 25*4*4.3]] — it is computed exactly. Never do math in your head.\n")
    if web:
        r += ("- If you lack data, add a final line  NEED: <specific web search query>  (max 2) and you'll get fresh research.\n"
              "- If a part of your task is too big for you alone, add a final line  HELP: <sub-task for a helper swarm>  (max 1).\n"
              f"TRUTH RULES: Use ONLY facts/numbers/prices/names found in the RESEARCH and cite them like [3]. If you need an unsourced "
              f"number write 'estimate:' and state the assumption. Never invent statistics, quotes, companies or URLs. Today is {TODAY()}.")
    return r


MISSIONS = {}


def extract_json(text):
    if not text:
        return None
    text = re.sub(r"```(?:json)?", "", text)
    s, e = text.find("{"), text.rfind("}")
    if s < 0 or e < 0:
        return None
    for chunk in (text[s:e + 1], re.sub(r",\s*([}\]])", r"\1", text[s:e + 1])):
        try:
            return json.loads(chunk)
        except Exception:
            pass
    return None


class Mission:
    def __init__(self, cfg):
        self.id = uuid.uuid4().hex[:10]
        self.cfg = cfg
        self.events, self.lock = [], threading.Lock()
        self.stop = threading.Event()
        self.agents, self.sources = [], []
        self.stats = {"calls": 0, "sources": 0, "pages_read": 0, "subswarms": 0, "calcs": 0,
                      "verified": 0, "claims": 0, "uncited": 0, "agents": 0}
        self.started = time.time()
        self.depth = DEPTH.get(cfg.get("depth", "accurate"), DEPTH["accurate"])
        self.sub_budget = self.depth["subswarms"]
        self.final = ""

    # ---------------------------------------------------------------- events
    def emit(self, **ev):
        with self.lock:
            ev["n"] = len(self.events)
            ev["t"] = round(time.time() - self.started, 1)
            self.events.append(ev)

    def log(self, msg, cls=""):
        self.emit(type="log", msg=msg, cls=cls)

    def push_stats(self):
        with self.lock:
            self.stats["sources"] = len(self.sources)
            self.stats["pages_read"] = sum(1 for s in self.sources if s.get("page_read"))
            self.stats["agents"] = len(self.agents)
            snapshot = dict(self.stats)
        self.emit(type="stats", stats=snapshot)

    def push_agent(self, i):
        a = {k: v for k, v in self.agents[i].items()}
        self.emit(type="agent", i=i, agent=a)

    def new_agent(self, a, parent=None):
        with self.lock:
            a.update({"status": "queued", "output": "", "parent": parent, "sub": parent is not None})
            self.agents.append(a)
            i = len(self.agents) - 1
        self.emit(type="spawn", i=i, agent=dict(a))
        return i

    def llm(self, system, user, who, temperature=0.6, max_tokens=1800, pulse=None):
        with self.lock:
            self.stats["calls"] += 1
        if pulse is not None:
            self.emit(type="pulse", i=pulse)
        out = chat_llm([{"role": "system", "content": system}, {"role": "user", "content": user}],
                       temperature, max_tokens, who, self.stop, self.log)
        out, n = run_calcs(out)
        if n:
            with self.lock:
                self.stats["calcs"] += n
        self.push_stats()
        return out

    def add_sources(self, srcs):
        pairs = []
        with self.lock:
            for s in srcs:
                url = s.get("url", "")
                if not url:
                    continue
                ex = next((x for x in self.sources if x["url"] == url), None)
                if not ex:
                    ex = {"id": len(self.sources) + 1, "title": s.get("title", "Untitled source"), "url": url,
                          "text": s.get("text", ""), "snippet": s.get("snippet", ""), "trust": s.get("trust", 0),
                          "query": s.get("query", ""), "queries": s.get("queries", [s.get("query", "")]),
                          "engine": s.get("engine", ""), "kind": s.get("kind", "web"), "relevance": s.get("relevance", 0),
                          "publisher": s.get("publisher", ""), "publisher_url": s.get("publisher_url", ""),
                          "page_read": bool(s.get("page_read")), "observed_at": s.get("observed_at", ""),
                          "read_at": s.get("read_at", "")}
                    self.sources.append(ex)
                else:
                    if len(s.get("text", "")) > len(ex.get("text", "")):
                        ex["text"] = s["text"]
                    for k in ("title", "snippet", "query", "engine", "kind", "relevance", "publisher", "publisher_url", "observed_at", "read_at"): 
                        if s.get(k): ex[k] = s[k]
                    if s.get("queries"):
                        ex["queries"] = list(dict.fromkeys(ex.get("queries", []) + s["queries"]))
                    ex["page_read"] = bool(ex.get("page_read") or s.get("page_read"))
                    ex["trust"] = max(ex.get("trust", 0), s.get("trust", 0))
                pairs.append((ex["id"], s))
        return pairs

    def research(self, queries, read_top, owner_i=-1):
        def on_event(ev):
            ev = dict(ev)
            kind = ev.pop("type", "research")
            if kind == "source":
                src = ev.get("source", {})
                stage = ev.get("stage", "found")
                # During a read event, retain the extracted excerpt for the evidence pane.
                if src.get("excerpt") and not src.get("text"):
                    src["text"] = src["excerpt"]
                mapped = self.add_sources([src])
                if mapped:
                    gid, _ = mapped[0]
                    src = {**src, "id": gid}
                    self.emit(type="source", stage=stage, source=src, owner_i=ev.get("owner_i", owner_i))
                    self.push_stats()
            elif kind == "search":
                self.emit(type="search", owner_i=ev.get("owner_i", owner_i), **{k: v for k, v in ev.items() if k != "owner_i"})
        ctx, srcs = websearch.research(queries, read_top=read_top, with_news=True, on_event=on_event, owner_i=owner_i)
        mapping = {s["id"]: gid for gid, s in self.add_sources(srcs)}
        ctx = re.sub(r"\[(\d+)\]", lambda m: f"[{mapping.get(int(m.group(1)), m.group(1))}]", ctx)
        self.push_stats()
        return ctx, len(srcs)

    # ---------------------------------------------------------------- planning
    def plan_agents(self, goal, size, mode, shared, existing=None, who="Queen"):
        agents = []
        existing = existing or []
        run_memory = memory_for(goal, mode)
        while len(agents) < size and not self.stop.is_set():
            need = min(12, size - len(agents))
            have = "\n".join(f"- {a['name']}: {a['task']}" for a in existing + agents)
            raw = self.llm(
                "You are the QUEEN orchestrator of an AI swarm. Reply with ONLY valid JSON, no prose.",
                f"MISSION: {goal}\n\n{('LIVE WEB SCOUTING:' + chr(10) + shared[:3500]) if shared else ''}\n\n"
                f"PRIOR RUN METRICS (historical only, NOT evidence): {run_memory or '(no similar run metrics)'}\n"
                "Use memory only to avoid duplicated angles and improve evidence coverage; re-check every changing fact now.\n"
                f"Design {need} specialist agents forming {MODES.get(mode, MODES['team'])}.\n"
                + (f"These agents ALREADY exist — new ones must cover DIFFERENT ground:\n{have}\n" if have else "")
                + "Each gets ONE distinct, concrete sub-task; together they fully cover the mission.\n"
                  'JSON: {"agents":[{"name":"short unique name","emoji":"one emoji","role":"job title",'
                  '"task":"1-2 sentence instruction","query":"one specific web search query"}]}',
                who, 0.6, 3000)
            got = (extract_json(raw) or {}).get("agents") or [{"name": f"Agent {len(agents)+1}", "emoji": "🐝", "role": "Specialist",
                                                                "task": f"Cover an important uncovered angle of: {goal}", "query": goal}]
            for a in got[:need]:
                agents.append({"name": str(a.get("name") or f"Agent {len(agents)+1}")[:40], "emoji": str(a.get("emoji") or "🐝")[:4],
                               "role": str(a.get("role") or "")[:60], "task": str(a.get("task") or "")[:400],
                               "query": str(a.get("query") or goal)[:150]})
        return agents[:size]

    # ---------------------------------------------------------------- one agent (recursive)
    def work(self, i, goal, roster, shared, web, level=0):
        a = self.agents[i]
        if self.stop.is_set():
            return
        a["status"] = "researching"; self.push_agent(i)
        own = ""
        if web:
            try:
                own, n = self.research([a["query"]], read_top=self.depth["read"], owner_i=i)
                self.log(f"{a['emoji']} {a['name']} found {n} unique web results; opened up to {self.depth['read']} pages")
            except Exception as e:
                self.log(f"{a['name']} search issue: {e} — continuing with shared intel", "a")
        a["status"] = "working"; self.push_agent(i)
        system = (f"You are {a['name']}, the {a['role']} in an elite AI swarm — world-class at your specialty. "
                  f"Stay on your sub-task; other agents cover the rest. Use markdown.\n{rules(web)}")
        base = (f"MISSION: {goal}\n\nSWARM ROSTER:\n{roster}\n\nYOUR SUB-TASK: {a['task']}\n\n"
                + (f"RESEARCH (live web, {TODAY()}):\n{own[:7000]}\n\nSHARED INTEL:\n{shared[:2500]}\n\n" if web else ""))
        try:
            out = self.llm(system, base + "Deliver your best concrete, specific contribution (250-500 words; cited numbers; exact steps, tools, prices; code if relevant).",
                           a["name"], pulse=i)
            # --- deep research loops (NEED:) and helper swarms (HELP:)
            for loop in range(self.depth["loops"] + 1):
                needs = re.findall(r"^\s*NEED:\s*(.+)$", out, re.M)[:2]
                helps = re.findall(r"^\s*HELP:\s*(.+)$", out, re.M)[:1]
                if not (needs or helps) or self.stop.is_set():
                    break
                extra = ""
                if needs and web and loop < self.depth["loops"]:
                    a["status"] = "researching"; self.push_agent(i)
                    more, n = self.research(needs, read_top=1, owner_i=i)
                    self.log(f"{a['emoji']} {a['name']} follow-up search: {' · '.join(needs)} (+{n} results)")
                    extra += f"\nFOLLOW-UP RESEARCH:\n{more[:6000]}\n"
                if helps and level == 0 and self.sub_budget > 0:
                    self.sub_budget -= 1
                    extra += "\nHELPER SWARM RESULTS:\n" + self.subswarm(i, helps[0], goal, web) + "\n"
                if not extra:
                    break
                a["status"] = "working"; self.push_agent(i)
                out = self.llm(system, base + f"YOUR DRAFT:\n{out}\n{extra}\nNow write your improved, complete final contribution using the new material. "
                               "Only add NEED:/HELP: lines if still truly necessary.", a["name"], pulse=i)
            a["output"] = re.sub(r"^\s*(NEED|HELP):.*$", "", out, flags=re.M).strip()
            a["status"] = "done"
        except Exception as e:
            if str(e) == "stopped":
                return
            a["status"], a["output"] = "error", f"_Failed: {e}_"
        self.push_agent(i)

    def subswarm(self, parent, subtask, goal, web):
        p = self.agents[parent]
        with self.lock:
            self.stats["subswarms"] += 1
        self.push_stats()
        self.log(f"🧠 {p['name']} called a helper swarm: “{subtask[:90]}”", "g")
        self.emit(type="subswarm", parent=parent, task=subtask)
        specs = self.plan_agents(f"{subtask}\n(Context — part of the larger mission: {goal})", 3, self.cfg.get("mode", "team"), "", who=f"{p['name']}·sub")
        ids = [self.new_agent(s, parent) for s in specs]
        roster = "\n".join(f"- {s['name']} ({s['role']}): {s['task']}" for s in specs)
        with ThreadPoolExecutor(3) as ex:
            list(ex.map(lambda i: self.work(i, subtask, roster, "", web, level=1), ids))
        return "\n\n".join(f"### {self.agents[i]['name']} ({self.agents[i]['role']})\n{self.agents[i]['output'][:2500]}" for i in ids)

    # ---------------------------------------------------------------- fact-check
    def fact_check(self, text):
        """Check whether cited numeric tokens occur in fetched source text; flag uncited numbers."""
        cited = sorted(set(int(x) for x in re.findall(r"\[(\d+)\]", text)))
        weak = [s for s in self.sources if s["id"] in cited and len(s.get("text", "")) < 800][:self.depth["verify_fetch"]]
        if weak:
            self.log(f"🔬 Opening {len(weak)} cited pages for numeric cross-checks…")
            for s in weak:
                self.emit(type="source", stage="verifying", source={k: s.get(k) for k in ("id", "title", "url", "snippet", "trust", "query", "engine", "kind")})
            with ThreadPoolExecutor(min(6, len(weak))) as ex:
                pages = list(ex.map(lambda s: websearch.read(s["url"], 12000), weak))
            for s, page in zip(weak, pages):
                if page and not page.startswith("(could not read"):
                    s["text"] = (s.get("text", "") + " " + page)[:16000]
                    s["page_read"] = True
                    s["read_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
                self.emit(type="source", stage="read" if s.get("page_read") else "read_error",
                          source={k: s.get(k) for k in ("id", "title", "url", "snippet", "trust", "query", "engine", "kind", "page_read", "read_at")}
                          | {"excerpt": page[:900]})
            self.push_stats()
        by_id = {s["id"]: s for s in self.sources}
        verified = claims = uncited = 0

        def check_claim(claim):
            nonlocal verified, claims, uncited
            ids = [int(x) for x in re.findall(r"\[(\d+)\]", claim)]
            plain = re.sub(r"\[\d+\]", "", claim)
            nums = norm_nums(plain)
            if not nums:
                return ""
            claims += 1
            if not ids:
                uncited += 1
                return "⚠️"
            src_text = " ".join(by_id[k].get("text", "") for k in ids if k in by_id)
            matched = len(nums.intersection(norm_nums(src_text)))
            if matched >= max(1, math.ceil(len(nums) / 2)):
                verified += 1
                return "✅"
            return "⚠️"

        out_lines = []
        table_headers = None
        in_code = False
        for line in text.split("\n"):
            if line.strip().startswith("```"):
                in_code = not in_code
                out_lines.append(line)
                continue
            if in_code or line.lstrip().startswith("#"):
                out_lines.append(line)
                continue
            if line.lstrip().startswith("|") and line.rstrip().endswith("|"):
                cells = [c.strip() for c in line.strip().strip("|").split("|")]
                if all(re.fullmatch(r"[:\-\s]+", c or "-") for c in cells):
                    out_lines.append(line)
                    continue
                if table_headers is None:
                    table_headers = cells
                    out_lines.append(line)
                    continue
                check_cells = list(cells)
                if table_headers and table_headers[0].lower().strip() in ("rank", "#", "no."):
                    check_cells[0] = ""  # rank/index is not a factual claim
                mark = check_claim(" ".join(check_cells))
                if mark:
                    line = line.rstrip()
                    line = line[:-1].rstrip() + f" {mark} |"
                out_lines.append(line)
                continue
            table_headers = None
            if not line.strip():
                out_lines.append(line)
                continue
            # Keep markdown list numbering out of the arithmetic check, but check the claim itself.
            parts = re.split(r"(?<=[.!?])\s+", line)
            rendered = []
            for sent in parts:
                tested = re.sub(r"^\s*(?:[-*+]\s+|\d+[.)]\s+)", "", sent)
                mark = check_claim(tested)
                rendered.append(sent + (f" {mark}" if mark else ""))
            out_lines.append(" ".join(rendered))
        self.stats["verified"], self.stats["claims"], self.stats["uncited"] = verified, claims, uncited
        self.push_stats()
        return "\n".join(out_lines), verified, claims, uncited

    # ---------------------------------------------------------------- pipeline
    def run(self):
        c = self.cfg
        goal, size, rounds = c["goal"], max(1, min(50, int(c.get("size", 5)))), int(c.get("rounds", 1))
        mode, style, web = c.get("mode", "team"), c.get("style", "detailed"), c.get("web", True)
        lanes = sum(p.nlanes for p in SHARED)
        self.log(f"Mission → {size} agents · depth {c.get('depth','accurate')} · web research {'ON' if web else 'OFF'}", "g")
        try:
            self.emit(type="phase", p="plan")
            shared = ""
            if web:
                self.log("🌐 Queen scouting the live web…")
                qraw = self.llm("You write web search queries. Reply ONLY JSON.",
                                f'MISSION: {goal}\nWrite 3 short, specific search queries that give the most useful real-world facts '
                                f'(prices, demand, competitors, official rules). JSON: {{"queries":["..."]}}', "Queen", 0.3, 300, pulse=-1)
                qs = (extract_json(qraw) or {}).get("queries") or [goal]
                shared, n = self.research(qs[:3], read_top=2, owner_i=-1)
                fx = websearch.fx_rates() if mode == "money" or re.search(r"\b(fx|forex|currency|exchange rate|foreign exchange)\b", goal, re.I) else ""
                if fx:
                    shared = fx + "\n" + shared
                    fs = {"title": "Bank of Canada — daily exchange-rate observations", "url": "https://www.bankofcanada.ca/valet/observations/FXUSDCAD,FXEURCAD,FXGBPCAD,FXCNYCAD,FXINRCAD/json?recent=1",
                          "snippet": fx, "text": fx, "query": "Bank of Canada official FX data", "queries": ["Bank of Canada official FX data"],
                          "engine": "Bank of Canada Valet API", "kind": "official_api", "trust": 2, "page_read": True,
                          "observed_at": time.strftime("%Y-%m-%d %H:%M:%S"), "read_at": time.strftime("%Y-%m-%d %H:%M:%S")}
                    gid = self.add_sources([fs])[0][0]
                    self.emit(type="source", stage="read", source={**fs, "id": gid, "excerpt": fx}, owner_i=-1)
                self.push_stats()
                self.log(f"🌐 Queen gathered {n} unique web results; {self.stats['pages_read']} pages opened", "g")

            self.log("👑 Queen designing the swarm…")
            specs = self.plan_agents(goal, size, mode, shared)
            ids = [self.new_agent(s) for s in specs]
            self.log(f"👑 {len(ids)} agents spawned.", "g")
            self.push_stats()
            roster = "\n".join(f"- {a['name']} ({a['role']}): {a['task']}" for a in specs)

            self.emit(type="phase", p="work")
            workers = max(1, min(len(ids), max(1, lanes)))  # free anonymous LLM: one in-flight call; avoid 429 storms
            with ThreadPoolExecutor(workers) as ex:
                list(ex.map(lambda i: self.work(i, goal, roster, shared, web), ids))
            mains = [i for i in ids if self.agents[i]["status"] == "done"]
            self.log(f"🐝 Workers finished ({len(mains)}/{len(ids)} succeeded, {self.stats['subswarms']} helper swarms).", "g")

            for r in range(1, rounds + 1):
                if self.stop.is_set():
                    break
                self.emit(type="phase", p="critic")
                batches = [mains[k:k + 8] for k in range(0, len(mains), 8)]
                feedback = {}

                def crit(batch):
                    dump = "\n\n".join(f"### {self.agents[i]['name']} ({self.agents[i]['role']})\n{self.agents[i]['output'][:3000]}" for i in batch)
                    raw = self.llm("You are the CRITIC: ruthless, constructive, fact-focused. Reply ONLY JSON.",
                                   f"MISSION: {goal}\n\nOUTPUTS:\n{dump}\n\nFor EACH agent: flag invented or uncited numbers, math not done with calc, "
                                   "vague advice, any 'impossible/can't' defeatism (demand a workaround), gaps and contradictions. Say exactly how to fix.\n"
                                   'JSON: {"feedback":{"<agent name>":"feedback"}}', "Critic", 0.3, 2000, pulse=-1)
                    return (extract_json(raw) or {}).get("feedback") or {}

                self.log(f"🔍 Critic reviewing (round {r}/{rounds})…")
                with ThreadPoolExecutor(max(1, min(len(batches), lanes))) as ex:
                    for fb in ex.map(crit, batches):
                        feedback.update({str(k).lower(): v for k, v in fb.items()})

                self.emit(type="phase", p="refine")

                def refine(i):
                    a = self.agents[i]
                    fb = feedback.get(a["name"].lower()) or next((v for k, v in feedback.items() if a["name"].lower() in k), None)
                    if not fb or self.stop.is_set():
                        return
                    a["status"] = "working"; self.push_agent(i)
                    try:
                        out = self.llm(f"You are {a['name']}, the {a['role']}. Use markdown.\n{rules(web)}",
                                       f"MISSION: {goal}\nSUB-TASK: {a['task']}\n\nYOUR DRAFT:\n{a['output']}\n\nCRITIC FEEDBACK:\n{fb}\n\n"
                                       "Write the improved full version. Keep valid citations [n]; remove anything unsupported. No NEED/HELP lines.",
                                       a["name"], pulse=i)
                        a["output"] = re.sub(r"^\s*(NEED|HELP):.*$", "", out, flags=re.M).strip()
                    except Exception:
                        pass
                    a["status"] = "done"; self.push_agent(i)

                with ThreadPoolExecutor(workers) as ex:
                    list(ex.map(refine, mains))
                self.log(f"♻ Refinement round {r} complete.", "g")

            self.emit(type="phase", p="synth")
            done = [self.agents[i] for i in mains]
            parts = [f"### {a['name']} ({a['role']})\n{a['output'][:3500]}" for a in done]
            if len(parts) > 8:
                self.log(f"🧬 Condensing {len(parts)} reports in groups…")
                groups = [parts[k:k + 8] for k in range(0, len(parts), 8)]
                with ThreadPoolExecutor(max(1, min(len(groups), lanes))) as ex:
                    parts = list(ex.map(lambda g: self.llm(
                        "You condense expert reports without losing concrete facts, numbers, steps or citations [n]. Markdown.",
                        f"MISSION: {goal}\n\nREPORTS:\n" + "\n\n".join(g) + "\n\nMerge into one dense brief (max 700 words), keep citations.",
                        "Synthesizer", 0.3, 1500, pulse=-1), groups))
            self.log("🧬 Synthesizer writing the final deliverable…")
            final = self.llm(
                "You are the SYNTHESIZER of an AI swarm. Merge expert work into one superior deliverable. Resolve contradictions, "
                f"remove duplication, keep every concrete insight and its citations [n]. Clean markdown.\n{rules(web)}",
                f"MISSION: {goal}\n\nSWARM WORK:\n" + "\n\n".join(parts) +
                f"\n\nProduce the final deliverable as {STYLES.get(style, STYLES['detailed'])}. "
                + ("Summarize positions, then the consensus verdict. " if mode == "debate" else "")
                + ("Rank EVERY money path found by (realistic monthly profit, startup cost, time-to-first-dollar, how much AI can automate). "
                   "For each: what to build/sell, exact free tools, pricing with citations, where customers are, and a first-7-days plan. "
                   "Where something is hard, give the workaround — never say impossible. " if mode == "money" else "")
                + "Start with a title. Do NOT write a sources list (it's appended automatically). No NEED/HELP lines.",
                "Synthesizer", 0.4, 4000, pulse=-1)
            final = re.sub(r"^\s*(NEED|HELP):.*$", "", final, flags=re.M).strip()

            if web:
                self.emit(type="phase", p="verify")
                final, v, cl, unc = self.fact_check(final)
                pct = round(100 * v / cl) if cl else None
                check_label = f"{pct}% of {cl} numeric claims had matching numbers in cited source text" if cl else "N/A — no checkable numeric claims were detected"
                self.log(f"🔬 Numeric cross-check: {v}/{cl} matched · {unc} uncited · {check_label}.", "g" if pct is not None and pct >= 50 else "a")
                used = sorted(set(int(x) for x in re.findall(r"\[(\d+)\]", final)))
                src_md = "\n".join(f"{s['id']}. {'⭐ ' if s['trust'] > 0 else ''}[{s['title']}]({s['url']})" for s in self.sources if s["id"] in used)
                final += (f"\n\n---\n**Numeric cross-check:** {v}/{cl} claims ({check_label}); {unc} uncited. "
                          "✅ means the cited page text contains a matching number; ⚠️ means uncited or not matched. This is an automated token check, not proof that a claim is true. ⭐ is a rough domain heuristic, not an endorsement.")
                if src_md:
                    final += f"\n\n## Sources (web research collected {TODAY()})\n{src_md}"
            self.final = final
            self.stats["elapsed_s"] = int(time.time() - self.started)
            self.push_stats()
            self.emit(type="final", md=final)
            self.log(f"✅ Mission complete in {self.stats['elapsed_s']}s · {self.stats['calls']} AI calls · {len(self.sources)} web results.", "g")
            self.save()
        except Exception as e:
            self.log("■ Swarm stopped." if str(e) == "stopped" else f"Mission issue: {e}", "e")
        finally:
            self.emit(type="done")

    def save(self):
        rec = {"id": self.id, "goal": self.cfg["goal"], "mode": self.cfg.get("mode"), "date": time.strftime("%Y-%m-%d %H:%M"),
               "final": self.final, "stats": dict(self.stats),
               "agents": [{k: a[k] for k in ("name", "emoji", "role", "task", "output", "parent")} for a in self.agents],
               "sources": [{k: s.get(k) for k in ("id", "title", "url", "snippet", "trust", "query", "queries", "engine", "kind", "relevance", "publisher", "publisher_url", "page_read", "observed_at", "read_at")} |
                           {"text": s.get("text", "")[:1600]} for s in self.sources]}
        with open(os.path.join(MEM_DIR, self.id + ".json"), "w") as f:
            json.dump(rec, f)


def history():
    out = []
    for fn in sorted(os.listdir(MEM_DIR), key=lambda f: -os.path.getmtime(os.path.join(MEM_DIR, f))):
        if fn.endswith(".json"):
            try:
                d = json.load(open(os.path.join(MEM_DIR, fn)))
                out.append({"id": d["id"], "goal": d["goal"], "date": d["date"], "mode": d.get("mode"), "stats": d.get("stats", {})})
            except Exception:
                pass
    return out


def memory_for(goal, mode=None):
    """Tiny transparent adaptation layer: reuse prior run metrics, never prior claims as facts."""
    stop = {"with", "from", "that", "this", "your", "make", "ways", "find", "every", "using", "about", "into", "what", "for", "the", "and", "with", "starting"}
    words = lambda s: {w for w in re.findall(r"[a-z0-9]{4,}", (s or "").lower()) if w not in stop}
    target = words(goal)
    ranked = []
    for h in history():
        st = h.get("stats", {})
        overlap = len(target & words(h.get("goal", "")))
        same_mode = mode and h.get("mode") == mode
        if overlap < 2 and not same_mode:
            continue
        ranked.append((overlap + (2 if same_mode else 0), h, st))
    ranked.sort(key=lambda x: (x[0], x[1].get("date", "")), reverse=True)
    out = []
    for _, h, st in ranked[:3]:
        claims = st.get("claims", 0)
        match = f"{st.get('verified', 0)}/{claims} numeric text matches" if claims else "no numeric match score"
        out.append(f"{h.get('goal','')[:90]}: {st.get('sources',0)} web results, {st.get('pages_read',0)} pages opened, {match}")
    return " | ".join(out)


# ============================================================================ Commander (chat agent)
COMMANDER = """You are COMMANDER, the strategic brain of HIVE — a free multi-agent AI swarm system with live web research.
You talk with the user, think like a sharp, honest, experienced entrepreneur and engineer, and you LAUNCH SWARMS to do heavy work.

Your style: direct, practical, specific; short paragraphs and bullets. For lawful tasks, find a workable route instead of giving up.
Be honest: never promise guaranteed income; point out real risks briefly, then focus on action. Use public, lawfully accessible sources only. Do not claim classified CIA/FBI/SpaceX access, break into accounts or systems, evade detection, or move money. Cyber requests must stay defensive and authorized; offer a safe alternative when needed.

HOW TO LAUNCH A SWARM: when the user wants research, a plan, a build, market data, or anything that benefits from many agents and
live web data, reply with one short sentence and then a line exactly like:
LAUNCH: {"goal": "<clear, detailed mission>", "size": 8, "mode": "money", "depth": "accurate", "rounds": 1, "style": "actionable"}
modes: money | team | research | debate | code | osint (public-source intelligence) | security (defensive cyber only). depth: fast | accurate | max. size 3-50; bigger is slower on the free, single-lane model.
You may launch several swarms in one reply (one LAUNCH line each) to attack different angles in parallel, but do not claim this creates extra compute capacity.
For quick questions, just answer yourself without launching. Today is {today}.

HISTORICAL RUN MEMORY (metrics and prior topics only; not evidence):
Use this to avoid repeating work and suggest useful follow-ups. Re-check changing facts in current public sources; old generated reports are not verified facts.
{memory}"""


def commander_reply(messages):
    mem = []
    for h in history()[:4]:
        try:
            d = json.load(open(os.path.join(MEM_DIR, h["id"] + ".json")))
            st = h.get("stats", {})
            den = st.get("claims", 0)
            pct = f"{round(100 * st.get('verified', 0) / den)}%" if den else "not scored"
            mem.append(f"- [{h['date']}] {h['goal'][:140]} · {st.get('agents', 0)} agents · {st.get('sources', 0)} web results · {st.get('pages_read', 0)} pages read · numeric text-match {pct}")
        except Exception:
            pass
    sysmsg = COMMANDER.replace("{today}", TODAY()).replace("{memory}", "\n".join(mem) or "(none yet)")
    msgs = [{"role": "system", "content": sysmsg}] + [m for m in messages[-14:] if m.get("role") in ("user", "assistant")]
    reply = chat_llm(msgs, 0.6, 1500, "Commander")
    reply, _ = run_calcs(reply)
    launched = []
    for m in re.finditer(r"^\s*LAUNCH:\s*(\{.*\})\s*$", reply, re.M):
        cfg = extract_json(m.group(1))
        if cfg and cfg.get("goal"):
            cfg.setdefault("web", cfg.get("mode") != "code")
            mi = Mission(cfg)
            MISSIONS[mi.id] = mi
            threading.Thread(target=mi.run, daemon=True).start()
            launched.append({"id": mi.id, "goal": cfg["goal"], "size": cfg.get("size", 5), "mode": cfg.get("mode", "team")})
    clean = re.sub(r"^\s*LAUNCH:.*$", "", reply, flags=re.M).strip()
    return clean, launched


# ============================================================================ http
class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *a, **kw):
        super().__init__(*a, directory=ROOT, **kw)

    def log_message(self, *a):
        pass

    def _json(self, code, obj):
        b = json.dumps(obj).encode()
        try:
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(b)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(b)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _q(self):
        return dict(x.split("=", 1) for x in self.path.split("?", 1)[-1].split("&") if "=" in x)

    def do_GET(self):
        if self.path.startswith("/api/events"):
            q = self._q()
            m = MISSIONS.get(q.get("id"))
            if not m:
                return self._json(404, {"error": "unknown mission"})
            since = int(q.get("since", 0))
            deadline = time.time() + 20
            while time.time() < deadline and len(m.events) <= since:
                time.sleep(0.25)
            return self._json(200, {"events": m.events[since:]})
        if self.path.startswith("/api/history"):
            return self._json(200, {"missions": history()})
        if self.path.startswith("/api/load"):
            try:
                return self._json(200, json.load(open(os.path.join(MEM_DIR, re.sub(r"\W", "", self._q().get("id", "")) + ".json"))))
            except Exception:
                return self._json(404, {"error": "not found"})
        if self.path.startswith("/api/active"):
            return self._json(200, {"missions": [{"id": k, "goal": m.cfg["goal"], "done": bool(m.events and m.events[-1]["type"] == "done")}
                                                 for k, m in MISSIONS.items()]})
        if self.path in ("/", ""):
            self.path = "/index.html"
        return super().do_GET()

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
        if self.path.startswith("/api/mission"):
            if not str(body.get("goal", "")).strip():
                return self._json(400, {"error": "goal required"})
            m = Mission(body)
            MISSIONS[m.id] = m
            threading.Thread(target=m.run, daemon=True).start()
            return self._json(200, {"id": m.id})
        if self.path.startswith("/api/stop"):
            m = MISSIONS.get(body.get("id"))
            if m:
                m.stop.set()
            return self._json(200, {"ok": True})
        if self.path.startswith("/api/chat"):
            try:
                reply, launched = commander_reply(body.get("messages", []))
                return self._json(200, {"reply": reply, "launched": launched})
            except Exception as e:
                return self._json(200, {"reply": f"(Commander is reconnecting to the free AI engine: {e}. Try again in a moment.)", "launched": []})
        self._json(404, {"error": "not found"})


if __name__ == "__main__":
    print(f"⬡ HIVE v3 running → http://localhost:{PORT}")
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
