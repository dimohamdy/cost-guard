"""Unit tests for plugin/scripts/cost_guard.py. Standard library only.

Run from the repository root:  python3 -m unittest discover -s tests -v

Every test runs against a throwaway HOME and plugin data folder with small
synthetic transcripts, so nothing on the machine is read or changed.
"""
import contextlib
import importlib.util
import io
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from datetime import datetime, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT = os.path.join(ROOT, "plugin", "scripts", "cost_guard.py")
TMP = tempfile.mkdtemp(prefix="cost-guard-test-")
HOME = os.path.join(TMP, "home")
DATA = os.path.join(TMP, "data")
BIN = os.path.join(TMP, "bin")
os.environ.update(HOME=HOME, CLAUDE_PLUGIN_DATA=DATA, COST_GUARD_NO_BACKGROUND="1")
for key in [k for k in os.environ if k.startswith("CLAUDE_PLUGIN_OPTION_")]:
    del os.environ[key]

spec = importlib.util.spec_from_file_location("cost_guard", SCRIPT)
cg = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cg)

PROJ = os.path.join(TMP, "work", "app")
OPUS = "claude-opus-5-5"


def tearDownModule():
    shutil.rmtree(TMP, ignore_errors=True)


def now_iso(offset_s=0):
    return datetime.fromtimestamp(time.time() + offset_s, timezone.utc).isoformat()


def assistant(mid, usd_input_tokens=0, out=0, content=None, offset_s=0, model=OPUS, cwd=PROJ, w1h=0):
    usage = {"input_tokens": usd_input_tokens, "output_tokens": out, "cache_read_input_tokens": 0,
             "cache_creation_input_tokens": w1h,
             "cache_creation": {"ephemeral_5m_input_tokens": 0, "ephemeral_1h_input_tokens": w1h}}
    return {"type": "assistant", "timestamp": now_iso(offset_s), "cwd": cwd,
            "message": {"id": mid, "model": model, "usage": usage, "content": content or []}}


def tool_use(uid, name, inp):
    return {"type": "tool_use", "id": uid, "name": name, "input": inp}


def tool_result(uid, text):
    return {"type": "user", "cwd": PROJ, "message": {"role": "user", "content": [
        {"type": "tool_result", "tool_use_id": uid, "content": text}]}}


def write_transcript(sid, entries, sub_of=None):
    folder = os.path.join(HOME, ".claude", "projects", "-work-app")
    path = os.path.join(folder, f"{sid}.jsonl")
    if sub_of:
        path = os.path.join(folder, sub_of, "subagents", f"{sid}.jsonl")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        for e in entries:
            f.write(json.dumps(e) + "\n")
    return path


def set_overrides(**values):
    values.setdefault("desktop_notify", "false")
    cg.save_json(cg.OVERRIDES_PATH, {k: str(v) if not isinstance(v, (dict, list)) else v
                                     for k, v in values.items()})


def hook(path, event="PreToolUse", tool="Bash", prompt="", sid="s1", cwd=PROJ):
    data = {"session_id": sid, "hook_event_name": event, "tool_name": tool, "prompt": prompt,
            "transcript_path": path, "cwd": cwd}
    return cg.check(data, cg.load_config(cwd))


def capture(fn, *args):
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        fn(*args)
    return buf.getvalue()


class Base(unittest.TestCase):
    def setUp(self):
        for d in (HOME, DATA, BIN, PROJ):
            shutil.rmtree(d, ignore_errors=True)
            os.makedirs(d, exist_ok=True)
        cg._HOOK_OWNERS = None
        set_overrides()


class Pricing(Base):
    def test_cost_of_prices_input_and_one_hour_cache_writes(self):
        cfg = cg.load_config()
        usd, ctx, ttl = cg.cost_of({"input_tokens": 1_000_000}, OPUS, cfg)
        self.assertAlmostEqual(usd, 4.0)
        usd, ctx, ttl = cg.cost_of({"cache_creation_input_tokens": 1_000_000, "cache_creation":
                                    {"ephemeral_1h_input_tokens": 1_000_000}}, OPUS, cfg)
        self.assertAlmostEqual(usd, 8.0)
        self.assertEqual((ctx, ttl), (1_000_000, 60))

    def test_one_response_logged_per_block_is_counted_once(self):
        e = assistant("m1", usd_input_tokens=1_000_000)
        path = write_transcript("s1", [e, e, e])
        self.assertAlmostEqual(cg.totals(cg.load_config(), path)["session"], 4.0)


