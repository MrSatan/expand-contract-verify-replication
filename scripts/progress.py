"""Shared progress reporter for long-running scripts in the replication package.

Prints percent done, items done/total, elapsed time, and ETA with immediate flush.
"""
import sys
import time


def _fmt(sec):
    sec = int(max(0, sec))
    h, rem = divmod(sec, 3600)
    m, s = divmod(rem, 60)
    return f"{h:d}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


class Progress:
    def __init__(self, total, label="task", stream=None):
        self.total = max(1, int(total))
        self.label = label
        self.n = 0
        self.t0 = time.time()
        self.out = stream or sys.stdout
        self._print(f"[PROGRESS] {label}  0/{self.total}  0.0%  started {time.strftime('%H:%M:%S')}")

    def _print(self, msg):
        print(msg, file=self.out, flush=True)

    def step(self, k=1, note=""):
        self.n += k
        el = time.time() - self.t0
        rate = el / self.n if self.n else 0
        eta = rate * (self.total - self.n)
        pct = 100.0 * self.n / self.total
        tail = f"  | last: {note}" if note else ""
        self._print(f"[PROGRESS] {self.label}  {self.n}/{self.total}  {pct:.1f}%  elapsed {_fmt(el)}  ETA {_fmt(eta)}{tail}")

    def beat(self, note="working"):
        self._print(f"[HEARTBEAT] {self.label}  {self.n}/{self.total}  elapsed {_fmt(time.time() - self.t0)}  | {note}")

    def done(self):
        self._print(f"[PROGRESS] {self.label}  DONE {self.n}/{self.total}  total time {_fmt(time.time() - self.t0)}")
