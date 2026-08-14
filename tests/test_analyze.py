#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Regression tests for the stuck-session decision.

Every fixture below is a hand-reproduced Claude Code terminal layout. The point
is not coverage of the code but coverage of the *screens*: the cost of a false
positive is typing into someone's working session, so each "must not fire" case
is a real situation that previously looked like a drop.

    python3 tests/test_analyze.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from watchdog import analyze  # noqa: E402

TAIL = 80
BOX = "─" * 60
STATUS = ("  [Opus 5] ░░░░░░░░░░ 116.0k (12%)\n"
          "  ⏵⏵ auto mode on (shift+tab to cycle) · ← for agents")

CASES = []


def case(name, expect, screen, require_error=True):
    CASES.append((name, expect, screen, require_error))


# ---------------------------------------------------------------- must fire

case("parked: only a duration line after the error", True, u"""\
⏺ Let me write that into the design doc.

● API Error: Connection closed mid-response. The response
  above may be incomplete.

* Churned for 1m 1s

{box}
❯
{box}
{status}""".format(box=BOX, status=STATUS))

case("parked: 'Jump to bottom' overlay after the error", True, u"""\
❯ please, continue

● API Error: Connection closed mid-response. The response
  above may be incomplete.
                    Jump to bottom (click) ↓

{box}
❯
{box}
{status}""".format(box=BOX, status=STATUS))

case("parked: a previous manual retry is visible above", True, u"""\
* Worked for 3m 31s

❯ please, continue

● API Error: Connection closed mid-response. The response above may be incomplete.

* Brewed for 2m 45s

{box}
❯
{box}
  ⏵⏵ auto mode on (shift+tab to cycle) · ← for agents""".format(box=BOX))

case("parked: error fits on one line", True, u"""\
● API Error: Connection closed mid-response. The response above may be incomplete.
✻ Cogitated for 12s
{box}
❯
{box}
{status}""".format(box=BOX, status=STATUS))

case("parked: input box shows only the placeholder hint", True, u"""\
● API Error: Connection closed mid-response. The response above may be incomplete.
{box}
❯ Try "edit <filepath> to..."
{box}
{status}""".format(box=BOX, status=STATUS))

case("hook ticket: idle session, no error on screen", True, u"""\
⏺ Wrote docs/design/schema.md.

* Churned for 25s
{box}
❯
{box}
{status}""".format(box=BOX, status=STATUS), require_error=False)


# ---------------------------------------------------------------- must NOT fire

case("finished normally, waiting for the user", False, u"""\
File: docs/design/semantic-recall.md (worklog snapshot alongside).

* Churned for 25s

※ recap: picked FTS5 keyword prefilter + main-engine dedup, no embeddings.
  (disable recaps in /config)
                              Image in clipboard · ctrl+v to paste
{box}
❯
{box}
  [Fable 5] ░░░░░░░░░░ 70.8k (7%)
  ⏵⏵ auto mode on (shift+tab to cycle) · ← for agents""".format(box=BOX))

case("built-in retry is running", False, u"""\
* API error · Retrying in 0s · attempt 1/15
{box}
❯
{box}
{status}""".format(box=BOX, status=STATUS))

case("built-in retry counting down after the drop", False, u"""\
● API Error: Connection closed mid-response. The response above may be incomplete.
* API error · Retrying in 7s · attempt 3/15
{box}
❯
{box}
{status}""".format(box=BOX, status=STATUS))

case("already recovered and kept writing", False, u"""\
● API Error: Connection closed mid-response. The response above may be incomplete.

❯ please, continue

⏺ Right, continuing from where it cut off. Schema is in docs/design/.

* Churned for 44s
{box}
❯
{box}
{status}""".format(box=BOX, status=STATUS))

case("recap after the error means the turn closed cleanly", False, u"""\
● API Error: Connection closed mid-response. The response above may be incomplete.
* Churned for 8s
※ recap: task complete. (disable recaps in /config)
{box}
❯
{box}
{status}""".format(box=BOX, status=STATUS))

case("actively working (spinner with a timer)", False, u"""\
● API Error: Connection closed mid-response. The response above may be incomplete.
✻ Sprouting… (15s · ↓ 447 tokens · esc to interrupt)
{box}
❯
{box}
{status}""".format(box=BOX, status=STATUS))

case("user has half-typed something", False, u"""\
● API Error: Connection closed mid-response. The response above may be incomplete.
* Churned for 8s
{box}
❯ can you also add
{box}
{status}""".format(box=BOX, status=STATUS))

case("the text merely appears in the conversation", False, u"""\
⏺ The exact wording is "API Error: Connection closed mid-response. The
  response above may be incomplete." -- that is the mid-stream case.

* Churned for 30s
{box}
❯
{box}
{status}""".format(box=BOX, status=STATUS))

case("waiting on a permission prompt", False, u"""\
● API Error: Connection closed mid-response. The response above may be incomplete.

Do you want to proceed?
  1. Yes
  2. No
{box}
❯
{box}
{status}""".format(box=BOX, status=STATUS))

case("subagent hit the error but the main loop runs on", False, u"""\
⏺ Agent terminated early due to an API error: API Error: Connection closed
  mid-response. The response above may be incomplete.

✻ Accomplishing… (32s · ↓ 1.2k tokens · esc to interrupt)
{box}
❯
{box}
{status}""".format(box=BOX, status=STATUS))

case("a plain shell, not Claude Code", False, u"""\
user@host ~ % ls
Desktop  Documents  Downloads
user@host ~ % """)

case("error has scrolled out of view", False, u"""\
⏺ Next I will write the conclusions into the doc.
* Churned for 12s
※ recap: done.
{box}
❯
{box}
{status}""".format(box=BOX, status=STATUS))

case("hook ticket but the session is busy again", False, u"""\
⏺ Continuing.
✻ Sprouting… (15s · ↓ 447 tokens · esc to interrupt)
{box}
❯
{box}
{status}""".format(box=BOX, status=STATUS), require_error=False)

case("hook ticket but the user is typing", False, u"""\
⏺ Continuing.
* Churned for 8s
{box}
❯ wait, do it differently
{box}
{status}""".format(box=BOX, status=STATUS), require_error=False)


def main():
    ok = fail = 0
    for name, expect, screen, require_error in CASES:
        got, reason, _ = analyze(screen, TAIL, require_error=require_error)
        if got == expect:
            ok += 1
            print("  PASS  %-46s -> %-5s (%s)" % (name, got, reason))
        else:
            fail += 1
            print("  FAIL  %-46s expected %s got %s (%s)" % (name, expect, got, reason))
    print("\n%d passed / %d failed" % (ok, fail))
    return 1 if fail else 0


if __name__ == "__main__":
    sys.exit(main())
