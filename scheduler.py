"""
scheduler.py - Process scheduling for mysh
Deliverable 2: Round-Robin and Priority-Based Scheduling


The scheduler works on a simulated workload instead of real host processes,
so every run is repeatable. Each simulated process has a known arrival time,
burst time, remaining time and priority. A simulated clock advances one time
unit per tick, and time.sleep() makes each tick take real time so the run can
be watched. Every dispatch, arrival, preemption and completion is recorded,
and the metrics are computed from those recorded times:

    turnaround = completion - arrival
    waiting    = turnaround - burst      (time spent ready but not running)
    response   = first run - arrival

Priority convention: a LOWER number means a HIGHER priority (1 is highest).
"""

import heapq
import sys
import time
from collections import deque

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
# Process model (shared by both schedulers)
# --------------------------------------------------------------------------
class SimProcess:
    """A simulated process: the scheduler's version of a PCB."""

    def __init__(self, pid, name, burst, priority, arrival):
        self.pid = pid
        self.name = name
        self.burst = burst
        self.priority = priority
        self.arrival = arrival
        self.remaining = burst
        self.state = "NEW"          # NEW -> READY -> RUNNING -> ... -> DONE
        self.first_run = None
        self.completion = None

    def copy(self):
        """Fresh copy so the same workload can be run by any policy."""
        return SimProcess(self.pid, self.name, self.burst, self.priority, self.arrival)

    @property
    def turnaround(self):
        return self.completion - self.arrival

    @property
    def waiting(self):
        return self.turnaround - self.burst

    @property
    def response(self):
        return self.first_run - self.arrival


# --------------------------------------------------------------------------
# Common scheduler machinery
# --------------------------------------------------------------------------
class Scheduler:
    name = "Scheduler"

    def __init__(self, workload, speed=0.3, verbose=True):
        # Processes are admitted in arrival order; ties keep the order they
        # were added, which is what makes FCFS tie-breaking possible.
        self.procs = sorted((p.copy() for p in workload),
                            key=lambda p: (p.arrival, p.pid))
        self.pending = deque(self.procs)   # not arrived yet
        self.speed = speed
        self.verbose = verbose
        self.clock = 0
        self.gantt = []                    # list of [name, start, end]
        self.context_switches = 0
        self.last_run = None

    # ---- output -----------------------------------------------------------
    def log(self, msg):
        if self.verbose:
            print(f"  t={self.clock:<3} {msg}", flush=True)

    # ---- time -------------------------------------------------------------
    def tick(self, proc):
        """Run proc for one time unit of simulated CPU time."""
        if self.speed:
            time.sleep(self.speed)
        name = proc.name if proc else "idle"
        if self.gantt and self.gantt[-1][0] == name and self.gantt[-1][2] == self.clock:
            self.gantt[-1][2] += 1
        else:
            self.gantt.append([name, self.clock, self.clock + 1])
        if proc:
            proc.remaining -= 1
        self.clock += 1

    def arrivals(self):
        """Move every process whose arrival time has come into READY."""
        arrived = []
        while self.pending and self.pending[0].arrival <= self.clock:
            p = self.pending.popleft()
            p.state = "READY"
            arrived.append(p)
        return arrived

    def dispatch(self, proc, ready_desc):
        if self.last_run is not None and self.last_run is not proc:
            self.context_switches += 1
        self.last_run = proc
        proc.state = "RUNNING"
        first = ""
        if proc.first_run is None:
            proc.first_run = self.clock
            first = ", first run"
        self.log(f"{cyan('RUN')}   {bold(proc.name)} (remaining {proc.remaining}{first})"
                 f"   ready: {ready_desc}")

    def finish(self, proc, note=""):
        proc.state = "DONE"
        proc.completion = self.clock
        self.log(f"{green('DONE')}  {proc.name} finished{note}")

    def all_done(self):
        return all(p.state == "DONE" for p in self.procs)

    def idle_until_next_arrival(self):
        nxt = self.pending[0].arrival
        self.log(f"CPU idle until t={nxt}")
        while self.clock < nxt:
            self.tick(None)

    # ---- results ----------------------------------------------------------
    def averages(self):
        n = len(self.procs)
        return (sum(p.waiting for p in self.procs) / n,
                sum(p.turnaround for p in self.procs) / n,
                sum(p.response for p in self.procs) / n)

    def report(self):
        print()
        print(bold(f"  {self.title()} - results"))
        hdr = (f"  {'Proc':<6}{'Arrive':>7}{'Burst':>7}{'Prio':>6}{'First':>7}"
               f"{'Done':>6}{'Wait':>7}{'Turn':>7}{'Resp':>7}")
        print(hdr)
        print("  " + "-" * (len(hdr) - 2))
        for p in sorted(self.procs, key=lambda p: p.pid):
            print(f"  {p.name:<6}{p.arrival:>7}{p.burst:>7}{p.priority:>6}{p.first_run:>7}"
                  f"{p.completion:>6}{p.waiting:>7}{p.turnaround:>7}{p.response:>7}")
        w, t, r = self.averages()
        print("  " + "-" * (len(hdr) - 2))
        print(f"  {'Average':<39}{w:>7.2f}{t:>7.2f}{r:>7.2f}")
        busy = sum(e - s for n, s, e in self.gantt if n != "idle")
        util = 100 * busy / self.clock if self.clock else 0
        print(f"  Context switches: {self.context_switches}   "
              f"Total time: {self.clock}   CPU utilization: {util:.0f}%")
        segs = [f"[{s}-{e} {n}]" for n, s, e in self.gantt]
        for i in range(0, len(segs), 6):        # six segments per line
            print(("  Gantt: " if i == 0 else "         ") + " ".join(segs[i:i + 6]))

    def title(self):
        return self.name


