#!/usr/bin/env python3
"""cost-guard: spending guardrails for Claude Code that need no admin rights.

Modes:
  cost_guard.py hook                Hook for UserPromptSubmit + PreToolUse (JSON on stdin)
  cost_guard.py sync                SessionStart hook: refresh the status line copy and saved limits
  cost_guard.py statusline          Status line (JSON on stdin)
  cost_guard.py report [--days N|--week|--month] [--by session|project|model|day] [--json]
  cost_guard.py insights [--days N]  What burns tokens on every message without being used
  cost_guard.py fix --list | apply ID… [--dry-run] | undo [last|all|ID]   Apply tips, with undo
  cost_guard.py limits [KEY VALUE]  Show the limits, or override one (KEY VALUE), or `limits reset`
  cost_guard.py context [--session ID]   What fills the context, and what to do about it
  cost_guard.py install-statusline [--force]   Point ~/.claude/settings.json statusLine at cost-guard

Limits come from, lowest to highest priority: built-in defaults, the plugin options
(set when the plugin is enabled, or under /config), then overrides set with `limits`.
Cost is estimated from the local transcripts (~/.claude/projects) at list prices.
Every error fails open: the guard never stops Claude Code because of its own bug.
"""
import glob
import json
import os
import re
import shutil
import sys
import time
import traceback
from datetime import datetime

# Plugin hooks get CLAUDE_PLUGIN_DATA. The status line copy lives in that folder itself.
DATA = os.environ.get("CLAUDE_PLUGIN_DATA") or os.path.dirname(os.path.abspath(__file__))
STATE_DIR = os.path.join(DATA, "state")
LEDGER_PATH = os.path.join(STATE_DIR, "ledger.json")
OVERRIDES_PATH = os.path.join(DATA, "overrides.json")
OPTIONS_PATH = os.path.join(DATA, "options.json")  # last plugin options seen by a hook
SETTINGS_PATH = os.path.expanduser("~/.claude/settings.json")
PROJECTS = os.path.expanduser("~/.claude/projects")
SCAN_WINDOW_S = 36 * 3600  # files touched in this window count toward "today"
SEEN_KEEP = 300
LEDGER_VERSION = 2  # bump when ledger entries gain fields; the ledger is then rebuilt
RECENT_KEEP_S = 3600  # per-message costs kept this long, for burn rate and the breaker

DEFAULT_CONFIG = {
    "session_warn_usd": [10, 25, 50],  # non-blocking alerts
    "session_cap_usd": 100,  # blocks new prompts and tool calls
    "daily_cap_usd": 150,  # all sessions on this machine, local calendar day
    "monthly_cap_usd": 300,  # all sessions on this machine, calendar month
    "monthly_hard_stop": False,  # False: past the monthly cap, warn once a day; True: stop like the others
    "warn_at_pct": 80,  # one-time warning at this % of each cap (session, day, month)
    "burst_usd_per_15min": 15,  # runaway breaker: deny one tool call when 15 minutes cost more
    "desktop_notify": True,  # desktop notification on stops and cap warnings
    "cold_cache_warn_usd": 1.0,  # block once when an idle session would re-cache more than this
    "override_word": "#override",  # put this in a prompt to continue past a cap or cold-cache stop
    "context_warn_k": 150,  # one-time advice when the context passes this many thousand tokens
    "insights_every_days": 1,  # cost tips at session start at most this often; 0 turns them off
    "insights_ignore": "",  # comma-separated plugin, skill, server or hook names to never suggest
    # Tools a cap never denies: they cost nothing and keep the app's task list and wrap-up working.
    "never_block": ["mcp__ccd_", "TodoWrite", "TaskCreate", "TaskUpdate", "TaskList", "TaskGet",
                    "ExitPlanMode", "AskUserQuestion"],
    # USD per 1M tokens: [input, output, cache read]. Cache writes: 1.25x input (5 min), 2x input (1 h).
    # Longest matching key wins, so exact model ids beat family fallbacks.
    "prices": {
        "claude-fable-5-2": [8, 40, 0.20],
        "claude-opus-5-5": [4, 20, 0.20],
        "claude-sonnet-5-5": [2, 10, 0.10],
        "claude-haiku-5-5": [0.10, 0.50, 0.01],
        "fable": [10, 50, 1.00],
        "opus": [5, 25, 0.50],
        "sonnet": [3, 15, 0.30],
        "haiku": [1, 5, 0.10],
    },
    "default_price": [5, 25, 0.50],
}


# ---------- config and state ----------

def load_json(path, default):
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return default


