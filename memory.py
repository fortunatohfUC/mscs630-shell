"""
memory.py - Paging and page replacement for mysh
Deliverable 3: Memory Management

MSCS-630 Advanced Operating Systems

Physical memory is a fixed number of equal-size page frames. Each process
gets a page table that maps its virtual pages to frames. Pages are loaded
on demand: the first time a process touches a page there is a page fault,
and the page is loaded into a free frame. When every frame is in use
(memory overflow), the replacement policy picks a victim page to evict:

    FIFO - evict the page that was loaded into memory first
    LRU  - evict the page that has gone the longest without being used

Replacement is global: the victim can belong to any process.
"""

import sys
from collections import OrderedDict, deque

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


FRAME_KB = 4   # size of one page / frame in KB


class PageTableEntry:
    def __init__(self):
        self.frame = None      # None = not in memory (valid bit off)

    @property
    def valid(self):
        return self.frame is not None


class MemProcess:
    def __init__(self, name, pages):
        self.name = name
        self.pages = pages
        self.table = [PageTableEntry() for _ in range(pages)]
        self.faults = 0
        self.accesses = 0

    @property
    def resident(self):
        return sum(1 for e in self.table if e.valid)


class MemoryManager:
    def __init__(self, frames=4, policy="FIFO", log=print):
        self.nframes = frames
        self.frames = [None] * frames        # frame -> (proc_name, page) or None
        self.policy = policy
        self.procs = {}
        self.fifo = deque()                  # (proc, page) in load order
        self.lru = OrderedDict()             # (proc, page) -> None, oldest first
        self.total_faults = 0
        self.total_accesses = 0
        self.replacements = 0
        self.log = log

    # ---- helpers ----------------------------------------------------------
    def free_frames(self):
        return [i for i, f in enumerate(self.frames) if f is None]

    def frames_desc(self):
        return "[" + ", ".join("-" if f is None else f"{f[0]}:{f[1]}"
                               for f in self.frames) + "]"

    # ---- allocation -------------------------------------------------------
    def allocate(self, name, pages):
        """Give a process its virtual pages (loaded later, on demand)."""
        self.procs[name] = MemProcess(name, pages)
        return self.procs[name]

    def deallocate(self, name):
        """Free every frame the process holds and remove its page table."""
        proc = self.procs.pop(name)
        freed = []
        for page, entry in enumerate(proc.table):
            if entry.valid:
                self.frames[entry.frame] = None
                freed.append(entry.frame)
                key = (name, page)
                if key in self.lru:
                    del self.lru[key]
        self.fifo = deque(k for k in self.fifo if k[0] != name)
        return proc, freed

    # ---- access and page faults -------------------------------------------
    def access(self, name, page):
        """Process `name` touches virtual page `page`.

        Returns a dict describing what happened, for logging and tests.
        """
        proc = self.procs[name]
        if not 0 <= page < proc.pages:
            raise IndexError(f"page {page} is outside {name}'s address space "
                             f"(pages 0-{proc.pages - 1})")
        proc.accesses += 1
        self.total_accesses += 1
        key = (name, page)
        entry = proc.table[page]

        if entry.valid:                                   # hit
            self.lru.move_to_end(key)
            return {"hit": True, "frame": entry.frame}

        # ---- page fault ----
        proc.faults += 1
        self.total_faults += 1
        victim = None
        free = self.free_frames()
        if free:
            frame = free[0]
        else:                                             # memory is full
            victim = self.choose_victim()
            vproc, vpage = victim
            frame = self.procs[vproc].table[vpage].frame
            self.procs[vproc].table[vpage].frame = None
            self.fifo.remove(victim)
            del self.lru[victim]
            self.replacements += 1
        self.frames[frame] = key
        entry.frame = frame
        self.fifo.append(key)
        self.lru[key] = None
        return {"hit": False, "frame": frame, "victim": victim}

    def choose_victim(self):
        if self.policy == "FIFO":
            return self.fifo[0]          # loaded first
        return next(iter(self.lru))      # least recently used

    # ---- reporting --------------------------------------------------------
    def status(self):
        used = self.nframes - len(self.free_frames())
        print(bold(f"  Physical memory: {self.nframes} frames x {FRAME_KB} KB = "
                   f"{self.nframes * FRAME_KB} KB   policy: {self.policy}"))
        print(f"  Frames: {self.frames_desc()}   used {used}/{self.nframes} "
              f"({used * FRAME_KB} KB)")
        if not self.procs:
            print("  (no processes allocated)")
        else:
            print(f"  {'Proc':<6}{'Pages':>6}{'Virtual':>9}{'Resident':>10}"
                  f"{'In RAM':>8}{'Faults':>8}{'Accesses':>10}  Page table")
            for p in self.procs.values():
                pt = " ".join(f"{i}->{e.frame if e.valid else '-'}"
                              for i, e in enumerate(p.table))
                print(f"  {p.name:<6}{p.pages:>6}{p.pages * FRAME_KB:>7}KB"
                      f"{p.resident:>10}{p.resident * FRAME_KB:>6}KB{p.faults:>8}"
                      f"{p.accesses:>10}  {pt}")
        rate = (100 * self.total_faults / self.total_accesses) if self.total_accesses else 0
        print(f"  Total accesses: {self.total_accesses}   page faults: {self.total_faults} "
              f"({rate:.0f}%)   replacements: {self.replacements}")


