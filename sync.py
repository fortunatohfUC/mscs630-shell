"""
sync.py - Process synchronization for mysh
Deliverable 3: Mutexes, semaphores, and the Producer-Consumer problem

MSCS-630 Advanced Operating Systems

Workers are Python threads that share memory, so they can race on shared
data the same way processes race on shared memory or a shared file.

    Mutex      - only one owner at a time (built on threading.Lock), and it
                 remembers who holds it so the output can show the owner.
    Semaphore  - a counting semaphore written with a condition variable:
                 wait() blocks while the count is 0, signal() adds 1 and
                 wakes one waiting thread.

`sync race` shows a lost-update race on a shared counter, first without a
lock and then with the mutex. `sync pc` runs the bounded-buffer
Producer-Consumer problem with two semaphores (empty, full) and a mutex,
then checks that every item was produced and consumed exactly once.
"""

import random
import sys
import threading
import time

USE_COLOR = sys.stdout.isatty()


def _c(text, code):
    return f"\033[{code}m{text}\033[0m" if USE_COLOR else text


def red(t):
    return _c(t, "1;31")


def green(t):
    return _c(t, "32")


def yellow(t):
    return _c(t, "33")


def cyan(t):
    return _c(t, "36")


def bold(t):
    return _c(t, "1")


# --------------------------------------------------------------------------
# Synchronization primitives
# --------------------------------------------------------------------------
class Mutex:
    """Mutual-exclusion lock that records its current owner."""

    def __init__(self, name):
        self.name = name
        self._lock = threading.Lock()
        self.owner = None

    def acquire(self, who):
        self._lock.acquire()
        self.owner = who

    def release(self, who):
        if self.owner != who:
            raise RuntimeError(f"{who} tried to release {self.name} owned by {self.owner}")
        self.owner = None
        self._lock.release()


class Semaphore:
    """Counting semaphore: wait() (P) and signal() (V)."""

    def __init__(self, name, value, stop=None):
        self.name = name
        self.value = value
        self.stop = stop                       # optional threading.Event
        self._cond = threading.Condition()

    def wait(self, who, on_block=None):
        """Take one unit. Returns False only if the run is being stopped."""
        with self._cond:
            if self.value == 0 and on_block:
                on_block()                     # report that `who` must wait
            while self.value == 0:
                if self.stop is not None and self.stop.is_set():
                    return False
                self._cond.wait(0.1)           # wake up now and then to check stop
            self.value -= 1
            return True

    def signal(self, who):
        with self._cond:
            self.value += 1
            self._cond.notify()


