#!/usr/bin/env python3
"""cost-guard: spending guardrails for Claude Code that need no admin rights.

Modes:
  cost_guard.py hook                Hook for UserPromptSubmit + PreToolUse (JSON on stdin)
  cost_guard.py sync                SessionStart hook: refresh the status line copy and saved limits
  cost_guard.py statusline          Status line (JSON on stdin)
  cost_guard.py report [--days N]   Spend per session for the last N days (default 1)
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

DEFAULT_CONFIG = {
    "session_warn_usd": [10, 25, 50],  # non-blocking alerts
    "session_cap_usd": 100,  # blocks new prompts and tool calls
    "daily_cap_usd": 150,  # all sessions on this machine, local calendar day
    "cold_cache_warn_usd": 1.0,  # block once when an idle session would re-cache more than this
    "override_word": "#override",  # put this in a prompt to continue past a cap or cold-cache stop
    "context_warn_k": 150,  # one-time advice when the context passes this many thousand tokens
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
    "context_warn_k": float,
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
    cfg["never_block"] = overrides.pop("never_block", cfg["never_block"])
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


# ---------- context analysis ----------

IMAGE_TOKENS = 1600  # rough cost of one image in context
APP_SERVERS = ("ccd_",)  # the desktop app's own tools: they can't be turned off
MCP_USAGE_DAYS = 14
MCP_NAME = re.compile(rb'"name":\s*"mcp__(.+?)__')
SKILL_NAME = re.compile(rb'"skill":\s*"([^":]+):')  # plugin prefix of a Skill tool call
PLUGIN_IN_PATH = re.compile(r"plugins/(?:cache|marketplaces)/[^/]+/([^/\"' ]+)")
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


def tool_usage():
    """Calls per MCP server and per skill plugin in the last MCP_USAGE_DAYS days.
    Returns {"mcp": {server: n}, "skills": {plugin: n}}. Cached for a day."""
    path = os.path.join(STATE_DIR, "tool_usage.json")
    cache = load_json(path, {})
    if cache.get("day") == today():
        return cache.get("counts", {"mcp": {}, "skills": {}})
    counts = {"mcp": {}, "skills": {}}
    cutoff = time.time() - MCP_USAGE_DAYS * 86400

    def bump(kind, name):
        counts[kind][name] = counts[kind].get(name, 0) + 1

    for p in glob.glob(os.path.join(PROJECTS, "**", "*.jsonl"), recursive=True):
        try:
            if os.path.getmtime(p) < cutoff:
                continue
            with open(p, "rb") as f:
                for raw in f:
                    if b'"tool_use"' not in raw or b'"type":"assistant"' not in raw:
                        continue
                    if b"mcp__" in raw:
                        for m in MCP_NAME.finditer(raw):
                            bump("mcp", m.group(1).decode(errors="ignore"))
                    if b'"Skill"' in raw:
                        for m in SKILL_NAME.finditer(raw):
                            bump("skills", m.group(1).decode(errors="ignore"))
        except OSError:
            continue
    save_json(path, {"day": today(), "counts": counts})
    return counts


def skill_listing_by_plugin(text):
    """{plugin: (tokens, skill count)} from a skill listing ("- plugin:skill: description")."""
    out = {}
    for line in text.splitlines():
        if not line.startswith("- "):
            continue
        name = line[2:].split(": ", 1)[0]
        plugin = name.split(":", 1)[0] if ":" in name else NO_PLUGIN
        t, n = out.get(plugin, (0, 0))
        out[plugin] = (t + toks(line), n + 1)
    return out


def hook_label(a):
    """Which hook produced this output: its plugin, else a short form of its command."""
    cmd = a.get("command") or ""
    m = PLUGIN_IN_PATH.search(cmd)
    if m:
        return m.group(1)
    cmd = " ".join(cmd.replace('"', "").replace("${CLAUDE_PLUGIN_ROOT}/", "").split())
    event = (a.get("hookName") or a.get("hookEvent") or "hook").split(":")[0]
    return f"{event}: {cmd[:40]}" if cmd else event


def hook_tokens(a):
    """Only the text a hook adds; the attachment's metadata is not sent to the model."""
    text = a.get("content") or a.get("stdout") or ""
    return toks(text if isinstance(text, str) else json.dumps(text))


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
    skills_by_plugin, hooks_by_label, loaded_skills = {}, {}, {}
    for e in entries[start:]:
        kind = e.get("type")
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
                for plugin, (t, c) in skill_listing_by_plugin(a.get("content") or "").items():
                    old = skills_by_plugin.get(plugin, (0, 0))
                    skills_by_plugin[plugin] = (old[0] + t, max(old[1], c))
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
    by_server, system_tok, tools_tok = {}, 0, 0
    if shape:
        system_tok = toks(shape.get("system") or "")
        for t in shape.get("tools") or []:
            n = toks(t)
            tools_tok += n
            srv = server_of(t.get("name", "")) if isinstance(t, dict) else None
            if srv:
                by_server[srv] = by_server.get(srv, 0) + n
    overhead = system_tok + tools_tok
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

    if images:
        findings.append({"score": worth(image_tok), "text": (
            f"{images} image(s) ≈{k(image_tok)} ride along with every message. "
            f"/compact drops them once you no longer need them.")})

    usage = tool_usage() if (by_server or skills_by_plugin) else {"mcp": {}, "skills": {}}

    if groups["skills_list"] >= 3000:
        idle = sorted(((t, p, c) for p, (t, c) in skills_by_plugin.items()
                       if p != NO_PLUGIN and t >= 200 and not usage["skills"].get(p)),
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

    counts = usage["mcp"]
    unused = sorted(((n, s) for s, n in by_server.items()
                     if not s.startswith(APP_SERVERS) and n >= 300 and not counts.get(s)), reverse=True)
    if unused:
        save = sum(n for n, _ in unused)
        listed = ", ".join(f"{s} ≈{k(n)}" for n, s in unused[:5])
        findings.append({"score": worth(save), "text": (
            f"Every message carries ≈{k(overhead)} of system prompt and tool definitions. "
            f"Not called once in {MCP_USAGE_DAYS} days: {listed}. Turn them off (/mcp, or the "
            f"app's connector settings) to save ≈{k(save)} on every message.")})

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
            "skills_by_plugin": skills_by_plugin, "hooks_by_label": hooks_by_label,
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


def check(data, cfg):
    """Return (block_message or None, warn_message or None)."""
    sid = data.get("session_id") or "unknown"
    event = data.get("hook_event_name")
    prompt = data.get("prompt") or ""
    tool = data.get("tool_name") or ""
    if event == "PreToolUse" and any(tool.startswith(p) for p in cfg["never_block"]):
        return None, None
    path = data.get("transcript_path")
    session, daily, main = totals(cfg, path)
    sub_usd = max(session - main.get("total", 0.0), 0.0)

    def tips():
        return advice(path, cfg, session, sub_usd)

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
                     f"{cfg['override_word']} in it to continue.") + tips()

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
                         f"message (this warning shows once).") + tips()

    if not block and not warn:
        crossed = [t for t in cfg["session_warn_usd"] if session >= t and t not in st["warned"]]
        if crossed:
            st["warned"] += crossed
            warn = (f"cost-guard: this session has now cost about {fmt(session)} "
                    f"(today: {fmt(daily)}). Hard stop at {fmt(s_cap)}."
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
    files, mine = refresh(cfg, path)
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
        ("context_warn_k", f"{cfg['context_warn_k']:g}k tokens", "one-time context advice"),
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
    interactive = mode in ("report", "limits", "install-statusline", "context")
    try:
        if mode == "statusline":
            run_statusline()
        elif mode == "context":
            run_context(sys.argv[sys.argv.index("--session") + 1] if "--session" in sys.argv else None)
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
        log_error(mode)
        if interactive:
            raise
    sys.exit(0)


if __name__ == "__main__":
    main()
