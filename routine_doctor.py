#!/usr/bin/env python3
"""routine-doctor — did your Claude Code scheduled tasks actually do their job?

The desktop app shows a green "ran" status. The official docs say that status does not
mean the task in your prompt succeeded. This tool reads the files Claude Code already
writes on your machine and answers, per task:

  * did it run when the schedule said it should (missed / late / ghost runs)?
  * what did each run cost (API list-price equivalent) and which model did it use?
  * did it hit a usage limit, error out, or finish?
  * did it push a notification, write files, or finish silently?
  * is the prompt rotting (growing, stale dates, dead file paths, embedded code)?
  * did it meet the outcome contract you declared for it?

Reads local files only. Nothing leaves your computer. Zero dependencies (Python 3.9+).

Usage:
  python3 routine_doctor.py                 # doctor = report + lint + contracts, last 7 days
  python3 routine_doctor.py report --days 14
  python3 routine_doctor.py lint
  python3 routine_doctor.py contracts
  python3 routine_doctor.py tasks           # what the app has registered
  python3 routine_doctor.py --json ...      # machine-readable
Exit code 1 when any task has a red flag (useful from cron or CI).

Paths (override with env vars when your layout differs):
  ROUTINE_DOCTOR_APP_DIR   the desktop app's data dir (macOS: ~/Library/Application Support/Claude)
  ROUTINE_DOCTOR_PROJECTS  ~/.claude/projects      (session transcripts, *.jsonl)
  ROUTINE_DOCTOR_TASKS     ~/.claude/scheduled-tasks (one folder per task, SKILL.md = prompt)
  ROUTINE_DOCTOR_STATE     ~/.routine-doctor        (prompt snapshots for growth checks)
"""
from __future__ import annotations

import argparse
import collections
import datetime as dt
import glob
import hashlib
import json
import os
import re
import statistics
import sys
from pathlib import Path

VERSION = "0.1.0"
HOME = Path.home()


# ----------------------------------------------------------------------------- paths
def app_dir() -> Path:
    env = os.environ.get("ROUTINE_DOCTOR_APP_DIR")
    if env:
        return Path(env).expanduser()
    if sys.platform == "darwin":
        return HOME / "Library/Application Support/Claude"
    if sys.platform.startswith("win"):
        return Path(os.environ.get("APPDATA", HOME / "AppData/Roaming")) / "Claude"
    return Path(os.environ.get("XDG_CONFIG_HOME", HOME / ".config")) / "Claude"


def projects_dir() -> Path:
    return Path(os.environ.get("ROUTINE_DOCTOR_PROJECTS", HOME / ".claude/projects")).expanduser()


def tasks_dir() -> Path:
    return Path(os.environ.get("ROUTINE_DOCTOR_TASKS", HOME / ".claude/scheduled-tasks")).expanduser()


def state_dir() -> Path:
    return Path(os.environ.get("ROUTINE_DOCTOR_STATE", HOME / ".routine-doctor")).expanduser()


# ----------------------------------------------------------------------------- pricing
# $ per million tokens: (input, output, cache read). Cache write = 1.25x input (5-minute
# TTL) or 2x input (1-hour TTL). These are API list prices; on a subscription your real
# cost is plan quota, so treat the dollar column as "API equivalent", not a bill.
# Last checked 2026-10-06. Unknown models fall back to Opus-class pricing and are listed.
PRICE = {
    "claude-fable-5-1": (10, 50, 0.25), "claude-fable-5": (10, 50, 1.0),
    "claude-opus-5-5": (4, 20, 0.20), "claude-opus-5": (5, 25, 0.5),
    "claude-opus-4-8": (5, 25, 0.5), "claude-opus-4-7": (5, 25, 0.5), "claude-opus-4-6": (5, 25, 0.5),
    "claude-sonnet-5-5": (2, 10, 0.20), "claude-sonnet-5": (2, 10, 0.2), "claude-sonnet-4-6": (3, 15, 0.3),
    "claude-haiku-5-5": (0.10, 0.50, 0.01), "claude-haiku-4-5": (1, 5, 0.1),
}
UNKNOWN_MODELS: set = set()


