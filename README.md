# mysh: Shell Simulation (Deliverables 1-2)

MSCS-630 Advanced Operating Systems, University of the Cumberlands

A Unix-like shell written in Python.

- **Deliverable 1:** basic shell, process management with `fork()`, `execvp()` and
  `waitpid()`, foreground and background execution, and job control.
- **Deliverable 2:** process scheduling with Round-Robin (configurable time slice)
  and preemptive Priority-Based scheduling, with waiting, turnaround and response
  time metrics.

## Running

```
python3 mysh.py
```

Keep `mysh.py` and `scheduler.py` in the same folder. Requires Python 3.8+ on Linux or macOS (`os.fork()` is not available on Windows).
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
