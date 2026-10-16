# routine-doctor

**Did your Claude Code scheduled tasks actually do their job?**

The desktop app shows a green "ran" badge. Anthropic's own docs say a green status does not
mean the task in your prompt succeeded. If you run more than a handful of routines, you
eventually learn this the hard way: a run that "completed" with zero API calls because DNS was
down, a Thursday run that never fired, a nightly task whose cost quietly tripled after a model
change, a prompt that still points at a folder you renamed in August.

`routine-doctor` is one Python file, zero dependencies, that reads the files Claude Code already
writes on your machine and prints a report per task:

| question | how it answers |
|---|---|
| Did it run when the schedule said? | expands each cron expression and pairs every slot with a real session: **missed**, **late** (app was closed), **extra** (manual runs) |
| Did it do any work? | **ghost** = app says it ran, no transcript; **error** = transcript, but no API call succeeded; **limit** = hit a usage limit |
| What did it cost? | API list-price equivalent per run, median per task, and **cost spikes** > 2.5× the task's own median |
| Which model ran it? | **model drift** across recent runs (you changed the app default, or a run fell back) |
| Did it tell anyone? | whether a run pushed a notification or wrote files, or finished silently |
| Is the prompt rotting? | size, growth since last check, embedded code blocks, MUST/NEVER density, stale dates, paths whose folder no longer exists |
| Did it meet *your* definition of done? | optional `contract.json` per task: expected tool call, file modified, final-message pattern, cost or time cap |

Nothing leaves your computer. There is no telemetry.

## Install

```bash
curl -fsSL https://raw.githubusercontent.com/siyuwan0218-boop/routine-doctor/main/routine_doctor.py -o ~/bin/routine-doctor
chmod +x ~/bin/routine-doctor
```

Or just clone and run `python3 routine_doctor.py`. Python 3.9+.

## Use

```bash
routine-doctor                 # report + prompt lint + contracts, last 7 days
routine-doctor report --days 14
routine-doctor lint            # prompts only
routine-doctor contracts       # outcome contracts only
routine-doctor tasks           # what the app has registered
routine-doctor --json          # everything, machine-readable
```

Exit code is `1` when any task has a red flag, so you can run it from `cron`, a git hook, or
another scheduled task that only pings you when something is red.

A real report (names changed) looks like this:

```
23 tasks · 27 runs · $137.5 API-equivalent · red flags present

| task              | schedule        | expected | ran | missed | last run    | outcome | pushed | $ total | $ median | flags |
| premarket-brief   | `5 6 * * 1-5`   | 5        | 4   | 1      | 10-16 06:07 | ok      | 4/4    | 3.88    | 0.97     | 🔴 🟡 🟡 |
| trade-sync        | `45 13 * * 1-5` | 5        | 6   | 0      | 10-16 13:46 | ok*     | 5/6    | 14.51   | 1.97     | ⚪ 🔴 🟡 |
| nightly-digest    | `30 22 * * 0-5` | 6        | 6   | 0      | 10-15 22:43 | ok      | 0/6    | 79.61   | 10.20    | 🟡 🟡 |

**premarket-brief**
- 🔴 missed 1 scheduled run(s): Thu 10-15 06:05
- 🟡 2 late run(s); worst 2.6 h after schedule (app closed at the time?)
**trade-sync**
- 🔴 1 run(s) did no work: API error before any call succeeded
  - 10-14 17:47 error: API Error: Can't reach the API server — check your internet or DNS (ENOTFOUND)
**nightly-digest**
- 🟡 cost spike: 1 run(s) > 2.5x this task's median $9.19
- 🟡 model drift across recent runs: claude-opus-5-5 ×9, claude-fable-5 ×3
```

## Outcome contracts

"It ran" is not "it worked". Put a `contract.json` next to a task's `SKILL.md`
(`~/.claude/scheduled-tasks/<task>/contract.json`) and the doctor checks every run against it:

```json
{"expect": [
  {"tool": "PushNotification"},
  {"file_modified": "~/reports/weekly.md"},
  {"text": "nothing to report|sent"},
  {"max_cost": 5},
  {"min_calls": 3},
  {"max_minutes": 30}
]}
```

`tool` = a tool the run must have called · `file_modified` = a path whose mtime must be after the
run started · `text` = a regex the run's final message must match · `max_cost` / `min_calls` /
`max_minutes` = sanity caps.

## What it reads

| file | what it gives |
|---|---|
| `~/Library/Application Support/Claude/claude-code-sessions/*/*/scheduled-tasks.json` | the tasks the app has registered: cron, enabled, created, last run |
| `~/Library/Application Support/Claude/claude-code-sessions/*/*/local_*.json` | each scheduled session: task id, transcript id, model, effort |
| `~/.claude/projects/*/<session>.jsonl` | the transcript: API calls with token usage, tool calls, errors, the final message |
| `~/.claude/scheduled-tasks/<task>/SKILL.md` | the prompt, for lint |

Override any of them with `ROUTINE_DOCTOR_APP_DIR`, `ROUTINE_DOCTOR_PROJECTS`, `ROUTINE_DOCTOR_TASKS`.
Prompt snapshots for the growth check live in `~/.routine-doctor/` (override `ROUTINE_DOCTOR_STATE`).

## Limits, honestly

- These files are **undocumented** and Claude Code ships a release most days. When a format changes,
  the doctor will say "ghost" for everything; open an issue with the first line of a transcript.
- Dollar figures are **API list-price equivalents**. On a subscription your real cost is plan quota.
  The price table is in `PRICE` near the top of the file; unknown model ids are priced as Opus-class and listed at the bottom of the report.
- Transcripts are kept for 30 days by default, so `--days` beyond that sees only ghosts.
- Tested on macOS. Windows and Linux paths are best-effort; set the env vars if the defaults miss.
- It reads one machine. If your routines run in the cloud, it does not see them.

## Tests

```bash
python3 -I tests/test_routine_doctor.py
```

Synthetic fixtures only; the tests never touch your real data.

## License

MIT.