def price(model):
    for k in sorted(PRICE, key=len, reverse=True):
        if model and model.startswith(k):
            return PRICE[k]
    if model:
        UNKNOWN_MODELS.add(model)
    return (4, 20, 0.2)


def call_cost(model, u) -> float:
    pin, pout, pread = price(model)
    cc = u.get("cache_creation") or {}
    w5, w1 = cc.get("ephemeral_5m_input_tokens"), cc.get("ephemeral_1h_input_tokens")
    if w5 is None and w1 is None:
        w5, w1 = u.get("cache_creation_input_tokens", 0) or 0, 0
    return ((u.get("input_tokens", 0) or 0) * pin
            + (w5 or 0) * pin * 1.25 + (w1 or 0) * pin * 2
            + (u.get("cache_read_input_tokens", 0) or 0) * pread
            + (u.get("output_tokens", 0) or 0) * pout) / 1e6


# ----------------------------------------------------------------------------- cron
_DOW_NAMES = {"sun": 0, "mon": 1, "tue": 2, "wed": 3, "thu": 4, "fri": 5, "sat": 6}
_MON_NAMES = {"jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6, "jul": 7, "aug": 8,
              "sep": 9, "oct": 10, "nov": 11, "dec": 12}


def _field(spec: str, lo: int, hi: int, names=None):
    """Return (set_of_values, restricted). '*' means unrestricted."""
    out, restricted = set(), spec.strip() != "*"
    for part in spec.split(","):
        part = part.strip().lower()
        step = 1
        if "/" in part:
            part, s = part.split("/", 1)
            step = int(s)
        if part in ("*", ""):
            a, b = lo, hi
        elif "-" in part:
            a, b = part.split("-", 1)
            a, b = _val(a, names), _val(b, names)
        else:
            a = b = _val(part, names)
        if a > b:  # wrap (e.g. 5-1 for dow)
            vals = list(range(a, hi + 1)) + list(range(lo, b + 1))
        else:
            vals = list(range(a, b + 1))
        out.update(vals[::step])
    return out, restricted


def _val(tok, names):
    if names and tok in names:
        return names[tok]
    return int(tok)


def cron_occurrences(expr: str, start: dt.datetime, end: dt.datetime):
    """All local-time moments in [start, end) when a 5-field cron expression fires."""
    parts = expr.split()
    if len(parts) != 5:
        raise ValueError(f"not a 5-field cron: {expr!r}")
    mins, _ = _field(parts[0], 0, 59)
    hours, _ = _field(parts[1], 0, 23)
    doms, dom_r = _field(parts[2], 1, 31)
    mons, _ = _field(parts[3], 1, 12, _MON_NAMES)
    dows, dow_r = _field(parts[4], 0, 7, _DOW_NAMES)
    if 7 in dows:
        dows.add(0)
    out = []
    day = start.replace(hour=0, minute=0, second=0, microsecond=0)
    while day < end:
        pydow = (day.weekday() + 1) % 7  # Monday=0 in Python -> Sunday=0 in cron
        dom_ok, dow_ok = day.day in doms, pydow in dows
        day_ok = (dom_ok or dow_ok) if (dom_r and dow_r) else (dom_ok and dow_ok)
        if day.month in mons and day_ok:
            for h in sorted(hours):
                for m in sorted(mins):
                    t = day.replace(hour=h, minute=m)
                    if start <= t < end:
                        out.append(t)
        day += dt.timedelta(days=1)
    return out


def describe_schedule(task) -> str:
    if task.get("cronExpression"):
        return task["cronExpression"]
    if task.get("fireAt"):
        return "once " + ms_to_local(task["fireAt"]).strftime("%Y-%m-%d %H:%M")
    return "ad-hoc"


# ----------------------------------------------------------------------------- loading
def local_tz():
    return dt.datetime.now().astimezone().tzinfo


def ms_to_local(ms) -> dt.datetime:
    return dt.datetime.fromtimestamp(int(ms) / 1000, tz=dt.timezone.utc).astimezone(local_tz())


