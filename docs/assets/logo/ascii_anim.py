#!/usr/bin/env python3
"""Codatiel ASCII animation.

    python3 ascii_anim.py          # play the intro once, leave the logo
    python3 ascii_anim.py --loop   # keep idling until Ctrl-C

Prints the plain logo with no escape codes when stdout is not a terminal,
or when TERM=dumb, NO_COLOR, CI or CODATIEL_NO_ANIM is set.
"""
import itertools
import os
import shutil
import signal
import sys
import time

WORD = "codatiel"


def frame(crest="//", eye="o", word=WORD):
    return f"   {crest:<2}\n  ({eye} >  {word}"


def intro():
    for crest in ("  ", "/ ", "//"):
        yield frame(crest, word=""), 0.12
    for i in range(1, len(WORD) + 1):
        yield frame(word=WORD[:i]), 0.06


def idle():
    yield frame(), 1.6
    for crest in ("||", "\\\\", "||"):  # crest perks
        yield frame(crest), 0.09
    yield frame(), 1.2
    yield frame(eye="-"), 0.12          # blink


def should_animate(out):
    env = os.environ
    return (
        out.isatty()
        and env.get("TERM", "dumb") != "dumb"
        and not any(env.get(k) for k in ("NO_COLOR", "CI", "CODATIEL_NO_ANIM"))
    )


def play(loop=False, out=sys.stdout):
    if shutil.get_terminal_size().columns < 20:
        out.write(WORD + "\n")
        return
    if not should_animate(out):
        out.write(frame() + "\n")
        return

    seq = itertools.chain(intro(), idle())
    if loop:
        seq = itertools.chain(intro(), itertools.cycle(list(idle())))
    # make SIGTERM unwind through `finally` so the cursor comes back
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(143))
    out.write("\x1b[?25l")  # hide cursor
    try:
        for n, (text, delay) in enumerate(seq):
            if n:
                out.write("\x1b[1A\r")  # back to the top line
            out.write("\x1b[2K" + text.replace("\n", "\n\x1b[2K"))
            out.flush()
            time.sleep(delay)
        out.write("\x1b[1A\r\x1b[2K" + frame().replace("\n", "\n\x1b[2K"))
    except KeyboardInterrupt:
        pass
    finally:
        out.write("\x1b[?25h\n")
        out.flush()


if __name__ == "__main__":
    try:
        play(loop="--loop" in sys.argv)
    except BrokenPipeError:
        os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())
