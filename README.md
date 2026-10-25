# mysh: Shell Simulation (Deliverables 1-4)

MSCS-630 Advanced Operating Systems, University of the Cumberlands
Fortunato Hernandez

A Unix-like shell written in Python.

- **Deliverable 1:** basic shell, process management with `fork()`, `execvp()` and
  `waitpid()`, foreground and background execution, and job control.
- **Deliverable 2:** process scheduling with Round-Robin (configurable time slice)
  and preemptive Priority-Based scheduling, with waiting, turnaround and response
  time metrics.
- **Deliverable 3:** memory management with paging (fixed-size frames, page
  tables, page faults, FIFO and LRU replacement) and process synchronization
  (mutex, counting semaphores, a race-condition demo and Producer-Consumer).
- **Deliverable 4:** one integrated shell with pipes (`cmd1 | cmd2 | cmd3`), user
  login with admin and standard roles, and read/write/execute file permissions.

## Running

```
python3 mysh.py
```

You log in first. Demo accounts:

| Username | Password | Role |
|---|---|---|
| admin | admin123 | admin |
| fortunato | mysh2026 | standard |
| maria | maria2026 | standard |

Passwords are stored as salted PBKDF2-SHA256 hashes in `.mysh_users.json`.

Keep `mysh.py`, `scheduler.py`, `memory.py`, `sync.py` and `security.py` in the same folder. Requires Python 3.8+ on Linux or macOS (`os.fork()` is not available on Windows).
No third-party packages are needed.

## Deliverable 1 features

- Built-in commands: `cd`, `pwd`, `exit`, `echo`, `clear`, `ls`, `cat`, `mkdir`, `rmdir`, `rm`, `touch`, `kill`
- External programs run with the real `fork()`, `execvp()` and `waitpid()` system calls
- Foreground and background execution (`command &`)
- Job control: `jobs`, `fg`, `bg`, `kill %job`, Ctrl+Z, Ctrl+C
- Clear error messages for invalid commands, files, options and jobs
- `trace on|off` prints each fork, exec and wait as it happens

## Commands

| Command | Description |
|---|---|
| `cd [dir]` | Change directory (no argument = home, `-` = previous) |
| `pwd` | Print the working directory |
| `echo [-n] [text]` | Print text |
| `clear` | Clear the screen |
| `ls [-a] [-l] [path...]` | List directory contents |
| `cat file...` | Show file contents |
| `mkdir [-p] dir...` | Create directories |
| `rmdir dir...` | Remove empty directories |
| `rm [-r] [-f] file...` | Remove files |
| `touch file...` | Create a file or update its timestamp |
| `kill [-SIGNAL] pid\|%job` | Send a signal (default SIGTERM) |
| `jobs` | List background and stopped jobs |
| `fg [%job]` | Bring a job to the foreground |
| `bg [%job]` | Resume a stopped job in the background |
| `trace on\|off` | Show process-management events |
| `sched ...` | Process scheduling (see below) |
| `mem ...` | Paging and page replacement (see below) |
| `sync ...` | Mutex, semaphores, Producer-Consumer (see below) |
| `cmd1 \| cmd2 \| ...` | Pipe each command's output into the next |
| `whoami`, `su USER`, `logout` | Identity and switching users |
| `chmod MODE FILE` | Change permissions (owner or admin) |
| `chown USER FILE`, `users`, `useradd`, `audit` | Admin only |
| `help` | List commands |
| `exit [n]` | Leave the shell |
| `command &` | Run any program in the background |

Ctrl+Z stops the foreground job, Ctrl+C interrupts it, and Ctrl+D exits.

## Quick demo

```
trace on
sleep 30 &
sleep 100        (press Ctrl+Z)
jobs
bg %2
fg %1            (press Ctrl+C)
kill %2
```

## Deliverable 2: process scheduling

Scheduling runs on a simulated workload, so every run is repeatable. Each
process has an arrival time, burst time, remaining time and priority. A
simulated clock advances one time unit per tick, and `time.sleep()` makes each
tick take real time so the run can be watched. Metrics are computed from the
recorded arrival, first-run and completion times.

| Command | Description |
|---|---|
| `sched demo` | Load the sample workload (P1-P6) |
| `sched add NAME BURST [PRIORITY] [ARRIVAL]` | Add a process (priority 1 = highest) |
| `sched list` | Show the workload |
| `sched clear` | Remove all processes |
| `sched speed SECONDS` | Real seconds per time unit (default 0.3) |
| `sched rr QUANTUM` | Run Round-Robin with the given time slice |
| `sched priority` | Run preemptive Priority scheduling |
| `sched compare [Q1 Q2 ...]` | Compare average metrics across policies |