# --------------------------------------------------------------------------
# Reference-string simulation (one process, fixed frames)
# --------------------------------------------------------------------------
def run_reference_string(refs, frames, policy, verbose=True):
    """Run a page-reference string through one policy. Returns fault count."""
    mm = MemoryManager(frames, policy)
    mm.allocate("R", max(refs) + 1)
    if verbose:
        print(bold(f"  {policy} with {frames} frames   refs: {' '.join(map(str, refs))}"))
        print(f"  {'Step':<6}{'Ref':<5}{'Frames':<{4 * frames + 4}}Result")
    for step, page in enumerate(refs, 1):
        r = mm.access("R", page)
        if verbose:
            frames_txt = "[" + ", ".join("-" if f is None else str(f[1]) for f in mm.frames) + "]"
            if r["hit"]:
                result = green("hit")
            elif r["victim"] is None:
                result = yellow(f"FAULT  load into free frame {r['frame']}")
            else:
                why = "oldest load" if policy == "FIFO" else "least recently used"
                result = red(f"FAULT  evict page {r['victim'][1]} ({why})")
            print(f"  {step:<6}{page:<5}{frames_txt:<{4 * frames + 4}}{result}")
    if verbose:
        n = len(refs)
        print(f"  Page faults: {mm.total_faults} of {n} references "
              f"({100 * mm.total_faults / n:.0f}%)   hits: {n - mm.total_faults}")
    return mm.total_faults


# --------------------------------------------------------------------------
# The `mem` built-in
# --------------------------------------------------------------------------
# A:0 is a "hot" page that A keeps coming back to, like a loop or a variable
DEMO_TRACE = [("A", 0), ("A", 1), ("B", 0), ("A", 0), ("B", 1), ("A", 0),
              ("C", 0), ("A", 0), ("A", 2), ("A", 0), ("C", 1), ("B", 1)]
DEFAULT_REFS = [1, 2, 3, 4, 1, 2, 5, 1, 2, 3, 4, 5]

MEM_HELP = """\
  mem init FRAMES [fifo|lru]     reset memory with FRAMES page frames
  mem policy fifo|lru            choose the page replacement algorithm
  mem alloc NAME PAGES           give a process PAGES virtual pages
  mem access NAME PAGE [PAGE..]  process touches pages (faults load them)
  mem free NAME                  deallocate a process and free its frames
  mem status                     frames, page tables, usage and faults
  mem demo [fifo|lru]            3 processes, 4 frames, a fixed access trace
  mem refs [-f N] [fifo|lru] R..  run a page-reference string step by step
  mem compare [R ...]            FIFO vs LRU fault counts for 3, 4 and 5 frames"""