def save_json(path, data):
    """Atomic write, readable only by this user (state can name projects and sessions)."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = f"{path}.{os.getpid()}.tmp"
    with os.fdopen(os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w") as f:
        json.dump(data, f)
    os.replace(tmp, path)


def as_bool(val):
    text = str(val).strip().lower()
    if text in ("1", "true", "yes", "on"):
        return True
    if text in ("0", "false", "no", "off"):
        return False
    raise ValueError(val)


LIMIT_KEYS = {  # plugin option / override key -> type
    "session_cap_usd": float,
    "daily_cap_usd": float,
    "monthly_cap_usd": float,
    "warn_at_pct": float,
    "burst_usd_per_15min": float,
    "desktop_notify": as_bool,
    "monthly_hard_stop": as_bool,
    "warn_at_usd": str,  # comma-separated, e.g. "10,25,50"
    "cold_cache_warn_usd": float,
    "override_word": str,
    "context_warn_k": float,
    "insights_every_days": float,
    "insights_ignore": str,  # comma-separated
}


def plugin_options():
    """Plugin options from the hook environment, else the last copy a hook saved."""
    opts = {}
    for key in LIMIT_KEYS:
        val = os.environ.get(f"CLAUDE_PLUGIN_OPTION_{key.upper()}")
        if val not in (None, ""):
            opts[key] = val
    if opts:
        if opts != load_json(OPTIONS_PATH, None):
            save_json(OPTIONS_PATH, opts)
        return opts
    return load_json(OPTIONS_PATH, {})


def apply_limits(cfg, values):
    for key, val in values.items():
        kind = LIMIT_KEYS.get(key)
        if kind is None:
            continue
        try:
            if key == "warn_at_usd":
                cfg["session_warn_usd"] = sorted(float(x) for x in str(val).split(",") if x.strip())
            else:
                cfg[key] = kind(val)
        except ValueError:
            pass


def project_for(cwd, projects):
    """The longest project path that contains cwd, or None."""
    if not cwd:
        return None
    cwd = os.path.abspath(cwd)
    hits = [p for p in projects if cwd == p or cwd.startswith(p.rstrip(os.sep) + os.sep)]
    return max(hits, key=len) if hits else None


def load_config(cwd=None):
    """Defaults < plugin options < overrides < per-project overrides (for cwd)."""
    cfg = json.loads(json.dumps(DEFAULT_CONFIG))
    apply_limits(cfg, plugin_options())
    overrides = load_json(OVERRIDES_PATH, {})
    cfg["prices"].update(overrides.pop("prices", {}))
    cfg["never_block"] = overrides.pop("never_block", cfg["never_block"])
    projects = overrides.pop("projects", {})
    apply_limits(cfg, overrides)
    cfg["project"] = project_for(cwd, projects)
    if cfg["project"]:
        apply_limits(cfg, projects[cfg["project"]])
    return cfg


def session_state_path(sid):
    return os.path.join(STATE_DIR, "sessions", f"{sid}.json")


def today():
    return datetime.now().strftime("%Y-%m-%d")


def this_month():
    return datetime.now().strftime("%Y-%m")


def month_start_ts():
    now = datetime.now()
    return datetime(now.year, now.month, 1).timestamp()


# ---------- pricing ----------

def price_for(model, cfg):
    model = (model or "").lower()
    for key in sorted(cfg["prices"], key=len, reverse=True):
        if key in model:
            return cfg["prices"][key]
    return cfg["default_price"]


def cost_of(usage, model, cfg):
    """Return (usd, context_tokens, cache_ttl_minutes or None)."""
    p_in, p_out, p_read = price_for(model, cfg)
    inp = usage.get("input_tokens") or 0
    out = usage.get("output_tokens") or 0
    read = usage.get("cache_read_input_tokens") or 0
    write = usage.get("cache_creation_input_tokens") or 0
    split = usage.get("cache_creation") or {}
    w5 = split.get("ephemeral_5m_input_tokens")
    w1h = split.get("ephemeral_1h_input_tokens")
    if w5 is None and w1h is None:
        w5, w1h = write, 0
    w5, w1h = w5 or 0, w1h or 0
    usd = (inp * p_in + out * p_out + read * p_read + w5 * p_in * 1.25 + w1h * p_in * 2) / 1e6
    ttl = 60 if w1h else (5 if w5 else None)
    return usd, inp + read + write, ttl


# ---------- incremental transcript ledger ----------

def parse_ts(ts):
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()
    except Exception:
        return None


def scan_file(path, entry, cfg):
    """Read only the new bytes of a transcript and add their usage to the entry."""
    try:
        size = os.path.getsize(path)
    except OSError:
        return entry
    if entry is None or size < entry.get("offset", 0):  # new or rewritten file
        entry = {"offset": 0, "total": 0.0, "by_day": {}, "by_day_model": {}, "recent": [],
                 "seen": [], "last_ts": None, "last_ctx": 0, "ttl": None, "model": None, "cwd": None}
    if size == entry["offset"]:
        return entry
    with open(path, "rb") as f:
        f.seek(entry["offset"])
        chunk = f.read()
    end = chunk.rfind(b"\n")
    if end < 0:
        return entry
    seen = entry["seen"]
    seen_set = set(seen)
    for raw in chunk[: end + 1].splitlines():
        if b'"usage"' not in raw:
            continue
        try:
            e = json.loads(raw)
        except Exception:
            continue
        if e.get("type") != "assistant":
            continue
        entry["cwd"] = e.get("cwd") or entry.get("cwd")
        msg = e.get("message") or {}
        usage = msg.get("usage")
        if not usage:
            continue
        mid = msg.get("id") or e.get("requestId") or e.get("uuid")
        if mid in seen_set:  # one API response is logged once per content block
            continue
        seen.append(mid)
        seen_set.add(mid)
        model = msg.get("model")
        usd, ctx, ttl = cost_of(usage, model, cfg)
        ts = parse_ts(e.get("timestamp", "")) or time.time()
        day = datetime.fromtimestamp(ts).strftime("%Y-%m-%d")
        entry["by_day"][day] = entry["by_day"].get(day, 0.0) + usd
        models = entry["by_day_model"].setdefault(day, {})
        models[model or "?"] = models.get(model or "?", 0.0) + usd
        entry["total"] += usd
        if ts > time.time() - RECENT_KEEP_S:
            entry["recent"].append([ts, usd])
        if ctx:  # skip synthetic zero-usage entries
            entry["last_ts"], entry["last_ctx"], entry["model"] = ts, ctx, model
            if ttl:
                entry["ttl"] = ttl
    entry["seen"] = seen[-SEEN_KEEP:]
    entry["offset"] += end + 1
    return entry


def recent_spend(entries, seconds):
    cutoff = time.time() - seconds
    return sum(usd for e in entries for ts, usd in e.get("recent", []) if ts >= cutoff)


def session_files(transcript_path):
    if not transcript_path:
        return []
    files = [transcript_path]
    base = transcript_path[:-6] if transcript_path.endswith(".jsonl") else transcript_path
    files += glob.glob(os.path.join(base, "**", "*.jsonl"), recursive=True)  # subagents
    return files


def refresh(cfg, transcript_path=None, window_s=None, deadline=None):
    """Update the ledger. Returns (ledger_files, session_paths, month_complete).

    The ledger covers files touched this calendar month (or the last 36 h, or
    `window_s`, whichever reaches back further). Files not scanned yet that are older
    than 36 h are skipped once `deadline` passes, so a hook never waits on a first
    scan of the whole month; month_complete then stays False."""
    ledger = load_json(LEDGER_PATH, {})
    if ledger.get("v") != LEDGER_VERSION:
        ledger = {"v": LEDGER_VERSION, "files": {}}
    files = ledger["files"]
    now = time.time()
    since = min(now - SCAN_WINDOW_S, month_start_ts(), now - (window_s or 0))
    wanted, backlog = set(), []
    for p in glob.glob(os.path.join(PROJECTS, "**", "*.jsonl"), recursive=True):
        try:
            mtime = os.path.getmtime(p)
        except OSError:
            continue
        if mtime >= since:
            wanted.add(p)
            if p not in files and now - mtime > SCAN_WINDOW_S:
                backlog.append(p)
    mine = [p for p in session_files(transcript_path) if os.path.exists(p)]
    wanted.update(mine)
    backlog_set = set(backlog)
    complete = True
    for p in sorted(wanted, key=lambda x: x in backlog_set):  # urgent files first
        if p in backlog_set and deadline and time.time() > deadline:
            complete = False
            continue
        files[p] = scan_file(p, files.get(p), cfg)
    for p in list(files):  # keep the ledger small
        if p not in wanted:
            del files[p]
        else:  # drop per-message costs older than an hour
            files[p]["recent"] = [r for r in files[p].get("recent", []) if r[0] >= now - RECENT_KEEP_S]
    save_json(LEDGER_PATH, ledger)
    return files, mine, complete


def totals(cfg, transcript_path, deadline=None):
    """Spend so far: session, daily, monthly (None until the month is fully scanned),
    the main transcript's ledger entry, and the session's burn rate."""
    files, mine, complete = refresh(cfg, transcript_path, deadline=deadline)
    if not complete:
        scan_in_background()
    day, month = today(), this_month()
    mine_entries = [files[p] for p in mine if p in files]
    return {
        "session": sum(e["total"] for e in mine_entries),
        "daily": sum(e["by_day"].get(day, 0.0) for e in files.values()),
        "monthly": sum(v for e in files.values() for d, v in e["by_day"].items() if d.startswith(month))
        if complete else None,
        "main": files.get(transcript_path) or {},
        "rate_per_h": recent_spend(mine_entries, 1800) * 2,
        "last_15min": recent_spend(mine_entries, 900),
    }


# ---------- context analysis ----------

IMAGE_TOKENS = 1600  # rough cost of one image in context
APP_SERVERS = ("ccd_",)  # the desktop app's own tools: they can't be turned off
MCP_USAGE_DAYS = 14
SCAN_VERSION = 2  # bump when scan_period() output gains fields
SCAN_DEADLINE_S = 6  # an inline scan gives up after this; hooks time out at 10 s
INSTALLED_PLUGINS = os.path.expanduser("~/.claude/plugins/installed_plugins.json")
ATTACHMENT_GROUPS = {  # attachment type -> breakdown row. Types not listed count as "other".
    "skill_listing": "skills_list",
    "invoked_skills": "skills_loaded",
    "hook_success": "hooks", "hook_additional_context": "hooks",
    "hook_system_message": "hooks", "async_hook_response": "hooks",
    "deferred_tools_delta": "tool_lists", "deferred_tools_record": "tool_lists",
    "mcp_instructions_delta": "tool_lists", "agent_listing_delta": "tool_lists",
}
NO_PLUGIN = "(built-in and personal)"
NOT_IN_CONTEXT = {"prompt_snapshot"}  # bookkeeping copies the app keeps in the transcript


def log_error(where):
    try:
        os.makedirs(STATE_DIR, exist_ok=True)
        with open(os.path.join(STATE_DIR, "errors.log"), "a") as f:
            f.write(f"{datetime.now().isoformat()} {where}\n{traceback.format_exc()}\n")
    except Exception:
        pass


def toks(obj):
    """Rough token count: about 4 characters per token."""
    return len(obj if isinstance(obj, str) else json.dumps(obj)) // 4


def k(tokens):
    return f"{tokens / 1000:,.1f}k" if tokens < 10_000 else f"{tokens / 1000:,.0f}k"


def iter_jsonl(path):
    with open(path, "rb") as f:
        for raw in f:
            try:
                yield json.loads(raw)
            except Exception:
                continue


def server_of(tool_name):
    parts = tool_name.split("__")
    return parts[1] if tool_name.startswith("mcp__") and len(parts) >= 3 else None


def content_size(content):
    """(text tokens, image count) of a message content value."""
    if isinstance(content, str):
        return toks(content), 0
    text = images = 0
    for b in content or []:
        if not isinstance(b, dict):
            continue
        kind = b.get("type")
        if kind == "image":
            images += 1
        elif kind == "tool_result":
            t, i = content_size(b.get("content"))
            text, images = text + t, images + i
        elif kind == "text":
            text += toks(b.get("text", ""))
        else:
            text += toks(b)
    return text, images


def describe_use(name, inp):
    inp = inp if isinstance(inp, dict) else {}
    if name in ("Read", "Write", "Edit", "NotebookEdit"):
        return f"{name} {os.path.basename(str(inp.get('file_path') or inp.get('notebook_path') or ''))}"
    if name == "Bash":
        cmd = " ".join(str(inp.get("command", "")).split())
        return f"Bash `{cmd[:45]}{'…' if len(cmd) > 45 else ''}`"
    if name in ("Grep", "Glob"):
        return f"{name} {str(inp.get('pattern', ''))[:40]}"
    if name == "WebFetch":
        return f"WebFetch {str(inp.get('url', ''))[:50]}"
    if name in ("Agent", "Task"):
        return f"subagent '{str(inp.get('description', ''))[:40]}'"
    return name


class ScanTimeout(Exception):
    pass


def cached_scan(days=MCP_USAGE_DAYS, deadline=None):
    """scan_period() result, cached per day. Raises ScanTimeout past `deadline`."""
    path = os.path.join(STATE_DIR, f"scan_v{SCAN_VERSION}_{days}d.json")
    cache = load_json(path, {})
    if cache.get("day") == today() and cache.get("scan"):
        return cache["scan"]
    scan = scan_period(days, deadline)
    save_json(path, {"day": today(), "scan": scan})
    return scan


def scan_in_background():
    """Fill the scan cache in a detached process, so a hook never waits for it."""
    lock = os.path.join(STATE_DIR, "prescan.lock")
    try:
        if time.time() - os.path.getmtime(lock) < 600:
            return
    except OSError:
        pass
    try:
        os.makedirs(STATE_DIR, exist_ok=True)
        open(lock, "w").close()
        import subprocess
        subprocess.Popen([sys.executable, os.path.abspath(__file__), "prescan"],
                         env=dict(os.environ, CLAUDE_PLUGIN_DATA=DATA), stdin=subprocess.DEVNULL,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
    except Exception:
        log_error("scan_in_background")


def usage_or_none():
    """Recent tool and skill use, or None when it isn't known yet (never guess "unused")."""
    try:
        return cached_scan(deadline=time.time() + SCAN_DEADLINE_S)
    except ScanTimeout:
        scan_in_background()
        return None


def uses_by_plugin(skill_uses):
    out = {}
    for name, n in skill_uses.items():
        if ":" in name:
            plugin = name.split(":", 1)[0]
            out[plugin] = out.get(plugin, 0) + n
    return out


def parse_listing(text):
    """{skill name: tokens} from a skill listing ("- plugin:skill: description")."""
    out = {}
    for line in text.splitlines():
        if line.startswith("- "):
            out[line[2:].split(": ", 1)[0]] = toks(line)
    return out


def listing_by_plugin(listing):
    """{plugin: (tokens, skill count)}; skills without a plugin prefix go under NO_PLUGIN."""
    out = {}
    for name, t in listing.items():
        plugin = name.split(":", 1)[0] if ":" in name else NO_PLUGIN
        tot, n = out.get(plugin, (0, 0))
        out[plugin] = (tot + t, n + 1)
    return out


def norm_cmd(cmd):
    return " ".join(str(cmd).replace('"', "").split())


_HOOK_OWNERS = None