def iso_to_local(s) -> dt.datetime:
    return dt.datetime.fromisoformat(str(s).replace("Z", "+00:00")).astimezone(local_tz())


def load_registry() -> list:
    """Scheduled tasks as the desktop app registered them (merged across workspaces)."""
    tasks = {}
    for f in sorted(glob.glob(str(app_dir() / "claude-code-sessions/*/*/scheduled-tasks.json"))):
        try:
            d = json.load(open(f, encoding="utf-8"))
        except (ValueError, OSError):
            continue
        for t in d.get("scheduledTasks", []):
            if t.get("id"):
                tasks[t["id"]] = t
    # tasks that exist on disk but are not registered (e.g. wiped by an app update, gh#85565)
    if tasks_dir().is_dir():
        for p in tasks_dir().iterdir():
            if (p / "SKILL.md").is_file() and p.name not in tasks:
                tasks[p.name] = {"id": p.name, "unregistered": True, "filePath": str(p / "SKILL.md")}
    return sorted(tasks.values(), key=lambda t: t["id"])


def load_sessions() -> list:
    """Session records of scheduled runs: which task, which transcript, which model."""
    out = []
    for f in glob.glob(str(app_dir() / "claude-code-sessions/*/*/local_*.json")):
        try:
            d = json.load(open(f, encoding="utf-8"))
        except (ValueError, OSError):
            continue
        if not d.get("scheduledTaskId"):
            continue
        out.append(dict(task=d["scheduledTaskId"], sid=d.get("cliSessionId"),
                        created=ms_to_local(d["createdAt"]) if d.get("createdAt") else None,
                        model=d.get("model"), effort=d.get("effort"), title=d.get("title"),
                        turns=d.get("completedTurns")))
    return out


def find_transcript(sid):
    if not sid:
        return None
    hits = glob.glob(str(projects_dir() / "*" / f"{sid}.jsonl"))
    return hits[0] if hits else None


def _text_of(c):
    if isinstance(c, list):
        return " ".join(x.get("text", "") for x in c if isinstance(x, dict) and x.get("type") == "text")
    return c or ""


PUSH_TOOL_RE = re.compile(r"push|notif|ntfy|pushover|telegram|slack|imessage|osascript.*display", re.I)


def parse_transcript(path) -> dict:
    calls, limit_hits, errors, tools = {}, [], [], collections.Counter()
    files, first_ts, last_ts, last_text, pushed = set(), None, None, "", False
    for line in open(path, errors="replace", encoding="utf-8"):
        try:
            d = json.loads(line)
        except ValueError:
            continue
        ts = d.get("timestamp")
        if ts:
            first_ts = first_ts or ts
            last_ts = ts
        if d.get("type") != "assistant":
            continue
        m = d.get("message", {})
        if d.get("isApiErrorMessage") or m.get("model") == "<synthetic>":
            txt = _text_of(m.get("content"))
            low = txt.lower()
            if "limit" in low and ("session" in low or "usage" in low or "rate" in low or "quota" in low):
                limit_hits.append(ts)
            elif txt.strip():
                errors.append(txt[:120])
            continue
        for x in m.get("content") or []:
            if not isinstance(x, dict):
                continue
            if x.get("type") == "tool_use":
                name = x.get("name") or "?"
                tools[name] += 1
                inp = x.get("input") or {}
                fp = inp.get("file_path") or inp.get("path")
                if name in ("Write", "Edit", "MultiEdit", "NotebookEdit") and fp:
                    files.add(str(fp))
                blob = name + " " + json.dumps(inp, ensure_ascii=False)[:400]
                if PUSH_TOOL_RE.search(blob):
                    pushed = True
            elif x.get("type") == "text" and x.get("text", "").strip():
                last_text = x["text"]
        u = m.get("usage")
        if u and ts:
            calls.setdefault(m.get("id") or d.get("uuid"), (ts, m.get("model"), u))
    return dict(calls=sorted(calls.values(), key=lambda c: c[0]), limit_hits=limit_hits, errors=errors,
                tools=tools, files=sorted(files), pushed=pushed, first=first_ts, last=last_ts,
                last_text=last_text.strip().replace("\n", " ")[:200])


