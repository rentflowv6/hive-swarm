#!/usr/bin/env python3
"""HIVE v4 — Supercharged AI Swarm Server. Free multi-agent AI swarm with
multi-provider routing, SQLite memory, 15+ task capabilities, enhanced web
scraping, and a Commander chat brain.

Run:   python3 server.py        then open  http://localhost:8080
Needs: Python 3.9+ only. No pip installs. No API keys required (Pollinations
is keyless; set GROQ/GEMINI/OPENROUTER/CEREBRAS/CLOUDFLARE/GITHUB_API_KEY env
vars to unlock faster/more capable models).
"""
import ast, json, math, operator, os, re, sys, time, uuid, random, threading, sqlite3, hashlib
import urllib.request, urllib.error, urllib.parse
from concurrent.futures import ThreadPoolExecutor
from http.server import ThreadingHTTPServer, SimpleHTTPRequestHandler
from datetime import datetime, timedelta
import websearch

PORT = int(os.environ.get("PORT", sys.argv[1] if len(sys.argv) > 1 else 8080))
ROOT = os.path.dirname(os.path.abspath(__file__))
MEM_DIR = os.path.join(ROOT, "missions")
DB_PATH = os.path.join(ROOT, "hive_memory.db")
os.makedirs(MEM_DIR, exist_ok=True)
TODAY = lambda: time.strftime("%Y-%m-%d")
MAX_CONCURRENT_MISSIONS = 3

# ============================================================================ SQLite Memory
_db_lock = threading.Lock()

def _db():
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn

def init_db():
    with _db() as c:
        c.executescript("""
            CREATE TABLE IF NOT EXISTS missions (
                id TEXT PRIMARY KEY, goal TEXT, mode TEXT, style TEXT, depth TEXT,
                created_at TEXT, completed_at TEXT, final_md TEXT, stats_json TEXT
            );
            CREATE TABLE IF NOT EXISTS sources_cache (
                url_hash TEXT PRIMARY KEY, url TEXT, title TEXT, content TEXT,
                fetched_at TEXT, ttl_seconds INTEGER DEFAULT 3600
            );
            CREATE TABLE IF NOT EXISTS learned_facts (
                id INTEGER PRIMARY KEY AUTOINCREMENT, fact TEXT, source_url TEXT,
                confidence REAL DEFAULT 0.5, created_at TEXT, mission_id TEXT
            );
            CREATE TABLE IF NOT EXISTS preferences (
                key TEXT PRIMARY KEY, value TEXT, updated_at TEXT
            );
            CREATE TABLE IF NOT EXISTS provider_stats (
                provider_id TEXT PRIMARY KEY, total_calls INTEGER DEFAULT 0,
                total_tokens INTEGER DEFAULT 0, errors INTEGER DEFAULT 0,
                last_used TEXT, avg_latency_ms REAL DEFAULT 0, cooldown_until TEXT
            );
            CREATE TABLE IF NOT EXISTS agent_outputs (
                id INTEGER PRIMARY KEY AUTOINCREMENT, mission_id TEXT,
                agent_name TEXT, role TEXT, output TEXT, created_at TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_sources_hash ON sources_cache(url_hash);
            CREATE INDEX IF NOT EXISTS idx_facts_mission ON learned_facts(mission_id);
            CREATE INDEX IF NOT EXISTS idx_agent_mission ON agent_outputs(mission_id);
        """)

init_db()

def db_exec(sql, params=()):
    with _db_lock:
        with _db() as c:
            cur = c.execute(sql, params)
            c.commit()
            return cur

def db_query(sql, params=()):
    with _db() as c:
        return c.execute(sql, params).fetchall()

# ============================================================================ Multi-Provider AI Router
PRESETS = {
    "pollinations": {
        "label": "Pollinations (keyless)", "base": "https://text.pollinations.ai/openai",
        "model": "openai", "lanes": 2, "full_url": True, "requires_key": False,
        "supports_vision": False, "speed": 3
    },
    "groq": {
        "label": "Groq", "base": "https://api.groq.com/openai/v1",
        "model": "llama-3.3-70b-versatile", "lanes": 4, "requires_key": True,
        "env_key": "GROQ_API_KEY", "supports_vision": True, "speed": 5
    },
    "gemini": {
        "label": "Google Gemini", "base": "https://generativelanguage.googleapis.com/v1beta/openai",
        "model": "gemini-2.0-flash", "lanes": 4, "requires_key": True,
        "env_key": "GEMINI_API_KEY", "supports_vision": True, "speed": 4
    },
    "openrouter": {
        "label": "OpenRouter", "base": "https://openrouter.ai/api/v1",
        "model": "google/gemini-2.0-flash-001:free", "lanes": 3, "requires_key": True,
        "env_key": "OPENROUTER_API_KEY", "supports_vision": False, "speed": 3
    },
    "cerebras": {
        "label": "Cerebras", "base": "https://api.cerebras.ai/v1",
        "model": "llama3.1-70b", "lanes": 4, "requires_key": True,
        "env_key": "CEREBRAS_API_KEY", "supports_vision": False, "speed": 5
    },
    "cloudflare": {
        "label": "Cloudflare Workers AI", "base": "https://api.cloudflare.com/client/v4/accounts/00000000000000000000000000000000/ai/run",
        "model": "@cf/meta/llama-3.1-8b-instruct", "lanes": 3, "requires_key": True,
        "env_key": "CLOUDFLARE_API_TOKEN", "supports_vision": False, "speed": 4,
        "account_id_env": "CLOUDFLARE_ACCOUNT_ID"
    },
    "github": {
        "label": "GitHub Models", "base": "https://models.inference.ai.azure.com",
        "model": "gpt-4o-mini", "lanes": 3, "requires_key": True,
        "env_key": "GITHUB_API_KEY", "supports_vision": True, "speed": 3
    },
    # === Zero-Auth Free Endpoints (HIVE Deep Hunt Discoveries) ===
    "lmsys_arena": {
        "label": "LMSYS Arena (anonymous frontier)", "base": "https://chat.lmsys.org/queue/join",
        "model": "claude-3-sonnet", "lanes": 2, "requires_key": False,
        "full_url": True, "supports_vision": False, "speed": 3,
        "fn_index": 104, "streaming_sse": True, "session_required": True
    },
    "duckduckgo_chat": {
        "label": "DuckDuckGo AI Chat", "base": "https://duckduckgo.com/chat",
        "model": "auto", "lanes": 2, "requires_key": False,
        "full_url": True, "supports_vision": False, "speed": 4,
        "x_vqd_header": True
    },
    "phind": {
        "label": "Phind (free, code-focused)", "base": "https://phind.com/api/infer",
        "model": "Phind-70B", "lanes": 2, "requires_key": False,
        "full_url": True, "supports_vision": False, "speed": 4
    },
    "huggingface_spaces": {
        "label": "HuggingFace Spaces (public)", "base": "https://huggingface.co/api/spaces",
        "model": "auto", "lanes": 3, "requires_key": False,
        "full_url": True, "supports_vision": False, "speed": 3
    },
    "gpt4free": {
        "label": "GPT4Free proxy (g4f)", "base": "http://localhost:1337/v1/chat/completions",
        "model": "gpt-4", "lanes": 2, "requires_key": False,
        "full_url": True, "supports_vision": False, "speed": 3
    },
    "deepseek_web": {
        "label": "DeepSeek Chat (free)", "base": "https://chat.deepseek.com/api/v0/chat/completions",
        "model": "deepseek-chat", "lanes": 2, "requires_key": False,
        "full_url": True, "supports_vision": False, "speed": 4
    },
}

PROVIDER_PRIORITY = ["lmsys_arena", "duckduckgo_chat", "phind", "groq", "cerebras", "gemini", "cloudflare", "github", "openrouter", "huggingface_spaces", "gpt4free", "deepseek_web", "pollinations"]