def hook_owners():
    """{normalized hook command: plugin name}, from each installed plugin's hooks/hooks.json."""
    global _HOOK_OWNERS
    if _HOOK_OWNERS is None:
        _HOOK_OWNERS = {}

        def commands(obj):
            if isinstance(obj, dict):
                for key, val in obj.items():
                    if key == "command" and isinstance(val, str):
                        yield val
                    else:
                        yield from commands(val)
            elif isinstance(obj, list):
                for val in obj:
                    yield from commands(val)

        plugins = load_json(INSTALLED_PLUGINS, {}).get("plugins") or {}
        for key, installs in plugins.items():
            for inst in installs if isinstance(installs, list) else []:
                hooks = load_json(os.path.join(inst.get("installPath", ""), "hooks", "hooks.json"), {})
                for cmd in commands(hooks):
                    _HOOK_OWNERS[norm_cmd(cmd)] = key.split("@")[0]
    return _HOOK_OWNERS


def hook_label(a):
    """Which hook produced this output: its plugin, else a short form of its command."""
    cmd = norm_cmd(a.get("command") or "")
    event = (a.get("hookName") or a.get("hookEvent") or "hook").split(":")[0]
    owner = hook_owners().get(cmd)
    if owner:
        return f"plugin {owner} ({event})"
    cmd = cmd.replace("${CLAUDE_PLUGIN_ROOT}/", "")
    if cmd:
        return f"{event}: {cmd[:40]}"
    text = a.get("content") or ""  # no command recorded: name it by what it says
    text = " ".join(text) if isinstance(text, list) else str(text)
    words = " ".join(re.sub(r"\x1b?\[[0-9;]*m|<[^>]+>|[*#`]", " ", text).split())
    return f'{event}: "{words[:32]}…"' if words else event


def hook_tokens(a):
    """Only the text a hook adds; the attachment's metadata is not sent to the model."""
    text = a.get("content") or a.get("stdout") or ""
    return toks(text if isinstance(text, str) else json.dumps(text))


def tools_by_server(shape):
    """({server: tokens}, {server: tool count}) of MCP tool definitions in a request shape."""
    tokens, counts = {}, {}
    for t in (shape or {}).get("tools") or []:
        srv = server_of(t.get("name", "")) if isinstance(t, dict) else None
        if srv:
            tokens[srv] = tokens.get(srv, 0) + toks(t)
            counts[srv] = counts.get(srv, 0) + 1
    return tokens, counts


# ---------- setup audits (shared by session advice and insights) ----------

LOCKFILE = re.compile(r"(^|/)(package-lock\.json|npm-shrinkwrap\.json|yarn\.lock|pnpm-lock\.yaml|"
                      r"Package\.resolved|[^/]+\.lock)$")
GENERATED = re.compile(r"(^|/)(node_modules|dist|build|\.next|DerivedData|Pods|\.gradle|target|"
                       r"vendor|coverage)/|\.min\.(js|css)$")
TEST_CMD = re.compile(r"\b(pytest|jest|vitest|mocha|(npm|yarn|pnpm|bun)( run)? test|go test|cargo test|"
                      r"swift test|flutter test|gradlew?\b[^|;]*\btest|xcodebuild\b[^|;]*\btest)\b")
TRIMMED = re.compile(r"\|\s*(tail|head|grep|rg)\b|>\s*/dev/null|-q\b|--quiet\b|--silent\b")
CLI_FOR_SERVER = [("github", "gh"), ("gitlab", "glab"), ("kubernetes", "kubectl"), ("k8s", "kubectl"),
                  ("supabase", "supabase"), ("vercel", "vercel"), ("docker", "docker"), ("aws", "aws"),
                  ("npm", "npm"), ("git", "git")]
MEMORY_NAMES = ("CLAUDE.md", "CLAUDE.local.md", os.path.join(".claude", "CLAUDE.md"))
PERSONAL_AGENTS = os.path.expanduser("~/.claude/agents")


def project_dir_for(cwd):
    """Where Claude Code keeps a project's transcripts and memory."""
    return os.path.join(PROJECTS, re.sub(r"[^A-Za-z0-9-]", "-", cwd)) if cwd else None


def memory_files(cwd):
    """[(path, tokens, lines)] of CLAUDE.md-style files loaded for cwd, plus MEMORY.md."""
    paths = [os.path.expanduser("~/.claude/CLAUDE.md")]
    d = os.path.abspath(cwd) if cwd else None
    while d:
        paths += [os.path.join(d, n) for n in MEMORY_NAMES]
        parent = os.path.dirname(d)
        d = None if parent == d else parent
    if cwd:
        paths.append(os.path.join(project_dir_for(cwd), "memory", "MEMORY.md"))
    out, seen = [], set()
    for p in paths:
        if p in seen or not os.path.isfile(p):
            continue
        seen.add(p)
        try:
            with open(p, errors="ignore") as f:
                text = f.read()
        except OSError:
            continue
        out.append((p, toks(text), text.count("\n") + 1))
    return out


def frontmatter(path):
    """Simple YAML frontmatter reader: top-level `key: value`, with folded continuation lines."""
    meta, key = {}, None
    try:
        with open(path, errors="ignore") as f:
            lines = f.read(20000).splitlines()
    except OSError:
        return meta
    if not lines or lines[0].strip() != "---":
        return meta
    for line in lines[1:]:
        if line.strip() == "---":
            break
        m = re.match(r"^([A-Za-z_-]+):\s*(.*)$", line)
        if m:
            key, val = m.group(1), m.group(2).strip()
            meta[key] = "" if val in (">", "|", ">-", "|-") else val.strip("'\"")
        elif key and line.startswith((" ", "\t")):
            meta[key] = (meta[key] + " " + line.strip()).strip()
    return meta


def agent_files(cwds):
    """[(name, path, description, personal)] for custom agents."""
    dirs = [(PERSONAL_AGENTS, True)] + [(os.path.join(c, ".claude", "agents"), False) for c in cwds if c]
    out, seen = [], set()
    for d, personal in dirs:
        for p in sorted(glob.glob(os.path.join(d, "*.md"))):
            if p in seen:
                continue
            seen.add(p)
            meta = frontmatter(p)
            name = meta.get("name") or os.path.splitext(os.path.basename(p))[0]
            out.append((name, p, meta.get("description", ""), personal))
    return out


def rec(score, key, text, action=None):
    return {"score": score, "key": key, "text": text, "action": action}


def setup_recs(by_server, tool_counts, scan, cwds, value):
    """Findings about what is loaded on every message: MCP servers, CLAUDE.md files and
    custom agents. `scan` (may be None) gives recent use; `value(tokens)` prices a
    per-message token cost."""
    recs = []
    used = scan["mcp_uses"] if scan else None
    for srv, t in by_server.items():
        if srv.startswith(APP_SERVERS) or t < 300:
            continue
        action = {"type": "mcp_remove", "target": srv}
        if used is not None and not used.get(srv):
            recs.append(rec(value(t), srv, (
                f"Turn off MCP server {srv}: not called once in {MCP_USAGE_DAYS} days, ≈{k(t)} of "
                f"tool definitions on every message (≈{fmt(value(t))}). Use /mcp, or the app's "
                f"connector settings."), action))
            continue
        cli = next((c for name, c in CLI_FOR_SERVER if name in srv.lower() and shutil.which(c)), None)
        if cli:
            recs.append(rec(value(t) * 0.5, srv, (
                f"MCP server {srv} wraps the `{cli}` CLI, which is installed. Claude can run {cli} "
                f"through Bash with no tool definitions; turning the server off saves ≈{k(t)} on every "
                f"message (≈{fmt(value(t))})."), action))
        elif tool_counts.get(srv, 0) > 20:
            recs.append(rec(value(t) * 0.3, srv, (
                f"MCP server {srv} loads {tool_counts[srv]} tools (≈{k(t)} on every message). If it "
                f"can expose only the tools you use (an allow-list or 'toolsets' option), set that.")))

    seen = set()
    for cwd in cwds:
        for path, t, lines in memory_files(cwd):
            if path in seen or (lines <= 300 and t <= 2500):
                continue
            seen.add(path)
            recs.append(rec(value(t - 1500) if t > 1500 else value(t) * 0.2, path, (
                f"{path.replace(os.path.expanduser('~'), '~')} is ≈{k(t)} ({lines} lines) and goes out "
                f"with every message. Keep the rules Claude needs every time; move the rest into "
                f"separate files and @-mention them when needed.")))

    agent_uses = scan.get("agent_uses", {}) if scan else None
    for name, path, desc, personal in agent_files(cwds):
        listed = toks(f"- {name}: {desc}")
        short = path.replace(os.path.expanduser("~"), "~")
        if listed < 60:  # a short listing costs next to nothing
            continue
        if agent_uses is not None and personal and not agent_uses.get(name):
            recs.append(rec(value(listed), name, (
                f"Custom agent {name} was not used in {MCP_USAGE_DAYS} days, and its description "
                f"(≈{k(listed)}) is listed on every message. Move it aside: {short}."),
                {"type": "agent_move", "target": os.path.basename(path)}))
        elif len(desc.split()) > 30:
            recs.append(rec(value(listed - 40), name, (
                f"Custom agent {name} has a {len(desc.split())}-word description, listed on every "
                f"message. One sentence of 'use when …' is enough (edit {short}).")))
    return recs