# ----------------------------------------------------------------------------- analysis
def build_runs(sessions, start, end) -> dict:
    """task id -> list of run dicts inside [start, end)."""
    runs = collections.defaultdict(list)
    for s in sessions:
        if not s["created"] or not (start <= s["created"] < end):
            continue
        path = find_transcript(s["sid"])
        t = parse_transcript(path) if path else None
        calls = t["calls"] if t else []
        cost = sum(call_cost(m, u) for _, m, u in calls)
        models = collections.Counter(m for _, m, _ in calls)
        started = iso_to_local(calls[0][0]) if calls else s["created"]
        ended = iso_to_local(t["last"]) if t and t["last"] else started
        if not t:
            outcome = "ghost"       # the app says it ran; there is no transcript at all
        elif not calls:
            outcome = "error"       # transcript exists, but not one API call succeeded
        elif t["limit_hits"]:
            outcome = "limit"
        elif t["errors"] and len(calls) <= 2:
            outcome = "error"
        elif t["errors"]:
            outcome = "ok*"         # finished, but with API errors on the way
        else:
            outcome = "ok"
        runs[s["task"]].append(dict(
            start=started, end=ended, minutes=round((ended - started).total_seconds() / 60, 1),
            calls=len(calls), cost=round(cost, 2), models="+".join(sorted(models)) or (s["model"] or "?"),
            primary_model=(models.most_common(1)[0][0] if models else (s["model"] or "?")),
            effort=s["effort"], outcome=outcome, pushed=bool(t and t["pushed"]),
            files=t["files"] if t else [], tools=dict(t["tools"]) if t else {},
            errors=t["errors"][:3] if t else [], last_text=t["last_text"] if t else "",
            out_tokens=sum((u.get("output_tokens") or 0) for _, _, u in calls), sid=s["sid"]))
    for k in runs:
        runs[k].sort(key=lambda r: r["start"])
    return runs


def _any_to_local(v):
    """The app stores times as epoch milliseconds (int or str) or ISO strings."""
    if v is None:
        return None
    try:
        return ms_to_local(v)
    except (ValueError, TypeError):
        try:
            return iso_to_local(v)
        except (ValueError, TypeError):
            return None


def expected_times(task, start, end, first_known_run=None):
    """When the schedule should have fired inside [start, end).
    createdAt is rewritten whenever a task is edited, so a run that happened before it proves
    the task existed earlier; the app's own missedRunScanFloor, when present, wins."""
    if task.get("enabled") is False:
        return []
    floor = _any_to_local(task.get("createdAt"))
    if floor and first_known_run and first_known_run < floor:
        floor = first_known_run
    scan_floor = _any_to_local(task.get("missedRunScanFloor"))
    if scan_floor and (floor is None or scan_floor > floor):
        floor = scan_floor
    if floor:
        start = max(start, floor)
    if task.get("cronExpression"):
        try:
            return cron_occurrences(task["cronExpression"], start, end)
        except (ValueError, KeyError):
            return []
    if task.get("fireAt"):
        t = ms_to_local(task["fireAt"])
        return [t] if start <= t < end else []
    return []


GRACE_BEFORE = dt.timedelta(minutes=10)
LATE_AFTER = dt.timedelta(minutes=30)


def match_schedule(expected, runs, now):
    """Pair each expected moment with the first unused run that starts after it (minus a small
    grace) and before the next expected moment. The desktop app runs a due task on next launch
    if it was closed, so a run can be hours late and still belong to that slot.
    Returns (missed: [t], late: [(t, start)], extra: [run]) — extra = manual "Run now" runs."""
    expected = sorted(t for t in expected if t <= now)
    missed, late, used = [], [], set()
    for i, t in enumerate(expected):
        nxt = expected[i + 1] if i + 1 < len(expected) else now
        hit = None
        for j, r in enumerate(runs):
            if j in used:
                continue
            if t - GRACE_BEFORE <= r["start"] < nxt:
                hit = j
                break
        if hit is None:
            missed.append(t)
        else:
            used.add(hit)
            if runs[hit]["start"] - t > LATE_AFTER:
                late.append((t, runs[hit]["start"]))
    extra = [r for j, r in enumerate(runs) if j not in used]
    return missed, late, extra


