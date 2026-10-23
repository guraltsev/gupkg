"""Run one batch of regular-expression matches in an isolated child process.

This file is a script, not a module of gupkg: ``System.regex_worker`` starts it with the
interpreter's ``-I -s -B -S`` flags so that it imports only ``json``, ``os``, ``re``, ``sys`` and ``time``
from the standard library and nothing from gupkg. Python's ``re`` cannot be interrupted, so the
parent (``gupkg.services.regexrun``) gives the child a hard deadline and kills its process tree
when the deadline passes; the child itself is deliberately simple and has no timeout logic.

Usage and API
-------------
The request is one JSON document on standard input, ASCII only::

    {"op": "finditer" | "search" | "match" | "fullmatch", "pattern": str, "flags": int,
     "texts": [str, ...], "limit": int, "cap": int}

The answer is one JSON document on standard output, ASCII only. On success::

    {"ok": true, "names": {group name: group number}, "results": [[match, ...], ...], "seconds": float}

with one list of matches per text, a match being ``[start, end, [group 0 text, group 1 text, ...]]``
(``null`` for a group that did not take part); ``seconds`` is how long the matching took, which the
parent uses only to account for its time budget. On failure ``{"ok": false, "error": text}``. The
first line the child writes to standard error is ``pid <number>``.

Implementation Approach
-----------------------
``finditer`` stops after ``limit`` matches per text, and every operation stops with an error when
the size of the captured text passes ``cap`` characters, so the answer is bounded however hostile
the pattern or the text. Group text is copied out of the match, never the whole page.
"""

import json
import os
import re
import sys
import time

_OPERATIONS = ("finditer", "search", "match", "fullmatch")


def _answer(document):
    """Write *document* as the single line of output."""
    sys.stdout.write(json.dumps(document, ensure_ascii=True, separators=(",", ":")))
    sys.stdout.write("\n")
    sys.stdout.flush()


def _match(found):
    """Return the JSON shape of one match."""
    return [found.start(), found.end(), [found.group(index) for index in range(found.re.groups + 1)]]


def main():
    """Read the request, run it and write the answer; return the exit status."""
    sys.stderr.write("pid %d\n" % os.getpid())
    sys.stderr.flush()
    try:
        request = json.loads(sys.stdin.buffer.read().decode("ascii"))
        operation = request["op"]
        if operation not in _OPERATIONS:
            raise ValueError("unknown operation %r" % (operation,))
        compiled = re.compile(request["pattern"], int(request["flags"]))
        limit, cap = int(request["limit"]), int(request["cap"])
        texts = request["texts"]
    except (KeyError, TypeError, ValueError, re.error, UnicodeError, RecursionError, OverflowError) as error:
        _answer({"ok": False, "error": "bad request: %s: %s" % (type(error).__name__, error)})
        return 2
    spent = 0
    results = []
    began = time.monotonic()
    for text in texts:
        if operation == "finditer":
            found = []
            for item in compiled.finditer(text):
                found.append(item)
                if len(found) >= limit:
                    break
        else:
            one = getattr(compiled, operation)(text)
            found = [one] if one is not None else []
        converted = [_match(item) for item in found]
        spent += sum(len(group) for _start, _end, groups in converted for group in groups if group) + 16 * len(converted)
        if spent > cap:
            _answer({"ok": False, "error": "the matches hold more than %d characters" % cap})
            return 3
        results.append(converted)
    _answer({"ok": True, "names": dict(compiled.groupindex), "results": results, "seconds": round(time.monotonic() - began, 3)})
    return 0


if __name__ == "__main__":
    sys.exit(main())