def analyze_context(transcript_path, cfg, session_usd=0.0, sub_usd=0.0):
    """Look at what fills the current context. Returns a dict with a breakdown and
    findings: [{"score": usd, "text": advice}], best first."""
    entries = list(iter_jsonl(transcript_path))
    start = compactions = 0
    for i, e in enumerate(entries):
        if e.get("type") == "system" and e.get("subtype") == "compact_boundary":
            start, compactions = i, compactions + 1
    shape = None
    for e in reversed(entries):
        if e.get("type") == "api-request-shape":
            shape = e.get("shape") or {}
            break

    uses, results, seen = {}, [], set()
    images, last_ctx, model = 0, 0, None
    groups = {"skills_list": 0, "skills_loaded": 0, "hooks": 0, "tool_lists": 0, "other": 0,
              "conversation": 0}
    listing, hooks_by_label, loaded_skills = {}, {}, {}
    cwd = None
    for e in entries[start:]:
        kind = e.get("type")
        cwd = e.get("cwd") or cwd
        msg = e.get("message") or {}
        content = msg.get("content")
        if kind == "attachment":
            a = e.get("attachment") or {}
            atype = a.get("type")
            if atype in NOT_IN_CONTEXT:
                continue
            group = ATTACHMENT_GROUPS.get(atype, "other")
            n = hook_tokens(a) if group == "hooks" else toks(a)
            if not n:
                continue
            groups[group] += n
            if atype == "skill_listing":
                listing.update(parse_listing(a.get("content") or ""))
            elif atype == "invoked_skills":
                for s in a.get("skills") or []:
                    if isinstance(s, dict):
                        loaded_skills[s.get("name", "?")] = toks(s.get("content") or "")
            elif group == "hooks":
                label = hook_label(a)
                hooks_by_label[label] = hooks_by_label.get(label, 0) + n
            continue
        if kind in ("user", "assistant"):
            t, _ = content_size(content)
            if isinstance(content, list):  # tool output is counted on its own
                t -= sum(content_size([b])[0] for b in content
                         if isinstance(b, dict) and b.get("type") == "tool_result")
            groups["conversation"] += t
        if kind == "assistant":
            usage = msg.get("usage")
            if usage and msg.get("id") not in seen:
                seen.add(msg.get("id"))
                ctx = cost_of(usage, msg.get("model"), cfg)[1]
                if ctx:
                    last_ctx, model = ctx, msg.get("model")
            for b in content if isinstance(content, list) else []:
                if isinstance(b, dict) and b.get("type") == "tool_use":
                    uses[b.get("id")] = (b.get("name", "tool"), b.get("input"))
        elif kind == "user" and isinstance(content, list):
            for b in content:
                if not isinstance(b, dict):
                    continue
                if b.get("type") == "tool_result":
                    t, i = content_size(b.get("content"))
                    images += i
                    results.append((t, b.get("tool_use_id")))
                elif b.get("type") == "image":
                    images += 1

    # Fixed overhead: system prompt + tool definitions, split per MCP server.
    by_server, tool_counts, system_tok, tools_tok = {}, {}, 0, 0
    if shape:
        system_tok = toks(shape.get("system") or "")
        by_server, tool_counts = tools_by_server(shape)
        tools_tok = sum(toks(t) for t in shape.get("tools") or [])
    overhead = system_tok + tools_tok
    skills_by_plugin = listing_by_plugin(listing)
    tool_out = sum(t for t, _ in results)
    image_tok = images * IMAGE_TOKENS

    p_in, _, p_read = price_for(model, cfg)

    def worth(tokens):  # 100 more messages re-read it, plus one cold re-cache
        return tokens * (p_read * 100 + p_in * 2) / 1e6

    findings = []

    big = sorted((r for r in results if r[0] >= 2000), key=lambda r: r[0], reverse=True)[:3]
    if big:
        names = [describe_use(*uses.get(uid, ("tool", {}))) for _, uid in big]
        listed = "; ".join(f"{n} ≈{k(t)}" for n, (t, _) in zip(names, big))
        targets = ", ".join(n.replace("`", "") for n in names)
        findings.append({"score": worth(sum(t for t, _ in big)), "text": (
            f"Tool output is ≈{k(tool_out)} of the context. Biggest: {listed}. "
            f"If you are done with them, run: /compact drop the raw output of {targets}; "
            f"keep decisions, file paths and open TODOs")})

    reads = {}
    for t, uid in results:
        name, inp = uses.get(uid, ("tool", {}))
        if name == "Read" and isinstance(inp, dict) and inp.get("file_path"):
            reads.setdefault(inp["file_path"], []).append(t)
    for path, sizes in reads.items():
        if len(sizes) >= 3:
            extra = sum(sorted(sizes)[:-1])
            findings.append({"score": worth(extra), "text": (
                f"{os.path.basename(path)} was read {len(sizes)} times (≈{k(extra)} duplicated). "
                f"Read a line range (offset/limit) or grep for what you need instead.")})

    waste, tests = [], []
    for t, uid in results:
        name, inp = uses.get(uid, ("tool", {}))
        inp = inp if isinstance(inp, dict) else {}
        if name == "Read" and t >= 500 and (LOCKFILE.search(str(inp.get("file_path", "")))
                                            or GENERATED.search(str(inp.get("file_path", "")))):
            waste.append((t, os.path.basename(str(inp["file_path"]))))
        cmd = str(inp.get("command", ""))
        if name == "Bash" and t >= 4000 and TEST_CMD.search(cmd) and not TRIMMED.search(cmd):
            tests.append((t, describe_use(name, inp)))
    if waste:
        n = sum(t for t, _ in waste)
        findings.append({"score": worth(n), "text": (
            f"Lock, generated or minified files were read whole: "
            f"{', '.join(f'{f} ≈{k(t)}' for t, f in sorted(waste, reverse=True)[:3])}. Grep for the "
            f"part you need instead; /compact drops what is already in context.")})
    if tests:
        n = sum(t for t, _ in tests)
        findings.append({"score": worth(n), "text": (
            f"Full test-run output stays in context (≈{k(n)}: "
            f"{'; '.join(d for _, d in sorted(tests, reverse=True)[:2])}). Run only the failing test, "
            f"or pipe the run through `| tail -50`.")})

    if images:
        findings.append({"score": worth(image_tok), "text": (
            f"{images} image(s) ≈{k(image_tok)} ride along with every message. "
            f"/compact drops them once you no longer need them.")})

    scan = usage_or_none() if (by_server or skills_by_plugin) else None
    plugin_uses = uses_by_plugin(scan["skill_uses"]) if scan else {}

    if scan and groups["skills_list"] >= 3000:
        idle = sorted(((t, p, c) for p, (t, c) in skills_by_plugin.items()
                       if p != NO_PLUGIN and t >= 200 and not plugin_uses.get(p)),
                      reverse=True)
        if idle:
            save = sum(t for t, _, _ in idle)
            listed = ", ".join(f"{p} ≈{k(t)} ({c} skills)" for t, p, c in idle[:5])
            findings.append({"score": worth(save), "text": (
                f"The skill list is ≈{k(groups['skills_list'])} on every message. No skill used "
                f"in {MCP_USAGE_DAYS} days from: {listed}. Disable those plugins with /plugin "
                f"(you can re-enable them any time) to save ≈{k(save)} per message.")})

    if loaded_skills:
        total = sum(loaded_skills.values())
        if total >= 3000:
            listed = ", ".join(f"{n} ≈{k(t)}" for n, t in
                               sorted(loaded_skills.items(), key=lambda x: -x[1])[:3])
            findings.append({"score": worth(total) * 0.5, "text": (
                f"Skills loaded earlier stay in context (≈{k(total)}: {listed}), and they are "
                f"carried over by /compact. If you are done with them, /clear drops them.")})

    if groups["hooks"] >= 2000 and hooks_by_label:
        top = sorted(hooks_by_label.items(), key=lambda x: -x[1])[:3]
        listed = ", ".join(f"{label} ≈{k(t)}" for label, t in top)
        findings.append({"score": worth(groups["hooks"]) * 0.5, "text": (
            f"Hook output adds ≈{k(groups['hooks'])} to the context: {listed}. If a plugin's "
            f"start-up text isn't useful to you, disable that plugin (/plugin) or remove the hook "
            f"from ~/.claude/settings.json.")})

    findings += setup_recs(by_server, tool_counts, scan, [cwd] if cwd else [], worth)

    if last_ctx >= cfg["context_warn_k"] * 1000:
        again = f" It was already compacted {compactions} time(s), so a fresh session is cheaper." \
            if compactions >= 2 else ""
        findings.append({"score": worth(groups["conversation"] + tool_out) * 0.5, "text": (
            f"The context is {k(last_ctx)}: ≈{k(overhead)} system and tools, "
            f"≈{k(groups['conversation'])} conversation, ≈{k(tool_out)} tool output. "
            f"New task? /clear and paste a 3-line summary. Same task? /compact <what to keep>.{again}")})

    if session_usd >= 5 and sub_usd / session_usd > 0.3:
        findings.append({"score": sub_usd * 0.2, "text": (
            f"Subagents made {sub_usd / session_usd:.0%} ({fmt(sub_usd)}) of this session's cost. "
            f"Give them narrower tasks, or a cheaper model (model: in the agent file, or "
            f"CLAUDE_CODE_SUBAGENT_MODEL=claude-sonnet-5-5).")})

    m = (model or "").lower()
    if session_usd >= 20 and ("opus" in m or "fable" in m):
        ratio = price_for("claude-sonnet-5-5", cfg)[0] / p_in
        if ratio < 0.9:
            findings.append({"score": session_usd * (1 - ratio) * 0.2, "text": (
                f"This session runs on {model}. Sonnet 5.5 costs {ratio:.0%} as much per token; "
                f"for routine edits switch with /model sonnet.")})

    findings.sort(key=lambda f: f["score"], reverse=True)
    return {"last_ctx": last_ctx, "model": model, "system": system_tok, "tools": tools_tok,
            "by_server": by_server, "tool_out": tool_out, "images": images, "groups": groups,
            "skills_by_plugin": skills_by_plugin, "hooks_by_label": hooks_by_label, "cwd": cwd,
            "compactions": compactions, "findings": findings}


def advice(transcript_path, cfg, session_usd, sub_usd, n=2):
    """Top findings as text to append to an alert. Empty on any problem."""
    if not transcript_path or not os.path.exists(transcript_path):
        return ""
    try:
        top = analyze_context(transcript_path, cfg, session_usd, sub_usd)["findings"][:n]
    except Exception:
        log_error("advice")
        return ""
    return "".join(f"\n• {f['text']}" for f in top)


# ---------- decisions ----------

def fmt(usd):
    return f"${usd:,.2f}"


def notify(cfg, title, text):
    """Desktop notification, detached; never fails the hook."""
    if not cfg.get("desktop_notify"):
        return
    try:
        import subprocess
        text = " ".join(text.split())[:220]
        if sys.platform == "darwin":
            cmd = ["osascript", "-e", "on run argv", "-e",
                   "display notification (item 1 of argv) with title (item 2 of argv)",
                   "-e", "end run", text, title]
        elif shutil.which("notify-send"):
            cmd = ["notify-send", title, text]
        else:
            return
        subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, start_new_session=True)
    except Exception:
        log_error("notify")