# --------------------------------------------------------------------------
# Round-Robin
# --------------------------------------------------------------------------
class RoundRobin(Scheduler):
    name = "Round-Robin"

    def __init__(self, workload, quantum, **kw):
        super().__init__(workload, **kw)
        self.quantum = quantum
        self.ready = deque()

    def title(self):
        return f"Round-Robin (quantum = {self.quantum})"

    def _admit(self):
        for p in self.arrivals():
            self.ready.append(p)
            self.log(f"ARRIVE {p.name} (burst {p.burst}) joins the back of the ready queue")

    def _queue(self):
        return "[" + ", ".join(p.name for p in self.ready) + "]" if self.ready else "[]"

    def run(self):
        if self.verbose:
            print(bold(f"  {self.title()}") +
                  f"   (1 time unit = {self.speed:g} s)")
        self._admit()
        while not self.all_done():
            if not self.ready:
                self.idle_until_next_arrival()
                self._admit()
                continue
            proc = self.ready.popleft()
            self.dispatch(proc, self._queue())
            used = 0
            while used < self.quantum and proc.remaining > 0:
                self.tick(proc)
                used += 1
                self._admit()          # arrivals during the slice queue up
            if proc.remaining == 0:
                note = (f" after {used} of its {self.quantum}-unit slice, removed from queue"
                        if used < self.quantum else ", removed from queue")
                self.finish(proc, note)
            else:
                proc.state = "READY"
                self.ready.append(proc)
                self.log(f"{yellow('SLICE')} {proc.name} used its {self.quantum}-unit slice "
                         f"(remaining {proc.remaining}), back of the queue")
        return self