def task_flags(task, runs, missed, late, extra, history_runs):
    flags = []
    if extra and task.get("cronExpression"):
        flags.append(f"⚪ {len(extra)} run(s) outside the schedule (manual 'Run now' or retries)")
    if task.get("unregistered"):
        flags.append("🔴 on disk but not registered in the app (deleted or wiped?)")
    if task.get("enabled") is False:
        flags.append("⚪ disabled")
    if missed:
        when = ", ".join(m.strftime("%a %m-%d %H:%M") for m in missed[:3]) + (" …" if len(missed) > 3 else "")
        flags.append(f"🔴 missed {len(missed)} scheduled run(s): {when}")
    ghosts = [r for r in runs if r["outcome"] == "ghost"]
    if ghosts:
        flags.append(f"🔴 {len(ghosts)} ghost run(s): the app says it ran, but there is no transcript")
    errs = [r for r in runs if r["outcome"] == "error"]
    if errs:
        flags.append(f"🔴 {len(errs)} run(s) did no work: API error before any call succeeded")
    lim = [r for r in runs if r["outcome"] == "limit"]
    if lim:
        flags.append(f"🟠 {len(lim)} run(s) hit a usage limit")
    if late:
        worst = max(late, key=lambda x: x[1] - x[0])
        hrs = (worst[1] - worst[0]).total_seconds() / 3600
        flags.append(f"🟡 {len(late)} late run(s); worst {hrs:.1f} h after schedule (app closed at the time?)")
    costs = [r["cost"] for r in history_runs if r["calls"]]
    if len(costs) >= 4:
        med = statistics.median(costs)
        spikes = [r for r in runs if r["calls"] and med > 0 and r["cost"] > 2.5 * med]
        if spikes:
            flags.append(f"🟡 cost spike: {len(spikes)} run(s) > 2.5x this task's median ${med:.2f}")
    models = collections.Counter(r["primary_model"] for r in history_runs if r["calls"])
    if len(models) > 1:
        mix = ", ".join(f"{m} ×{n}" for m, n in models.most_common())
        flags.append(f"🟡 model drift across recent runs: {mix}")
    ok_runs = [r for r in runs if r["calls"]]
    if ok_runs and not any(r["pushed"] for r in ok_runs) and not any(r["files"] for r in ok_runs):
        flags.append("⚪ no pushes and no files written: silent by design, or silently useless?")
    return flags


# ----------------------------------------------------------------------------- lint
MUST_RE = re.compile(r"必须|不得|绝不|禁止|务必|一律|切勿|千万|\bCRITICAL\b|\bMUST\b|\bNEVER\b|\bIMPORTANT\b|\bALWAYS\b")
DATE_RE = re.compile(r"\b(20\d\d)-(\d\d)-(\d\d)\b")
HEREDOC_RE = re.compile(r"<<-?\s*['\"]?([A-Z_]{2,})['\"]?(?:\s|$)")
PATH_RE = re.compile(r"(?<![\w/])((?:~|/Users/[^/\s]+|/home/[^/\s]+)/[\w./@-]+)")
MAX_CHARS, MAX_GROWTH, MAX_HEREDOC, MAX_MUST_PER_K, STALE_DAYS = 30000, 0.20, 2000, 6.0, 90


def _heredoc_chars(text):
    total, end = 0, None
    for line in text.splitlines(keepends=True):
        if end is None:
            m = HEREDOC_RE.search(line.rstrip("\n"))
            if m:
                end = m.group(1)
        elif line.strip() == end:
            end = None
        else:
            total += len(line)
    return total


