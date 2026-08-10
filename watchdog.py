#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
cc-retry-watchdog -- watch running Claude Code sessions and, when one is
killed by a mid-stream connection drop, type the retry prompt into it for you.

Claude Code retries failures that happen *before* a response starts streaming.
It does not retry a stream that dies halfway: the partial reply is finalized,
`API Error: Connection closed mid-response.` is printed, and the session just
sits there until a human types something. This watchdog is that human.

Two independent ways of noticing a dead turn:

  1. The StopFailure hook (accurate, ~1s). Claude Code fires StopFailure when an
     API error ends a turn. The hook cannot resume the turn itself -- it is
     fire-and-forget, its stdout and exit code are ignored -- but it can leave a
     ticket naming the tty that just died. See hook_stopfailure.py.
  2. Screen polling (fallback, ~5s). Read what each terminal is showing and
     recognize the layout of a session parked on that error.

Backends: macOS Terminal.app and iTerm2 via AppleScript, plus tmux anywhere.

Refuses to act unless ALL of these hold:
  * the error is the last thing that happened this turn (polling path), or a
    hook ticket says so (hook path);
  * the session is idle -- no spinner, no `esc to interrupt`, and crucially no
    built-in `Retrying in Ns - attempt n/m` (never interrupt its own recovery);
  * the input box is empty, so half-typed text is never clobbered;
  * a claude process is actually running there;
  * cooldown has elapsed and the per-session retry cap is not exhausted.
