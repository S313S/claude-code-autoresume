#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
StopFailure hook -- Claude Code fires this (instead of Stop) when an API error
ends a turn.

This hook cannot resume the turn. StopFailure is fire-and-forget: its stdout and
exit code are ignored, so the usual `{"decision": "block"}` trick that works on
Stop does nothing here. What it *can* do is leave a ticket naming the terminal
that just died, which the watchdog picks up within a second.

Two things this had to learn the hard way:

  * The hook process has no controlling terminal of its own -- `ps` reports `??`
    for it -- so the tty has to be found by walking up the parent chain.
  * The payload's `error` field is only a coarse class: a mid-stream drop and a
    502 both arrive as "server_error". The actual message lives in
    `last_assistant_message`, so both fields have to be inspected.

Never allowed to disturb the user's session, so every failure path is swallowed
and the exit code is always 0.
"""

import json
import os
import subprocess
import sys
import time

BASE = os.environ.get("CC_AUTORESUME_HOME") or os.path.expanduser("~/.claude/cc-autoresume")
TRIG_DIR = os.path.join(BASE, "triggers")

# Only open a ticket for a dropped stream. Retrying a rate-limit or an auth
# failure would just burn another turn.
PATTERNS = (
    "connection closed mid-response",
    "response stalled mid-stream",
)


def parent_tty():
    """Walk up the process chain until a real controlling tty appears."""
    pid = os.getpid()
    for _ in range(8):
        try:
            out = subprocess.run(["ps", "-o", "ppid=,tty=", "-p", str(pid)],
                                 capture_output=True, text=True, timeout=5).stdout.strip()
        except Exception:
            return None
        parts = out.split()
        if len(parts) < 2:
            return None
        ppid, tty = parts[0], parts[1]
        if tty and tty != "??":
            return tty if tty.startswith("/dev/") else "/dev/" + tty
        if ppid in ("0", "1", str(pid)):
            return None
        try:
            pid = int(ppid)
        except ValueError:
            return None
    return None


def main():
    raw = sys.stdin.read()
    try:
        data = json.loads(raw)
    except Exception:
        data = {}

    kind = str(data.get("error") or "")
    msg = str(data.get("last_assistant_message") or "")
    haystack = (msg + " " + kind + " " + raw).lower()
    if not any(p in haystack for p in PATTERNS):
        return

    tty = parent_tty()
    if not tty:
        return

    os.makedirs(TRIG_DIR, exist_ok=True)
    rec = {
        "tty": tty,
        "session_id": data.get("session_id"),
        "cwd": data.get("cwd"),
        "transcript_path": data.get("transcript_path"),
        "error": (msg or kind)[:300],
        "error_kind": kind,
        "ts": time.time(),
    }
    path = os.path.join(TRIG_DIR, "%d-%d.json" % (int(rec["ts"] * 1000), os.getpid()))
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(rec, f, ensure_ascii=False)
    os.replace(tmp, path)


if __name__ == "__main__":
    try:
        main()
    except Exception:
        pass          # a broken hook must never break the session
    sys.exit(0)