class Stops(Base):
    def test_session_cap_denies_tools_but_not_app_tools_and_override_lifts_it(self):
        set_overrides(session_cap_usd=1)
        path = write_transcript("s1", [assistant("m1", usd_input_tokens=500_000)])  # $2
        block, _ = hook(path)
        self.assertIn("cost-guard stopped this", block)
        self.assertEqual(hook(path, tool="mcp__ccd_turn__update_plan"), (None, None))
        _, warn = hook(path, event="UserPromptSubmit", prompt="go on #override")
        self.assertIn("override accepted", warn)
        self.assertIsNone(hook(path)[0])

    def test_percent_warning_fires_once(self):
        set_overrides(session_cap_usd=10, warn_at_usd="100", context_warn_k=100000)
        path = write_transcript("s1", [assistant("m1", usd_input_tokens=2_125_000)])  # $8.50
        _, warn = hook(path)
        self.assertIn("85% of the session cap", warn)
        self.assertIsNone(hook(path)[1])

    def test_runaway_breaker_denies_one_tool_call(self):
        set_overrides(burst_usd_per_15min=5)
        path = write_transcript("s1", [assistant("m1", usd_input_tokens=1_000_000, offset_s=-60),
                                       assistant("m2", usd_input_tokens=1_000_000, offset_s=-30)])  # $8
        block, _ = hook(path)
        self.assertIn("in the last 15 minutes", block)
        self.assertIsNone(hook(path)[0])

    def test_monthly_cap_warns_by_default_and_stops_when_asked(self):
        set_overrides(monthly_cap_usd=1, warn_at_usd="100")
        path = write_transcript("s1", [assistant("m1", usd_input_tokens=500_000)])
        block, warn = hook(path)
        self.assertIsNone(block)
        self.assertIn("over the $1.00 monthly budget", warn)
        set_overrides(monthly_cap_usd=1, monthly_hard_stop="true", warn_at_usd="100")
        block, _ = hook(path, sid="s2")
        self.assertIn("this month's spend", block)

    def test_project_limits_apply_inside_the_folder_only(self):
        set_overrides(projects={PROJ: {"session_cap_usd": "0.5"}})
        self.assertEqual(cg.load_config(os.path.join(PROJ, "src"))["session_cap_usd"], 0.5)
        self.assertEqual(cg.load_config(os.path.join(TMP, "other"))["session_cap_usd"], 100)
        self.assertEqual(cg.load_config(PROJ + "-sibling")["session_cap_usd"], 100)

    def test_limits_project_command_and_reset_keeps_it(self):
        capture(cg.run_limits, ["project", "session_cap_usd", "7", "--cwd", PROJ])
        self.assertEqual(cg.load_config(PROJ)["session_cap_usd"], 7)
        capture(cg.run_limits, ["reset"])
        self.assertEqual(cg.load_config(PROJ)["session_cap_usd"], 7)

    def test_month_backfill_is_deferred_past_the_deadline(self):
        old = write_transcript("old", [assistant("m0", usd_input_tokens=1_000_000)])
        three_days = time.time() - 3 * 86400
        if three_days < cg.month_start_ts():
            self.skipTest("too early in the month for a 3-day-old file")
        os.utime(old, (three_days, three_days))
        path = write_transcript("s1", [assistant("m1", 10)])
        calls = []
        real, cg.scan_in_background = cg.scan_in_background, lambda: calls.append(1)
        try:
            t = cg.totals(cg.load_config(), path, deadline=time.time() - 1)
            self.assertIsNone(t["monthly"])
            self.assertEqual(calls, [1])
            self.assertAlmostEqual(cg.totals(cg.load_config(), path)["monthly"], 4.0, places=3)
        finally:
            cg.scan_in_background = real

    def test_state_files_are_private(self):
        cg.save_json(os.path.join(cg.STATE_DIR, "x.json"), {})
        mode = stat.S_IMODE(os.stat(os.path.join(cg.STATE_DIR, "x.json")).st_mode)
        self.assertEqual(mode, 0o600)


class Scanning(Base):
    def test_command_names_in_tool_output_are_ignored_and_subagent_use_counts(self):
        write_transcript("s1", [
            {"type": "user", "cwd": PROJ, "message": {"content": "<command-name>/model</command-name>"}},
            tool_result("t1", "<command-name>/loop</command-name>"),
            assistant("m1", 10),
        ])
        write_transcript("a1", [assistant("m2", 10, content=[
            tool_use("t2", "mcp__github__get_issue", {})])], sub_of="s1")
        d = cg.scan_period(14)
        self.assertEqual(d["skill_uses"].get("model"), 1)
        self.assertNotIn("loop", d["skill_uses"])
        self.assertEqual(d["mcp_uses"].get("github"), 1)
        self.assertEqual(d["messages"], 1)  # subagent messages don't count as main messages

    def test_deadline_raises(self):
        write_transcript("s1", [assistant("m1", 10)])
        with self.assertRaises(cg.ScanTimeout):
            cg.scan_period(14, deadline=time.time() - 1)