class MemShell:
    """State and sub-commands for the shell's `mem` built-in."""

    def __init__(self, err):
        self.err = err
        self.mm = MemoryManager(4, "FIFO")

    def run(self, args):
        if not args or args[0] in ("help", "-h"):
            print(MEM_HELP)
            return 0 if args else 2
        handler = getattr(self, "c_" + args[0], None)
        if handler is None:
            return self.err(f"mem: unknown sub-command '{args[0]}' (try 'mem help')", 2)
        return handler(args[1:])

    @staticmethod
    def _policy(text):
        p = text.upper()
        if p not in ("FIFO", "LRU"):
            raise ValueError
        return p

    # ---- configuration ----------------------------------------------------
    def c_init(self, args):
        try:
            frames = int(args[0])
            policy = self._policy(args[1]) if len(args) > 1 else self.mm.policy
            if frames < 1:
                raise ValueError
        except (IndexError, ValueError):
            return self.err("mem init: usage: mem init FRAMES [fifo|lru] (FRAMES 1 or more)", 2)
        self.mm = MemoryManager(frames, policy)
        print(f"memory reset: {frames} frames x {FRAME_KB} KB, policy {policy}")
        return 0

    def c_policy(self, args):
        try:
            self.mm.policy = self._policy(args[0])
        except (IndexError, ValueError):
            return self.err("mem policy: usage: mem policy fifo|lru", 2)
        print(f"replacement policy: {self.mm.policy}")
        return 0

    # ---- allocation -------------------------------------------------------
    def c_alloc(self, args):
        if len(args) != 2:
            return self.err("mem alloc: usage: mem alloc NAME PAGES", 2)
        name = args[0]
        if name in self.mm.procs:
            return self.err(f"mem alloc: {name} already has memory (free it first)")
        try:
            pages = int(args[1])
            if pages < 1:
                raise ValueError
        except ValueError:
            return self.err("mem alloc: PAGES must be a whole number, 1 or more")
        self.mm.allocate(name, pages)
        print(f"allocated {name}: {pages} pages ({pages * FRAME_KB} KB virtual), "
              "nothing loaded yet (demand paging)")
        return 0

    def c_free(self, args):
        if len(args) != 1:
            return self.err("mem free: usage: mem free NAME", 2)
        if args[0] not in self.mm.procs:
            return self.err(f"mem free: no process named '{args[0]}'")
        proc, freed = self.mm.deallocate(args[0])
        frames = ", ".join(map(str, freed)) if freed else "none"
        print(f"freed {proc.name}: released frames {frames} "
              f"({len(freed) * FRAME_KB} KB returned)")
        print(f"  frames now: {self.mm.frames_desc()}")
        return 0

    def c_access(self, args):
        if len(args) < 2:
            return self.err("mem access: usage: mem access NAME PAGE [PAGE ...]", 2)
        name = args[0]
        if name not in self.mm.procs:
            return self.err(f"mem access: no process named '{name}' (use 'mem alloc')")
        try:
            pages = [int(a) for a in args[1:]]
        except ValueError:
            return self.err("mem access: pages must be whole numbers")
        for page in pages:
            try:
                r = self.mm.access(name, page)
            except IndexError as e:
                return self.err(f"mem access: invalid access, {e}")
            self._log_access(name, page, r)
        return 0

    def _log_access(self, name, page, r):
        mm = self.mm
        if r["hit"]:
            print(f"  {name}:{page}  {green('hit')}      frame {r['frame']}            "
                  f"frames {mm.frames_desc()}")
        elif r["victim"] is None:
            print(f"  {name}:{page}  {yellow('FAULT')}    loaded into free frame {r['frame']}  "
                  f"frames {mm.frames_desc()}")
        else:
            v = f"{r['victim'][0]}:{r['victim'][1]}"
            why = "loaded first" if mm.policy == "FIFO" else "least recently used"
            print(f"  {name}:{page}  {red('FAULT')}    memory full, {mm.policy} evicts {v} "
                  f"({why}) from frame {r['frame']}")
            print(f"  {'':<8}{'':<9}frames {mm.frames_desc()}")

    def c_status(self, args):
        self.mm.status()
        return 0

    # ---- demos ------------------------------------------------------------
    def c_demo(self, args):
        try:
            policy = self._policy(args[0]) if args else "FIFO"
        except ValueError:
            return self.err("mem demo: usage: mem demo [fifo|lru]", 2)
        self.mm = MemoryManager(4, policy)
        print(bold(f"  Demo: 4 frames x {FRAME_KB} KB, policy {policy}"))
        for name, pages in (("A", 3), ("B", 2), ("C", 2)):
            self.c_alloc([name, str(pages)])
        print("  total virtual memory 7 pages (28 KB) > 4 frames (16 KB): overflow expected")
        for name, page in DEMO_TRACE:
            self._log_access(name, page, self.mm.access(name, page))
        print()
        self.mm.status()
        return 0

    def _parse_refs(self, args, who):
        try:
            refs = [int(a) for a in args]
            if any(r < 0 for r in refs):
                raise ValueError
        except ValueError:
            self.err(f"mem {who}: page references must be whole numbers, 0 or more")
            return None
        return refs or list(DEFAULT_REFS)

    def c_refs(self, args):
        frames, policy = 3, None
        args = list(args)
        if args[:1] == ["-f"]:
            try:
                frames = int(args[1])
                if frames < 1:
                    raise ValueError
            except (IndexError, ValueError):
                return self.err("mem refs: -f needs a frame count, 1 or more", 2)
            args = args[2:]
        if args and args[0].lower() in ("fifo", "lru"):
            policy = args[0].upper()
            args = args[1:]
        refs = self._parse_refs(args, "refs")
        if refs is None:
            return 1
        for p in ([policy] if policy else ["FIFO", "LRU"]):
            run_reference_string(refs, frames, p)
            print()
        return 0

    def c_compare(self, args):
        refs = self._parse_refs(args, "compare")
        if refs is None:
            return 1
        print(bold(f"  Page faults for refs: {' '.join(map(str, refs))}  ({len(refs)} references)"))
        print(f"  {'Frames':<8}{'FIFO':>6}{'LRU':>6}")
        for frames in (3, 4, 5):
            f = run_reference_string(refs, frames, "FIFO", verbose=False)
            l = run_reference_string(refs, frames, "LRU", verbose=False)
            print(f"  {frames:<8}{f:>6}{l:>6}")
        return 0