"""

import hashlib
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime

IS_MAC = sys.platform == "darwin"

# Code lives in the repo; runtime state lives elsewhere so `git pull` never
# collides with logs, and so a checkout stays clean.
CODE_DIR = os.path.dirname(os.path.abspath(os.path.realpath(__file__)))
BASE = os.environ.get("CC_AUTORESUME_HOME") or os.path.expanduser("~/.claude/cc-autoresume")
try:
    os.makedirs(BASE, exist_ok=True)
except Exception:
    BASE = CODE_DIR

CONFIG_PATH = os.path.join(BASE, "config.json")
STATE_PATH = os.path.join(BASE, "state.json")
LOG_PATH = os.path.join(BASE, "watchdog.log")
PAUSE_FLAG = os.path.join(BASE, "PAUSED")       # this file exists => never inject
TRIG_DIR = os.path.join(BASE, "triggers")       # tickets dropped by the hook
LOG_MAX_BYTES = 2 * 1024 * 1024

DEFAULTS = {
    "retry_text": "please, retry",
    "poll_interval_sec": 5,
    "confirm_polls": 2,          # consecutive stuck observations before acting
    "cooldown_sec": 30,          # min seconds between two injections per session
    "max_consecutive": 6,        # per-session auto-retry cap (resets on recovery)
    "dry_run": False,            # log what would happen, inject nothing
    "notify": True,              # desktop notification on injection (macOS)
    "watch_terminal_app": True,
    "watch_iterm": True,
    "watch_tmux": True,
    "exclude_title_regex": "",   # skip sessions whose title matches
    "exclude_tty": [],           # e.g. ["/dev/ttys003"]
    "tail_lines": 80,            # only inspect this many lines from the bottom
    "use_hook_triggers": True,   # trust tickets left by the StopFailure hook
    "trigger_ttl_sec": 180,      # tickets older than this are discarded
    "fast_poll_sec": 1,          # poll interval while a ticket is pending
}

# ---------------------------------------------------------------- patterns

# The error body, tolerant of the line wrap that splits it on narrow terminals.
ERR_TEXT = re.compile(r"API\s+Error:\s*Connection\s+closed\s+mid-?response", re.I)
# Start of the error line: an optional bullet glyph (do not hardcode which) + text.
ERR_HEAD = re.compile(r"^\s*(?:[^\w\s]{1,2}\s+)?API\s+Error:", re.U)

# Any of these on screen means the session is working. Never touch it.
BUSY_PATTERNS = [
    re.compile(r"esc to interrupt", re.I),
    re.compile(r"Retrying in\s+\d+s", re.I),        # built-in retry in progress
    re.compile(r"attempt\s+\d+\s*/\s*\d+", re.I),   # built-in retry counter
    re.compile(r"…\s*\(\s*\d+[smh]"),               # "Sprouting… (15s ·"
    re.compile(r"\(\s*\d+[smh]\s*·"),               # "(15s ·"
    re.compile(r"ctrl\+b to run in background", re.I),
]

PROMPT_LINE = re.compile(r"^\s*[❯>]\s?(.*)$")
# Placeholder hint inside the input box counts as empty.
PLACEHOLDER = re.compile(r'^\s*(?:Try\s+"|/\s*$|$)')
RULE_LINE = re.compile(r"^\s*[─━═┄╌—_\-╭╮╰╯│\s]{6,}$")

# Decoration allowed to appear after the error. Anything else means the turn
# moved on and must not be retried.
CHROME_AFTER_ERR = [
    RULE_LINE,
    re.compile(r"^\s*\S{0,2}\s*[A-Za-z]+ for(?:\s+\d+[hms])+\s*$"),  # "* Brewed for 2m 45s"
    re.compile(r"^\s*Jump to bottom", re.I),
    re.compile(r"(?:ctrl\+v to paste|/clear to save|to edit in Vim|Auto-update failed|Run claude doctor)\s*$", re.I),
    re.compile(r"^\s*❯\s*$"),
]

# If any of these follow the error, the turn actually finished normally.
DISQUALIFY_AFTER_ERR = [
    re.compile(r"^\s*[※*·]?\s*recap:", re.I),
    re.compile(r"disable recaps in /config", re.I),
]


def now():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def log(msg):
    try:
        if os.path.exists(LOG_PATH) and os.path.getsize(LOG_PATH) > LOG_MAX_BYTES:
            with open(LOG_PATH, "r", encoding="utf-8", errors="replace") as f:
                tail = f.readlines()[-2000:]
            with open(LOG_PATH, "w", encoding="utf-8") as f:
                f.writelines(tail)
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write("[%s] %s\n" % (now(), msg))
    except Exception:
        pass
    print("[%s] %s" % (now(), msg), flush=True)


def load_json(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return dict(default) if isinstance(default, dict) else default


def save_json(path, data):
    try:
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
    except Exception as e:
        log("WARN could not write %s: %s" % (path, e))


def run(argv, timeout=20):
    try:
        p = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
        if p.returncode != 0:
            return None, (p.stderr or "").strip() or "exit %d" % p.returncode
        return p.stdout, None
    except subprocess.TimeoutExpired:
        return None, "timed out"
    except FileNotFoundError:
        return None, "not installed"
    except Exception as e:
        return None, str(e)


def osa(script, timeout=20):
    if not IS_MAC:
        return None, "not macOS"
    return run(["osascript", "-e", script], timeout=timeout)


def app_running(name):
    out, _ = osa('tell application "System Events" to return (exists process "%s")' % name)
    return bool(out) and out.strip() == "true"


# ---------------------------------------------------------------- reading screens

MARK = "<<<CCA-SESSION>>>"

# Index-based iteration with a try at every level: windows and tabs opening or
# closing mid-scan must not abort the whole read.
ITERM_READ = '''
tell application "iTerm2"
  set out to ""
  repeat with wi from 1 to (count of windows)
    try
      repeat with ti from 1 to (count of tabs of window wi)
        try
          repeat with si from 1 to (count of sessions of tab ti of window wi)
            try
              set s to session si of tab ti of window wi
              set out to out & "%s" & (id of s as string) & "|" & (tty of s) & "|" & "" & "|" & (name of s) & linefeed & (text of s) & linefeed
            end try
          end repeat
        end try
      end repeat
    end try
  end repeat
  return out
end tell
''' % MARK

# Two Terminal.app scripting traps, both of which fail *silently*:
#   1. `repeat with w in windows` yields nothing -- must index as `window wi`.
#   2. `set tb to tab ti of window wi` then `contents of tb` fails -- the full
#      specifier has to be repeated every time.
# Also: tty is not a unique key. A window whose process exited still reports its
# old tty, and that number gets recycled by new windows, so two entries collide.
# Key on "window id : tab index" instead, and use the tab's own process list.
TERMINAL_READ = '''
tell application "Terminal"
  set out to ""
  repeat with wi from 1 to (count of windows)
    try
      set wname to name of window wi
      set widd to (id of window wi) as string
      repeat with ti from 1 to (count of tabs of window wi)
        try
          set out to out & "%s" & widd & ":" & ti & "|" & (tty of tab ti of window wi) & "|" & ((processes of tab ti of window wi) as string) & "|" & wname & linefeed & (contents of tab ti of window wi) & linefeed
        end try
      end repeat
    end try
  end repeat
  return out
end tell
''' % MARK


def parse_sessions(raw, app):
    """Split one batched AppleScript read into [{app,key,tty,procs,title,screen}]."""
    sessions = []
    if not raw:
        return sessions
    for chunk in raw.split(MARK)[1:]:
        nl = chunk.find("\n")
        if nl < 0:
            continue
        header, body = chunk[:nl], chunk[nl + 1:]
        parts = header.split("|", 3)
        if len(parts) < 4:
            continue
        sessions.append({
            "app": app,
            "key": parts[0].strip(),
            "tty": parts[1].strip(),
            "procs": parts[2].strip(),
            "title": parts[3].strip(),
            "screen": body,
        })
    return sessions


_empty_warned = set()


def _read_app(app, script, out):
    raw, err = osa(script, timeout=25)
    if err:
        log("WARN could not read %s: %s" % (app, err))
        return
    got = parse_sessions(raw, app)
    # AppleScript `try` swallows errors, so "app is running but zero sessions
    # were read" is a bug signal that must be surfaced.
    if not got and app not in _empty_warned:
        _empty_warned.add(app)
        log("WARN %s is running but 0 sessions were read -- the script may be "
            "failing silently (raw output %d bytes)" % (app, len(raw or "")))
    elif got:
        _empty_warned.discard(app)
    out += got


def tmux_sessions():
    """Enumerate tmux panes. Works on Linux/WSL/macOS alike."""
    fmt = "#{pane_id}\t#{pane_tty}\t#{pane_current_command}\t#{session_name}:#{window_index}.#{pane_index}"
    out, err = run(["tmux", "list-panes", "-a", "-F", fmt], timeout=10)
    if err or not out:
        return []
    sessions = []
    for line in out.splitlines():
        parts = line.split("\t")
        if len(parts) < 4:
            continue
        pane_id, tty, cmd, title = parts[0], parts[1], parts[2], parts[3]
        screen, cerr = run(["tmux", "capture-pane", "-p", "-t", pane_id], timeout=10)
        if cerr:
            continue
        sessions.append({
            "app": "tmux",
            "key": pane_id,
            "tty": tty,
            "procs": cmd,
            "title": title,
            "screen": screen or "",
        })
    return sessions


def collect_sessions(cfg):
    out = []
    if IS_MAC and cfg["watch_iterm"] and app_running("iTerm2"):
        _read_app("iTerm2", ITERM_READ, out)
    if IS_MAC and cfg["watch_terminal_app"] and app_running("Terminal"):
        _read_app("Terminal", TERMINAL_READ, out)
    if cfg.get("watch_tmux", True):
        out += tmux_sessions()
    return out


def claude_ttys():
    """ttys that currently have a claude process on them."""
    ttys = set()
    try:
        p = subprocess.run(["ps", "-Ao", "tty=,command="], capture_output=True,
                           text=True, timeout=10)
        for line in p.stdout.splitlines():
            parts = line.strip().split(None, 1)
            if len(parts) < 2:
                continue
            tty, cmd = parts
            # Claude Code rewrites its own process title; it shows up as either
            # "claude" or "Claude" depending on state.
            head = cmd.split()[0] if cmd.split() else ""
            if os.path.basename(head).lower() == "claude":
                ttys.add(tty if tty.startswith("/dev/") else "/dev/" + tty)
    except Exception as e:
        log("WARN ps failed: %s" % e)
    return ttys


def has_claude(sess, live_ttys):
    """Is a claude process really running in this terminal session?

    Terminal.app reports every process on a tab, which is exact -- and it has to
    be used, because a Terminal window whose process exited still reports its old
    tty and that number gets recycled, so a tty match there can be a false
    positive.

    tmux reports only `pane_current_command`, which is the executable name: a
    Claude Code started through a node wrapper reads as "node". So accept either
    that name or a claude process on the pane's tty. Pane ids are unique, so the
    tty-recycling problem does not apply here.

    iTerm2 exposes no per-session process list at all, so the tty lookup is all
    there is; its session ids are unique and closed sessions disappear.
    """
    if sess["app"] == "Terminal":
        return "claude" in (sess.get("procs") or "").lower()
    if sess["app"] == "tmux":
        return "claude" in (sess.get("procs") or "").lower() or sess["tty"] in live_ttys
    return sess["tty"] in live_ttys


# ---------------------------------------------------------------- hook tickets

def read_triggers(ttl_sec):
    """Collect tickets left by the StopFailure hook, keyed by tty.

    A ticket means the drop is a known fact rather than something inferred from
    pixels, so the screen does not have to show the error at all.
    """
    out = {}
    if not os.path.isdir(TRIG_DIR):
        return out
    stamp = time.time()
    for name in sorted(os.listdir(TRIG_DIR)):
        if not name.endswith(".json"):
            continue
        path = os.path.join(TRIG_DIR, name)
        try:
            with open(path, encoding="utf-8") as f:
                rec = json.load(f)
        except Exception:
            _drop(path)
            continue
        if stamp - float(rec.get("ts") or 0) > ttl_sec:
            log("ticket expired, discarded | %s | %s" % (rec.get("tty"), name))
            _drop(path)
            continue
        rec["_path"] = path
        out[rec.get("tty")] = rec        # keep only the newest per tty
    return out


def _drop(path):
    try:
        os.remove(path)
    except Exception:
        pass


def tickets_pending():
    try:
        return any(n.endswith(".json") for n in os.listdir(TRIG_DIR))
    except Exception:
        return False


# ---------------------------------------------------------------- the decision

def _norm(s):
    return re.sub(r"\s+", " ", s).strip()


def analyze(screen, tail_lines, require_error=True):
    """Return (stuck, reason, fingerprint).

    stuck=True means: this turn ended on the mid-response drop, the session is
    idle, and the input box is empty.

    require_error=False is the hook path -- the drop is already confirmed, so
    the error does not need to be visible, but every other guard still applies.
    """
    lines = [l.rstrip() for l in screen.splitlines()]
    while lines and not lines[-1].strip():
        lines.pop()
    if not lines:
        return False, "blank screen", ""
    lines = lines[-tail_lines:]
    joined = "\n".join(lines)

    # 1) must be idle
    for pat in BUSY_PATTERNS:
        if pat.search(joined):
            return False, "session busy (%s)" % pat.pattern[:28], ""

    # 2) locate the input box: the last prompt line
    prompt_idx = None
    prompt_body = ""
    for i in range(len(lines) - 1, -1, -1):
        m = PROMPT_LINE.match(lines[i])
        if m:
            prompt_idx, prompt_body = i, m.group(1)
            break
    if prompt_idx is None:
        return False, "no input box found -- probably not a Claude Code UI", ""

    # 3) user has typed something -- hands off
    if prompt_body.strip() and not PLACEHOLDER.match(prompt_body):
        return False, "input box not empty, skipping", ""

    # content area = everything above the input box (minus its rule line)
    end = prompt_idx
    while end - 1 >= 0 and RULE_LINE.match(lines[end - 1]):
        end -= 1
    content = lines[:end]
    while content and not content[-1].strip():
        content.pop()
    if not content:
        return False, "empty content area", ""

    if not require_error:
        fp = hashlib.md5("\n".join(content[-6:]).encode("utf-8")).hexdigest()[:12]
        return True, "hook ticket: session idle, input box empty", fp

    # 4) find the last error line
    err_i = None
    for i in range(len(content) - 1, -1, -1):
        if ERR_HEAD.match(content[i]) and ERR_TEXT.search(_norm(" ".join(content[i:i + 3]))):
            err_i = i
            break
    if err_i is None:
        return False, "no such error this turn", ""

    # the error block is the error line plus its wrapped continuations
    err_end = err_i
    while err_end + 1 < len(content):
        nxt = content[err_end + 1]
        if not nxt.strip() or nxt[:1] not in (" ", "\t"):
            break
        if any(p.search(nxt) for p in CHROME_AFTER_ERR + DISQUALIFY_AFTER_ERR):
            break
        err_end += 1

    # 5) only decoration may follow; real output or a recap means it moved on
    for j in range(err_end + 1, len(content)):
        line = content[j]
        if not line.strip():
            continue
        if any(p.search(line) for p in DISQUALIFY_AFTER_ERR):
            return False, "recap after the error -- turn ended normally", ""
        if any(p.search(line) for p in CHROME_AFTER_ERR):
            continue
        return False, "output after the error (%s) -- turn moved on" % _norm(line)[:40], ""

    fp = hashlib.md5("\n".join(content[max(0, err_i - 2):]).encode("utf-8")).hexdigest()[:12]
    return True, "parked on Connection closed mid-response", fp


# ---------------------------------------------------------------- injecting

# Type the text, pause, then send Return separately: a TUI that sees text and
# newline in one read may treat it as a paste and insert a line break instead of
# submitting.
ITERM_SEND = '''
tell application "iTerm2"
  repeat with wi from 1 to (count of windows)
    try
      repeat with ti from 1 to (count of tabs of window wi)
        try
          repeat with si from 1 to (count of sessions of tab ti of window wi)
            try
              if ((id of session si of tab ti of window wi) as string) is "%(key)s" then
                tell session si of tab ti of window wi to write text "%(text)s" newline NO
                delay 0.4
                tell session si of tab ti of window wi to write text "" newline YES
                return "OK"
              end if
            end try
          end repeat
        end try
      end repeat
    end try
  end repeat
  return "NOTFOUND"
end tell
'''

TERMINAL_SEND = '''
tell application "Terminal"
  repeat with wi from 1 to (count of windows)
    try
      if ((id of window wi) as string) is "%(wid)s" then
        do script "%(text)s" in tab %(ti)s of window wi
        return "OK"
      end if
    end try
  end repeat
  return "NOTFOUND"
end tell
'''


def esc(s):
    return s.replace("\\", "\\\\").replace('"', '\\"')


def send_retry(sess, text):
    app = sess["app"]
    if app == "tmux":
        _, err = run(["tmux", "send-keys", "-t", sess["key"], "-l", text], timeout=15)
        if err:
            return False, err
        time.sleep(0.4)
        _, err = run(["tmux", "send-keys", "-t", sess["key"], "Enter"], timeout=15)
        return (err is None), (err or "OK")

    if app == "iTerm2":
        script = ITERM_SEND % {"key": esc(sess["key"]), "text": esc(text)}
    else:
        wid, _, ti = sess["key"].partition(":")
        script = TERMINAL_SEND % {"wid": esc(wid), "ti": esc(ti or "1"), "text": esc(text)}
    out, err = osa(script, timeout=25)
    if err:
        return False, err
    return (out or "").strip() == "OK", (out or "").strip()


def notify(title, msg):
    if IS_MAC:
        osa('display notification "%s" with title "%s"' % (esc(msg), esc(title)), timeout=10)


# ---------------------------------------------------------------- main loop

def scan_once(cfg, state, act=True):
    sessions = collect_sessions(cfg)
    live = claude_ttys()
    excl_re = re.compile(cfg["exclude_title_regex"]) if cfg.get("exclude_title_regex") else None
    triggers = read_triggers(cfg["trigger_ttl_sec"]) if cfg.get("use_hook_triggers", True) else {}
    report = []
    seen_keys = set()

    for s in sessions:
        sid = "%s:%s" % (s["app"], s["key"])
        if sid in seen_keys:       # duplicate key in one sweep: trust the first
            continue
        seen_keys.add(sid)
        st = state.setdefault(sid, {"streak": 0, "consecutive": 0, "last_sent": 0, "last_fp": ""})

        if s["tty"] in (cfg.get("exclude_tty") or []):
            report.append((sid, s["title"], "skipped: tty excluded"))
            st["streak"] = 0
            continue
        if excl_re and excl_re.search(s["title"]):
            report.append((sid, s["title"], "skipped: title excluded"))
            st["streak"] = 0
            continue
        if not has_claude(s, live):
            report.append((sid, s["title"], "skipped: no claude process here"))
            st["streak"] = 0
            continue

        trig = triggers.get(s["tty"])
        # A ticket means StopFailure already confirmed this turn died, so the
        # error need not be on screen and the debounce is unnecessary. Idle and
        # empty-input-box guards still apply.
        stuck, reason, fp = analyze(s["screen"], cfg["tail_lines"], require_error=not trig)
        if not stuck:
            if st["streak"] or st["consecutive"]:
                st["streak"] = 0
                st["consecutive"] = 0
            if trig and "busy" in reason:
                _drop(trig["_path"])       # it recovered on its own; void the ticket
                reason += "; ticket voided"
            report.append((sid, s["title"], "ok: %s" % reason))
            continue

        st["streak"] = st.get("streak", 0) + 1
        if not trig and st["streak"] < cfg["confirm_polls"]:
            report.append((sid, s["title"], "looks stuck (%d/%d confirmations)"
                           % (st["streak"], cfg["confirm_polls"])))
            continue

        elapsed = time.time() - st.get("last_sent", 0)
        if elapsed < cfg["cooldown_sec"]:
            report.append((sid, s["title"], "stuck but cooling down (%ds left)"
                           % int(cfg["cooldown_sec"] - elapsed)))
            continue
        if st.get("consecutive", 0) >= cfg["max_consecutive"]:
            report.append((sid, s["title"], "stuck but hit the retry cap (%d) -- needs a human"
                           % cfg["max_consecutive"]))
            continue

        if not act or cfg["dry_run"] or os.path.exists(PAUSE_FLAG):
            why = "DRY-RUN" if (cfg["dry_run"] or not act) else "PAUSED"
            report.append((sid, s["title"], "[%s] would inject %r" % (why, cfg["retry_text"])))
            log("DRY %s | %s | %s | would inject" % (sid, s["tty"], s["title"]))
            continue

        ok, detail = send_retry(s, cfg["retry_text"])
        st["last_sent"] = time.time()
        st["last_fp"] = fp
        st["streak"] = 0
        if trig:
            _drop(trig["_path"])           # consumed either way; never reused
        if ok:
            st["consecutive"] = st.get("consecutive", 0) + 1
            src = "hook" if trig else "poll"
            log("SENT[%s] %s | %s | %s | retry #%d"
                % (src, sid, s["tty"], s["title"], st["consecutive"]))
            report.append((sid, s["title"], "injected %r (%s, retry #%d)"
                           % (cfg["retry_text"], src, st["consecutive"])))
            if cfg["notify"]:
                notify("Claude Code auto-resume", "%s\nsent %s" % (s["title"][:60], cfg["retry_text"]))
        else:
            log("FAIL %s | %s | injection failed: %s" % (sid, s["tty"], detail))
            report.append((sid, s["title"], "injection failed: %s" % detail))

    for k in list(state.keys()):
        if k not in seen_keys:
            del state[k]
    return report


def main():
    args = sys.argv[1:]
    cfg = dict(DEFAULTS)
    cfg.update(load_json(CONFIG_PATH, {}))
    if not os.path.exists(CONFIG_PATH):
        save_json(CONFIG_PATH, cfg)

    if "--once" in args or "--check" in args:
        state = load_json(STATE_PATH, {})
        cfg["confirm_polls"] = 1       # a single sweep must be able to conclude
        act = "--once" in args
        rows = scan_once(cfg, state, act=act)
        save_json(STATE_PATH, state)
        if not rows:
            print("No Claude Code sessions found.")
        for sid, title, msg in rows:
            print("  %-30s %-42s %s" % (sid[:30], title[:42], msg))
        return

    log("watchdog up | poll %ds | dry_run=%s | text=%r"
        % (cfg["poll_interval_sec"], cfg["dry_run"], cfg["retry_text"]))
    state = load_json(STATE_PATH, {})
    rounds = 0
    last_beat = 0.0
    while True:
        try:
            cfg2 = dict(DEFAULTS)
            cfg2.update(load_json(CONFIG_PATH, {}))     # config is re-read live
            t0 = time.time()
            rows = scan_once(cfg2, state, act=True)
            dur = time.time() - t0
            save_json(STATE_PATH, state)
            rounds += 1
            if time.time() - last_beat > 600:
                last_beat = time.time()
                log("heartbeat | round %d | %.1fs | %d sessions" % (rounds, dur, len(rows)))
            # Drop to a 1s cadence while a ticket is waiting so the hook path
            # feels immediate.
            time.sleep(max(1, int(cfg2["fast_poll_sec"])) if tickets_pending()
                       else max(2, int(cfg2["poll_interval_sec"])))
        except KeyboardInterrupt:
            log("watchdog stopped")
            return
        except Exception as e:
            log("ERROR main loop: %r" % e)
            time.sleep(10)


if __name__ == "__main__":
    main()
