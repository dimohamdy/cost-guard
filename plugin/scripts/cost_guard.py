#!/usr/bin/env python3
"""cost-guard: spending guardrails for Claude Code that need no admin rights.

Modes:
  cost_guard.py hook                Hook for UserPromptSubmit + PreToolUse (JSON on stdin)
  cost_guard.py sync                SessionStart hook: refresh the status line copy and saved limits
  cost_guard.py statusline          Status line (JSON on stdin)
  cost_guard.py report [--days N]   Spend per session for the last N days (default 1)
  cost_guard.py limits [KEY VALUE]  Show the limits, or override one (KEY VALUE), or `limits reset`
  cost_guard.py install-statusline [--force]   Point ~/.claude/settings.json statusLine at cost-guard

Limits come from, lowest to highest priority: built-in defaults, the plugin options
(set when the plugin is enabled, or under /config), then overrides set with `limits`.
Cost is estimated from the local transcripts (~/.claude/projects) at list prices.
Every error fails open: the guard never stops Claude Code because of its own bug.
"""
import glob
import json
import os
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

DEFAULT_CONFIG = {
    "session_warn_usd": [10, 25, 50],  # non-blocking alerts
    "session_cap_usd": 100,  # blocks new prompts and tool calls
    "daily_cap_usd": 150,  # all sessions on this machine, local calendar day
    "cold_cache_warn_usd": 1.0,  # block once when an idle session would re-cache more than this
    "override_word": "#override",  # put this in a prompt to continue past a cap or cold-cache stop
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
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = f"{path}.{os.getpid()}.tmp"
    with open(tmp, "w") as f:
        json.dump(data, f)
    os.replace(tmp, path)


LIMIT_KEYS = {  # plugin option / override key -> type
    "session_cap_usd": float,
    "daily_cap_usd": float,
    "warn_at_usd": str,  # comma-separated, e.g. "10,25,50"
    "cold_cache_warn_usd": float,
    "override_word": str,
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


def load_config():
    cfg = json.loads(json.dumps(DEFAULT_CONFIG))
    apply_limits(cfg, plugin_options())
    overrides = load_json(OVERRIDES_PATH, {})
    cfg["prices"].update(overrides.pop("prices", {}))
    apply_limits(cfg, overrides)
    return cfg


def session_state_path(sid):
    return os.path.join(STATE_DIR, "sessions", f"{sid}.json")


def today():
    return datetime.now().strftime("%Y-%m-%d")


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
        entry = {"offset": 0, "total": 0.0, "by_day": {}, "seen": [], "last_ts": None,
                 "last_ctx": 0, "ttl": None, "model": None}
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
        entry["total"] += usd
        if ctx:  # skip synthetic zero-usage entries
            entry["last_ts"], entry["last_ctx"], entry["model"] = ts, ctx, model
            if ttl:
                entry["ttl"] = ttl
    entry["seen"] = seen[-SEEN_KEEP:]
    entry["offset"] += end + 1
    return entry


def session_files(transcript_path):
    if not transcript_path:
        return []
    files = [transcript_path]
    base = transcript_path[:-6] if transcript_path.endswith(".jsonl") else transcript_path
    files += glob.glob(os.path.join(base, "**", "*.jsonl"), recursive=True)  # subagents
    return files


def refresh(cfg, transcript_path=None, window_s=SCAN_WINDOW_S):
    """Update the ledger. Returns (ledger_files, session_paths)."""
    ledger = load_json(LEDGER_PATH, {"files": {}})
    files = ledger.setdefault("files", {})
    now = time.time()
    wanted = set()
    for p in glob.glob(os.path.join(PROJECTS, "**", "*.jsonl"), recursive=True):
        try:
            if now - os.path.getmtime(p) < window_s:
                wanted.add(p)
        except OSError:
            pass
    mine = [p for p in session_files(transcript_path) if os.path.exists(p)]
    wanted.update(mine)
    for p in wanted:
        files[p] = scan_file(p, files.get(p), cfg)
    for p in list(files):  # keep the ledger small
        if p not in wanted:
            del files[p]
    save_json(LEDGER_PATH, ledger)
    return files, mine


def totals(cfg, transcript_path):
    files, mine = refresh(cfg, transcript_path)
    day = today()
    session = sum(files[p]["total"] for p in mine if p in files)
    daily = sum(e["by_day"].get(day, 0.0) for e in files.values())
    main = files.get(transcript_path) or {}
    return session, daily, main


# ---------- decisions ----------

def fmt(usd):
    return f"${usd:,.2f}"


def check(data, cfg):
    """Return (block_message or None, warn_message or None)."""
    sid = data.get("session_id") or "unknown"
    event = data.get("hook_event_name")
    prompt = data.get("prompt") or ""
    session, daily, main = totals(cfg, data.get("transcript_path"))

    st_path = session_state_path(sid)
    st = load_json(st_path, {"warned": [], "extra": 0.0, "cold_ack": None})
    daily_st_path = os.path.join(STATE_DIR, "daily.json")
    daily_st = load_json(daily_st_path, {})
    daily_extra = daily_st.get(today(), 0.0)

    s_cap = cfg["session_cap_usd"] + st["extra"]
    d_cap = cfg["daily_cap_usd"] + daily_extra
    override = event == "UserPromptSubmit" and cfg["override_word"] in prompt
    block = warn = None

    over = []
    if session >= s_cap:
        over.append(f"this session has cost about {fmt(session)} (cap {fmt(s_cap)})")
    if daily >= d_cap:
        over.append(f"today's spend on this machine is about {fmt(daily)} (cap {fmt(d_cap)})")
    if over:
        if override:
            # New cap = current spend + one more cap-sized allowance.
            if session >= s_cap:
                st["extra"] = session
            if daily >= d_cap:
                save_json(daily_st_path, {today(): daily})
            warn = (f"cost-guard: override accepted; you have another "
                    f"{fmt(cfg['session_cap_usd'])} before the next stop.")
        else:
            block = ("cost-guard stopped this: " + "; ".join(over) + ". "
                     f"Start a fresh session with a short summary, or re-send your message with "
                     f"{cfg['override_word']} in it to continue.")

    # Idle session with a large context: the next request re-writes the whole cache.
    if not block and event == "UserPromptSubmit" and main.get("last_ts") and main.get("ttl"):
        idle_min = (time.time() - main["last_ts"]) / 60
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
                         f"message (this warning shows once).")

    if not block and not warn:
        crossed = [t for t in cfg["session_warn_usd"] if session >= t and t not in st["warned"]]
        if crossed:
            st["warned"] += crossed
            warn = (f"cost-guard: this session has now cost about {fmt(session)} "
                    f"(today: {fmt(daily)}). Hard stop at {fmt(s_cap)}. "
                    f"Tip: /clear between tasks keeps each message cheap.")

    save_json(st_path, st)
    return block, warn


def run_hook():
    data = json.load(sys.stdin)
    cfg = load_config()
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
    cfg = load_config()
    session, daily, main = totals(cfg, data.get("transcript_path"))
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

def run_report(days):
    cfg = load_config()
    files, _ = refresh(cfg, None, window_s=days * 86400)
    cutoff = datetime.fromtimestamp(time.time() - days * 86400).strftime("%Y-%m-%d")
    rows = []
    for p, e in files.items():
        usd = sum(v for d, v in e["by_day"].items() if d >= cutoff)
        if usd >= 0.01:
            rows.append((usd, p))
    rows.sort(reverse=True)
    print(f"Spend estimate for the last {days} day(s), list prices:\n")
    for usd, p in rows[:20]:
        rel = os.path.relpath(p, PROJECTS)
        print(f"  {fmt(usd):>10}  {rel}")
    print(f"\n  {fmt(sum(r[0] for r in rows)):>10}  total across {len(rows)} transcript(s)")


# ---------- limits, sync, status line setup ----------

def run_limits(args):
    overrides = load_json(OVERRIDES_PATH, {})
    if args[:1] == ["reset"]:
        overrides = {}
        save_json(OVERRIDES_PATH, overrides)
        print("Overrides cleared; using the plugin options.")
    elif len(args) >= 2:
        key, val = args[0], ",".join(args[1:]) if args[0] == "warn_at_usd" else args[1]
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
        ("warn_at_usd", ", ".join(fmt(x) for x in cfg["session_warn_usd"]) or "none", "one-time alerts"),
        ("cold_cache_warn_usd", fmt(cfg["cold_cache_warn_usd"]), "stop once before an expensive re-cache"),
        ("override_word", cfg["override_word"], "type this to continue past a stop"),
    ]
    for key, val, what in rows:
        src = "override" if key in overrides else "plugin option" if key in session else "default"
        print(f"  {key:<20} {val:<22} {what}  [{src}]")


def run_sync():
    """SessionStart: keep a copy of this script in the data folder for the status line."""
    plugin_options()
    src = os.path.abspath(__file__)
    dst = os.path.join(DATA, "cost_guard.py")
    if src != dst:
        os.makedirs(DATA, exist_ok=True)
        shutil.copy2(src, dst)


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


def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else "hook"
    interactive = mode in ("report", "limits", "install-statusline")
    try:
        if mode == "statusline":
            run_statusline()
        elif mode == "sync":
            run_sync()
        elif mode == "report":
            days = int(sys.argv[sys.argv.index("--days") + 1]) if "--days" in sys.argv else 1
            run_report(days)
        elif mode == "limits":
            run_limits(sys.argv[2:])
        elif mode == "install-statusline":
            run_install_statusline("--force" in sys.argv)
        else:
            run_hook()
    except SystemExit:
        raise
    except Exception:
        try:
            os.makedirs(STATE_DIR, exist_ok=True)
            with open(os.path.join(STATE_DIR, "errors.log"), "a") as f:
                f.write(f"{datetime.now().isoformat()} {mode}\n{traceback.format_exc()}\n")
        except Exception:
            pass
        if interactive:
            raise
    sys.exit(0)


if __name__ == "__main__":
    main()