class Audits(Base):
    def test_big_claude_md_and_unused_agent_are_flagged(self):
        with open(os.path.join(PROJ, "CLAUDE.md"), "w") as f:
            f.write("rule\n" * 400)
        os.makedirs(cg.PERSONAL_AGENTS)
        with open(os.path.join(cg.PERSONAL_AGENTS, "helper.md"), "w") as f:
            f.write("---\nname: helper\ndescription: " + "Use this agent for many kinds of helpful things " * 6 + "\n---\nbody\n")
        recs = cg.setup_recs({}, {}, {"mcp_uses": {}, "agent_uses": {}}, [PROJ], lambda t: t / 1000)
        keys = {r["key"]: r for r in recs}
        self.assertIn(os.path.join(PROJ, "CLAUDE.md"), keys)
        self.assertEqual(keys["helper"]["action"], {"type": "agent_move", "target": "helper.md"})

    def test_used_server_that_wraps_an_installed_cli_is_flagged(self):
        fake = os.path.join(BIN, "gh")
        with open(fake, "w") as f:
            f.write("#!/bin/sh\n")
        os.chmod(fake, 0o755)
        old = os.environ["PATH"]
        os.environ["PATH"] = BIN + os.pathsep + old
        try:
            recs = cg.setup_recs({"github": 1000}, {"github": 5}, {"mcp_uses": {"github": 3}}, [], lambda t: t)
        finally:
            os.environ["PATH"] = old
        self.assertTrue(any("`gh` CLI" in r["text"] for r in recs))

    def test_lockfile_read_and_untrimmed_test_run_are_flagged(self):
        path = write_transcript("s1", [
            assistant("m1", 10, content=[tool_use("t1", "Read", {"file_path": "/x/package-lock.json"}),
                                         tool_use("t2", "Bash", {"command": "npm test"})]),
            tool_result("t1", "x" * 20000),
            tool_result("t2", "y" * 20000),
            assistant("m2", 10),
        ])
        texts = " ".join(f["text"] for f in cg.analyze_context(path, cg.load_config())["findings"])
        self.assertIn("package-lock.json", texts)
        self.assertIn("tail -50", texts)


class Fixes(Base):
    def listing(self, names):
        return {"type": "attachment", "attachment": {"type": "skill_listing", "content":
                "\n".join(f"- {n}: " + "a long description " * 20 for n in names)}}

    def test_skill_move_apply_and_undo(self):
        skill = os.path.join(cg.PERSONAL_SKILLS, "unused")
        os.makedirs(skill)
        open(os.path.join(skill, "SKILL.md"), "w").close()
        write_transcript("s1", [self.listing(["unused"]), assistant("m1", 10)])
        out = capture(cg.run_fix, ["apply", "skill_move:unused"])
        self.assertIn("✓", out)
        self.assertFalse(os.path.exists(skill))
        self.assertTrue(os.path.exists(cg.PERSONAL_SKILLS + "-off/unused/SKILL.md"))
        capture(cg.run_fix, ["undo"])
        self.assertTrue(os.path.exists(os.path.join(skill, "SKILL.md")))

    def test_plugin_disable_runs_claude_and_undo_reenables(self):
        log = os.path.join(TMP, "claude-calls.txt")
        fake = os.path.join(BIN, "claude")
        with open(fake, "w") as f:
            f.write(f'#!/bin/sh\necho "$@" >> "{log}"\n')
        os.chmod(fake, 0o755)
        cg.save_json(cg.SETTINGS_PATH, {"enabledPlugins": {"bulky@market": True}})
        write_transcript("s1", [self.listing(["bulky:one", "bulky:two"]), assistant("m1", 10)])
        old = os.environ["PATH"]
        os.environ["PATH"] = BIN + os.pathsep + old
        try:
            self.assertIn("would disable plugin bulky@market",
                          capture(cg.run_fix, ["apply", "plugin_disable:bulky", "--dry-run"]))
            self.assertFalse(os.path.exists(log))
            capture(cg.run_fix, ["apply", "plugin_disable:bulky"])
            capture(cg.run_fix, ["undo", "all"])
        finally:
            os.environ["PATH"] = old
        with open(log) as f:
            self.assertEqual(f.read().split("\n")[:2], ["plugin disable bulky@market", "plugin enable bulky@market"])