# --------------------------------------------------------------------------
# Priority-Based (preemptive)
# --------------------------------------------------------------------------
class PriorityScheduler(Scheduler):
    name = "Priority (preemptive)"

    def __init__(self, workload, **kw):
        super().__init__(workload, **kw)
        self.heap = []      # entries: (priority, arrival, pid, proc)

    def title(self):
        return "Priority-Based, preemptive (lower number = higher priority)"

    def _push(self, p):
        # Ties on priority are broken by arrival time, then by the order the
        # process was added (pid), which gives FCFS among equal priorities.
        heapq.heappush(self.heap, (p.priority, p.arrival, p.pid, p))

    def _heap_desc(self):
        if not self.heap:
            return "[]"
        return "[" + ", ".join(f"{e[3].name}(p{e[0]})" for e in sorted(self.heap)) + "]"

    def _admit(self):
        new = self.arrivals()
        for p in new:
            self._push(p)
            self.log(f"ARRIVE {p.name} (burst {p.burst}, priority {p.priority})")
        return new

    def _select(self):
        _, _, _, proc = heapq.heappop(self.heap)
        ties = [e[3] for e in self.heap if e[0] == proc.priority]
        if ties:
            others = ", ".join(f"{t.name} (arrived t={t.arrival})" for t in ties)
            self.log(f"TIE   priority {proc.priority}: {proc.name} (arrived t={proc.arrival}) "
                     f"goes before {others} [FCFS]")
        return proc

    def run(self):
        if self.verbose:
            print(bold(f"  {self.title()}") +
                  f"   (1 time unit = {self.speed:g} s)")
        current = None
        self._admit()
        while not self.all_done():
            if current is None:
                if not self.heap:
                    self.idle_until_next_arrival()
                    self._admit()
                    continue
                current = self._select()
                self.dispatch(current, self._heap_desc())
            self.tick(current)
            new = self._admit()
            if current.remaining == 0:
                self.finish(current)
                current = None
                continue
            better = [p for p in new if p.priority < current.priority]
            if better:
                top = min(better, key=lambda p: (p.priority, p.arrival, p.pid))
                self.log(f"{red('PREEMPT')} {top.name} (priority {top.priority}) beats running "
                         f"{current.name} (priority {current.priority}); {current.name} back to "
                         f"heap, remaining {current.remaining}")
                current.state = "READY"
                self._push(current)
                current = None
        return self


# --------------------------------------------------------------------------
# The `sched` built-in
# --------------------------------------------------------------------------
DEMO_WORKLOAD = [
    # name, burst, priority, arrival
    ("P1", 5, 3, 0),
    ("P2", 3, 1, 1),
    ("P3", 1, 3, 2),
    ("P4", 2, 2, 3),
    ("P5", 4, 1, 4),
    ("P6", 1, 1, 9),
]

SCHED_HELP = """\
  sched demo                      load the sample workload (P1-P6)
  sched add NAME BURST [PRIO] [ARRIVAL]
                                  add a process (PRIO 1 = highest, default 1;
                                  ARRIVAL default 0)
  sched list                      show the workload
  sched clear                     remove all processes
  sched speed SECONDS             real seconds per time unit (default 0.3)
  sched rr QUANTUM                run Round-Robin with the given time slice
  sched priority                  run preemptive Priority scheduling
  sched compare [Q1 Q2 ...]       compare average metrics (default quanta 1 2 4)"""


