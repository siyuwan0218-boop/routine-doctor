"""Synthetic-fixture tests. Run: python3 -I tests/test_routine_doctor.py"""
import datetime as dt
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
import routine_doctor as rd  # noqa: E402

TZ = dt.datetime.now().astimezone().tzinfo


def ms(t: dt.datetime) -> str:
    return str(int(t.timestamp() * 1000))


def iso(t: dt.datetime) -> str:
    return t.astimezone(dt.timezone.utc).isoformat().replace("+00:00", "Z")


class Fixture:
    """A fake desktop-app data dir + projects dir + tasks dir."""

    def __init__(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.app = root / "app"
        self.projects = root / "projects"
        self.tasks = root / "tasks"
        self.state = root / "state"
        ws = self.app / "claude-code-sessions/ws/tab"
        ws.mkdir(parents=True)
        (self.projects / "p").mkdir(parents=True)
        self.ws = ws
        self.registry = []
        os.environ.update(ROUTINE_DOCTOR_APP_DIR=str(self.app), ROUTINE_DOCTOR_PROJECTS=str(self.projects),
                          ROUTINE_DOCTOR_TASKS=str(self.tasks), ROUTINE_DOCTOR_STATE=str(self.state))

    def task(self, tid, cron=None, enabled=True, created=None, prompt="Do the thing.", **extra):
        d = self.tasks / tid
        d.mkdir(parents=True, exist_ok=True)
        (d / "SKILL.md").write_text(f"---\nname: {tid}\n---\n{prompt}", encoding="utf-8")
        t = dict(id=tid, enabled=enabled, filePath=str(d / "SKILL.md"), **extra)
        if cron:
            t["cronExpression"] = cron
        if created:
            t["createdAt"] = ms(created)
        self.registry.append(t)
        json.dump({"scheduledTasks": self.registry}, open(self.ws / "scheduled-tasks.json", "w"))
        return t

    def run(self, tid, start: dt.datetime, calls=3, model="claude-opus-5-5", transcript=True,
            push=False, write=None, limit=False, api_error=None, out_tokens=500, final="done"):
        sid = f"sid-{tid}-{int(start.timestamp())}"
        json.dump(dict(scheduledTaskId=tid, cliSessionId=sid, createdAt=ms(start), model=model, effort="medium",
                       title=tid), open(self.ws / f"local_{sid}.json", "w"))
        if not transcript:
            return sid
        lines = []
        t = start
        if api_error:
            lines.append(dict(type="assistant", timestamp=iso(t), isApiErrorMessage=True,
                              message=dict(model="<synthetic>", content=[dict(type="text", text=api_error)])))
        for i in range(calls):
            t = t + dt.timedelta(minutes=1)
            content = [dict(type="text", text=final if i == calls - 1 else f"step {i}")]
            if push and i == 0:
                content.append(dict(type="tool_use", name="PushNotification", input=dict(title="x", body="y")))
            if write and i == 0:
                content.append(dict(type="tool_use", name="Write", input=dict(file_path=write, content="z")))
            lines.append(dict(type="assistant", timestamp=iso(t),
                              message=dict(id=f"m{i}", model=model, content=content,
                                           usage=dict(input_tokens=1000, output_tokens=out_tokens,
                                                      cache_read_input_tokens=20000))))
        if limit:
            lines.append(dict(type="assistant", timestamp=iso(t + dt.timedelta(minutes=1)),
                              message=dict(model="<synthetic>", content=[dict(type="text", text="You've hit your session limit")])))
        with open(self.projects / "p" / f"{sid}.jsonl", "w") as f:
            for ln in lines:
                f.write(json.dumps(ln) + "\n")
        return sid

    def close(self):
        self.tmp.cleanup()


NOW = dt.datetime(2026, 10, 16, 12, 0, tzinfo=TZ)   # a Friday


class CronTests(unittest.TestCase):
    def occ(self, expr, days=7):
        return rd.cron_occurrences(expr, NOW - dt.timedelta(days=days), NOW)

    def test_daily(self):
        self.assertEqual(len(self.occ("30 7 * * *")), 7)

    def test_weekdays_and_names(self):
        self.assertEqual(len(self.occ("5 6 * * 1-5")), 5)
        self.assertEqual(len(self.occ("0 9 * * mon-fri")), 5)

    def test_sunday_alias_and_lists(self):
        a = self.occ("0 20 * * 0")
        b = self.occ("0 20 * * 7")
        self.assertEqual(a, b)
        self.assertEqual(len(a), 1)
        self.assertEqual(len(self.occ("0 9 * * 1,3,5")), 3)

    def test_steps_and_dom(self):
        self.assertEqual(len(self.occ("*/30 8 * * *")), 14)
        self.assertEqual(len(self.occ("0 9 1 * *", days=40)), 1)   # Oct 1 only

    def test_dom_or_dow(self):
        # vixie semantics: both restricted -> either matches
        self.assertEqual(len(self.occ("0 9 15 * 3", days=30)), 5)  # 4 Wednesdays + the 15th (a Thursday)

    def test_bad_expr(self):
        with self.assertRaises(ValueError):
            rd.cron_occurrences("0 9 * *", NOW - dt.timedelta(days=1), NOW)


class AnalysisTests(unittest.TestCase):
    def setUp(self):
        self.fx = Fixture()

    def tearDown(self):
        self.fx.close()

    def row(self, res, tid):
        return next(r for r in res["tasks"] if r["task"] == tid)

    def test_ok_run_cost_and_push(self):
        self.fx.task("daily", cron="0 9 * * *", created=NOW - dt.timedelta(days=30))
        for d in range(0, 7):
            self.fx.run("daily", NOW - dt.timedelta(days=d, hours=3), push=True, write="/tmp/x.md")
        res = rd.analyze(7, now=NOW)
        r = self.row(res, "daily")
        self.assertEqual((r["expected"], r["ran"], r["missed"]), (7, 7, 0))
        self.assertEqual(r["pushed"], 7)
        self.assertEqual(r["last_outcome"], "ok")
        # 3 calls x (1000 in * $4 + 500 out * $20 + 20000 read * $0.20) / 1e6
        self.assertAlmostEqual(r["runs"][0]["cost"], round(3 * (0.004 + 0.01 + 0.004), 2), places=2)
        self.assertFalse(any(f.startswith("🔴") for f in r["flags"]))
        self.assertFalse(res["red"])

    def test_missed_and_late(self):
        self.fx.task("daily", cron="0 9 * * *", created=NOW - dt.timedelta(days=30))
        # ran on 5 of 7 days; one of them 5 hours late (app closed)
        for d in (0, 1, 2, 4, 6):
            late = dt.timedelta(hours=5) if d == 2 else dt.timedelta(minutes=4)
            self.fx.run("daily", (NOW - dt.timedelta(days=d)).replace(hour=9, minute=0) + late)
        r = self.row(rd.analyze(7, now=NOW), "daily")
        self.assertEqual((r["expected"], r["ran"], r["missed"], r["late"]), (7, 5, 2, 1))
        self.assertTrue(any("missed 2" in f for f in r["flags"]))
        self.assertTrue(any("late" in f and "5.0 h" in f for f in r["flags"]))

    def test_ghost_error_limit(self):
        self.fx.task("w", cron="0 9 * * *", created=NOW - dt.timedelta(days=30))
        base = (NOW - dt.timedelta(days=3)).replace(hour=9, minute=1)
        self.fx.run("w", base, transcript=False)                                      # ghost
        self.fx.run("w", base + dt.timedelta(days=1), calls=0, api_error="API Error: ENOTFOUND")  # error
        self.fx.run("w", base + dt.timedelta(days=2), limit=True)                     # limit
        r = self.row(rd.analyze(7, now=NOW), "w")
        outcomes = [x["outcome"] for x in r["runs"]]
        self.assertEqual(outcomes, ["ghost", "error", "limit"])
        self.assertTrue(any("ghost" in f for f in r["flags"]))
        self.assertTrue(any("did no work" in f for f in r["flags"]))
        self.assertTrue(any("usage limit" in f for f in r["flags"]))

    def test_disabled_and_created_bounds(self):
        self.fx.task("off", cron="0 9 * * *", enabled=False, created=NOW - dt.timedelta(days=30))
        self.fx.task("new", cron="0 9 * * *", created=NOW - dt.timedelta(days=2, hours=1))
        res = rd.analyze(7, now=NOW)
        self.assertEqual(self.row(res, "off")["expected"], 0)
        self.assertEqual(self.row(res, "new")["expected"], 2)   # only the 2 days since creation

    def test_created_at_rewritten_on_edit(self):
        # createdAt says "yesterday" but a run 6 days ago proves the task is older
        self.fx.task("old", cron="0 9 * * *", created=NOW - dt.timedelta(days=1))
        self.fx.run("old", (NOW - dt.timedelta(days=6)).replace(hour=9, minute=2))
        r = self.row(rd.analyze(7, now=NOW), "old")
        self.assertEqual(r["expected"], 6)

    def test_unregistered_task_on_disk(self):
        self.fx.task("reg", cron="0 9 * * *", created=NOW - dt.timedelta(days=3))
        (self.fx.tasks / "orphan").mkdir()
        (self.fx.tasks / "orphan/SKILL.md").write_text("---\nname: orphan\n---\nhi")
        r = self.row(rd.analyze(7, now=NOW), "orphan")
        self.assertTrue(any("not registered" in f for f in r["flags"]))

    def test_cost_spike_and_model_drift(self):
        self.fx.task("n", cron="0 9 * * *", created=NOW - dt.timedelta(days=30))
        for d in range(1, 7):
            self.fx.run("n", (NOW - dt.timedelta(days=d)).replace(hour=9, minute=1), out_tokens=500)
        self.fx.run("n", NOW.replace(hour=9, minute=1), out_tokens=50000, model="claude-fable-5-1")
        r = self.row(rd.analyze(7, now=NOW), "n")
        self.assertTrue(any("cost spike" in f for f in r["flags"]))
        self.assertTrue(any("model drift" in f for f in r["flags"]))

    def test_contract(self):
        t = self.fx.task("c", cron="0 9 * * *", created=NOW - dt.timedelta(days=30))
        target = self.fx.tasks / "c" / "out.md"
        json.dump({"expect": [{"tool": "PushNotification"}, {"file_modified": str(target)},
                              {"text": "done"}, {"max_cost": 1.0}]},
                  open(self.fx.tasks / "c" / "contract.json", "w"))
        good = (NOW - dt.timedelta(days=1)).replace(hour=9, minute=1)
        self.fx.run("c", good, push=True)
        target.write_text("x")  # modified after the run started
        self.fx.run("c", NOW.replace(hour=9, minute=1), push=False)   # no push -> fails
        r = self.row(rd.analyze(7, now=NOW), "c")
        checks = [all(ok for ok, _ in c["checks"]) for c in r["contract"]]
        self.assertEqual(checks, [True, False])
        self.assertTrue(any("contract failed in 1/2" in f for f in r["flags"]))

    def test_lint(self):
        big = "x" * 31000 + "\nsee ~/definitely/not/here/file.md and ~/also/YYYY-MM-DD.md\n" \
              + "MUST MUST NEVER ALWAYS " * 100 + "\ncat <<'EOF'\n" + "code\n" * 600 + "EOF\n2023-01-01"
        self.fx.task("l", cron="0 9 * * *", created=NOW - dt.timedelta(days=3), prompt=big)
        r = self.row(rd.analyze(7, now=NOW), "l")
        joined = " ".join(r["lint"])
        for needle in ("chars (> 30,000)", "folder does not exist", "MUST/NEVER", "embedded in the prompt", "hard-coded date"):
            self.assertIn(needle, joined)
        self.assertNotIn("YYYY-MM-DD", joined)
        # growth is measured against the previous snapshot
        (self.fx.tasks / "l/SKILL.md").write_text("---\nname: l\n---\n" + big + "y" * 20000)
        r = self.row(rd.analyze(7, now=NOW), "l")
        self.assertTrue(any("grew" in f for f in r["lint"]))

    def test_report_renders(self):
        self.fx.task("daily", cron="0 9 * * *", created=NOW - dt.timedelta(days=30))
        self.fx.run("daily", (NOW - dt.timedelta(days=1)).replace(hour=9, minute=1))
        res = rd.analyze(7, now=NOW)
        md = rd.fmt_report(res)
        self.assertIn("| daily |", md)
        self.assertIn("## Prompt lint", md)
        json.dumps(res, default=rd._json_default)


if __name__ == "__main__":
    unittest.main(verbosity=1)
