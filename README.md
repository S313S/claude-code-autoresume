# cc-retry-watchdog

Claude Code stops dead when a response stream is cut mid-flight:

```
● API Error: Connection closed mid-response. The response above may be incomplete.
```

No countdown, no retry — the turn is finalized and the session sits there until a
human types something. On long autonomous runs this is the difference between
"came back to finished work" and "came back to a session that died 40 minutes ago".

This watchdog is that human. It notices the drop and types your retry prompt into
that exact terminal, and nothing else.

[English] · [中文](README.zh-CN.md)

---

## Why the built-in retry does not cover this

Verified by string-inspecting the shipped binary (v2.1.226) and by reading local
transcripts, not by guessing:

**There is a mid-stream retry, but it is gated on having emitted nothing yet.**
After a stream drops, Claude Code checks whether any non-thinking block was
already yielded. If only thinking went out, it retries. The moment a single text
or `tool_use` block has streamed, it takes the finalize path instead, synthesizes
a stop reason, and writes the error you see. Limits are hardcoded (2 stale
connection retries, 1 idle timeout); no environment variable changes them.

The consequence is backwards from what you want: **the longer and more useful the
turn, the more certain it is that no retry is even attempted.**

**No hook can rescue it either.** A turn killed by an API error fires
`StopFailure`, not `Stop`. `StopFailure` is fire-and-forget — its stdout and exit
code are ignored — so the usual `{"decision": "block"}` trick that auto-continues
a `Stop` hook does nothing here.

**The timeout knobs are real but irrelevant.** `CLAUDE_ENABLE_BYTE_WATCHDOG`,
`CLAUDE_BYTE_STREAM_IDLE_TIMEOUT_MS`, `CLAUDE_ENABLE_STREAM_WATCHDOG`,
`CLAUDE_STREAM_IDLE_TIMEOUT_MS` and `keepPartialMessageOnAbort` all exist in the
binary. They control *detection*. They cannot re-issue a turn.