def pace(rate_per_h, spent, cap, label):
    """' At ≈$X/h the session cap is reached in ~N min.' when that is within 2 hours."""
    if rate_per_h < 1 or spent >= cap:
        return ""
    minutes = (cap - spent) / rate_per_h * 60
    if minutes > 120:
        return ""
    return f" At the current pace (≈{fmt(rate_per_h)}/h) the {label} cap is reached in ~{minutes:.0f} min."


def check(data, cfg):
    """Return (block_message or None, warn_message or None)."""
    sid = data.get("session_id") or "unknown"
    event = data.get("hook_event_name")
    prompt = data.get("prompt") or ""
    tool = data.get("tool_name") or ""
    if event == "PreToolUse" and any(tool.startswith(p) for p in cfg["never_block"]):
        return None, None
    path = data.get("transcript_path")
    t = totals(cfg, path, deadline=time.time() + SCAN_DEADLINE_S)
    session, daily, monthly, main = t["session"], t["daily"], t["monthly"], t["main"]
    rate = t["rate_per_h"]
    sub_usd = max(session - main.get("total", 0.0), 0.0)
    now = time.time()

    def tips():
        return advice(path, cfg, session, sub_usd)

    st_path = session_state_path(sid)
    st = load_json(st_path, {})
    st.setdefault("warned", [])
    st.setdefault("extra", 0.0)
    shared_path = os.path.join(STATE_DIR, "shared.json")  # day/month overrides and warnings
    shared = load_json(shared_path, {})
    day_key, month_key = f"day:{today()}", f"month:{this_month()}"
    shared = {k_: v for k_, v in shared.items() if k_.endswith((today(), this_month()))}

    caps = [("session", session, cfg["session_cap_usd"] + st["extra"], "this session has cost about"),
            ("daily", daily, cfg["daily_cap_usd"] + shared.get(f"extra:{day_key}", 0.0),
             "today's spend on this machine is about"),
            ("monthly", monthly, cfg["monthly_cap_usd"] + shared.get(f"extra:{month_key}", 0.0),
             "this month's spend on this machine is about")]
    caps = [c for c in caps if c[1] is not None]
    s_cap = caps[0][2]
    override = event == "UserPromptSubmit" and cfg["override_word"] in prompt
    block = warn = None
    where = f" (project limits for {cfg['project']})" if cfg.get("project") else ""

    over = [(name, spent, cap, what) for name, spent, cap, what in caps if spent >= cap
            and (name != "monthly" or cfg["monthly_hard_stop"])]
    if over:
        if override:
            # New cap = current spend + one more cap-sized allowance.
            for name, spent, _, _ in over:
                if name == "session":
                    st["extra"] = session
                else:
                    shared[f"extra:{day_key if name == 'daily' else month_key}"] = spent
            warn = (f"cost-guard: override accepted; you have another "
                    f"{fmt(cfg['session_cap_usd'])} before the next stop.")
        else:
            block = ("cost-guard stopped this: "
                     + "; ".join(f"{what} {fmt(spent)} (cap {fmt(cap)})" for _, spent, cap, what in over)
                     + f"{where}. Start a fresh session with a short summary, or re-send your message "
                     f"with {cfg['override_word']} in it to continue.") + tips()
            notify(cfg, "Claude Code stopped by cost-guard",
                   "; ".join(f"{name} {fmt(spent)} of {fmt(cap)}" for name, spent, cap, _ in over))

    # Runaway breaker: a burst of spend in 15 minutes stops the next tool call once.
    burst = cfg["burst_usd_per_15min"]
    if (not block and event == "PreToolUse" and burst > 0 and t["last_15min"] >= burst
            and now - st.get("burst_ack", 0) > 900):
        st["burst_ack"] = now
        block = (f"cost-guard paused this: the session spent {fmt(t['last_15min'])} in the last 15 "
                 f"minutes (limit {fmt(burst)}), which usually means a loop or a very large context."
                 + pace(rate, session, s_cap, "session") + tips())
        notify(cfg, "Claude Code: runaway spend paused",
               f"{fmt(t['last_15min'])} in 15 min. Check the session.")
    if event == "UserPromptSubmit" and override:
        st["burst_ack"] = now

    # Idle session with a large context: the next request re-writes the whole cache.
    if not block and event == "UserPromptSubmit" and main.get("last_ts") and main.get("ttl"):
        idle_min = (now - main["last_ts"]) / 60
        if idle_min > main["ttl"]:
            p_in = price_for(main.get("model"), cfg)[0]
            mult = 2 if main["ttl"] == 60 else 1.25
            est = main["last_ctx"] * p_in * mult / 1e6
            if est >= cfg["cold_cache_warn_usd"] and st.get("cold_ack") != main["last_ts"] and not override:
                st["cold_ack"] = main["last_ts"]
                hours = idle_min / 60
                idle = f"{hours:.1f} h" if hours >= 1 else f"{idle_min:.0f} min"
                block = (f"cost-guard: this session was idle for {idle}, so its prompt cache expired. "
                         f"Your next message re-caches about {main['last_ctx'] / 1000:,.0f}k tokens "
                         f"(about {fmt(est)} for that message alone, and the large context makes "
                         f"every later message expensive too). Cheaper: run /clear, or start a new "
                         f"session and paste a short summary. To continue here anyway, re-send your "
                         f"message (this warning shows once).") + tips()

    # Monthly budget in warn-only mode: once a day, not a stop.
    if not block and not warn and monthly is not None and not cfg["monthly_hard_stop"]:
        m_cap = cfg["monthly_cap_usd"]
        if monthly >= m_cap and not shared.get(f"over:{day_key}"):
            shared[f"over:{day_key}"] = True
            warn = (f"cost-guard: this month's spend on this machine is about {fmt(monthly)}, over the "
                    f"{fmt(m_cap)} monthly budget. Every session counts toward it."
                    + (tips() or " /clear between tasks keeps each message cheap."))
            notify(cfg, "Claude Code is over the monthly budget", f"{fmt(monthly)} of {fmt(m_cap)}")

    # Percent-of-cap warnings, once per session / day / month.
    if not block and not warn:
        pct = cfg["warn_at_pct"] / 100
        for name, spent, cap, what in caps:
            key = {"session": None, "daily": f"pct:{day_key}", "monthly": f"pct:{month_key}"}[name]
            done = st.get("pct_warned") if name == "session" else shared.get(key)
            if 0 < pct < 1 and spent >= cap * pct and spent < cap and not done:
                if name == "session":
                    st["pct_warned"] = True
                else:
                    shared[key] = True
                warn = (f"cost-guard: {what} {fmt(spent)}, {spent / cap:.0%} of the {name} cap "
                        f"({fmt(cap)}){where}." + pace(rate, spent, cap, name) + tips())
                notify(cfg, f"Claude Code at {spent / cap:.0%} of the {name} cap",
                       f"{fmt(spent)} of {fmt(cap)}")
                break

    if not block and not warn:
        crossed = [x for x in cfg["session_warn_usd"] if session >= x and x not in st["warned"]]
        if crossed:
            st["warned"] += crossed
            warn = (f"cost-guard: this session has now cost about {fmt(session)} "
                    f"(today: {fmt(daily)}). Hard stop at {fmt(s_cap)}."
                    + pace(rate, session, s_cap, "session")
                    + (tips() or " Tip: /clear between tasks keeps each message cheap."))

    # Large context: advise once, again only after it shrank (compaction or /clear).
    ctx, ctx_limit = main.get("last_ctx", 0), cfg["context_warn_k"] * 1000
    if ctx < ctx_limit * 0.6:
        st["ctx_warned"] = False
    elif not block and not warn and ctx >= ctx_limit and not st.get("ctx_warned"):
        st["ctx_warned"] = True
        warn = (f"cost-guard: the context is now {k(ctx)} tokens, and every message re-reads "
                f"all of it." + (tips() or " /compact or /clear to shrink it."))

    save_json(st_path, st)
    save_json(shared_path, shared)
    return block, warn


def run_hook():
    data = json.load(sys.stdin)
    cfg = load_config(data.get("cwd"))
    block, warn = check(data, cfg)
    event = data.get("hook_event_name")
    out = {}
    if block and event == "UserPromptSubmit":
        out = {"decision": "block", "reason": block}
    elif block and event == "PreToolUse":
        out = {
            "systemMessage": block,
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": block + " Do not call more tools: stop now and give the "
                                                    "user a short summary of where the work stands.",
            },
        }
    elif warn:
        out = {"systemMessage": warn}
    if out:
        print(json.dumps(out))


# ---------- status line ----------

def run_statusline():
    data = load_json_stdin()
    cfg = load_config((data.get("workspace") or {}).get("current_dir") or data.get("cwd"))
    t = totals(cfg, data.get("transcript_path"), deadline=time.time() + 2)
    session, daily, monthly, main = t["session"], t["daily"], t["monthly"], t["main"]
    reported = (data.get("cost") or {}).get("total_cost_usd")
    if reported is not None:
        session = reported
    red, yellow, green, dim, reset = "\033[31m", "\033[33m", "\033[32m", "\033[2m", "\033[0m"

    def color(v, cap):
        return red if v >= cap * 0.8 else yellow if v >= cap * 0.4 else green

    parts = []
    model = (data.get("model") or {}).get("display_name")
    if model:
        parts.append(f"{dim}{model}{reset}")
    parts.append(f"{color(session, cfg['session_cap_usd'])}session {fmt(session)}{reset}")
    parts.append(f"{color(daily, cfg['daily_cap_usd'])}today {fmt(daily)}{reset}")
    if monthly is not None:
        parts.append(f"{color(monthly, cfg['monthly_cap_usd'])}month {fmt(monthly)}{reset}")
    if t["rate_per_h"] >= 1:
        hot = t["last_15min"] >= cfg["burst_usd_per_15min"] * 0.5
        parts.append(f"{red if hot else dim}{fmt(t['rate_per_h'])}/h{reset}")
    if main.get("last_ctx"):
        ctx = main["last_ctx"]
        parts.append(f"{red if ctx > 300_000 else yellow if ctx > 150_000 else dim}ctx {ctx // 1000}k{reset}")
        if main.get("ttl") and (time.time() - main["last_ts"]) / 60 > main["ttl"]:
            parts.append(f"{yellow}cache cold{reset}")
    print(" · ".join(parts))