class Reports(Base):
    def test_report_json_by_model(self):
        write_transcript("s1", [assistant("m1", usd_input_tokens=1_000_000)])
        data = json.loads(capture(cg.run_report, ["--days", "1", "--by", "model", "--json"]))
        self.assertEqual(data["by"], "model")
        self.assertAlmostEqual(data["total_usd"], 4.0)
        self.assertEqual(data["rows"][0]["key"], OPUS)


def plan_reading(five=None, week=None, spend=None, resets_in=3 * 3600, ts=None, sid="s1"):
    """Record a status-line style rate_limits reading."""
    raw, reset = {}, time.time() + resets_in
    if five is not None:
        raw["five_hour"] = {"used_percentage": five, "resets_at": reset}
    if week is not None:
        raw["seven_day"] = {"used_percentage": week, "resets_at": reset + 3 * 86400}
    if spend is not None:
        raw["spend_limit"] = dict({"resets_at": reset + 10 * 86400}, **spend)
    return cg.record_limits(raw, "test", now=ts, session_id=sid)


class PlanLimits(Base):
    def test_plan_is_detected_and_dollar_caps_never_block(self):
        set_overrides(session_cap_usd=1, daily_cap_usd=1, monthly_cap_usd=1, monthly_hard_stop="true",
                      warn_at_usd="100", context_warn_k=100000, burst_usd_per_15min=0)
        plan_reading(five=10, week=20)
        self.assertEqual(cg.billing_mode(cg.load_config(), cg.load_limits(), "s1"), "subscription")
        self.assertEqual(cg.billing_mode(cg.load_config(), cg.load_limits(), "other"), "api")
        path = write_transcript("s1", [assistant("m1", usd_input_tokens=50_000_000)])  # $200
        block, warn = hook(path)
        self.assertIsNone(block)
        self.assertIn("$200.00 of API-equivalent usage; your 5-hour limit is at 10%", warn)
        set_overrides(session_cap_usd=1, billing="api", warn_at_usd="100")
        self.assertIn("cost-guard stopped this", hook(path, sid="s2")[0])

    def test_weekly_warning_once_per_window_then_at_95_and_again_after_reset(self):
        set_overrides(context_warn_k=100000, warn_at_usd="100")
        path = write_transcript("s1", [assistant("m1", usd_input_tokens=1000)])
        plan_reading(five=10, week=82)
        block, warn = hook(path)
        self.assertIsNone(block)
        self.assertIn("weekly limit 82% used", warn)
        self.assertIsNone(hook(path)[1])
        plan_reading(five=10, week=96)
        self.assertIn("weekly limit 96% used", hook(path)[1])
        self.assertIsNone(hook(path)[1])
        plan_reading(five=10, week=85, resets_in=4 * 3600)  # a new window instance
        self.assertIn("weekly limit 85% used", hook(path)[1])

    def test_a_plan_session_elsewhere_leaves_an_api_session_alone(self):
        set_overrides(session_cap_usd=1, warn_at_usd="100", context_warn_k=100000)
        plan_reading(five=90, week=20, sid="desktop-session")
        path = write_transcript("s1", [assistant("m1", usd_input_tokens=500_000)])  # $2, an API session
        block, _ = hook(path)
        self.assertIn("cost-guard stopped this", block)  # its dollar cap still applies
        stdin = {"transcript_path": path, "session_id": "s1"}
        out = subprocess.run([sys.executable, SCRIPT, "statusline"], capture_output=True, text=True,
                             env=dict(os.environ), input=json.dumps(stdin)).stdout
        self.assertNotIn("5h", out)
        self.assertIn("session $", out)

    def test_pace_predicts_when_the_window_fills(self):
        now = time.time()
        reset = now + 4 * 3600
        samples = [[now - 3600, 40, reset], [now - 1800, 50, reset], [now, 60, reset]]
        p = cg.limit_pace(samples, {"pct": 60, "resets_at": reset}, now)
        self.assertAlmostEqual(p["per_h"], 20)
        self.assertAlmostEqual(p["full_at"], now + 2 * 3600, delta=1)
        self.assertTrue(p["before_reset"])
        self.assertIsNone(cg.limit_pace(samples[-1:], {"pct": 60, "resets_at": reset}, now))

    def test_gateway_spend_limit_warns_and_stops_only_with_hard_stop(self):
        set_overrides(context_warn_k=100000, warn_at_usd="100")
        path = write_transcript("s1", [assistant("m1", usd_input_tokens=1000)])
        plan_reading(spend={"used_percentage": 84, "used_usd": 252, "limit_usd": 300, "period": "monthly"})
        self.assertEqual(cg.billing_mode(cg.load_config(), cg.load_limits(), "s1"), "gateway")
        block, warn = hook(path)
        self.assertIsNone(block)
        self.assertIn("company spend limit $252.00 of $300.00 monthly", warn)
        for sid in ("s2", "s3"):
            plan_reading(spend={"used_percentage": 101, "used_usd": 303, "limit_usd": 300}, sid=sid)
        self.assertIsNone(hook(path, sid="s2")[0])
        set_overrides(context_warn_k=100000, warn_at_usd="100", monthly_hard_stop="true")
        self.assertIn("spend limit", hook(path, sid="s3")[0])
        self.assertIn("override accepted", hook(path, sid="s3", event="UserPromptSubmit",
                                                prompt="#override")[1])
        self.assertIsNone(hook(path, sid="s3")[0])

    def test_record_limits_accepts_the_desktop_list_and_drops_reset_windows(self):
        reset_iso = datetime.fromtimestamp(time.time() + 3600, timezone.utc).isoformat()
        done = subprocess.run([sys.executable, SCRIPT, "record-limits", "--session", "d1"], capture_output=True, text=True,
                              env=dict(os.environ), input=json.dumps(
                                  [{"kind": "five_hour", "percentUsed": 23.5, "resetsAt": reset_iso},
                                   {"kind": "seven_day", "percentUsed": 41, "resetsAt": "2000-01-01T00:00:00Z"}]))
        self.assertEqual(done.returncode, 0, done.stderr)
        lim = cg.load_limits()
        self.assertEqual(lim["kinds"], ["five_hour", "seven_day"])
        self.assertEqual(list(lim["windows"]), ["five_hour"])  # the weekly one already reset
        self.assertEqual(cg.billing_mode(cg.load_config(), lim, "d1"), "subscription")
        self.assertAlmostEqual(lim["windows"]["five_hour"]["resets_at"], time.time() + 3600, delta=5)

    def test_panel_json_shape(self):
        write_transcript("s1", [assistant("m1", usd_input_tokens=1_000_000)])
        plan_reading(five=62, week=41)
        start = time.time()
        done = subprocess.run([sys.executable, SCRIPT, "panel-json", "--session", "s1"],
                              capture_output=True, text=True, env=dict(os.environ))
        self.assertLess(time.time() - start, 5)
        d = json.loads(done.stdout)
        self.assertEqual(d["billing"], "subscription")
        self.assertEqual([w["kind"] for w in d["limits"]], ["five_hour", "seven_day"])
        self.assertAlmostEqual(d["session"]["usd"], 4.0)
        self.assertEqual(len(d["by_day"]), 14)
        self.assertAlmostEqual(d["by_day"][-1]["usd"], 4.0)
        self.assertEqual(d["by_project"][0]["name"], "app")

    def test_status_line_shows_plan_bars_and_records_the_reading(self):
        path = write_transcript("s1", [assistant("m1", usd_input_tokens=1_000_000)])
        stdin = {"transcript_path": path, "session_id": "s1", "model": {"display_name": "Opus 5.5"},
                 "rate_limits": {"five_hour": {"used_percentage": 62, "resets_at": time.time() + 6000},
                                 "seven_day": {"used_percentage": 41, "resets_at": time.time() + 4 * 86400}}}
        done = subprocess.run([sys.executable, SCRIPT, "statusline"], capture_output=True, text=True,
                              env=dict(os.environ), input=json.dumps(stdin))
        out = done.stdout
        self.assertIn("5h ▓▓▓░░ 62%", out)
        self.assertIn("wk ▓▓░░░ 41%", out)
        self.assertIn("API value", out)
        self.assertNotIn("session $", out)
        self.assertEqual(cg.load_limits()["windows"]["five_hour"]["pct"], 62)


class FailOpen(Base):
    def test_garbage_input_exits_zero_with_no_output(self):
        done = subprocess.run([sys.executable, SCRIPT, "hook"], input="not json", capture_output=True,
                              text=True, env=dict(os.environ))
        self.assertEqual((done.returncode, done.stdout), (0, ""))


if __name__ == "__main__":
    unittest.main()