class Provider:
    def __init__(self, pid, key="", model=None):
        p = PRESETS[pid]
        self.id, self.label, self.key = pid, p["label"], key
        if pid == "cloudflare":
            acct = os.environ.get(p.get("account_id_env", ""), "")
            self.url = "https://api.cloudflare.com/client/v4/accounts/" + acct + "/ai/run/" + p["model"]
        elif p.get("full_url"):
            self.url = p["base"]
        else:
            self.url = p["base"].rstrip("/") + "/chat/completions"
        self.model = model or p["model"]
        self.lanes = threading.Semaphore(p["lanes"])
        self.nlanes = p["lanes"]
        self.cool_until, self.fails = 0.0, 0
        self.supports_vision = p.get("supports_vision", False)
        self.total_calls = 0
        self.total_errors = 0
        self.avg_latency = 0.0

    def call(self, messages, temperature, max_tokens):
        body = {"model": self.model, "messages": messages, "temperature": temperature, "max_tokens": max_tokens}
        if self.id == "pollinations":
            body["seed"] = random.randint(1, 10**9)
        if self.id == "cloudflare":
            body = {"messages": messages, "temperature": temperature, "max_tokens": max_tokens}
        headers = {"Content-Type": "application/json", "User-Agent": "hive-swarm/4.0"}
        if self.key:
            headers["Authorization"] = "Bearer " + self.key
        req = urllib.request.Request(self.url, data=json.dumps(body).encode(), headers=headers, method="POST")
        t0 = time.time()
        with urllib.request.urlopen(req, timeout=180) as r:
            j = json.loads(r.read())
        latency = (time.time() - t0) * 1000
        self.total_calls += 1
        self.avg_latency = (self.avg_latency * (self.total_calls - 1) + latency) / self.total_calls
        try:
            db_exec(
                "INSERT OR REPLACE INTO provider_stats (provider_id, total_calls, avg_latency_ms, last_used) VALUES (?, ?, ?, ?)",
                (self.id, self.total_calls, self.avg_latency, datetime.now().isoformat())
            )
        except Exception:
            pass
        if self.id == "cloudflare":
            return (j.get("result", {}).get("response") or "").strip()
        return (j["choices"][0]["message"].get("content") or "").strip()


SHARED = []
for pid in PROVIDER_PRIORITY:
    p = PRESETS[pid]
    if not p.get("requires_key", False):
        SHARED.append(Provider(pid))
    else:
        key = os.environ.get(p.get("env_key", pid.upper() + "_API_KEY"), "")
        if key:
            SHARED.append(Provider(pid, key))

if not SHARED:
    SHARED.append(Provider("pollinations"))


# ============================================================================ NEVES Bridge Worker Integration
# Connect to the local NEVES bridge workers for specialized execution:
# - WorkBuddy: repository execution (code, tests, git ops)
# - CodeRabbit: AI code review
# - Founder Chat: local AI chat via bridge-bot JWT
# - Model Router (:8819): local router with auto-free model selection

BRIDGE_ROOT = os.environ.get("NEVES_BRIDGE_ROOT", "/home/rentflowv6/neves-bridge")
BRIDGE_OUTBOX = os.path.join(BRIDGE_ROOT, "bridge", "outbox", "workbuddy")
BRIDGE_RESULTS = os.path.join(BRIDGE_ROOT, "bridge", "results", "workbuddy")
FOUNDER_CHAT_URL = os.environ.get("FOUNDER_CHAT_URL", "http://127.0.0.1:3000")
FOUNDER_TOKEN_FILE = os.path.join(os.path.expanduser("~"), "neves-atlas-rentflow", ".bridge-bot-token")
MODEL_ROUTER_URL = os.environ.get("MODEL_ROUTER_URL", "http://127.0.0.1:8819/v1")
CODERABBIT_KEY_FILE = os.path.join(os.path.expanduser("~"), "neves-atlas-rentflow", ".coderabbit-key")

# Ensure bridge directories exist
os.makedirs(BRIDGE_OUTBOX, exist_ok=True)
os.makedirs(BRIDGE_RESULTS, exist_ok=True)


def _load_founder_token():
    """Load the bridge-bot JWT token for Founder Chat."""
    try:
        if os.path.exists(FOUNDER_TOKEN_FILE):
            return open(FOUND_TOKEN_FILE).read().strip()
    except Exception:
        pass
    return ""


def _load_coderabbit_key():
    """Load CodeRabbit API key from env or file."""
    key = os.environ.get("CODERABBIT_API_KEY", "")
    if key:
        return key
    try:
        if os.path.exists(CODERABBIT_KEY_FILE):
            return open(CODERABBIT_KEY_FILE).read().strip()
    except Exception:
        pass
    return ""


def check_workers():
    """Check which NEVES bridge workers are available."""
    workers = {
        "model_router": {"label": "Model Router (:8819)", "status": "unknown"},
        "founder_chat": {"label": "Founder Chat (:3000)", "status": "unknown"},
        "workbuddy": {"label": "WorkBuddy (repo execution)", "status": "unknown"},
        "coderabbit": {"label": "CodeRabbit (code review)", "status": "unknown"},
    }
    # Check model router
    try:
        req = urllib.request.Request(MODEL_ROUTER_URL + "/models", headers={"User-Agent": "hive-swarm/4.0"}, method="GET")
        with urllib.request.urlopen(req, timeout=3) as r:
            j = json.loads(r.read())
            models = len(j.get("data", []))
            workers["model_router"]["status"] = "active"
            workers["model_router"]["models"] = models
    except Exception as e:
        workers["model_router"]["status"] = "unavailable"
        workers["model_router"]["error"] = str(e)[:100]
    # Check founder chat
    try:
        req = urllib.request.Request(FOUNDER_CHAT_URL + "/health", headers={"User-Agent": "hive-swarm/4.0"}, method="GET")
        with urllib.request.urlopen(req, timeout=3) as r:
            j = json.loads(r.read())
            workers["founder_chat"]["status"] = "active"
            workers["founder_chat"]["version"] = j.get("version", "?")
    except Exception as e:
        workers["founder_chat"]["status"] = "unavailable"
        workers["founder_chat"]["error"] = str(e)[:100]
    # Check workbuddy (look for result files / service)
    try:
        outbox_exists = os.path.isdir(BRIDGE_OUTBOX)
        results_exists = os.path.isdir(BRIDGE_RESULTS)
        if outbox_exists and results_exists:
            workers["workbuddy"]["status"] = "ready"
            workers["workbuddy"]["outbox"] = BRIDGE_OUTBOX
            workers["workbuddy"]["results"] = BRIDGE_RESULTS
        else:
            workers["workbuddy"]["status"] = "no_dirs"
    except Exception as e:
        workers["workbuddy"]["status"] = "error"
        workers["workbuddy"]["error"] = str(e)[:100]
    # Check coderabbit
    key = _load_coderabbit_key()
    if key:
        workers["coderabbit"]["status"] = "configured"
    else:
        workers["coderabbit"]["status"] = "no_key"
    return workers