def lint_task(task, today: dt.date, snapshots: dict) -> list:
    fp = task.get("filePath") or str(tasks_dir() / task["id"] / "SKILL.md")
    p = Path(fp).expanduser()
    if not p.is_file():
        return [f"🔴 prompt file missing: {fp}"]
    text = p.read_text(encoding="utf-8", errors="replace")
    body = re.sub(r"^---.*?---\s*", "", text, count=1, flags=re.S)
    n = len(body)
    out = []
    if n > MAX_CHARS:
        out.append(f"🟡 prompt is {n:,} chars (> {MAX_CHARS:,}); long prompts rot fastest")
    prev = snapshots.get(task["id"])
    if prev and prev.get("chars") and n > prev["chars"] * (1 + MAX_GROWTH):
        out.append(f"🟡 grew {n / prev['chars'] - 1:+.0%} since {prev['at'][:10]} ({prev['chars']:,} → {n:,} chars)")
    hd = _heredoc_chars(body)
    if hd > MAX_HEREDOC:
        out.append(f"🟡 {hd:,} chars of code embedded in the prompt (heredoc); move it to a file the prompt calls")
    per_k = len(MUST_RE.findall(body)) / max(n, 1) * 1000
    if per_k > MAX_MUST_PER_K:
        out.append(f"🟡 {per_k:.1f} MUST/NEVER-style words per 1,000 chars; shouting usually means a rule the model keeps breaking — fix the rule, not the volume")
    stale = []
    for y, mo, d in DATE_RE.findall(body):
        try:
            when = dt.date(int(y), int(mo), int(d))
        except ValueError:
            continue
        if (today - when).days > STALE_DAYS:
            stale.append(f"{y}-{mo}-{d}")
    if stale:
        out.append(f"⚪ {len(stale)} hard-coded date(s) older than {STALE_DAYS} days (e.g. {stale[0]}); fine for facts, a smell for 'today' logic")
    dead = []
    for m in sorted(set(PATH_RE.findall(body))):
        if re.search(r"YYYY|MM-DD|<|>|\*|\{|\}|\$", m) or m.endswith(("-", "/", ".")):
            continue  # a template, not a path
        q = Path(m).expanduser()
        if not q.exists() and not q.parent.exists():
            dead.append(m)
    if dead:
        out.append(f"🔴 {len(dead)} path(s) whose folder does not exist (e.g. {dead[0]}); the prompt will fail there")
    snapshots[task["id"]] = dict(chars=n, sha=hashlib.sha256(body.encode()).hexdigest()[:12],
                                 at=dt.datetime.now().isoformat(timespec="seconds"))
    return out