# --------------------------------------------------------------------------
# Race condition demo
# --------------------------------------------------------------------------
def race_demo(n=500, workers=2):
    """Increment a shared counter n times per worker, without and with a mutex."""
    def run(use_lock):
        state = {"counter": 0}
        mutex = Mutex("counter_lock")

        def worker(name):
            for _ in range(n):
                if use_lock:
                    mutex.acquire(name)
                value = state["counter"]       # read
                time.sleep(0)                  # give up the CPU between read and write
                state["counter"] = value + 1   # write
                if use_lock:
                    mutex.release(name)

        threads = [threading.Thread(target=worker, args=(f"T{i + 1}",)) for i in range(workers)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        return state["counter"]

    expected = n * workers
    print(bold(f"  Shared counter, {workers} threads x {n} increments "
               f"(expected {expected})"))
    print("  Each increment is: read counter -> yield the CPU -> write counter + 1")
    unsafe = run(False)
    lost = expected - unsafe
    print(f"  Without mutex: counter = {unsafe:<6} "
          + (red(f"RACE: {lost} updates lost") if lost else yellow("no loss this run")))
    safe = run(True)
    ok = safe == expected
    print(f"  With mutex:    counter = {safe:<6} "
          + (green("correct, no updates lost") if ok else red("unexpected result")))
    return unsafe, safe


# --------------------------------------------------------------------------
# Producer-Consumer (bounded buffer)
# --------------------------------------------------------------------------
class ProducerConsumer:
    def __init__(self, capacity=3, producers=2, consumers=2, items=4, speed=0.15, seed=7):
        self.capacity = capacity
        self.nprod = producers
        self.ncons = consumers
        self.items = items                         # items per producer
        self.speed = speed
        self.rand = random.Random(seed)
        self.buffer = []
        self.stop = threading.Event()              # set by Ctrl+C
        self.mutex = Mutex("mutex")
        self.empty = Semaphore("empty", capacity, self.stop)  # free slots
        self.full = Semaphore("full", 0, self.stop)           # filled slots
        self.out = threading.Lock()                # keeps log lines from mixing
        self.produced, self.consumed = [], []
        self.max_seen = 0
        self.violations = 0
        self.start = time.time()

    def log(self, who, msg):
        if self.stop.is_set():
            return
        with self.out:
            t = time.time() - self.start
            print(f"  {t:5.2f}s  {who:<3} {msg}", flush=True)

    def _buf(self):
        return "[" + ", ".join(self.buffer) + "]" + f" {len(self.buffer)}/{self.capacity}"

    def _pause(self, scale=1.0):
        if self.speed:
            time.sleep(self.speed * scale * self.rand.uniform(0.5, 1.5))

    def producer(self, pid):
        who = f"P{pid}"
        for i in range(1, self.items + 1):
            if self.stop.is_set():
                return
            item = f"{'ABCDEFGHIJKLMNOPQRST'[pid - 1]}{i}"
            self._pause()                                  # "make" the item
            if not self.empty.wait(who, lambda: self.log(who, yellow(f"waits: buffer full (empty=0), cannot add {item}"))):
                return
            self.mutex.acquire(who)
            # ---- critical section ----
            self.buffer.append(item)
            self.produced.append(item)
            self.max_seen = max(self.max_seen, len(self.buffer))
            if len(self.buffer) > self.capacity:
                self.violations += 1
            self.log(who, f"{cyan('acquire mutex')} -> add {item:<3} -> buffer {self._buf()} "
                          f"-> release, signal(full)")
            # ---- end critical section ----
            self.mutex.release(who)
            self.full.signal(who)

    def consumer(self, cid, quota):
        who = f"C{cid}"
        for _ in range(quota):
            if self.stop.is_set():
                return
            if not self.full.wait(who, lambda: self.log(who, yellow("waits: buffer empty (full=0)"))):
                return
            self.mutex.acquire(who)
            # ---- critical section ----
            item = self.buffer.pop(0)
            self.consumed.append(item)
            self.log(who, f"{cyan('acquire mutex')} -> take {item:<3} -> buffer {self._buf()} "
                          f"-> release, signal(empty)")
            # ---- end critical section ----
            self.mutex.release(who)
            self.empty.signal(who)
            self._pause(4.0)               # "use" the item (slower than producing)

    def run(self):
        total = self.nprod * self.items
        plural = lambda n, w: f"{n} {w}" + ("" if n == 1 else "s")
        print(bold(f"  Producer-Consumer: buffer size {self.capacity}, "
                   f"{plural(self.nprod, 'producer')}, {plural(self.ncons, 'consumer')}, "
                   f"{plural(total, 'item')}"))
        print(f"  Semaphores: empty={self.capacity} (free slots), full=0 (filled slots); "
              "mutex protects the buffer")
        quotas = [total // self.ncons + (1 if i < total % self.ncons else 0)
                  for i in range(self.ncons)]
        threads = [threading.Thread(target=self.producer, args=(i + 1,), daemon=True)
                   for i in range(self.nprod)]
        threads += [threading.Thread(target=self.consumer, args=(i + 1, q), daemon=True)
                    for i, q in enumerate(quotas)]
        for t in threads:
            t.start()
        try:
            for t in threads:
                while t.is_alive():
                    t.join(0.1)             # short joins so Ctrl+C still works
        except KeyboardInterrupt:
            self.stop.set()                 # tell every thread to finish
            for t in threads:
                t.join(1.0)
            raise
        self.check(total)

    def check(self, total):
        print()
        print(bold("  Checks"))
        ok_all = True

        def line(ok, text):
            nonlocal ok_all
            ok_all &= ok
            print(f"  {green('PASS') if ok else red('FAIL')}  {text}")

        line(len(self.produced) == total, f"produced {len(self.produced)} of {total} items")
        line(sorted(self.consumed) == sorted(self.produced),
             "every item consumed exactly once (none lost, none duplicated)")
        line(self.violations == 0 and self.max_seen <= self.capacity,
             f"buffer never over capacity (max {self.max_seen}/{self.capacity})")
        line(not self.buffer, "buffer empty at the end")
        line(self.empty.value == self.capacity and self.full.value == 0,
             f"semaphores back to start (empty={self.empty.value}, full={self.full.value})")
        print("  Result: " + (green("synchronized correctly") if ok_all else red("problem found")))
        return ok_all


# --------------------------------------------------------------------------
# The `sync` built-in
# --------------------------------------------------------------------------
SYNC_HELP = """\
  sync race [N]                  shared counter with and without a mutex
                                 (N increments per thread, default 500)
  sync pc [BUF] [PROD] [CONS] [ITEMS]
                                 Producer-Consumer with semaphores and a mutex
                                 (defaults: buffer 3, 2 producers, 2 consumers,
                                 4 items per producer)
  sync speed SECONDS             average pause between steps (default 0.15)"""


class SyncShell:
    def __init__(self, err):
        self.err = err
        self.speed = 0.15

    def run(self, args):
        if not args or args[0] in ("help", "-h"):
            print(SYNC_HELP)
            return 0 if args else 2
        handler = getattr(self, "c_" + args[0], None)
        if handler is None:
            return self.err(f"sync: unknown sub-command '{args[0]}' (try 'sync help')", 2)
        try:
            return handler(args[1:])
        except KeyboardInterrupt:
            print()
            return self.err("sync: run interrupted", 130)

    def c_race(self, args):
        try:
            n = int(args[0]) if args else 500
            if not 1 <= n <= 100000:
                raise ValueError
        except ValueError:
            return self.err("sync race: N must be a whole number from 1 to 100000", 2)
        race_demo(n)
        return 0

    def c_pc(self, args):
        names = ["buffer size", "producers", "consumers", "items per producer"]
        values = [3, 2, 2, 4]
        if len(args) > 4:
            return self.err("sync pc: usage: sync pc [BUF] [PROD] [CONS] [ITEMS]", 2)
        for i, a in enumerate(args):
            try:
                values[i] = int(a)
                if not 1 <= values[i] <= 20:
                    raise ValueError
            except ValueError:
                return self.err(f"sync pc: {names[i]} must be a whole number from 1 to 20", 2)
        ProducerConsumer(*values, speed=self.speed).run()
        return 0

    def c_speed(self, args):
        try:
            value = float(args[0])
            if value < 0:
                raise ValueError
        except (IndexError, ValueError):
            return self.err("sync speed: usage: sync speed SECONDS (0 or more)", 2)
        self.speed = value
        print(f"pause between steps = {value:g} s")
        return 0