def load_json_stdin():
    try:
        return json.load(sys.stdin)
    except Exception:
        return {}


# ---------- report ----------

def run_report(args):
    """report [--days N | --week | --month] [--by session|project|model|day] [--json]"""
    cfg = load_config()
    if "--month" in args:
        since, label = month_start_ts(), "this month"
    else:
        days = 7 if "--week" in args else int(arg_after("--days", args) or 1)
        since, label = time.time() - days * 86400, f"the last {days} day(s)"
    by = arg_after("--by", args) or "session"
    if by not in ("session", "project", "model", "day"):
        sys.exit("--by takes one of: session, project, model, day")
    files, _, _ = refresh(cfg, None, window_s=time.time() - since)
    first_day = datetime.fromtimestamp(since).strftime("%Y-%m-%d")
    groups = {}
    for p, e in files.items():
        for day, models in e.get("by_day_model", {}).items():
            if day < first_day:
                continue
            for model, usd in models.items():
                key = {"session": os.path.relpath(p, PROJECTS),
                       "project": e.get("cwd") or os.path.basename(os.path.dirname(p)),
                       "model": model, "day": day}[by]
                groups[key] = groups.get(key, 0.0) + usd
    rows = sorted(((usd, key) for key, usd in groups.items() if usd >= 0.01),
                  key=lambda r: r[1] if by == "day" else -r[0])
    total = sum(usd for usd, _ in rows)
    if "--json" in args:
        print(json.dumps({"period": {"from": first_day, "to": today()}, "by": by, "total_usd": round(total, 4),
                          "rows": [{"key": key, "usd": round(usd, 4)} for usd, key in rows]}, indent=2))
        return
    print(f"Spend estimate for {label}, by {by}, list prices:\n")
    for usd, key in rows[:30]:
        print(f"  {fmt(usd):>10}  {key}")
    if len(rows) > 30:
        print(f"  {'':>10}  … {len(rows) - 30} more")
    print(f"\n  {fmt(total):>10}  total")


# ---------- context report ----------

def find_transcript(session_id):
    """This session's transcript, else the most recently used one."""
    if session_id and "{" not in session_id:
        hits = glob.glob(os.path.join(PROJECTS, "*", f"{session_id}.jsonl"))
        if hits:
            return hits[0]
    mains = glob.glob(os.path.join(PROJECTS, "*", "*.jsonl"))
    return max(mains, key=os.path.getmtime) if mains else None


def run_context(session_id):
    cfg = load_config()
    path = find_transcript(session_id)
    if not path:
        print("No transcript found.")
        return
    files, mine, _ = refresh(cfg, path)
    session = sum(files[p]["total"] for p in mine if p in files)
    sub = max(session - (files.get(path) or {}).get("total", 0.0), 0.0)
    a = analyze_context(path, cfg, session, sub)
    ctx = a["last_ctx"]
    print(f"Session {os.path.basename(path)[:8]} · {a['model'] or 'unknown model'} · "
          f"cost ≈{fmt(session)} (subagents {fmt(sub)}) · compacted {a['compactions']}x\n")
    print(f"Context now ≈{k(ctx)} tokens")
    g = a["groups"]
    rows = [("System prompt", a["system"]), ("Tool definitions", a["tools"]),
            ("Skill list", g["skills_list"]), ("Loaded skills", g["skills_loaded"]),
            ("Hook output", g["hooks"]), ("Tool and agent lists", g["tool_lists"]),
            ("Conversation", g["conversation"]), ("Tool output", a["tool_out"]),
            ("Images", a["images"] * IMAGE_TOKENS), ("Other", g["other"])]
    print("(by transcript size, about 4 characters per token; the sum may differ a little)")
    for label, n in rows:
        share = f"{n / ctx:.0%}" if ctx else ""
        print(f"  {label:<24} {k(n):>8}  {share:>4}")

    def top(title, items):
        if items:
            print(f"\n{title}:")
            for name, n in sorted(items, key=lambda x: -x[1])[:8]:
                print(f"  {name[:52]:<52} {k(n):>8}")

    top("Tool definitions per MCP server (sent with every message)", a["by_server"].items())
    top("Skill list per plugin (sent with every message)",
        [(f"{p} ({c} skills)", t) for p, (t, c) in a["skills_by_plugin"].items()])
    top("Hook output", a["hooks_by_label"].items())
    print("\nAdvice, biggest saving first:")
    if not a["findings"]:
        print("  Nothing stands out. The context is in good shape.")
    for i, f in enumerate(a["findings"], 1):
        print(f"  {i}. {f['text']}  (≈{fmt(f['score'])})")
    print("\nSavings are rough: 100 more messages re-reading the tokens, plus one cold re-cache.")


# ---------- insights across sessions ----------

PERSONAL_SKILLS = os.path.expanduser("~/.claude/skills")
COMMAND_NAME = re.compile(r"<command-name>/?([^<\s]+)</command-name>")


def user_text(content):
    """What the user typed: plain text blocks only, never tool output."""
    if isinstance(content, str):
        return content
    return " ".join(b.get("text", "") for b in content or []
                    if isinstance(b, dict) and b.get("type") == "text")


def scan_period(days, deadline=None):
    """One pass over the last `days` of transcripts. Collects what is sent with every
    message (skill list, tool definitions) and what hooks add, from main transcripts,
    and what gets used, from main and subagent transcripts. Raises ScanTimeout past
    `deadline` (a time.time() value)."""
    cutoff = time.time() - days * 86400
    out = {"messages": 0, "sessions": 0, "rewrites": 0, "models": {}, "skill_uses": {},
           "mcp_uses": {}, "skill_bodies": {}, "hook_burn": {}, "listing": {}, "shape": None,
           "agent_uses": {}, "cwds": {}}
    newest_listing = newest_shape = 0
    paths = []
    for p in glob.glob(os.path.join(PROJECTS, "**", "*.jsonl"), recursive=True):
        try:
            if os.path.getmtime(p) >= cutoff:
                paths.append(p)
        except OSError:
            pass
    markers = (b'"usage"', b'"attachment"', b"compact_boundary", b"<command-name>", b'"Skill"',
               b"api-request-shape", b"mcp__")
    for p in paths:
        if deadline and time.time() > deadline:
            raise ScanTimeout()
        sub = f"{os.sep}subagents{os.sep}" in p  # counts for "used", not for what is sent
        try:
            mtime = os.path.getmtime(p)
            f = open(p, "rb")
        except OSError:
            continue
        seen, live_hooks, listing, file_cwd = set(), {}, {}, None
        if not sub:
            out["sessions"] += 1
            out["rewrites"] += 1
        with f:
            for i, raw in enumerate(f):
                if deadline and i % 5000 == 0 and time.time() > deadline:
                    raise ScanTimeout()
                if not any(m in raw for m in markers):
                    continue
                try:
                    e = json.loads(raw)
                except Exception:
                    continue
                kind = e.get("type")
                msg = e.get("message") or {}
                content = msg.get("content")
                if sub and kind != "assistant":
                    continue
                if not sub and not file_cwd and e.get("cwd"):
                    file_cwd = e["cwd"]
                    out["cwds"][file_cwd] = out["cwds"].get(file_cwd, 0) + 1
                if not sub and kind == "assistant" and msg.get("usage") and msg.get("id") not in seen:
                    seen.add(msg.get("id"))
                    out["messages"] += 1
                    m = msg.get("model") or "?"
                    out["models"][m] = out["models"].get(m, 0) + 1
                    for label, n in live_hooks.items():  # hook text re-read by this message
                        out["hook_burn"].setdefault(label, [0, 0])[1] += n
                if kind == "assistant" and isinstance(content, list):
                    for b in content:
                        if not isinstance(b, dict) or b.get("type") != "tool_use":
                            continue
                        name = b.get("name", "")
                        if name == "Skill":
                            s = str((b.get("input") or {}).get("skill", ""))
                            out["skill_uses"][s] = out["skill_uses"].get(s, 0) + 1
                        elif name in ("Agent", "Task"):
                            a = str((b.get("input") or {}).get("subagent_type", ""))
                            out["agent_uses"][a] = out["agent_uses"].get(a, 0) + 1
                        srv = server_of(name)
                        if srv:
                            out["mcp_uses"][srv] = out["mcp_uses"].get(srv, 0) + 1
                elif kind == "user":
                    for name in COMMAND_NAME.findall(user_text(content)):
                        out["skill_uses"][name] = out["skill_uses"].get(name, 0) + 1
                elif kind == "system" and e.get("subtype") == "compact_boundary":
                    out["rewrites"] += 1
                    live_hooks = {}
                elif kind == "api-request-shape" and mtime >= newest_shape:
                    newest_shape, out["shape"] = mtime, e.get("shape")
                elif kind == "attachment":
                    a = e.get("attachment") or {}
                    atype = a.get("type")
                    if atype == "skill_listing":
                        listing.update(parse_listing(a.get("content") or ""))
                    elif atype == "invoked_skills":
                        for s in a.get("skills") or []:
                            if isinstance(s, dict):
                                n = toks(s.get("content") or "")
                                name = s.get("name", "?")
                                out["skill_bodies"][name] = max(out["skill_bodies"].get(name, 0), n)
                    elif ATTACHMENT_GROUPS.get(atype) == "hooks":
                        n = hook_tokens(a)
                        if n:
                            label = hook_label(a)
                            out["hook_burn"].setdefault(label, [0, 0])[0] += n
                            live_hooks[label] = live_hooks.get(label, 0) + n
        if listing and mtime >= newest_listing:
            newest_listing, out["listing"] = mtime, listing
    return out


def skill_source(name):
    """("plugin", plugin) | ("personal", folder) | ("built-in", None)"""
    if ":" in name:
        return "plugin", name.split(":", 1)[0]
    folder = os.path.join(PERSONAL_SKILLS, name)
    if os.path.isdir(folder):
        return "personal", folder
    return "built-in", None