class SchedShell:
    """State and sub-commands for the shell's `sched` built-in."""

    def __init__(self, err):
        self.err = err                 # mysh's error printer
        self.workload = []
        self.speed = 0.3
        self.next_pid = 1

    def _add(self, name, burst, prio, arrival):
        self.workload.append(SimProcess(self.next_pid, name, burst, prio, arrival))
        self.next_pid += 1

    def run(self, args):
        if not args or args[0] in ("help", "-h"):
            print(SCHED_HELP)
            return 0 if args else 2
        cmd, rest = args[0], args[1:]
        handler = getattr(self, "c_" + cmd, None)
        if handler is None:
            return self.err(f"sched: unknown sub-command '{cmd}' (try 'sched help')", 2)
        try:
            return handler(rest)
        except KeyboardInterrupt:
            print()
            return self.err("sched: run interrupted", 130)

    # ---- workload ---------------------------------------------------------
    def c_demo(self, args):
        self.workload, self.next_pid = [], 1
        for name, burst, prio, arr in DEMO_WORKLOAD:
            self._add(name, burst, prio, arr)
        print("Loaded the sample workload:")
        return self.c_list([])

    def c_add(self, args):
        if not 2 <= len(args) <= 4:
            return self.err("sched add: usage: sched add NAME BURST [PRIORITY] [ARRIVAL]", 2)
        name = args[0]
        if any(p.name == name for p in self.workload):
            return self.err(f"sched add: a process named '{name}' already exists")
        try:
            burst = int(args[1])
            prio = int(args[2]) if len(args) > 2 else 1
            arrival = int(args[3]) if len(args) > 3 else 0
        except ValueError:
            return self.err("sched add: BURST, PRIORITY and ARRIVAL must be whole numbers")
        if burst < 1:
            return self.err("sched add: BURST must be at least 1")
        if prio < 1:
            return self.err("sched add: PRIORITY must be 1 or higher (1 = highest)")
        if arrival < 0:
            return self.err("sched add: ARRIVAL cannot be negative")
        self._add(name, burst, prio, arrival)
        print(f"added {name}: burst {burst}, priority {prio}, arrival {arrival}")
        return 0

    def c_list(self, args):
        if not self.workload:
            print("(workload is empty - use 'sched demo' or 'sched add')")
            return 0
        print(f"  {'PID':<5}{'Name':<6}{'Arrival':>8}{'Burst':>7}{'Priority':>10}")
        for p in self.workload:
            print(f"  {p.pid:<5}{p.name:<6}{p.arrival:>8}{p.burst:>7}{p.priority:>10}")
        return 0

    def c_clear(self, args):
        self.workload, self.next_pid = [], 1
        print("workload cleared")
        return 0

    def c_speed(self, args):
        try:
            value = float(args[0])
            if value < 0:
                raise ValueError
        except (IndexError, ValueError):
            return self.err("sched speed: usage: sched speed SECONDS (0 or more)", 2)
        self.speed = value
        print(f"1 time unit = {value:g} s")
        return 0

    # ---- running ----------------------------------------------------------
    def _need_workload(self, who):
        if not self.workload:
            self.err(f"sched {who}: workload is empty (use 'sched demo' or 'sched add')")
            return False
        return True

    @staticmethod
    def _quantum(text):
        q = int(text)
        if q < 1:
            raise ValueError
        return q

    def c_rr(self, args):
        if not args:
            return self.err("sched rr: missing time slice, usage: sched rr QUANTUM", 2)
        try:
            q = self._quantum(args[0])
        except ValueError:
            return self.err(f"sched rr: '{args[0]}' is not a valid time slice "
                            "(whole number, 1 or more)", 2)
        if not self._need_workload("rr"):
            return 1
        RoundRobin(self.workload, q, speed=self.speed).run().report()
        return 0

    def c_priority(self, args):
        if not self._need_workload("priority"):
            return 1
        PriorityScheduler(self.workload, speed=self.speed).run().report()
        return 0

    def c_compare(self, args):
        if not self._need_workload("compare"):
            return 1
        try:
            quanta = [self._quantum(a) for a in args] or [1, 2, 4]
        except ValueError:
            return self.err("sched compare: quanta must be whole numbers, 1 or more", 2)
        runs = [(f"Round-Robin q={q}", RoundRobin(self.workload, q, speed=0, verbose=False).run())
                for q in quanta]
        runs.append(("Priority (preemptive)",
                      PriorityScheduler(self.workload, speed=0, verbose=False).run()))
        print(bold("  Same workload, different policies (averages in time units)"))
        print(f"  {'Policy':<24}{'Waiting':>9}{'Turnaround':>12}{'Response':>10}{'Switches':>10}")
        print("  " + "-" * 63)
        for label, s in runs:
            w, t, r = s.averages()
            print(f"  {label:<24}{w:>9.2f}{t:>12.2f}{r:>10.2f}{s.context_switches:>10}")
        return 0