def load_snapshots() -> dict:
    f = state_dir() / "prompts.json"
    try:
        return json.load(open(f, encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def save_snapshots(s: dict):
    state_dir().mkdir(parents=True, exist_ok=True)
    json.dump(s, open(state_dir() / "prompts.json", "w", encoding="utf-8"), ensure_ascii=False, indent=1)


# ----------------------------------------------------------------------------- contracts
def load_contract(task) -> dict | None:
    """~/.claude/scheduled-tasks/<task>/contract.json — what should be true after a run.
    {"expect": [{"tool": "PushNotification"}, {"file_modified": "~/x.md"},
                {"text": "regex on the final message"}, {"max_cost": 5}, {"min_calls": 3},
                {"max_minutes": 30}]}"""
    f = tasks_dir() / task["id"] / "contract.json"
    if not f.is_file():
        return None
    try:
        return json.load(open(f, encoding="utf-8"))
    except (ValueError, OSError) as e:
        return {"error": str(e)}


def check_contract(contract, run) -> list:
    """Return list of (ok, description)."""
    res = []
    for e in contract.get("expect", []):
        if "tool" in e:
            ok = run["tools"].get(e["tool"], 0) > 0
            res.append((ok, f"tool {e['tool']} called"))
        elif "file_modified" in e:
            p = Path(e["file_modified"]).expanduser()
            ok = p.exists() and dt.datetime.fromtimestamp(p.stat().st_mtime, tz=local_tz()) >= run["start"]
            res.append((ok, f"{e['file_modified']} modified during/after the run"))
        elif "text" in e:
            ok = re.search(e["text"], run["last_text"] or "") is not None
            res.append((ok, f"final message matches /{e['text']}/"))
        elif "max_cost" in e:
            res.append((run["cost"] <= e["max_cost"], f"cost ${run['cost']} ≤ ${e['max_cost']}"))
        elif "min_calls" in e:
            res.append((run["calls"] >= e["min_calls"], f"≥ {e['min_calls']} API calls (got {run['calls']})"))
        elif "max_minutes" in e:
            res.append((run["minutes"] <= e["max_minutes"], f"finished within {e['max_minutes']} min (took {run['minutes']})"))
        else:
            res.append((False, f"unknown expectation {e}"))
    return res


# ----------------------------------------------------------------------------- report
def analyze(days: int, now=None) -> dict:
    now = now or dt.datetime.now(local_tz())
    start = now - dt.timedelta(days=days)
    hist_start = now - dt.timedelta(days=max(days * 4, 28))
    tasks = load_registry()
    sessions = load_sessions()
    runs = build_runs(sessions, start, now)
    hist = build_runs(sessions, hist_start, now)
    snapshots = load_snapshots()
    today = now.date()
    rows, red = [], False
    for t in tasks:
        tr = runs.get(t["id"], [])
        h = hist.get(t["id"], [])
        exp = expected_times(t, start, now, h[0]["start"] if h else None)
        missed, late, extra = match_schedule(exp, tr, now)
        flags = task_flags(t, tr, missed, late, extra, hist.get(t["id"], []))
        lint = lint_task(t, today, snapshots)
        contract = load_contract(t)
        cres = []
        if contract and "expect" in contract:
            for r in tr:
                if r["calls"]:
                    cres.append(dict(start=r["start"], checks=check_contract(contract, r)))
            fails = [c for c in cres if not all(ok for ok, _ in c["checks"])]
            if fails:
                flags.append(f"🔴 contract failed in {len(fails)}/{len(cres)} run(s)")
            elif cres:
                flags.append(f"✅ contract met in {len(cres)}/{len(cres)} run(s)")
        elif contract and "error" in contract:
            flags.append(f"🔴 contract.json unreadable: {contract['error']}")
        cost = round(sum(r["cost"] for r in tr), 2)
        last = tr[-1] if tr else None
        red = red or any(f.startswith("🔴") for f in flags + lint)
        rows.append(dict(task=t["id"], schedule=describe_schedule(t), enabled=t.get("enabled", True),
                         expected=len([e for e in exp if e <= now]), ran=len(tr), missed=len(missed),
                         late=len(late), missed_at=[m.strftime("%m-%d %H:%M") for m in missed],
                         last_run=last["start"].strftime("%m-%d %H:%M") if last else "—",
                         last_outcome=last["outcome"] if last else "—",
                         pushed=sum(1 for r in tr if r["pushed"]), cost=cost,
                         median_cost=round(statistics.median([r["cost"] for r in tr]), 2) if tr else 0,
                         models=sorted({r["models"] for r in tr}), flags=flags, lint=lint,
                         runs=tr, contract=cres, has_contract=bool(contract and "expect" in contract)))
    save_snapshots(snapshots)
    return dict(version=VERSION, generated=now.isoformat(timespec="minutes"), days=days,
                tasks=rows, unknown_models=sorted(UNKNOWN_MODELS), red=red,
                total_cost=round(sum(r["cost"] for r in rows), 2),
                total_runs=sum(r["ran"] for r in rows))


def fmt_report(a: dict, sections=("report", "lint", "contracts")) -> str:
    out = [f"# routine-doctor — last {a['days']} days (generated {a['generated']})", ""]
    out.append(f"{len(a['tasks'])} tasks · {a['total_runs']} runs · ${a['total_cost']} API-equivalent · "
               + ("**red flags present**" if a["red"] else "no red flags"))
    out.append("")
    if "report" in sections:
        out += ["| task | schedule | expected | ran | missed | last run | outcome | pushed | $ total | $ median | flags |",
                "|---|---|---|---|---|---|---|---|---|---|---|"]
        for r in a["tasks"]:
            fl = " ".join(f[0] for f in r["flags"]) or "—"
            out.append(f"| {r['task']} | `{r['schedule']}` | {r['expected']} | {r['ran']} | {r['missed']} | {r['last_run']} | "
                       f"{r['last_outcome']} | {r['pushed']}/{r['ran']} | {r['cost']} | {r['median_cost']} | {fl} |")
        out.append("")
        flagged = [r for r in a["tasks"] if r["flags"]]
        if flagged:
            out.append("## Flags")
            for r in flagged:
                out.append(f"**{r['task']}**")
                out += [f"- {f}" for f in r["flags"]]
                for run in r["runs"]:
                    if run["outcome"] in ("ghost", "error", "limit"):
                        why = "; ".join(run["errors"]) or run["last_text"] or "no transcript"
                        out.append(f"  - {run['start'].strftime('%m-%d %H:%M')} {run['outcome']}: {why[:140]}")
            out.append("")
    if "lint" in sections:
        linted = [r for r in a["tasks"] if r["lint"]]
        out.append("## Prompt lint")
        if not linted:
            out.append("all prompts clean")
        for r in linted:
            out.append(f"**{r['task']}**")
            out += [f"- {f}" for f in r["lint"]]
        out.append("")
    if "contracts" in sections:
        withc = [r for r in a["tasks"] if r["contract"]]
        declared = [r for r in a["tasks"] if r.get("has_contract")]
        out.append("## Outcome contracts")
        if not declared:
            out.append("no contract.json found in any task folder — add one to turn 'it ran' into 'it worked'. "
                       "Example: {\"expect\": [{\"tool\": \"PushNotification\"}, {\"file_modified\": \"~/report.md\"}]}")
        elif not withc:
            out.append(f"{len(declared)} task(s) declare a contract ({', '.join(r['task'] for r in declared)}); no runs in the window to check yet")
        for r in withc:
            out.append(f"**{r['task']}**")
            for c in r["contract"]:
                marks = ", ".join(("✅ " if ok else "❌ ") + d for ok, d in c["checks"])
                out.append(f"- {c['start'].strftime('%m-%d %H:%M')}: {marks}")
        out.append("")
    if a["unknown_models"]:
        out.append(f"_Unknown model id(s) priced as Opus-class: {', '.join(a['unknown_models'])}. Edit PRICE in routine_doctor.py._")
    return "\n".join(out)


def fmt_tasks(tasks) -> str:
    out = ["| task | schedule | enabled | last run (app) | prompt file |", "|---|---|---|---|---|"]
    for t in tasks:
        last = iso_to_local(t["lastRunAt"]).strftime("%Y-%m-%d %H:%M") if t.get("lastRunAt") else "never"
        out.append(f"| {t['id']} | `{describe_schedule(t)}` | {t.get('enabled', '?')} | {last} | {t.get('filePath', '?')} |")
    return "\n".join(out)


def _json_default(o):
    if isinstance(o, (dt.datetime, dt.date)):
        return o.isoformat()
    return str(o)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="routine-doctor", description=__doc__.split("\n\n")[0])
    ap.add_argument("command", nargs="?", default="doctor", choices=["doctor", "report", "lint", "contracts", "tasks"])
    ap.add_argument("--days", type=int, default=7)
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--version", action="version", version=VERSION)
    a = ap.parse_args(argv)
    if a.command == "tasks":
        tasks = load_registry()
        print(json.dumps(tasks, indent=1, default=_json_default) if a.json else fmt_tasks(tasks))
        return 0
    res = analyze(a.days)
    sections = {"doctor": ("report", "lint", "contracts"), "report": ("report",),
                "lint": ("lint",), "contracts": ("contracts",)}[a.command]
    if a.json:
        print(json.dumps(res, indent=1, default=_json_default, ensure_ascii=False))
    else:
        print(fmt_report(res, sections))
    return 1 if res["red"] else 0


if __name__ == "__main__":
    sys.exit(main())