def call_model_router(messages, model="auto-free", temperature=0.6, max_tokens=1800, timeout=120):
    """Call the local NEVES Model Router (:8819) as an AI provider."""
    body = json.dumps({
        "model": model,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
    }).encode()
    req = urllib.request.Request(
        MODEL_ROUTER_URL + "/chat/completions",
        data=body,
        headers={"Content-Type": "application/json", "User-Agent": "hive-swarm/4.0"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        j = json.loads(r.read())
    if "error" in j:
        raise Exception(j.get("error", "model router error"))
    return (j["choices"][0]["message"].get("content") or "").strip()


def call_founder_chat(message, timeout=60):
    """Send a message to Founder Chat via bridge-bot JWT."""
    token = _load_founder_token()
    if not token:
        return {"error": "No bridge-bot token found at " + FOUNDER_TOKEN_FILE}
    payload = json.dumps({"message": message}).encode()
    req = urllib.request.Request(
        FOUNDER_CHAT_URL + "/api/chat",
        data=payload,
        headers={
            "Content-Type": "application/json",
            "Authorization": "Bearer " + token,
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            j = json.loads(r.read())
            return {"response": j.get("text", j.get("response", "")), "raw": j}
    except urllib.error.HTTPError as e:
        if e.code == 401:
            return {"error": "Token expired — regenerate .bridge-bot-token"}
        return {"error": "HTTP " + str(e.code)}
    except Exception as e:
        return {"error": str(e)}


def dispatch_workbuddy(objective, context=None, allowed_files=None, timeout=300):
    """Dispatch a task to WorkBuddy via the bridge outbox pattern."""
    envelope = {
        "id": "hive-" + uuid.uuid4().hex[:12],
        "objective": objective,
        "context": context or {},
        "allowed": allowed_files or [],
        "forbidden": [],
        "expected_outputs": [],
        "from": "hive",
        "root_id": "hive-" + uuid.uuid4().hex[:12],
        "created_at": datetime.now().isoformat(),
        "timeout": timeout,
    }
    out_path = os.path.join(BRIDGE_OUTBOX, envelope["id"] + ".json")
    with open(out_path, "w") as f:
        json.dump(envelope, f)
    return envelope["id"]


def poll_workbuddy_result(envelope_id, timeout=300, poll_interval=2.0):
    """Poll for a WorkBuddy result. Returns the result dict or None on timeout."""
    result_file = os.path.join(BRIDGE_RESULTS, "R-" + envelope_id.split("-")[-1] + ".json")
    deadline = time.time() + timeout
    while time.time() < deadline:
        if os.path.exists(result_file):
            try:
                with open(result_file) as f:
                    return json.load(f)
            except Exception:
                pass
        # Also check for any result file matching the pattern
        for fn in os.listdir(BRIDGE_RESULTS):
            if envelope_id[:12] in fn or envelope_id[-12:] in fn:
                try:
                    with open(os.path.join(BRIDGE_RESULTS, fn)) as f:
                        data = json.load(f)
                        if data.get("agent_envelope_id") == envelope_id:
                            return data
                except Exception:
                    pass
        time.sleep(poll_interval)
    return None


def call_coderabbit(code, filepath="review.py"):
    """Send code to CodeRabbit for review."""
    key = _load_coderabbit_key()
    if not key:
        return {"error": "No CodeRabbit key. Set CODERABBIT_API_KEY or save to .coderabbit-key"}
    body = json.dumps({"file": filepath, "content": code[:50000]}).encode()
    req = urllib.request.Request(
        "https://api.coderabbit.ai/api/v1/review",
        data=body,
        headers={
            "Content-Type": "application/json",
            "x-coderabbitai-api-key": key,
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        return {"error": "HTTP " + str(e.code), "message": e.read().decode()[:200]}
    except Exception as e:
        return {"error": str(e)}


# Auto-add Model Router as a provider if reachable
try:
    req = urllib.request.Request(MODEL_ROUTER_URL + "/models", headers={"User-Agent": "hive-swarm/4.0"}, method="GET")
    with urllib.request.urlopen(req, timeout=3) as r:
        j = json.loads(r.read())
        if j.get("data"):
            # Add model router as a special local provider
            class ModelRouterProvider(Provider):
                def __init__(self):
                    self.id = "model_router"
                    self.label = "NEVES Model Router (local)"
                    self.key = ""
                    self.url = MODEL_ROUTER_URL + "/chat/completions"
                    self.model = "auto-free"
                    self.lanes = threading.Semaphore(4)
                    self.nlanes = 4
                    self.cool_until = 0.0
                    self.fails = 0
                    self.supports_vision = False
                    self.total_calls = 0
                    self.total_errors = 0
                    self.avg_latency = 0.0

                def call(self, messages, temperature, max_tokens):
                    body = json.dumps({
                        "model": "auto-free",
                        "messages": messages,
                        "temperature": temperature,
                        "max_tokens": max_tokens,
                    }).encode()
                    headers = {"Content-Type": "application/json", "User-Agent": "hive-swarm/4.0"}
                    req = urllib.request.Request(self.url, data=body, headers=headers, method="POST")
                    t0 = time.time()
                    with urllib.request.urlopen(req, timeout=180) as r:
                        j = json.loads(r.read())
                    latency = (time.time() - t0) * 1000
                    self.total_calls += 1
                    self.avg_latency = (self.avg_latency * (self.total_calls - 1) + latency) / self.total_calls
                    if "error" in j:
                        raise Exception(str(j.get("error", "router error")))
                    return (j["choices"][0]["message"].get("content") or "").strip()

            # Insert model router at the beginning of SHARED (highest priority for local)
            SHARED.insert(0, ModelRouterProvider())
            print("   Model Router (:8819) integrated as primary provider")
except Exception as e:
    print("   Model Router not available: " + str(e)[:80])


def chat_llm(messages, temperature=0.6, max_tokens=1800, who="agent", stop=None, log=None):
    last_err, attempt = "no providers", 0
    while attempt < 20:
        if stop and stop.is_set():
            raise RuntimeError("stopped")
        now = time.time()
        ready = [p for p in SHARED if p.cool_until <= now]
        if not ready:
            time.sleep(min(1.0, max(0.1, min(p.cool_until for p in SHARED) - now)))
            continue
        ready.sort(key=lambda p: (p.id == "pollinations", p.fails, -p.nlanes, random.random()))
        prov = next((p for p in ready if p.lanes.acquire(blocking=False)), None)
        if not prov:
            time.sleep(0.1)
            continue
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
            last_err = prov.label + " HTTP " + str(e.code)
            prov.total_errors += 1
            if e.code in (401, 403) and prov.key:
                prov.cool_until = time.time() + 3600
            elif e.code == 429 or e.code >= 500:
                prov.cool_until = time.time() + min(3 * attempt, 25)
            else:
                prov.fails += 1
                prov.cool_until = time.time() + 8
        except Exception as e:
            last_err = prov.label + ": " + str(e)
            prov.fails += 1
            prov.total_errors += 1
            prov.cool_until = time.time() + 4
        finally:
            prov.lanes.release()
        if log and attempt in (5, 10, 15):
            log(who + ": AI provider busy — backing off, then retrying...", "a")
    raise RuntimeError(last_err)


# ============================================================================ Task Classification & Specialized Prompts
TASK_CATEGORIES = {
    "web_research": {
        "emoji": "...", "label": "Web Research",
        "system": "You are a world-class research analyst. Synthesize findings from provided sources. Cite all facts with [n]. Be specific with numbers, dates, names."
    },
    "data_analysis": {
        "emoji": "...", "label": "Data Analysis",
        "system": "You are a data scientist. Analyze the provided data, find patterns, calculate statistics, and generate actionable insights. Use [[calc: expr]] for all math."
    },
    "content_writing": {
        "emoji": "...", "label": "Content Writing",
        "system": "You are a professional copywriter and content creator. Write compelling, well-structured content tailored to the audience and purpose specified."
    },
    "code_generation": {
        "emoji": "...", "label": "Code Generation",
        "system": "You are a senior software engineer. Write clean, complete, production-ready code with comments. Explain your approach and include error handling."
    },
    "math_calc": {
        "emoji": "...", "label": "Math & Calculations",
        "system": "You are a mathematician and quantitative analyst. Show all work using [[calc: expr]] notation. Explain each step clearly."
    },
    "competitive_research": {
        "emoji": "...", "label": "Competitive Research",
        "system": "You are a competitive intelligence analyst. Compare features, pricing, market position, and strategies. Create comparison tables when useful."
    },
    "property_research": {
        "emoji": "...", "label": "Property/Market Research",
        "system": "You are a real estate market analyst. Provide market data, rent estimates, cap rates, neighborhood analysis, and investment insights."
    },
    "image_analysis": {
        "emoji": "...", "label": "Image Analysis",
        "system": "You are a visual analysis expert. Describe images in detail, identify elements, and provide context and interpretation."
    },
    "translation": {
        "emoji": "...", "label": "Translation",
        "system": "You are a professional translator. Provide accurate, natural translations preserving tone and meaning. Note any cultural nuances."
    },
    "summarization": {
        "emoji": "...", "label": "Summarization",
        "system": "You are an expert summarizer. Extract key points, maintain accuracy, and preserve the most important information in a condensed format."
    },
    "planning": {
        "emoji": "...", "label": "Planning",
        "system": "You are a strategic planner. Create detailed action plans with timelines, milestones, resource requirements, and risk mitigation."
    },
    "creative": {
        "emoji": "...", "label": "Creative Tasks",
        "system": "You are a creative director. Generate innovative ideas, names, concepts, and strategies. Think outside the box while staying practical."
    },
    "due_diligence": {
        "emoji": "...", "label": "Due Diligence",
        "system": "You are a due diligence investigator. Research thoroughly, verify facts, identify risks and opportunities, and provide balanced assessments."
    },
    "lead_generation": {
        "emoji": "...", "label": "Lead Generation",
        "system": "You are a business development expert. Identify potential customers, partners, or opportunities with contact details and qualification criteria."
    },
    "report_generation": {
        "emoji": "...", "label": "Report Generation",
        "system": "You are a professional report writer. Create well-structured reports with executive summary, findings, analysis, recommendations, and sources."
    },
}

def classify_task(goal):
    goal_lower = goal.lower()
    patterns = {
        "web_research": [r"\b(research|investigate|find out|look up|scrape|search for)\b"],
        "data_analysis": [r"\b(analyze|data|csv|statistics|trend|numbers|metrics|chart)\b"],
        "content_writing": [r"\b(write|article|blog|email|copy|content|essay|report draft)\b"],
        "code_generation": [r"\b(code|program|function|script|api|debug|build.*app|develop)\b"],
        "math_calc": [r"\b(calculate|math|formula|compute|equation|financial model|roi|profit margin)\b"],
        "competitive_research": [r"\b(competitor|compare|vs|market share|benchmark|competitive)\b"],
        "property_research": [r"\b(property|real estate|rent|housing|mortgage|cap rate|zillow|listing)\b"],
        "image_analysis": [r"\b(image|photo|picture|visual|screenshot|describe.*img)\b"],
        "translation": [r"\b(translate|translation|language|spanish|french|german|chinese|japanese)\b"],
        "summarization": [r"\b(summarize|summary|condense|tl;dr|brief|overview of)\b"],
        "planning": [r"\b(plan|timeline|sop|roadmap|schedule|milestone|gantt|project plan)\b"],
        "creative": [r"\b(brainstorm|name|brand|creative|idea|concept|innovative|design)\b"],
        "due_diligence": [r"\b(due diligence|vet|background|verify|investigate.*company|audit)\b"],
        "lead_generation": [r"\b(lead|prospect|contact|find.*business|b2b|pipeline|outreach list)\b"],
        "report_generation": [r"\b(full report|comprehensive report|formal report|white paper|case study)\b"],
    }
    scores = {}
    for category, regexes in patterns.items():
        score = sum(1 for r in regexes if re.search(r, goal_lower))
        if score > 0:
            scores[category] = score
    if scores:
        return max(scores, key=scores.get)
    return "web_research"


# ============================================================================ Tools
OPS = {ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul, ast.Div: operator.truediv,
        ast.Pow: operator.pow, ast.Mod: operator.mod, ast.FloorDiv: operator.floordiv, ast.USub: operator.neg, ast.UAdd: operator.pos}
FUN = {"round": round, "min": min, "max": max, "abs": abs, "sqrt": math.sqrt, "log": math.log,
        "ceil": math.ceil, "floor": math.floor, "sin": math.sin, "cos": math.cos, "tan": math.tan}


def safe_eval(expr):
    expr = expr.replace(",", "").replace("$", "").replace("%", "/100").replace("x", "*").replace("^", "**")
    def ev(n):
        if isinstance(n, ast.Expression):
            return ev(n.body)
        if isinstance(n, ast.Constant) and isinstance(n.value, (int, float)):
            return n.value
        if isinstance(n, ast.BinOp) and type(n.op) in OPS:
            a, b = ev(n.left), ev(n.right)
            if isinstance(n.op, ast.Pow) and abs(b) > 12:
                raise ValueError("too big")
            return OPS[type(n.op)](a, b)
        if isinstance(n, ast.UnaryOp) and type(n.op) in OPS:
            return OPS[type(n.op)](ev(n.operand))
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id in FUN:
            return FUN[n.func.id](*[ev(a) for a in n.args])
        raise ValueError("unsupported")
    v = ev(ast.parse(expr.strip(), mode="eval"))
    return f"{v:,.2f}".rstrip("0").rstrip(".") if isinstance(v, float) else f"{v:,}"


def run_calcs(text):
    n = [0]
    def rep(m):
        try:
            n[0] += 1
            return "**" + safe_eval(m.group(1)) + "**"
        except Exception:
            return m.group(1)
    return re.sub(r"\[\[\s*calc\s*:\s*([^\]]+?)\s*\]\]", rep, text), n[0]


NUM_RE = re.compile(r"(?<![\w.])\$?\d[\d,]*(?:\.\d+)?\s?(?:%|[kKmMbB])?")


def norm_nums(s):
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
        if re.fullmatch(r"(19|20)\d\d", d):
            continue
        out.add(d)
    return out


# ============================================================================ Swarm Brain
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
DEPTH = {
    "fast": {"loops": 0, "subswarms": 0, "read": 1, "verify_fetch": 6},
    "accurate": {"loops": 1, "subswarms": 2, "read": 2, "verify_fetch": 14},
    "max": {"loops": 2, "subswarms": 4, "read": 3, "verify_fetch": 24},
}


def rules(web):
    r = ("NEVER-QUIT RULES: Never say something is impossible, that you can't help, or tell the user to ask someone else. "
         "If a path is blocked, find a workaround — an alternative method, free tool, partial solution or creative route — and deliver it. "
         "Only legal, ethical methods (no scams, spam, fake reviews or ToS violations) — within that, be relentlessly resourceful.\n"
         "SAFETY / AUTHORIZATION: Use only lawfully accessible information. Do not break into systems, bypass logins/paywalls, steal credentials, deploy malware, evade detection, dox or surveil private people, or claim access to classified data. For cybersecurity, limit work to defensive guidance or systems the user is authorized to test; for intelligence, use traceable public sources and label uncertainty. Never initiate account access, payments, trades or money movement. If asked for an unsafe action, offer a concrete lawful alternative.\n"
         "TOOLS you can use inside your answer:\n"
         "- For ANY arithmetic write [[calc: expression]] e.g. [[calc: 25*4*4.3]] — it is computed exactly. Never do math in your head.\n")
    if web:
        r += ("- If you lack data, add a final line  NEED: <specific web search query>  (max 2) and you'll get fresh research.\n"
              "- If a part of your task is too big for you alone, add a final line  HELP: <sub-task for a helper swarm>  (max 1).\n"
              "TRUTH RULES: Use ONLY facts/numbers/prices/names found in the RESEARCH and cite them like [3]. If you need an unsourced "
              "number write 'estimate:' and state the assumption. Never invent statistics, quotes, companies or URLs. Today is " + TODAY() + ".")
    return r


MISSIONS = {}
_missions_lock = threading.Lock()


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
                      "verified": 0, "claims": 0, "uncited": 0, "agents": 0, "tokens_used": 0}
        self.started = time.time()
        self.depth = DEPTH.get(cfg.get("depth", "accurate"), DEPTH["accurate"])
        self.sub_budget = self.depth["subswarms"]
        self.final = ""
        self.task_category = classify_task(cfg.get("goal", ""))

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
            self.stats["tokens_used"] += max_tokens
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
                        if s.get(k):
                            ex[k] = s[k]
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
                if src.get("excerpt") and not src.get("text"):
                    src["text"] = src["excerpt"]
                mapped = self.add_sources([src])
                if mapped:
                    gid, _ = mapped[0]
                    src = {**src, "id": gid}
                    self.emit(type="source", stage=stage, source=src, owner_i=ev.get("owner_i", owner_i))
                    self.push_stats()
            elif kind == "search":
                self.emit(type="search", owner_i=ev.get("owner_i", owner_i),
                          **{k: v for k, v in ev.items() if k != "owner_i"})
        ctx, srcs = websearch.research(queries, read_top=read_top, with_news=True, on_event=on_event, owner_i=owner_i)
        mapping = {s["id"]: gid for gid, s in self.add_sources(srcs)}
        ctx = re.sub(r"\[(\d+)\]", lambda m: "[" + str(mapping.get(int(m.group(1)), m.group(1))) + "]", ctx)
        self.push_stats()
        return ctx, len(srcs)

    def plan_agents(self, goal, size, mode, shared, existing=None, who="Queen"):
        agents = []
        existing = existing or []
        run_memory = memory_for(goal, mode)
        task_info = TASK_CATEGORIES.get(self.task_category, {})
        while len(agents) < size and not self.stop.is_set():
            need = min(12, size - len(agents))
            have = "\n".join(f"- {a['name']}: {a['task']}" for a in existing + agents)
            raw = self.llm(
                "You are the QUEEN orchestrator of an AI swarm. Reply with ONLY valid JSON, no prose.",
                "MISSION: " + goal + "\n\nTASK CATEGORY: " + task_info.get("label", "General") + "\n\n"
                + (("LIVE WEB SCOUTING:\n" + shared[:3500]) if shared else "") + "\n\n"
                + "PRIOR RUN METRICS (historical only, NOT evidence): " + (run_memory or "(no similar run metrics)") + "\n"
                + "Use memory only to avoid duplicated angles and improve evidence coverage; re-check every changing fact now.\n"
                + "Design " + str(need) + " specialist agents forming " + MODES.get(mode, MODES["team"]) + ".\n"
                + (("These agents ALREADY exist — new ones must cover DIFFERENT ground:\n" + have + "\n") if have else "")
                + "Each gets ONE distinct, concrete sub-task; together they fully cover the mission.\n"
                + 'JSON: {"agents":[{"name":"short unique name","emoji":"one emoji","role":"job title",'
                '"task":"1-2 sentence instruction","query":"one specific web search query"}]}',
                who, 0.6, 3000)
            got = (extract_json(raw) or {}).get("agents") or [{"name": "Agent " + str(len(agents)+1), "emoji": "...", "role": "Specialist",
                                                                "task": "Cover an important uncovered angle of: " + goal, "query": goal}]
            for a in got[:need]:
                agents.append({"name": str(a.get("name") or ("Agent " + str(len(agents)+1)))[:40], "emoji": str(a.get("emoji") or "...")[:4],
                               "role": str(a.get("role") or "")[:60], "task": str(a.get("task") or "")[:400],
                               "query": str(a.get("query") or goal)[:150]})
        return agents[:size]

    def work(self, i, goal, roster, shared, web, level=0):
        a = self.agents[i]
        if self.stop.is_set():
            return
        a["status"] = "researching"; self.push_agent(i)
        own = ""
        if web:
            try:
                own, n = self.research([a["query"]], read_top=self.depth["read"], owner_i=i)
                self.log(a["emoji"] + " " + a["name"] + " found " + str(n) + " unique web results; opened up to " + str(self.depth["read"]) + " pages")
            except Exception as e:
                self.log(a["name"] + " search issue: " + str(e) + " — continuing with shared intel", "a")
        a["status"] = "working"; self.push_agent(i)
        task_sys = TASK_CATEGORIES.get(self.task_category, {}).get("system", "")
        system = ("You are " + a["name"] + ", the " + a["role"] + " in an elite AI swarm — world-class at your specialty. "
                  "Stay on your sub-task; other agents cover the rest. Use markdown.\n" + task_sys + "\n" + rules(web))
        base = ("MISSION: " + goal + "\n\nSWARM ROSTER:\n" + roster + "\n\nYOUR SUB-TASK: " + a["task"] + "\n\n"
                + (("RESEARCH (live web, " + TODAY() + "):\n" + own[:7000] + "\n\nSHARED INTEL:\n" + shared[:2500] + "\n\n") if web else ""))
        try:
            out = self.llm(system, base + "Deliver your best concrete, specific contribution (250-500 words; cited numbers; exact steps, tools, prices; code if relevant).",
                           a["name"], pulse=i)
            for loop in range(self.depth["loops"] + 1):
                needs = re.findall(r"^\s*NEED:\s*(.+)$", out, re.M)[:2]
                helps = re.findall(r"^\s*HELP:\s*(.+)$", out, re.M)[:1]
                if not (needs or helps) or self.stop.is_set():
                    break
                extra = ""
                if needs and web and loop < self.depth["loops"]:
                    a["status"] = "researching"; self.push_agent(i)
                    more, n = self.research(needs, read_top=1, owner_i=i)
                    self.log(a["emoji"] + " " + a["name"] + " follow-up search: " + " · ".join(needs) + " (+" + str(n) + " results)")
                    extra += "\nFOLLOW-UP RESEARCH:\n" + more[:6000] + "\n"
                if helps and level == 0 and self.sub_budget > 0:
                    self.sub_budget -= 1
                    extra += "\nHELPER SWARM RESULTS:\n" + self.subswarm(i, helps[0], goal, web) + "\n"
                if not extra:
                    break
                a["status"] = "working"; self.push_agent(i)
                out = self.llm(system, base + "YOUR DRAFT:\n" + out + "\n" + extra + "\nNow write your improved, complete final contribution using the new material. "
                               "Only add NEED:/HELP: lines if still truly necessary.", a["name"], pulse=i)
            a["output"] = re.sub(r"^\s*(NEED|HELP):.*$", "", out, flags=re.M).strip()
            a["status"] = "done"
        except Exception as e:
            if str(e) == "stopped":
                return
            a["status"], a["output"] = "error", "_Failed: " + str(e) + "_"
        self.push_agent(i)

    def subswarm(self, parent, subtask, goal, web):
        p = self.agents[parent]
        with self.lock:
            self.stats["subswarms"] += 1
        self.push_stats()
        self.log("... " + p["name"] + " called a helper swarm: \"" + subtask[:90] + "\"", "g")
        self.emit(type="subswarm", parent=parent, task=subtask)
        specs = self.plan_agents(subtask + "\n(Context — part of the larger mission: " + goal + ")", 3, self.cfg.get("mode", "team"), "", who=p["name"]+"-sub")
        ids = [self.new_agent(s, parent) for s in specs]
        roster = "\n".join(f"- {s['name']} ({s['role']}): {s['task']}" for s in specs)
        with ThreadPoolExecutor(3) as ex:
            list(ex.map(lambda i: self.work(i, subtask, roster, "", web, level=1), ids))
        return "\n\n".join("### " + self.agents[i]["name"] + " (" + self.agents[i]["role"] + ")\n" + self.agents[i]["output"][:2500] for i in ids)

    def fact_check(self, text):
        cited = sorted(set(int(x) for x in re.findall(r"\[(\d+)\]", text)))
        weak = [s for s in self.sources if s["id"] in cited and len(s.get("text", "")) < 800][:self.depth["verify_fetch"]]
        if weak:
            self.log("... Opening " + str(len(weak)) + " cited pages for numeric cross-checks...")
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
                return "..."
            src_text = " ".join(by_id[k].get("text", "") for k in ids if k in by_id)
            matched = len(nums.intersection(norm_nums(src_text)))
            if matched >= max(1, math.ceil(len(nums) / 2)):
                verified += 1
                return "OK"
            return "..."

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
                if all(re.fullmatch(r"[:\\-\\s]+", c or "-") for c in cells):
                    out_lines.append(line)
                    continue
                if table_headers is None:
                    table_headers = cells
                    out_lines.append(line)
                    continue
                check_cells = list(cells)
                if table_headers and table_headers[0].lower().strip() in ("rank", "#", "no."):
                    check_cells[0] = ""
                mark = check_claim(" ".join(check_cells))
                if mark:
                    line = line.rstrip()
                    line = line[:-1].rstrip() + " " + mark + " |"
                out_lines.append(line)
                continue
            table_headers = None
            if not line.strip():
                out_lines.append(line)
                continue
            parts = re.split(r"(?<=[.!?])\s+", line)
            rendered = []
            for sent in parts:
                tested = re.sub(r"^\s*(?:[-*+]\s+|\d+[.)]\s+)", "", sent)
                mark = check_claim(tested)
                rendered.append(sent + (" " + mark if mark else ""))
            out_lines.append(" ".join(rendered))
        self.stats["verified"], self.stats["claims"], self.stats["uncited"] = verified, claims, uncited
        self.push_stats()
        return "\n".join(out_lines), verified, claims, uncited

    def run(self):
        c = self.cfg
        goal = c["goal"]
        size = max(1, min(50, int(c.get("size", 5))))
        rounds = int(c.get("rounds", 1))
        mode = c.get("mode", "team")
        style = c.get("style", "detailed")
        web = c.get("web", True)
        lanes = sum(p.nlanes for p in SHARED)
        self.log("Mission -> " + str(size) + " agents | depth " + c.get("depth", "accurate") + " | category: " + TASK_CATEGORIES.get(self.task_category, {}).get("label", "General") + " | web " + ("ON" if web else "OFF"), "g")
        try:
            self.emit(type="phase", p="plan")
            shared = ""
            if web:
                self.log("... Queen scouting the live web...")
                qraw = self.llm("You write web search queries. Reply ONLY JSON.",
                                'MISSION: ' + goal + '\nWrite 3 short, specific search queries that give the most useful real-world facts '
                                '(prices, demand, competitors, official rules). JSON: {"queries":["..."]}', "Queen", 0.3, 300, pulse=-1)
                qs = (extract_json(qraw) or {}).get("queries") or [goal]
                shared, n = self.research(qs[:3], read_top=2, owner_i=-1)
                fx = websearch.fx_rates() if mode == "money" or re.search(r"\b(fx|forex|currency|exchange rate|foreign exchange)\b", goal, re.I) else ""
                if fx:
                    shared = fx + "\n" + shared
                    fs = {"title": "Bank of Canada — daily exchange-rate observations",
                          "url": "https://www.bankofcanada.ca/valet/observations/FXUSDCAD,FXEURCAD,FXGBPCAD,FXCNYCAD,FXINRCAD/json?recent=1",
                          "snippet": fx, "text": fx, "query": "Bank of Canada official FX data",
                          "queries": ["Bank of Canada official FX data"],
                          "engine": "Bank of Canada Valet API", "kind": "official_api", "trust": 2,
                          "page_read": True, "observed_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                          "read_at": time.strftime("%Y-%m-%d %H:%M:%S")}
                    gid = self.add_sources([fs])[0][0]
                    self.emit(type="source", stage="read", source={**fs, "id": gid, "excerpt": fx}, owner_i=-1)
                self.push_stats()
                self.log("... Queen gathered " + str(n) + " unique web results; " + str(self.stats["pages_read"]) + " pages opened", "g")

            self.log("... Queen designing the swarm...")
            specs = self.plan_agents(goal, size, mode, shared)
            ids = [self.new_agent(s) for s in specs]
            self.log("... " + str(len(ids)) + " agents spawned.", "g")
            self.push_stats()
            roster = "\n".join(f"- {a['name']} ({a['role']}): {a['task']}" for a in specs)

            self.emit(type="phase", p="work")
            workers = max(1, min(len(ids), max(1, lanes)))
            with ThreadPoolExecutor(workers) as ex:
                list(ex.map(lambda i: self.work(i, goal, roster, shared, web), ids))
            mains = [i for i in ids if self.agents[i]["status"] == "done"]
            self.log("... Workers finished (" + str(len(mains)) + "/" + str(len(ids)) + " succeeded, " + str(self.stats["subswarms"]) + " helper swarms).", "g")

            for r in range(1, rounds + 1):
                if self.stop.is_set():
                    break
                self.emit(type="phase", p="critic")
                batches = [mains[k:k + 8] for k in range(0, len(mains), 8)]
                feedback = {}

                def crit(batch):
                    dump = "\n\n".join("### " + self.agents[i]["name"] + " (" + self.agents[i]["role"] + ")\n" + self.agents[i]["output"][:3000] for i in batch)
                    raw = self.llm("You are the CRITIC: ruthless, constructive, fact-focused. Reply ONLY JSON.",
                                   "MISSION: " + goal + "\n\nOUTPUTS:\n" + dump + "\n\nFor EACH agent: flag invented or uncited numbers, math not done with calc, "
                                   "vague advice, any 'impossible/can't' defeatism (demand a workaround), gaps and contradictions. Say exactly how to fix.\n"
                                   'JSON: {"feedback":{"<agent name>":"feedback"}}', "Critic", 0.3, 2000, pulse=-1)
                    return (extract_json(raw) or {}).get("feedback") or {}

                self.log("... Critic reviewing (round " + str(r) + "/" + str(rounds) + ")...")
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
                        out = self.llm("You are " + a["name"] + ", the " + a["role"] + ". Use markdown.\n" + rules(web),
                                       "MISSION: " + goal + "\nSUB-TASK: " + a["task"] + "\n\nYOUR DRAFT:\n" + a["output"] + "\n\nCRITIC FEEDBACK:\n" + fb + "\n\n"
                                       "Write the improved full version. Keep valid citations [n]; remove anything unsupported. No NEED/HELP lines.",
                                       a["name"], pulse=i)
                        a["output"] = re.sub(r"^\s*(NEED|HELP):.*$", "", out, flags=re.M).strip()
                    except Exception:
                        pass
                    a["status"] = "done"; self.push_agent(i)

                with ThreadPoolExecutor(workers) as ex:
                    list(ex.map(refine, mains))
                self.log("... Refinement round " + str(r) + " complete.", "g")

            self.emit(type="phase", p="synth")
            done_agents = [self.agents[i] for i in mains]
            parts = ["### " + a["name"] + " (" + a["role"] + ")\n" + a["output"][:3500] for a in done_agents]
            if len(parts) > 8:
                self.log("... Condensing " + str(len(parts)) + " reports in groups...")
                groups = [parts[k:k + 8] for k in range(0, len(parts), 8)]
                with ThreadPoolExecutor(max(1, min(len(groups), lanes))) as ex:
                    parts = list(ex.map(lambda g: self.llm(
                        "You condense expert reports without losing concrete facts, numbers, steps or citations [n]. Markdown.",
                        "MISSION: " + goal + "\n\nREPORTS:\n" + "\n\n".join(g) + "\n\nMerge into one dense brief (max 700 words), keep citations.",
                        "Synthesizer", 0.3, 1500, pulse=-1), groups))
            self.log("... Synthesizer writing the final deliverable...")
            final = self.llm(
                "You are the SYNTHESIZER of an AI swarm. Merge expert work into one superior deliverable. Resolve contradictions, "
                "remove duplication, keep every concrete insight and its citations [n]. Clean markdown.\n" + rules(web),
                "MISSION: " + goal + "\n\nSWARM WORK:\n" + "\n\n".join(parts) +
                "\n\nProduce the final deliverable as " + STYLES.get(style, STYLES["detailed"]) + ". "
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
                check_label = (str(pct) + "% of " + str(cl) + " numeric claims had matching numbers in cited source text") if cl else "N/A — no checkable numeric claims were detected"
                self.log("... Numeric cross-check: " + str(v) + "/" + str(cl) + " matched | " + str(unc) + " uncited | " + check_label + ".", "g" if pct is not None and pct >= 50 else "a")
                used = sorted(set(int(x) for x in re.findall(r"\[(\d+)\]", final)))
                src_md = "\n".join(str(s["id"]) + ". " + ("*** " if s["trust"] > 0 else "") + "[" + s["title"] + "](" + s["url"] + ")" for s in self.sources if s["id"] in used)
                final += ("\n\n---\n**Numeric cross-check:** " + str(v) + "/" + str(cl) + " claims (" + check_label + "); " + str(unc) + " uncited. "
                          "OK means the cited page text contains a matching number; WARN means uncited or not matched. This is an automated token check, not proof that a claim is true. *** is a rough domain heuristic, not an endorsement.")
                if src_md:
                    final += "\n\n## Sources (web research collected " + TODAY() + ")\n" + src_md
            self.final = final
            self.stats["elapsed_s"] = int(time.time() - self.started)
            self.push_stats()
            self.emit(type="final", md=final)
            self.log("... Mission complete in " + str(self.stats["elapsed_s"]) + "s | " + str(self.stats["calls"]) + " AI calls | " + str(len(self.sources)) + " web results.", "g")
            self.save_to_db()
            self.save()
        except Exception as e:
            self.log("Swarm stopped." if str(e) == "stopped" else "Mission issue: " + str(e), "e")
        finally:
            self.emit(type="done")

    def save(self):
        rec = {"id": self.id, "goal": self.cfg["goal"], "mode": self.cfg.get("mode"), "date": time.strftime("%Y-%m-%d %H:%M"),
               "final": self.final, "stats": dict(self.stats),
               "agents": [{k: a[k] for k in ("name", "emoji", "role", "task", "output", "parent")} for a in self.agents],
               "sources": [{k: s.get(k) for k in ("id", "title", "url", "snippet", "trust", "query", "queries", "engine", "kind", "relevance", "publisher", "publisher_url", "page_read", "observed_at", "read_at")}
                           | {"text": s.get("text", "")[:1600]} for s in self.sources]}
        with open(os.path.join(MEM_DIR, self.id + ".json"), "w") as f:
            json.dump(rec, f)

    def save_to_db(self):
        try:
            db_exec(
                "INSERT OR REPLACE INTO missions (id, goal, mode, style, depth, created_at, completed_at, final_md, stats_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (self.id, self.cfg["goal"], self.cfg.get("mode", ""), self.cfg.get("style", ""),
                 self.cfg.get("depth", ""), datetime.fromtimestamp(self.started).isoformat(),
                 datetime.now().isoformat(), self.final, json.dumps(dict(self.stats)))
            )
            for a in self.agents:
                db_exec(
                    "INSERT INTO agent_outputs (mission_id, agent_name, role, output, created_at) VALUES (?, ?, ?, ?, ?)",
                    (self.id, a.get("name", ""), a.get("role", ""), a.get("output", "")[:5000], datetime.now().isoformat())
                )
        except Exception:
            pass


def history(limit=50):
    try:
        rows = db_query(
            "SELECT id, goal, mode, style, depth, created_at, stats_json FROM missions ORDER BY created_at DESC LIMIT ?",
            (limit,)
        )
        return [{"id": r["id"], "goal": r["goal"], "mode": r["mode"], "style": r["style"],
                 "depth": r["depth"], "date": r["created_at"], "stats": json.loads(r["stats_json"] or "{}")}
                for r in rows]
    except Exception:
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
        match = str(st.get("verified", 0)) + "/" + str(claims) + " numeric text matches" if claims else "no numeric match score"
        out.append(h.get("goal", "")[:90] + ": " + str(st.get("sources", 0)) + " web results, " + str(st.get("pages_read", 0)) + " pages opened, " + match)
    return " | ".join(out)


# ============================================================================ Commander (chat agent)
COMMANDER = """You are COMMANDER, the strategic brain of HIVE — a free multi-agent AI swarm system with live web research.
You talk with the user, think like a sharp, honest, experienced entrepreneur and engineer, and you LAUNCH SWARMS to do heavy work.

Your style: direct, practical, specific; short paragraphs and bullets. For lawful tasks, find a workable route instead of giving up.
Be honest: never promise guaranteed income; point out real risks briefly, then focus on action. Use public, lawfully accessible sources only. Do not claim classified access, break into accounts or systems, evade detection, or move money. Cyber requests must stay defensive and authorized; offer a safe alternative when needed.

TASK CATEGORIES I CAN HANDLE:
Web Research | Data Analysis | Content Writing | Code Generation | Math & Calculations
Competitive Research | Property/Market Research | Image Analysis | Translation | Summarization
Planning | Creative Tasks | Due Diligence | Lead Generation | Report Generation

HOW TO LAUNCH A SWARM: when the user wants research, a plan, a build, market data, or anything that benefits from many agents and
live web data, reply with one short sentence and then a line exactly like:
LAUNCH: {"goal": "<clear, detailed mission>", "size": 8, "mode": "money", "depth": "accurate", "rounds": 1, "style": "actionable"}
modes: money | team | research | debate | code | osint (public-source intelligence) | security (defensive cyber only). depth: fast | accurate | max. size 3-50.
You may launch several swarms in one reply (one LAUNCH line each) to attack different angles in parallel.
For quick questions, just answer yourself without launching. Today is {today}.

AVAILABLE AI PROVIDERS: {providers}

NEVES BRIDGE WORKERS (specialized execution backends):
- Model Router (:8819): local auto-free model selection across Nous/Gemini/Grok/etc
- Founder Chat (:3000): local AI chat via bridge-bot JWT
- WorkBuddy: repository execution agent (~/neves-atlas-rentflow) — code, tests, git ops
- CodeRabbit: AI code review (needs CODERABBIT_API_KEY or .coderabbit-key)

HISTORICAL RUN MEMORY (metrics and prior topics only; not evidence):
Use this to avoid repeating work and suggest useful follow-ups. Re-check changing facts in current public sources; old generated reports are not verified facts.
{memory}"""


def commander_reply(messages):
    mem = []
    for h in history()[:4]:
        try:
            st = h.get("stats", {})
            den = st.get("claims", 0)
            pct = str(round(100 * st.get("verified", 0) / den)) + "%" if den else "not scored"
            mem.append("- [" + h["date"] + "] " + h["goal"][:140] + " | " + str(st.get("agents", 0)) + " agents | " + str(st.get("sources", 0)) + " web results | " + str(st.get("pages_read", 0)) + " pages read | numeric text-match " + pct)
        except Exception:
            pass
    prov_list = ", ".join(p.label + " (" + ("active" if p.cool_until <= time.time() else "cooldown") + ")" for p in SHARED)
    sysmsg = COMMANDER.replace("{today}", TODAY()).replace("{providers}", prov_list).replace("{memory}", "\n".join(mem) or "(none yet)")
    msgs = [{"role": "system", "content": sysmsg}] + [m for m in messages[-14:] if m.get("role") in ("user", "assistant")]
    reply = chat_llm(msgs, 0.6, 1500, "Commander")
    reply, _ = run_calcs(reply)
    launched = []
    for m in re.finditer(r"^\s*LAUNCH:\s*(\{.*\})\s*$", reply, re.M):
        cfg = extract_json(m.group(1))
        if cfg and cfg.get("goal"):
            cfg.setdefault("web", cfg.get("mode") != "code")
            active_count = sum(1 for m in MISSIONS.values() if not m.stop.is_set())
            if active_count >= MAX_CONCURRENT_MISSIONS:
                reply += "\n\nWARNING: Mission limit reached (" + str(MAX_CONCURRENT_MISSIONS) + " concurrent). Please wait for one to finish."
                continue
            mi = Mission(cfg)
            MISSIONS[mi.id] = mi
            threading.Thread(target=mi.run, daemon=True).start()
            launched.append({"id": mi.id, "goal": cfg["goal"], "size": cfg.get("size", 5), "mode": cfg.get("mode", "team")})
    clean = re.sub(r"^\s*LAUNCH:.*$", "", reply, flags=re.M).strip()
    return clean, launched


def quick_analyze(task_description, data=None):
    category = classify_task(task_description)
    task_info = TASK_CATEGORIES.get(category, {})
    system = "You are a " + task_info.get("label", "General") + " expert. " + task_info.get("system", "")
    user_msg = task_description
    if data:
        user_msg += "\n\nDATA:\n" + data[:8000]
    try:
        result = chat_llm([{"role": "system", "content": system}, {"role": "user", "content": user_msg}],
                          0.5, 2000, "QuickAnalysis")
        result, _ = run_calcs(result)
        return {"category": category, "result": result}
    except Exception as e:
        return {"category": category, "error": str(e)}


# ============================================================================ HTTP Server
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
        path = self.path.split("?", 1)[0]
        if path.startswith("/api/events"):
            q = self._q()
            m = MISSIONS.get(q.get("id"))
            if not m:
                return self._json(404, {"error": "unknown mission"})
            since = int(q.get("since", 0))
            deadline = time.time() + 20
            while time.time() < deadline and len(m.events) <= since:
                time.sleep(0.25)
            return self._json(200, {"events": m.events[since:]})
        if path.startswith("/api/history"):
            limit = int(self._q().get("limit", 50))
            return self._json(200, {"missions": history(limit)})
        if path.startswith("/api/load"):
            mid = re.sub(r"\W", "", self._q().get("id", ""))
            try:
                rows = db_query("SELECT * FROM missions WHERE id = ?", (mid,))
                if rows:
                    r = rows[0]
                    return self._json(200, {"id": r["id"], "goal": r["goal"], "mode": r["mode"],
                                            "final": r["final_md"], "stats": json.loads(r["stats_json"] or "{}"),
                                            "date": r["created_at"]})
                return self._json(200, json.load(open(os.path.join(MEM_DIR, mid + ".json"))))
            except Exception:
                return self._json(404, {"error": "not found"})
        if path.startswith("/api/mission/"):
            mid = path.split("/api/mission/", 1)[-1]
            m = MISSIONS.get(mid)
            if not m:
                return self._json(404, {"error": "unknown mission"})
            return self._json(200, {
                "id": m.id, "goal": m.cfg["goal"], "status": "running" if not m.stop.is_set() else "stopped",
                "stats": m.stats, "agents": len(m.agents), "sources": len(m.sources),
                "elapsed": round(time.time() - m.started, 1), "category": m.task_category
            })
        if path.startswith("/api/active"):
            return self._json(200, {"missions": [
                {"id": k, "goal": m.cfg["goal"], "category": m.task_category,
                 "done": bool(m.events and m.events[-1]["type"] == "done"),
                 "elapsed": round(time.time() - m.started, 1)}
                for k, m in MISSIONS.items()
            ]})
        if path.startswith("/api/providers"):
            providers = []
            for p in SHARED:
                stat_row = db_query("SELECT * FROM provider_stats WHERE provider_id = ?", (p.id,))
                stat = dict(stat_row[0]) if stat_row else {}
                providers.append({
                    "id": p.id, "label": p.label, "model": p.model,
                    "active": p.cool_until <= time.time(), "cooldown_remaining": max(0, round(p.cool_until - time.time(), 1)),
                    "lanes": p.nlanes, "fails": p.fails, "supports_vision": p.supports_vision,
                    "total_calls": stat.get("total_calls", 0), "errors": p.total_errors,
                    "avg_latency_ms": round(stat.get("avg_latency_ms", 0), 1)
                })
            return self._json(200, {"providers": providers})
        if path.startswith("/api/stats"):
            try:
                total = db_query("SELECT COUNT(*) as c FROM missions")[0]["c"]
                total_calls = db_query("SELECT SUM(total_calls) as s FROM provider_stats")[0]["s"] or 0
                recent = db_query("SELECT COUNT(*) as c FROM missions WHERE created_at > datetime('now', '-24 hours')")[0]["c"]
                return self._json(200, {
                    "total_missions": total, "missions_24h": recent,
                    "total_ai_calls": total_calls, "active_missions": sum(1 for m in MISSIONS.values() if not m.stop.is_set()),
                    "providers": len(SHARED), "max_concurrent": MAX_CONCURRENT_MISSIONS,
                    "workers": check_workers(),
                })
            except Exception:
                return self._json(200, {"total_missions": 0, "providers": len(SHARED)})
        if path.startswith("/api/workers"):
            workers = check_workers()
            return self._json(200, {"workers": workers})
        if path.startswith("/api/bridge/dispatch"):
            # GET with ?envelope_id=xxx to poll for result
            q = self._q()
            env_id = q.get("envelope_id")
            if env_id:
                result = poll_workbuddy_result(env_id, timeout=int(q.get("timeout", 10)), poll_interval=1.0)
                return self._json(200, {"envelope_id": env_id, "result": result, "found": result is not None})
            return self._json(400, {"error": "envelope_id required for polling"})
        if path.startswith("/api/export/"):
            mid = path.split("/api/export/", 1)[-1].split("?")[0]
            fmt = self._q().get("format", "markdown")
            try:
                rows = db_query("SELECT * FROM missions WHERE id = ?", (mid,))
                if not rows:
                    raise Exception("not found")
                r = rows[0]
                if fmt == "json":
                    return self._json(200, {"id": r["id"], "goal": r["goal"], "mode": r["mode"],
                                            "final": r["final_md"], "stats": json.loads(r["stats_json"] or "{}"),
                                            "created_at": r["created_at"]})
                md = "# " + r["goal"] + "\n\n*Generated by HIVE v4 on " + r["created_at"] + "*\n\n" + r["final_md"]
                b = md.encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/markdown; charset=utf-8")
                self.send_header("Content-Disposition", 'attachment; filename="mission_' + mid + '.md"')
                self.send_header("Content-Length", str(len(b)))
                self.end_headers()
                self.wfile.write(b)
                return
            except Exception:
                return self._json(404, {"error": "not found"})
        if path in ("/", ""):
            self.path = "/index.html"
        return super().do_GET()

    def do_DELETE(self):
        path = self.path.split("?", 1)[0]
        if path.startswith("/api/mission/"):
            mid = path.split("/api/mission/", 1)[-1]
            m = MISSIONS.pop(mid, None)
            if m:
                m.stop.set()
            try:
                os.remove(os.path.join(MEM_DIR, mid + ".json"))
                db_exec("DELETE FROM missions WHERE id = ?", (mid,))
                db_exec("DELETE FROM agent_outputs WHERE mission_id = ?", (mid,))
            except Exception:
                pass
            return self._json(200, {"ok": True})
        self._json(404, {"error": "not found"})

    def do_POST(self):
        path = self.path.split("?", 1)[0]
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
        if path.startswith("/api/mission"):
            if not str(body.get("goal", "")).strip():
                return self._json(400, {"error": "goal required"})
            active_count = sum(1 for m in MISSIONS.values() if not m.stop.is_set())
            if active_count >= MAX_CONCURRENT_MISSIONS:
                return self._json(429, {"error": "max " + str(MAX_CONCURRENT_MISSIONS) + " concurrent missions"})
            m = Mission(body)
            MISSIONS[m.id] = m
            threading.Thread(target=m.run, daemon=True).start()
            return self._json(200, {"id": m.id, "category": m.task_category})
        if path.startswith("/api/stop"):
            m = MISSIONS.get(body.get("id"))
            if m:
                m.stop.set()
            return self._json(200, {"ok": True})
        if path.startswith("/api/chat"):
            try:
                reply, launched = commander_reply(body.get("messages", []))
                return self._json(200, {"reply": reply, "launched": launched})
            except Exception as e:
                return self._json(200, {"reply": "(Commander is reconnecting: " + str(e) + ". Try again in a moment.)", "launched": []})
        if path.startswith("/api/analyze"):
            result = quick_analyze(body.get("task", ""), body.get("data"))
            return self._json(200, result)
        if path.startswith("/api/bridge/workbuddy"):
            # Dispatch a task to WorkBuddy
            envelope_id = dispatch_workbuddy(
                objective=body.get("objective", ""),
                context=body.get("context"),
                allowed_files=body.get("allowed_files"),
                timeout=body.get("timeout", 300),
            )
            return self._json(200, {"envelope_id": envelope_id, "status": "dispatched", "poll": "/api/bridge/dispatch?envelope_id=" + envelope_id})
        if path.startswith("/api/bridge/founder"):
            # Send message to Founder Chat
            result = call_founder_chat(body.get("message", ""), timeout=body.get("timeout", 60))
            return self._json(200, result)
        if path.startswith("/api/bridge/coderabbit"):
            # Send code to CodeRabbit for review
            result = call_coderabbit(body.get("code", ""), body.get("filepath", "review.py"))
            return self._json(200, result)
        self._json(404, {"error": "not found"})


if __name__ == "__main__":
    print("HIVE v4 running -> http://localhost:" + str(PORT))
    print("   Providers: " + ", ".join(p.label for p in SHARED))
    workers = check_workers()
    print("   Workers: " + ", ".join(k + "=" + v["status"] for k, v in workers.items()))
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