def build_insights(cfg, days, scan=None):
    """Returns None when there is no data, else a dict with "header", "table" (lines)
    and "recs": [rec dicts] sorted best first. Items in insights_ignore are left out."""
    d = scan or cached_scan(days)
    msgs = d["messages"]
    if not msgs:
        return None
    model = max(d["models"], key=d["models"].get)
    p_in, _, p_read = price_for(model, cfg)
    ignore = {x.strip().lower() for x in str(cfg.get("insights_ignore", "")).split(",") if x.strip()}

    def burn(tokens_per_msg):  # re-read by every message, re-written at each start/compaction
        return tokens_per_msg * (msgs * p_read + d["rewrites"] * p_in * 1.25) / 1e6

    uses = d["skill_uses"]
    plugins, personal = {}, []
    for name, t in d["listing"].items():
        kind, where = skill_source(name)
        if kind == "plugin":
            g = plugins.setdefault(where, {"tokens": 0, "skills": 0, "used": 0})
            g["tokens"] += t
            g["skills"] += 1
            g["used"] += uses.get(name, 0)
        elif kind == "personal":
            personal.append((t, name, uses.get(name, 0), where))
    by_server, tool_counts = tools_by_server(d["shape"])

    table = []
    rows = [(burn(g["tokens"]), f"plugin {p} ({g['skills']} skills)", g["tokens"], g["used"])
            for p, g in plugins.items()]
    rows += [(burn(t), f"skill {name} (personal)", t, n) for t, name, n, _ in personal]
    rows += [(burn(t), f"MCP {srv}", t, d["mcp_uses"].get(srv, 0)) for srv, t in by_server.items()]
    rows.sort(reverse=True)
    table.append(f"  {'Sent with every message':<46} {'tokens':>7} {'uses':>6} {'≈cost':>9}")
    for usd, label, t, n in rows[:20]:
        table.append(f"  {label[:46]:<46} {k(t):>7} {n:>6} {fmt(usd):>9}")
    hooks = sorted(((tm * p_read / 1e6 + tot * p_in * 1.25 / 1e6, label, tot)
                    for label, (tot, tm) in d["hook_burn"].items()), reverse=True)
    if hooks:
        table.append(f"\n  {'Hook text added to the context':<46} {'total':>7} {'':>6} {'≈cost':>9}")
        for usd, label, tot in hooks[:6]:
            table.append(f"  {label[:46]:<46} {k(tot):>7} {'':>6} {fmt(usd):>9}")

    recs = []
    for plugin, g in plugins.items():
        if g["used"]:
            continue
        also = [s for s in d["mcp_uses"] if s.startswith(f"plugin_{plugin}_")]
        note = (f" It also provides an MCP server you used ({', '.join(also)}); "
                f"disabling removes that too.") if also else ""
        recs.append(rec(burn(g["tokens"]), plugin, (
            f"Disable plugin {plugin}: none of its {g['skills']} skills was used, and its "
            f"skill list costs ≈{k(g['tokens'])} per message (≈{fmt(burn(g['tokens']))} in {days} "
            f"days). Run /plugin, then disable it; re-enable any time.{note}"),
            {"type": "plugin_disable", "target": plugin}))
    for t, name, n, folder in personal:
        if n == 0:
            recs.append(rec(burn(t), name, (
                f"Move personal skill {name} aside: never used, ≈{k(t)} per message "
                f"(≈{fmt(burn(t))}). Reversible: it goes to ~/.claude/skills-off/."),
                {"type": "skill_move", "target": name}))
        elif t >= 150:
            recs.append(rec(burn(t - 60), name, (
                f"Shorten the description of skill {name}: ≈{k(t)} per message. One or two "
                f"sentences of 'use when …' is enough (edit {folder}/SKILL.md).")))
    top_cwds = sorted(d.get("cwds", {}), key=lambda c: -d["cwds"][c])[:5]
    recs += setup_recs(by_server, tool_counts, d, top_cwds, burn)
    for name, body in d["skill_bodies"].items():
        n = uses.get(name, 0)
        kind, where = skill_source(name)
        if body >= 3000 and n >= 2:
            fix = (f"move its reference sections into separate files in {where} that the "
                   f"skill reads only when needed") if kind == "personal" else \
                "check that it only triggers when you really need it"
            recs.append(rec(body * n * p_in * 1.25 / 1e6, name, (
                f"Skill {name} loads ≈{k(body)} into the context each time it runs ({n}x). "
                f"To make it cheaper, {fix}.")))
    for usd, label, tot in hooks[:3]:
        if usd >= 0.5:
            recs.append(rec(usd, label, (
                f"Hook '{label}' added ≈{k(tot)} of text in total (≈{fmt(usd)}). If you don't "
                f"need what it injects, disable its plugin or remove it from ~/.claude/settings.json.")))
    recs = [r for r in recs if not any(i in r["key"].lower() for i in ignore)]
    recs.sort(key=lambda r: r["score"], reverse=True)
    header = (f"Last {days} day(s): {msgs:,} messages in {d['sessions']} sessions, mostly {model}.")
    return {"header": header, "table": table, "recs": recs}


def run_insights(days):
    cfg = load_config()
    ins = build_insights(cfg, days)
    if not ins:
        print(f"No Claude Code messages in the last {days} day(s).")
        return
    print(ins["header"])
    print("Costs below are what each item adds by being sent with every message (rough).\n")
    print("\n".join(ins["table"]))
    print("\nWhat to remove or change, biggest saving first:")
    if not ins["recs"]:
        print("  Nothing stands out: everything sent with each message gets used.")
    for i, r in enumerate(ins["recs"][:12], 1):
        fixable = r.get("action") and resolve_action(r["action"])
        print(f"  {i}. {r['text']}" + ("  [/cost-guard:fix can do this]" if fixable else ""))
    print("\nTo stop a tip from coming back: /cost-guard:limits ignore <name>[,<name>…]")


def insights_tip(cfg, data):
    """SessionStart: the top cost tips from recent sessions, at most every N days."""
    every = cfg.get("insights_every_days", 1)
    if every <= 0 or data.get("source") not in (None, "startup"):
        return None
    path = os.path.join(STATE_DIR, "insights.json")
    st = load_json(path, {})
    last = st.get("shown")
    if last and (time.time() - last) < every * 86400 - 3600:
        return None
    try:
        scan = cached_scan(MCP_USAGE_DAYS, deadline=time.time() + SCAN_DEADLINE_S)
    except ScanTimeout:  # big history: finish in the background, show the tip next start
        scan_in_background()
        return None
    ins = build_insights(cfg, MCP_USAGE_DAYS, scan)
    st["shown"] = time.time()
    save_json(path, st)
    if not ins or not ins["recs"]:
        return None
    top = ins["recs"][:3]
    total = sum(r["score"] for r in top)
    lines = [f"cost-guard tips: {len(top)} change(s) could save ≈{fmt(total)} per 2 weeks "
             f"(based on your last 14 days):"]
    lines += [f"{i}. {r['text']}" for i, r in enumerate(top, 1)]
    fixable = " · apply with /cost-guard:fix (undo any time)" \
        if any(r.get("action") and resolve_action(r["action"]) for r in top) else ""
    lines.append("Full list: /cost-guard:insights" + fixable + " · hide a tip: /cost-guard:limits "
                 "ignore <name> · turn these tips off: /cost-guard:limits tips 0")
    return "\n".join(lines)


# ---------- reversible fixes ----------

CLAUDE_JSON = os.path.expanduser("~/.claude.json")
FIXES_PATH = os.path.join(STATE_DIR, "fixes.json")


def plugin_id(name):
    """Full plugin@marketplace id of an enabled plugin, from settings or the install list."""
    enabled = load_json(SETTINGS_PATH, {}).get("enabledPlugins") or {}
    for key, on in enabled.items():
        if on and key.split("@")[0] == name:
            return key
    for key in (load_json(INSTALLED_PLUGINS, {}).get("plugins") or {}):
        if key.split("@")[0] == name:
            return key
    return None


def find_mcp_server(srv):
    """(name, scope, project dir or None, config) for a server defined in ~/.claude.json."""
    def norm(name):
        return re.sub(r"[^A-Za-z0-9_-]", "_", name)

    data = load_json(CLAUDE_JSON, {})
    places = [("user", None, data.get("mcpServers") or {})]
    places += [("local", path, (proj or {}).get("mcpServers") or {})
               for path, proj in (data.get("projects") or {}).items()]
    for scope, where, servers in places:
        for name, config in servers.items():
            if srv in (name, norm(name)):
                return name, scope, where, config
    return None


def resolve_action(action):
    """Concrete steps for an action, or None when it can't be done automatically."""
    kind, target = action.get("type"), action.get("target", "")
    if kind == "plugin_disable":
        pid = plugin_id(target)
        if pid:
            return {"run": ["claude", "plugin", "disable", pid], "undo_run": ["claude", "plugin", "enable", pid],
                    "what": f"disable plugin {pid}"}
    elif kind in ("skill_move", "agent_move"):
        home = PERSONAL_SKILLS if kind == "skill_move" else PERSONAL_AGENTS
        src, dst = os.path.join(home, target), os.path.join(home + "-off", target)
        if os.path.exists(src) and not os.path.exists(dst):
            return {"move": [src, dst], "what": f"move {src} to {dst}"}
    elif kind == "mcp_remove":
        found = find_mcp_server(target)
        if found:
            name, scope, where, config = found
            return {"run": ["claude", "mcp", "remove", name, "-s", scope], "cwd": where,
                    "undo_run": ["claude", "mcp", "add-json", name, json.dumps(config), "-s", scope],
                    "what": f"remove MCP server {name} ({scope} scope; its config is saved for undo)"}
    return None


def fix_candidates(cfg):
    ins = build_insights(cfg, MCP_USAGE_DAYS) or {"recs": []}
    out = []
    for r in ins["recs"]:
        steps = resolve_action(r["action"]) if r.get("action") else None
        if steps:
            out.append(dict(r, id=f"{r['action']['type']}:{r['action']['target']}", steps=steps))
    return out