- **Round-Robin:** FIFO ready queue; each process runs for at most one quantum,
  then goes to the back of the queue. A process that finishes early is removed and
  the next one is scheduled right away.
- **Priority:** ready processes are kept in a heap (`heapq`). The highest priority
  (lowest number) runs first, equal priorities run first-come, first-served, and a
  newly arrived higher-priority process preempts the running one immediately.
- **Metrics:** turnaround = completion - arrival, waiting = turnaround - burst,
  response = first run - arrival.

```
sched demo
sched rr 2
sched priority
sched compare
```

## Deliverable 3: memory management

Physical memory is a fixed number of 4 KB page frames. Each process gets a page
table, and pages are loaded on demand: touching a page that is not in memory is
a page fault. When every frame is in use, the replacement policy picks a victim
(global replacement, any process).

- **FIFO:** evict the page that was loaded first.
- **LRU:** evict the page that has gone the longest without being used.

| Command | Description |
|---|---|
| `mem init FRAMES [fifo\|lru]` | Reset memory with FRAMES frames |
| `mem policy fifo\|lru` | Choose the replacement algorithm |
| `mem alloc NAME PAGES` | Give a process PAGES virtual pages |
| `mem access NAME PAGE [PAGE ...]` | Touch pages (faults load them) |
| `mem free NAME` | Deallocate a process and free its frames |
| `mem status` | Frames, page tables, memory usage and fault counts |
| `mem demo [fifo\|lru]` | 3 processes, 4 frames, one fixed access trace |
| `mem refs [-f N] [fifo\|lru] [R ...]` | Run a page-reference string step by step |
| `mem compare [R ...]` | FIFO vs LRU page faults for 3, 4 and 5 frames |

## Deliverable 3: process synchronization

Worker threads share memory, so they can race on shared data the same way
processes race on shared memory or a file.

- **Mutex:** one owner at a time; remembers the owner.
- **Semaphore:** counting semaphore built on a condition variable (`wait` / `signal`).
- **Producer-Consumer:** bounded buffer protected by `empty` and `full`
  semaphores plus a mutex; the run ends with checks that every item was produced
  and consumed exactly once and the buffer never overflowed.

| Command | Description |
|---|---|
| `sync race [N]` | Shared counter without and with a mutex |
| `sync pc [BUF] [PROD] [CONS] [ITEMS]` | Producer-Consumer (defaults 3 2 2 4) |
| `sync speed SECONDS` | Pause between steps (default 0.15) |

```
mem demo fifo
mem demo lru
mem compare
sync race
sync pc
```

## Deliverable 4: pipes

`cmd1 | cmd2 | cmd3` forks one child per stage. The shell creates a pipe with
`os.pipe()` between each pair of stages, and each child uses `os.dup2()` to
connect its stdin to the previous pipe and its stdout to the next one. All
stages share one process group, so the whole pipeline is one job (Ctrl+Z,
`jobs`, `fg`, `bg` and `kill %n` work on it). Built-ins such as `ls`, `cat`,
`sched`, `mem` and `sync` can be pipeline stages too.

```
ls | grep txt
cat system/app.log | grep error | sort -r
sched compare | grep -i robin
```

## Deliverable 4: security

- **Authentication:** username and password at startup (3 tries), `su` and `logout`.
- **Roles:** admin (group admin) and standard (group users). Admin-only commands:
  `users`, `useradd`, `chown`, `audit`.
- **File permissions:** files in the workspace (the folder mysh starts in) have an
  owner, group and mode like `rw-r--r--`, stored in `.mysh_meta.json`. Before
  every protected action the shell checks read, write or execute for the current
  user (owner, group, or others bits). Admin can read and write anything, but can
  only execute a file with an x bit. Files mysh did not create are treated as
  system files owned by admin (`rw-r--r--`).
- On first run mysh creates demo files: `system/config.txt`, `system/app.log`,
  `system/secret.txt` (admin only) and `scripts/report.sh` (not executable yet).

```
cat system/config.txt        # allowed for everyone (r--)
rm system/config.txt         # denied for standard users (no w)
cat system/secret.txt        # denied for standard users (rw-------)
chmod +x scripts/report.sh   # owner or admin only
```