**`CLAUDE_CODE_AUTO_RESUME_ON_DROP` does not exist.** It appears in
[anthropics/claude-code#69415](https://github.com/anthropics/claude-code/issues/69415)
as a *proposal*. It is not in the binary; exporting it does nothing. (Check your
own build: `strings -a "$(readlink -f "$(command -v claude)")" | grep AUTO_RESUME`.)

One local data point on cause: across 160 drops the time-to-failure had a median
of 22s and a max of 203s, with no clustering at the 180s/300s watchdog thresholds
— consistent with the network path cutting the connection rather than a watchdog
aborting it. Tuning those timeouts is not the fix.

---

## Install

```bash
git clone https://github.com/S313S/claude-code-autoresume.git
cd claude-code-autoresume
./install.sh --hook     # omit --hook to print the snippet instead of editing settings.json
```

Requires Python 3.6+ (stdlib only, no packages). `--hook` backs up
`~/.claude/settings.json` before touching it and is idempotent.

```bash
ccwatch check     # what does it think of your terminals right now? never injects
ccwatch start     # start the watchdog
ccwatch hook      # is the hook registered? any pending tickets?
```

Claude Code sessions **already running** will not load a newly added hook until
they restart. They still get the polling fallback in the meantime.

---

## Supported terminals

| Platform | Backend | Status |
|---|---|---|
| macOS — Terminal.app | AppleScript | tested |
| macOS — iTerm2 | AppleScript | tested |
| any — tmux | `capture-pane` / `send-keys` | tested |
| Linux / WSL / Windows without tmux | — | not supported |
| VS Code integrated terminal | — | not supported |

Off macOS, tmux is the only way in: there has to be some way to read a terminal's
screen and type into it, and tmux is the portable one.

**macOS: start it from a real terminal window.** Driving Terminal/iTerm needs
AppleScript automation rights, which are granted to the *responsible* app. A
process spawned by launchd has no such identity and its AppleEvents hang forever.
Re-run `ccwatch start` after a reboot. tmux-only setups are unaffected.

---

## How it decides

Two independent paths.

**1. StopFailure hook — accurate, ~1s.** The hook cannot resume the turn, but it
can leave a ticket naming the tty that just died. The watchdog acts on it within
a second. No screen-reading involved; the drop is a known fact.

**2. Screen polling — fallback, ~5s.** Reads what each terminal shows and
recognizes the layout of a session parked on the error. Covers sessions that
started before the hook was installed, and the case where the daemon was down.

Either way it refuses to act unless **all** of these hold:

- the error is the last thing that happened this turn (polling), or a ticket says
  so (hook);
- the session is idle — no spinner, no `esc to interrupt`, and crucially no
  built-in `Retrying in Ns · attempt n/m`, so it never interrupts self-recovery;
- the input box is empty, so half-typed text is never clobbered;
- a claude process is actually running there;
- cooldown elapsed (30s) and the per-session retry cap (6) is not exhausted.

Situations it deliberately ignores: a turn that finished normally and is waiting
for you, a session that already recovered and kept writing, a permission prompt,
a subagent error while the main loop runs on, and the error text merely appearing
in conversation.

`tests/test_analyze.py` pins all of this — 20 hand-reproduced terminal layouts,
half of them "must not fire". Run it after changing any pattern:

```bash
python3 tests/test_analyze.py
```

---

## Caveat worth understanding

The retry prompt makes the model **redo the turn**. Because the partial response
stays in the transcript, it usually continues rather than starting over — but if
the drop happened after an `Edit` or `Bash` call, re-running the turn can repeat
that side effect. This is inherent to retrying by re-prompting, not something
this tool adds; automating it just makes it happen more often. Set
`max_consecutive` low if that worries you, or run with `dry_run` first.

---

## Configuration

`~/.claude/cc-autoresume/config.json`, re-read live — no restart needed.

| Key | Default | Meaning |
|---|---|---|
| `retry_text` | `please, retry` | what gets typed |
| `poll_interval_sec` | `5` | polling cadence |
| `confirm_polls` | `2` | consecutive stuck observations before acting (polling path only) |
| `cooldown_sec` | `30` | minimum gap between injections into one session |
| `max_consecutive` | `6` | per-session retry cap; then it stops and waits for a human |
| `dry_run` | `false` | log what it would do, inject nothing |
| `notify` | `true` | desktop notification on injection (macOS) |
| `use_hook_triggers` | `true` | trust tickets from the StopFailure hook |
| `trigger_ttl_sec` | `180` | tickets older than this are discarded |
| `fast_poll_sec` | `1` | cadence while a ticket is pending |
| `exclude_title_regex` | `""` | skip sessions whose title matches |
| `exclude_tty` | `[]` | e.g. `["/dev/ttys003"]` |
| `watch_terminal_app` / `watch_iterm` / `watch_tmux` | `true` | per-backend switches |
| `tail_lines` | `80` | how much of the screen bottom to inspect |

State, logs and tickets live in `~/.claude/cc-autoresume/` (override with
`CC_AUTORESUME_HOME`), deliberately outside the checkout so `git pull` never
fights with them.

---

## Implementation notes

Things that cost real debugging time, recorded so nobody repeats them:

- **Terminal.app's scripting dictionary fails silently, twice.**
  `repeat with w in windows` yields nothing — you must index `window wi`. And
  `set tb to tab ti of window wi` followed by `contents of tb` returns empty —
  the full specifier has to be repeated. AppleScript's `try` swallows both, so
  the watchdog explicitly warns when a running app yields zero sessions.
- **A tty is not a unique session key.** A Terminal window whose process exited
  still reports its old tty, and that number gets recycled by new windows. Two
  entries then collide and overwrite each other's counters, so the debounce never
  converges. Key on `window id : tab index` instead.
- **The hook process has no controlling terminal** (`ps` shows `??`). The tty must
  be found by walking up the parent chain.
- **The hook payload's `error` field is only a coarse class** — a mid-stream drop
  and a 502 both arrive as `"server_error"`. The real message is in
  `last_assistant_message`.
- **Send the text and the Return separately.** A TUI that receives text and
  newline in one read may treat it as a paste and insert a line break instead of
  submitting.
- **AppleScript keystroke simulation is not an option** unless the user has
  granted osascript Accessibility rights; `System Events` keystroke fails with
  "osascript is not allowed to send keystrokes". Everything here goes through each
  terminal's own scripting interface instead.

---

## Credits

Grew out of [anthropics/claude-code#69415](https://github.com/anthropics/claude-code/issues/69415),
where the `StopFailure` behaviour and the undocumented watchdog environment
variables were first dug out of the binary. This repo is an external stand-in for
the auto-resume layer proposed there, until something official lands.

MIT licensed.