def run_steps(steps, undo=False):
    """Run a fix (or its undo). Returns (ok, message)."""
    import subprocess
    if "move" in steps:
        src, dst = steps["move"][::-1] if undo else steps["move"]
        if os.path.exists(dst):
            return False, f"{dst} already exists"
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        shutil.move(src, dst)
        return True, f"moved {src} to {dst}"
    cmd = steps["undo_run"] if undo else steps["run"]
    if not shutil.which(cmd[0]):
        return False, f"`{cmd[0]}` is not on PATH"
    done = subprocess.run(cmd, cwd=steps.get("cwd") or None, capture_output=True, text=True, timeout=120)
    out = (done.stdout + done.stderr).strip()
    return done.returncode == 0, out or " ".join(cmd[:4])


def run_fix(args):
    sub = args[0] if args else "--list"
    log = load_json(FIXES_PATH, [])
    if sub in ("--list", "list"):
        cands = fix_candidates(load_config())
        if "--json" in args:
            print(json.dumps([{"id": c["id"], "usd": round(c["score"], 2), "what": c["steps"]["what"],
                               "why": c["text"]} for c in cands], indent=2))
            return
        if not cands:
            print("Nothing to fix automatically. /cost-guard:insights lists manual tips.")
            return
        print("Fixes cost-guard can apply (each can be undone with: fix undo <id>):\n")
        for c in cands:
            print(f"  {c['id']}  ≈{fmt(c['score'])}/2 weeks\n      {c['steps']['what']}\n      why: {c['text']}\n")
        return
    if sub == "apply":
        ids = [a for a in args[1:] if not a.startswith("--")]
        dry = "--dry-run" in args
        cands = {c["id"]: c for c in fix_candidates(load_config())}
        for fid in ids:
            c = cands.get(fid)
            if not c:
                print(f"✗ {fid}: not a current fix (run fix --list)")
                continue
            steps = c["steps"]
            if dry:
                print(f"would {steps['what']}: " + (" ".join(steps["run"]) if "run" in steps else "move"))
                continue
            ok, msg = run_steps(steps)
            print(f"{'✓' if ok else '✗'} {fid}: {msg}")
            if ok:
                log.append({"id": fid, "ts": time.time(), "steps": steps, "undone": False})
        if not dry:
            save_json(FIXES_PATH, log)
            print("\nRestart Claude Code sessions for this to take effect. Undo: /cost-guard:fix undo")
        return
    if sub == "undo":
        which = args[1] if len(args) > 1 else "last"
        live = [e for e in log if not e.get("undone")]
        if which == "last":
            pick = live[-1:]
        elif which == "all":
            pick = live[::-1]
        else:
            pick = [e for e in live if e["id"] == which]
        if not pick:
            print("Nothing to undo.")
            for e in log[-5:]:
                print(f"  {'undone' if e.get('undone') else 'applied'}  {e['id']}")
            return
        for e in pick:
            ok, msg = run_steps(e["steps"], undo=True)
            e["undone"] = ok
            print(f"{'✓' if ok else '✗'} undo {e['id']}: {msg}")
        save_json(FIXES_PATH, log)
        return
    sys.exit("Use: fix --list | fix apply <id>… [--dry-run] | fix undo [last|all|<id>]")


# ---------- limits, sync, status line setup ----------

def run_limits(args):
    overrides = load_json(OVERRIDES_PATH, {})
    if args[:1] == ["project"]:  # project KEY VALUE | project reset — for the current folder
        here = os.path.abspath(arg_after("--cwd", args) or os.getcwd())
        rest = [a for a in args[1:] if a != "--cwd" and a != arg_after("--cwd", args)]
        projects = overrides.setdefault("projects", {})
        if rest[:1] == ["reset"]:
            projects.pop(here, None)
            print(f"Project limits removed for {here}.")
        elif len(rest) >= 2 and rest[0] in LIMIT_KEYS:
            probe = {}
            apply_limits(probe, {rest[0]: rest[1]})
            if not probe:
                sys.exit(f"{rest[1]!r} is not a valid value for {rest[0]}.")
            projects.setdefault(here, {})[rest[0]] = rest[1]
            print(f"Set {rest[0]} = {rest[1]} for {here} and its subfolders.")
        else:
            sys.exit(f"Use: limits project <{'|'.join(LIMIT_KEYS)}> <value>, or limits project reset")
        if not projects:
            overrides.pop("projects")
        save_json(OVERRIDES_PATH, overrides)
    elif args[:1] == ["reset"]:
        kept = {key: overrides[key] for key in ("insights_ignore", "projects") if overrides.get(key)}
        overrides = kept
        save_json(OVERRIDES_PATH, overrides)
        print("Limits are back to the plugin options."
              + (" Your ignore list and project limits are kept (clear them with: limits insights_ignore "
                 "clear / limits project reset)." if kept else ""))
    elif len(args) >= 2:
        key, val = args[0], ",".join(args[1:]) if args[0] in ("warn_at_usd", "insights_ignore") else args[1]
        if key == "insights_ignore":  # add to the list instead of replacing it; "clear" empties it
            names = [] if val.strip().lower() == "clear" else \
                [x.strip() for x in f"{overrides.get(key, '')},{val}".split(",") if x.strip()]
            val = ",".join(dict.fromkeys(names))
        if key not in LIMIT_KEYS:
            sys.exit(f"Unknown limit {key!r}. Use one of: {', '.join(LIMIT_KEYS)}")
        probe = {}
        apply_limits(probe, {key: val})
        if not probe:
            sys.exit(f"{val!r} is not a valid value for {key}.")
        overrides[key] = val
        save_json(OVERRIDES_PATH, overrides)
        print(f"Set {key} = {val} (takes effect on the next prompt).")
    cfg = load_config()
    session = load_json(OPTIONS_PATH, {})
    print("\nCurrent limits (override > plugin option > default):")
    rows = [
        ("session_cap_usd", fmt(cfg["session_cap_usd"]), "hard stop per session"),
        ("daily_cap_usd", fmt(cfg["daily_cap_usd"]), "hard stop per day, all sessions"),
        ("monthly_cap_usd", fmt(cfg["monthly_cap_usd"]), "monthly budget, all sessions"),
        ("monthly_hard_stop", "yes" if cfg["monthly_hard_stop"] else "no (warn daily)", "stop at the monthly cap"),
        ("warn_at_pct", f"{cfg['warn_at_pct']:g}%", "one-time warning per cap"),
        ("burst_usd_per_15min", fmt(cfg["burst_usd_per_15min"]), "runaway breaker (0 = off)"),
        ("desktop_notify", "on" if cfg["desktop_notify"] else "off", "desktop notifications"),
        ("warn_at_usd", ", ".join(fmt(x) for x in cfg["session_warn_usd"]) or "none", "one-time alerts"),
        ("cold_cache_warn_usd", fmt(cfg["cold_cache_warn_usd"]), "stop once before an expensive re-cache"),
        ("override_word", cfg["override_word"], "type this to continue past a stop"),
        ("context_warn_k", f"{cfg['context_warn_k']:g}k tokens", "one-time context advice"),
        ("insights_every_days", f"{cfg['insights_every_days']:g} day(s)" if cfg["insights_every_days"] > 0
         else "off", "cost tips at session start"),
        ("insights_ignore", cfg["insights_ignore"] or "none", "never suggest these"),
    ]
    for key, val, what in rows:
        src = "override" if key in overrides else "plugin option" if key in session else "default"
        print(f"  {key:<20} {val:<22} {what}  [{src}]")
    for path, vals in (overrides.get("projects") or {}).items():
        print(f"\n  Project {path}: " + ", ".join(f"{key} {val}" for key, val in vals.items()))


def run_sync():
    """SessionStart: keep a copy of this script in the data folder for the status line,
    and show the top cost tips from recent sessions (at most every insights_every_days)."""
    data = load_json_stdin()
    plugin_options()
    src = os.path.abspath(__file__)
    dst = os.path.join(DATA, "cost_guard.py")
    if src != dst:
        os.makedirs(DATA, exist_ok=True)
        shutil.copy2(src, dst)
    try:
        tip = insights_tip(load_config(), data)
    except Exception:
        log_error("insights_tip")
        tip = None
    if tip:
        print(json.dumps({"systemMessage": tip}))


def run_install_statusline(force):
    run_sync()
    script = os.path.join(DATA, "cost_guard.py")
    settings = load_json(SETTINGS_PATH, {})
    current = settings.get("statusLine")
    if current and "cost_guard.py" not in current.get("command", "") and not force:
        print(f"You already have a status line: {current.get('command')}\n"
              "Run again with --force to replace it.")
        return
    if os.path.exists(SETTINGS_PATH):
        backup = f"{SETTINGS_PATH}.bak-{time.strftime('%Y%m%d-%H%M%S')}"
        shutil.copy2(SETTINGS_PATH, backup)
        print(f"Backed up settings to {backup}")
    settings["statusLine"] = {"type": "command", "command": f'python3 "{script}" statusline'}
    with open(SETTINGS_PATH, "w") as f:
        json.dump(settings, f, indent=2)
        f.write("\n")
    print("Status line installed. It shows in new sessions in the terminal.")


def arg_after(flag, args=None):
    """Value after `flag`, or None when the flag is missing or has no value."""
    args = sys.argv if args is None else args
    if flag in args:
        i = args.index(flag) + 1
        if i < len(args) and not args[i].startswith("--"):
            return args[i]
    return None


def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else "hook"
    interactive = mode in ("report", "limits", "install-statusline", "context", "insights", "fix")
    try:
        if mode == "statusline":
            run_statusline()
        elif mode == "fix":
            run_fix(sys.argv[2:])
        elif mode == "insights":
            run_insights(int(arg_after("--days") or MCP_USAGE_DAYS))
        elif mode == "context":
            run_context(arg_after("--session"))
        elif mode == "prescan":
            refresh(load_config())  # the whole month, with no deadline
            cached_scan()
        elif mode == "sync":
            run_sync()
        elif mode == "report":
            run_report(sys.argv[2:])
        elif mode == "limits":
            run_limits(sys.argv[2:])
        elif mode == "install-statusline":
            run_install_statusline("--force" in sys.argv)
        else:
            run_hook()
    except SystemExit:
        raise
    except Exception:
        log_error(mode)
        if interactive:
            raise
    sys.exit(0)


if __name__ == "__main__":
    main()
