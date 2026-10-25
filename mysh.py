#!/usr/bin/env python3
"""
mysh - Shell Simulation with Integrated OS Concepts
Deliverable 1: Basic Shell Implementation and Process Management
Deliverable 2: Process Scheduling (see scheduler.py and the `sched` built-in)
Deliverable 3: Memory Management and Process Synchronization
               (see memory.py / `mem` and sync.py / `sync`)
Deliverable 4: Integration and Security - pipes, user login, file permissions
               (see security.py)

MSCS-630 Advanced Operating Systems

What the shell covers
  * A read-parse-execute loop that accepts user commands, after a login.
  * Built-in commands: cd, pwd, exit, echo, clear, ls, cat, mkdir, rmdir,
    rm, touch, kill (plus jobs, fg, bg, help, trace).
  * External programs are started with the real Unix system calls
    os.fork(), os.execvp(), os.waitpid() and os._exit().
  * Foreground and background execution ("&"), a job table that tracks
    the state of every child, and job control (jobs, fg, bg, Ctrl+Z, Ctrl+C).
  * Deliverable 2: a `sched` built-in that runs a simulated workload through
    Round-Robin (configurable time slice) and preemptive Priority scheduling
    and reports waiting, turnaround and response time.
  * Deliverable 3: a `mem` built-in that simulates paging with fixed-size
    frames, page faults and FIFO / LRU replacement, and a `sync` built-in
    with a mutex, counting semaphores, a race-condition demo and the
    Producer-Consumer problem.
  * Deliverable 4: pipelines of any length (cmd1 | cmd2 | cmd3) built with
    os.pipe() and os.dup2(); user authentication with admin and standard
    roles; read/write/execute permission checks on every protected action.

Run it with:   python3 mysh.py
It needs a Unix-like OS (Linux or macOS) because os.fork() does not exist
on Windows. No third-party packages are required.
"""

import getpass
import os
import pwd
import shlex
import shutil
import signal
import stat
import sys
import time

import memory
import scheduler
import sync
from memory import MemShell
from scheduler import SchedShell
from security import Security
from sync import SyncShell

try:
    import readline  # noqa: F401  arrow-key history and line editing
    HAVE_READLINE = True
except ImportError:
    HAVE_READLINE = False

try:
    import termios
except ImportError:  # pragma: no cover
    termios = None


# --------------------------------------------------------------------------
# Small helpers for colored output
# --------------------------------------------------------------------------
USE_COLOR = sys.stdout.isatty()


def color(text, code):
    """Wrap text in an ANSI color code when writing to a terminal."""
    return f"\033[{code}m{text}\033[0m" if USE_COLOR else text


def disable_color():
    """Used in a pipeline stage whose output goes into a pipe, not a screen."""
    global USE_COLOR
    USE_COLOR = False
    for module in (scheduler, memory, sync):
        module.USE_COLOR = False


RED, GREEN, YELLOW, BLUE, CYAN, DIM = "31", "32", "33", "1;34", "36", "2"

# Signals the shell itself ignores so that Ctrl+Z, background reads/writes
# and Ctrl+\ only affect the job in the foreground, never the shell.
SHELL_IGNORED_SIGNALS = (signal.SIGTSTP, signal.SIGTTIN,
                         signal.SIGTTOU, signal.SIGQUIT)

# Signals a child must reset to default before exec() so the new program
# behaves normally (Python ignores SIGPIPE by default, and ignored signals
# stay ignored across exec).
CHILD_RESET_SIGNALS = (signal.SIGINT, signal.SIGQUIT, signal.SIGTSTP,
                       signal.SIGTTIN, signal.SIGTTOU, signal.SIGCHLD,
                       signal.SIGPIPE)

# Operators this shell does not support (pipes "|" are supported).
UNSUPPORTED_OPERATORS = {"||", "&&", ";", "<", ">", ">>", "(", ")"}

# Built-ins that can safely run inside a forked child when followed by "&".
FORKABLE_BUILTINS = {"echo", "pwd", "ls", "cat"}

# Built-ins that change the shell's own state. Inside a pipeline they would
# run in a child process and the change would be lost, so they are refused.
STATEFUL_BUILTINS = {"cd", "exit", "jobs", "fg", "bg", "su", "logout", "trace",
                     "clear", "kill", "useradd"}


# --------------------------------------------------------------------------
# Job table
# --------------------------------------------------------------------------
class Job:
    """One entry in the job table: a single process, or every process of a
    pipeline. All processes of a job share one process group."""

    def __init__(self, job_id, pids, command, background):
        self.id = job_id
        self.pids = list(pids)
        self.pid = self.pids[0]
        self.pgid = self.pids[0]     # each job gets its own process group
        self.alive = set(self.pids)  # processes not reaped yet
        self.command = command       # command text without a trailing "&"
        self.background = background
        self.status = "Running"      # Running | Stopped | Done | Exit N | ...
        self.returncode = None
        self.final = None            # (status text, code) of the last stage
        self.finished = False
        self.changed = False         # set when the state changes in the background
        self.started = time.time()


# --------------------------------------------------------------------------
# The shell
# --------------------------------------------------------------------------
class Shell:
    def __init__(self):
        self.jobs = {}               # job_id -> Job
        self.last_status = 0         # exit status of the last command ($?)
        self.trace = False           # print fork/exec/wait/pipe events when True
        self.warned_stopped = False  # "There are stopped jobs" warning given
        self.interactive = sys.stdin.isatty()
        self.tty_fd = sys.stdin.fileno()
        self.shell_pgid = os.getpgrp()
        self.shell_tmodes = None

        self.builtins = {
            "cd": self.b_cd, "pwd": self.b_pwd, "exit": self.b_exit,
            "echo": self.b_echo, "clear": self.b_clear, "ls": self.b_ls,
            "cat": self.b_cat, "mkdir": self.b_mkdir, "rmdir": self.b_rmdir,
            "rm": self.b_rm, "touch": self.b_touch, "kill": self.b_kill,
            "jobs": self.b_jobs, "fg": self.b_fg, "bg": self.b_bg,
            "help": self.b_help, "trace": self.b_trace,
            "sched": self.b_sched, "mem": self.b_mem, "sync": self.b_sync,
            "su": self.b_su, "logout": self.b_logout,
        }
        self.sched = SchedShell(self.err)   # Deliverable 2 scheduler
        self.mem = MemShell(self.err)       # Deliverable 3 paging
        self.sync = SyncShell(self.err)     # Deliverable 3 synchronization
        self.sec = Security(os.getcwd(), self.err)   # Deliverable 4 security
        for name in ("whoami", "users", "useradd", "chmod", "chown", "audit"):
            self.builtins[name] = getattr(self.sec, "b_" + name)

        if self.interactive:
            self._init_job_control()

    def _init_job_control(self):
        """Put the shell in its own process group and take the terminal."""
        # If we were started in the background, wait until we are foreground.
        while os.tcgetpgrp(self.tty_fd) != os.getpgrp():
            os.kill(-os.getpgrp(), signal.SIGTTIN)
        for sig in SHELL_IGNORED_SIGNALS:
            signal.signal(sig, signal.SIG_IGN)
        try:
            os.setpgid(0, 0)
        except OSError:
            pass  # already a session or group leader
        self.shell_pgid = os.getpgrp()
        os.tcsetpgrp(self.tty_fd, self.shell_pgid)
        if termios:
            self.shell_tmodes = termios.tcgetattr(self.tty_fd)

    # ---------------------------------------------------------------- output
    def err(self, message, code=1):
        """Print an error in a consistent 'mysh: ...' format and return code."""
        sys.stdout.flush()
        print(color(f"mysh: {message}", RED), file=sys.stderr)
        return code

    def tmsg(self, message):
        """Print a trace line describing a process-management event."""
        if self.trace:
            sys.stdout.flush()
            print(color(f"  [trace] {message}", YELLOW), file=sys.stderr)

    def prompt(self):
        try:
            cwd = os.getcwd()
        except FileNotFoundError:
            cwd = os.environ.get("PWD", "?")
        home = os.path.expanduser("~")
        if cwd == home or cwd.startswith(home + os.sep):
            cwd = "~" + cwd[len(home):]
        user = self.sec.user
        mark = "#" if user.is_admin else "$"       # like root's prompt
        if not USE_COLOR:
            return f"{user.name}@mysh:{cwd}{mark} "
        # \001 and \002 tell readline the color codes take up no width.
        s, e = ("\001", "\002") if HAVE_READLINE else ("", "")
        ucol = "1;31" if user.is_admin else "1;32"
        return (f"{s}\033[{ucol}m{e}{user.name}@mysh{s}\033[0m{e}:"
                f"{s}\033[1;34m{e}{cwd}{s}\033[0m{e}{mark} ")

    # ------------------------------------------------------------- main loop
    def run(self):
        print(color("mysh - MSCS-630 shell simulation (Deliverables 1-4). "
                    "Type 'help' for commands.", CYAN))
        if not self.sec.login():
            sys.exit(1)
        while True:
            self.reap_jobs()
            self.report_job_changes()
            try:
                line = input(self.prompt())
            except EOFError:          # Ctrl+D
                print()
                line = "exit"
            except KeyboardInterrupt:  # Ctrl+C at the prompt
                print()
                self.last_status = 130
                continue
            self.execute_line(line)

    # ---------------------------------------------------------------- parser
    def tokenize(self, line):
        """Split a command line into words, honoring quotes.

        punctuation_chars=True makes '&', '|', ';', '<' and '>' separate
        tokens, so "sleep 5&" is read the same way as "sleep 5 &" and
        "ls|grep txt" the same as "ls | grep txt".
        """
        lexer = shlex.shlex(line, posix=True, punctuation_chars=True)
        lexer.whitespace_split = True
        lexer.commenters = ""
        return list(lexer)

    def expand(self, word):
        """Expand ~, $? and $VARIABLE inside a word."""
        if word.startswith("~"):
            word = os.path.expanduser(word)
        word = word.replace("$?", str(self.last_status))
        return os.path.expandvars(word)

    def execute_line(self, line):
        line = line.strip()
        if not line or line.startswith("#"):
            return
        try:
            tokens = self.tokenize(line)
        except ValueError as e:
            self.last_status = self.err(f"syntax error: {str(e).lower()}", 2)
            return

        background = False
        if tokens and tokens[-1] == "&":
            background = True
            tokens = tokens[:-1]
        if not tokens:
            self.last_status = self.err("syntax error near unexpected token '&'", 2)
            return
        for tok in tokens:
            if tok == "&" or tok in UNSUPPORTED_OPERATORS:
                self.last_status = self.err(
                    f"unsupported operator '{tok}' (this shell supports pipes '|' "
                    "and '&', but not redirection or command lists)", 2)
                return

        # Split into pipeline stages on "|".
        stages, current = [], []
        for tok in tokens:
            if tok == "|":
                stages.append(current)
                current = []
            else:
                current.append(tok)
        stages.append(current)
        if any(not s for s in stages):
            self.last_status = self.err("syntax error near unexpected token '|'", 2)
            return

        argvs = [[self.expand(t) for t in s] for s in stages]
        cmdline = " ".join(tokens)
        if argvs[0][0] != "exit":
            self.warned_stopped = False

        if len(argvs) > 1:
            for argv in argvs:
                if argv[0] in STATEFUL_BUILTINS:
                    self.last_status = self.err(
                        f"{argv[0]}: cannot be used in a pipeline "
                        "(it changes the shell itself)", 2)
                    return
            self.launch_pipeline(argvs, background, cmdline)
            return

        argv = argvs[0]
        name, args = argv[0], argv[1:]
        if name in self.builtins:
            if background and name in FORKABLE_BUILTINS:
                self.launch_pipeline([argv], True, cmdline)
            else:
                if background:
                    self.err(f"{name}: shell built-in runs in the foreground")
                self.last_status = self.builtins[name](args)
        else:
            self.launch_pipeline([argv], background, cmdline)

    # ------------------------------------------------------ process creation
    def resolve_program(self, program):
        """Check that an external command exists before forking.

        Returns an error message, or None if the program can be executed.
        """
        if "/" in program:
            if not os.path.exists(program):
                return f"{program}: No such file or directory"
            if os.path.isdir(program):
                return f"{program}: Is a directory"
            if not os.access(program, os.X_OK):
                return f"{program}: Permission denied"
            return None
        if shutil.which(program) is None:
            return f"{program}: command not found"
        return None

    def check_stage(self, argv, pipeline):
        """Validate one stage before anything is forked. Returns True if OK."""
        name = argv[0]
        if name in self.builtins:
            return True
        problem = self.resolve_program(name)
        if problem:
            code = 127 if "not found" in problem or "No such" in problem else 126
            note = " (pipeline not started)" if pipeline else ""
            self.last_status = self.err(problem + note, code)
            return False
        # mysh's own execute permission for scripts inside the workspace
        if "/" in name and not self.sec.require("exec", name, "x"):
            self.last_status = 126
            return False
        # An outside program opens its file arguments itself, so mysh checks
        # read access for any workspace file it was given (grep x secret.txt).
        for arg in argv[1:]:
            if os.path.isfile(arg) and not self.sec.require(name, arg, "r"):
                self.last_status = 126
                return False
        return True

    def launch_pipeline(self, stages, background, cmdline):
        """Fork one child per stage and connect them with pipes.

        Stage i writes to pipe i and stage i+1 reads from it. A single
        command is just a pipeline with one stage and no pipes.
        """
        n = len(stages)
        for argv in stages:
            if not self.check_stage(argv, n > 1):
                return

        sys.stdout.flush()
        sys.stderr.flush()
        pipes = [os.pipe() for _ in range(n - 1)]
        for i, (r, w) in enumerate(pipes, 1):
            self.tmsg(f"pipe() #{i}: read end fd {r}, write end fd {w} "
                      f"(stage {i} -> stage {i + 1})")
        pids, pgid = [], 0
        for i, argv in enumerate(stages):
            try:
                pid = os.fork()
            except OSError as e:
                self.last_status = self.err(f"fork failed: {e.strerror}")
                break
            if pid == 0:
                self._child(argv, i, n, pipes, background, pgid)   # never returns
            if not pgid:
                pgid = pid
            try:
                os.setpgid(pid, pgid)  # done in both processes to avoid a race
            except OSError:
                pass
            pids.append(pid)
            if n == 1:
                self.tmsg(f"fork(): shell pid {os.getpid()} created child pid {pid} "
                          f"in process group {pid}")
            else:
                src = "terminal" if i == 0 else f"pipe #{i}"
                dst = "terminal" if i == n - 1 else f"pipe #{i + 1}"
                self.tmsg(f"fork(): stage {i + 1} '{' '.join(argv)}' is pid {pid} "
                          f"(group {pgid}), stdin <- {src}, stdout -> {dst}")
        # The shell keeps no pipe ends open; otherwise the last reader would
        # never see end-of-file.
        for r, w in pipes:
            os.close(r)
            os.close(w)
        if not pids:
            return

        job = self.add_job(pids, cmdline, background)
        if background:
            self.tmsg(f"background job [{job.id}]: shell does not wait, "
                      "returns to the prompt")
            if self.trace:
                # Parent and child write to the same terminal at the same
                # time; a short pause keeps the child's trace line from
                # landing on top of the next prompt.
                time.sleep(0.05)
            print(f"[{job.id}] {' '.join(map(str, pids))}")
            self.last_status = 0
        else:
            self.wait_foreground(job)

    def _child(self, argv, i, n, pipes, background, pgid):
        """Runs in a child right after fork(): wire up the pipes, then exec."""
        try:
            os.setpgid(0, pgid)      # pgid 0 = start a new group with my pid
        except OSError:
            pass
        if self.interactive and not background:
            try:
                os.tcsetpgrp(self.tty_fd, os.getpgrp())  # job owns the terminal
            except OSError:
                pass
        for sig in CHILD_RESET_SIGNALS:
            signal.signal(sig, signal.SIG_DFL)
        if i > 0:
            os.dup2(pipes[i - 1][0], 0)   # stdin  <- previous stage
        if i < n - 1:
            os.dup2(pipes[i][1], 1)       # stdout -> next stage
        for r, w in pipes:                # close every original pipe end
            os.close(r)
            os.close(w)
        if not os.isatty(1):
            disable_color()

        name = argv[0]
        if name in self.builtins:
            try:
                status = self.builtins[name](argv[1:])
                sys.stdout.flush()
            except SystemExit as e:
                status = e.code if isinstance(e.code, int) else 0
            except BrokenPipeError:
                status = 0
            os._exit(status if isinstance(status, int) else 0)
        self.tmsg(f"child {os.getpid()}: execvp('{name}', {argv})")
        try:
            os.execvp(name, argv)
        except OSError as e:
            # Only reached if exec fails; the child must not return
            # to the shell loop, so it exits right away.
            self.err(f"{name}: {e.strerror}")
            os._exit(126)

    # ------------------------------------------------------------ job table
    def add_job(self, pids, command, background):
        job_id = max(self.jobs, default=0) + 1
        job = Job(job_id, pids, command, background)
        self.jobs[job_id] = job
        return job

    def find_job_by_pid(self, pid):
        for job in self.jobs.values():
            if pid in job.pids:
                return job
        return None

    def find_job(self, spec):
        """Look up a job from '%1', '1', '%+', '%-' or None (current job)."""
        ids = sorted(self.jobs)
        if not ids:
            return None
        if spec in (None, "%", "%%", "%+", "+"):
            return self.jobs[ids[-1]]
        if spec in ("%-", "-"):
            return self.jobs[ids[-2]] if len(ids) > 1 else None
        try:
            return self.jobs.get(int(spec.lstrip("%")))
        except ValueError:
            return None

    def apply_status(self, job, pid, status):
        """Translate a raw waitpid() status for one process into job state.

        A pipeline is finished when every stage has exited, and like bash its
        exit status is the exit status of the last stage.
        """
        if os.WIFSTOPPED(status):
            job.status = "Stopped"
            return
        if os.WIFCONTINUED(status):
            job.status = "Running"
            return
        if os.WIFEXITED(status):
            code = os.WEXITSTATUS(status)
            result = ("Done" if code == 0 else f"Exit {code}", code)
        else:  # WIFSIGNALED
            sig = os.WTERMSIG(status)
            names = {signal.SIGTERM: "Terminated", signal.SIGKILL: "Killed",
                     signal.SIGINT: "Interrupt", signal.SIGHUP: "Hangup",
                     signal.SIGPIPE: "Broken pipe"}
            result = (names.get(sig, signal.Signals(sig).name), 128 + sig)
        job.alive.discard(pid)
        if pid == job.pids[-1]:
            job.final = result
        if not job.alive:
            job.status, job.returncode = job.final or result
            job.finished = True

    def reap_jobs(self):
        """Collect status changes of children without blocking (WNOHANG).

        This is what keeps finished background jobs from staying zombies.
        """
        flags = os.WNOHANG | os.WUNTRACED | os.WCONTINUED
        while True:
            try:
                pid, status = os.waitpid(-1, flags)
            except ChildProcessError:
                break  # no children left
            if pid == 0:
                break  # children exist, but none changed state
            job = self.find_job_by_pid(pid)
            if job:
                before = job.status
                self.apply_status(job, pid, status)
                if job.status != before or job.finished:
                    job.changed = True
                self.tmsg(f"waitpid(-1, WNOHANG) reaped pid {pid}: {job.status}")

    def report_job_changes(self):
        """Print notices like bash does: '[1]+  Done  sleep 5'."""
        for job_id in sorted(self.jobs):
            job = self.jobs[job_id]
            if job.changed and (job.finished or job.status == "Stopped"):
                print(self.format_job(job))
            job.changed = False
            if job.finished:
                del self.jobs[job_id]

    def format_job(self, job):
        ids = sorted(self.jobs)
        mark = "+" if job.id == ids[-1] else "-" if len(ids) > 1 and job.id == ids[-2] else " "
        cmd = job.command + (" &" if job.background and job.status == "Running" else "")
        return f"[{job.id}]{mark}  {job.pid:<7} {job.status:<11} {cmd}"

    def wait_foreground(self, job, send_cont=False):
        """Give the job the terminal and block in waitpid() until every
        process exits or the job is stopped (WUNTRACED catches Ctrl+Z)."""
        if self.interactive:
            try:
                os.tcsetpgrp(self.tty_fd, job.pgid)
            except OSError:
                pass
        if send_cont:
            try:
                os.killpg(job.pgid, signal.SIGCONT)
            except ProcessLookupError:
                pass
        target = job.pid if len(job.pids) == 1 else -job.pgid
        self.tmsg(f"foreground job: shell blocks in waitpid({target}, WUNTRACED)")
        try:
            while job.alive:
                try:
                    pid, status = os.waitpid(target, os.WUNTRACED)
                except ChildProcessError:
                    job.alive.clear()
                    job.status, job.returncode = job.final or ("Done", 0)
                    job.finished = True
                    break
                self.apply_status(job, pid, status)
                if job.status == "Stopped":
                    break
        finally:
            if self.interactive:
                # Take the terminal back and restore its settings, in case
                # the program (an editor, for example) changed them.
                os.tcsetpgrp(self.tty_fd, self.shell_pgid)
                if termios and self.shell_tmodes:
                    termios.tcsetattr(self.tty_fd, termios.TCSADRAIN,
                                      self.shell_tmodes)

        self.tmsg(f"waitpid({target}) returned: {job.status}")
        if job.status == "Stopped":
            job.background = True
            print()
            print(self.format_job(job))
            self.last_status = 148
        else:
            if job.status in ("Interrupt",):
                print()
            elif job.finished and job.status not in ("Done", "Broken pipe") \
                    and not job.status.startswith("Exit"):
                print(job.status)
            self.last_status = job.returncode or 0
            del self.jobs[job.id]

    # ============================================================ built-ins
    def b_help(self, args):
        rows = [
            ("cd [dir]", "change directory (no arg = home, '-' = previous)"),
            ("pwd", "print the working directory"),
            ("echo [-n] [text]", "print text"),
            ("clear", "clear the screen"),
            ("ls [-a] [-l] [path..]", "list directory contents"),
            ("cat [file..]", "show file contents (reads a pipe if no file)"),
            ("mkdir [-p] dir..", "create directories"),
            ("rmdir dir..", "remove empty directories"),
            ("rm [-r] [-f] file..", "remove files (or trees with -r)"),
            ("touch file..", "create a file or update its timestamp"),
            ("kill [-SIG] pid|%job", "send a signal (default SIGTERM)"),
            ("jobs", "list background and stopped jobs"),
            ("fg [%job]", "bring a job to the foreground"),
            ("bg [%job]", "resume a stopped job in the background"),
            ("trace on|off", "show fork/exec/wait/pipe events as they happen"),
            ("sched help", "process scheduling (Round-Robin, Priority)"),
            ("mem help", "paging, page faults, FIFO / LRU replacement"),
            ("sync help", "mutex, semaphores, Producer-Consumer"),
            ("cmd1 | cmd2 | ...", "pipe each command's output into the next"),
            ("whoami", "show the logged-in user, role and group"),
            ("chmod MODE FILE", "change permissions (owner or admin): 600, +x"),
            ("chown USER FILE", "change a file's owner (admin only)"),
            ("users / useradd", "list or add users (admin only)"),
            ("audit", "show denied actions and logins (admin only)"),
            ("su USER", "switch to another user (asks for the password)"),
            ("logout", "log out and return to the login prompt"),
            ("exit [n]", "leave the shell"),
            ("cmd &", "run any command in the background"),
        ]
        for usage, desc in rows:
            print(f"  {usage:<24} {desc}")
        return 0

    def b_sched(self, args):
        """Deliverable 2: simulated process scheduling."""
        return self.sched.run(args)

    def b_mem(self, args):
        """Deliverable 3: simulated paging and page replacement."""
        return self.mem.run(args)

    def b_sync(self, args):
        """Deliverable 3: mutexes, semaphores and Producer-Consumer."""
        return self.sync.run(args)

    def b_trace(self, args):
        if args and args[0] in ("on", "off"):
            self.trace = args[0] == "on"
            print(f"trace {'enabled' if self.trace else 'disabled'}")
            return 0
        if not args:
            print(f"trace is {'on' if self.trace else 'off'}")
            return 0
        return self.err("trace: usage: trace on|off", 2)

    # ------------------------------------------------- Deliverable 4: users
    def b_su(self, args):
        if len(args) != 1:
            return self.err("su: usage: su USER", 2)
        try:
            password = getpass.getpass("password: ")
        except (EOFError, KeyboardInterrupt):
            print()
            return 1
        user = self.sec.verify(args[0], password)
        if user is None:
            self.sec.audit("su", args[0], "FAILED")
            time.sleep(1)
            return self.err("su: authentication failure")
        self.sec.user = user
        os.environ["MYSH_USER"] = user.name
        self.sec.audit("su", user.name, "ok")
        print(f"now logged in as {user.name} (role: {user.role}, group: {user.group})")
        return 0

    def b_logout(self, args):
        self.reap_jobs()
        for job in self.jobs.values():   # a user's jobs end with the session
            try:
                os.killpg(job.pgid, signal.SIGHUP)
                os.killpg(job.pgid, signal.SIGCONT)
            except ProcessLookupError:
                pass
        print(f"{self.sec.user.name} logged out")
        self.sec.audit("logout", "-", "ok")
        os.chdir(self.sec.root)
        if not self.sec.login(banner=False):
            sys.exit(1)
        return 0

    # ------------------------------------------------- file system built-ins
    def b_cd(self, args):
        if len(args) > 1:
            return self.err("cd: too many arguments")
        target = args[0] if args else os.path.expanduser("~")
        if target == "-":
            target = os.environ.get("OLDPWD")
            if not target:
                return self.err("cd: OLDPWD not set")
            print(target)
        if os.path.isdir(target) and not self.sec.require("cd", target, "x"):
            return 1
        try:
            old = os.getcwd()
        except FileNotFoundError:
            old = os.environ.get("PWD", "")
        try:
            os.chdir(target)
        except FileNotFoundError:
            return self.err(f"cd: {target}: No such file or directory")
        except NotADirectoryError:
            return self.err(f"cd: {target}: Not a directory")
        except PermissionError:
            return self.err(f"cd: {target}: Permission denied")
        os.environ["OLDPWD"] = old
        os.environ["PWD"] = os.getcwd()
        return 0

    def b_pwd(self, args):
        try:
            print(os.getcwd())
        except FileNotFoundError:
            return self.err("pwd: current directory no longer exists")
        return 0

    def b_echo(self, args):
        newline = True
        if args and args[0] == "-n":
            newline, args = False, args[1:]
        print(" ".join(args), end="\n" if newline else "")
        return 0

    def b_clear(self, args):
        sys.stdout.write("\033[H\033[2J\033[3J")
        sys.stdout.flush()
        return 0

    def b_ls(self, args):
        show_all = long_format = False
        paths = []
        for a in args:
            if a.startswith("-") and len(a) > 1:
                for ch in a[1:]:
                    if ch == "a":
                        show_all = True
                    elif ch == "l":
                        long_format = True
                    else:
                        return self.err(f"ls: invalid option -- '{ch}'", 2)
            else:
                paths.append(a)
        paths = paths or ["."]
        status = 0
        for i, path in enumerate(paths):
            try:
                st = os.stat(path)
            except FileNotFoundError:
                status = self.err(f"ls: cannot access '{path}': No such file or directory", 2)
                continue
            except PermissionError:
                status = self.err(f"ls: cannot access '{path}': Permission denied", 2)
                continue
            if stat.S_ISDIR(st.st_mode):
                if not self.sec.require("ls", path, "r"):
                    status = 2
                    continue
                try:
                    names = sorted(os.listdir(path), key=lambda n: n.lower().lstrip("."))
                except PermissionError:
                    status = self.err(f"ls: cannot open directory '{path}': Permission denied", 2)
                    continue
                if show_all:
                    names = [".", ".."] + names
                else:
                    names = [n for n in names if not n.startswith(".")]
                entries = [(n, os.path.join(path, n)) for n in names]
                if len(paths) > 1:
                    print(f"{path}:")
            else:
                entries = [(path, path)]
            self._print_entries(entries, long_format)
            if len(paths) > 1 and i < len(paths) - 1:
                print()
        return status

    def _styled_name(self, name, full):
        if os.path.isdir(full):
            return color(name, BLUE)
        cols = self.sec.ls_columns(full)
        executable = ("x" in cols[0]) if cols else os.access(full, os.X_OK)
        if executable:
            return color(name, "1;32")
        return name

    def _print_entries(self, entries, long_format):
        if not entries:
            return
        if long_format:
            for name, full in entries:
                try:
                    st = os.lstat(full)
                except OSError:
                    continue
                mtime = time.strftime("%b %d %H:%M", time.localtime(st.st_mtime))
                cols = self.sec.ls_columns(full)
                if cols:                      # mysh's own owner / group / mode
                    mode, owner, group = cols
                else:                         # outside the workspace: real values
                    mode = stat.filemode(st.st_mode)
                    try:
                        owner = pwd.getpwuid(st.st_uid).pw_name
                    except KeyError:
                        owner = str(st.st_uid)
                    group = "-"
                print(f"{mode} {owner:<10}{group:<7}{st.st_size:>7} {mtime} "
                      f"{self._styled_name(name, full)}")
            return
        if not sys.stdout.isatty():           # into a pipe: one name per line,
            for name, full in entries:        # like the real ls, so grep and
                print(name)                   # sort see one file per line
            return
        width = max(len(n) for n, _ in entries) + 2
        term_cols = shutil.get_terminal_size((80, 24)).columns
        cols = max(1, term_cols // width)
        rows = (len(entries) + cols - 1) // cols
        for r in range(rows):
            line = ""
            for c in range(cols):
                idx = c * rows + r
                if idx < len(entries):
                    name, full = entries[idx]
                    pad = " " * (width - len(name))
                    line += self._styled_name(name, full) + pad
            print(line.rstrip())

    def b_cat(self, args):
        if not args:
            if sys.stdin.isatty():
                return self.err("cat: missing file operand")
            data = sys.stdin.buffer.read()       # used inside a pipeline
            sys.stdout.flush()
            sys.stdout.buffer.write(data)
            sys.stdout.buffer.flush()
            return 0
        status = 0
        for name in args:
            if os.path.isfile(name) and not self.sec.require("cat", name, "r"):
                status = 1
                continue
            try:
                with open(name, "rb") as f:
                    data = f.read()
                sys.stdout.flush()
                sys.stdout.buffer.write(data)
                sys.stdout.buffer.flush()
            except FileNotFoundError:
                status = self.err(f"cat: {name}: No such file or directory")
            except IsADirectoryError:
                status = self.err(f"cat: {name}: Is a directory")
            except PermissionError:
                status = self.err(f"cat: {name}: Permission denied")
        return status

    def b_mkdir(self, args):
        parents = "-p" in args
        names = [a for a in args if a != "-p"]
        if not names:
            return self.err("mkdir: missing operand")
        status = 0
        for name in names:
            # the closest folder that already exists must be writable
            new_dirs, probe = [], os.path.abspath(name)
            while not os.path.exists(probe):
                new_dirs.append(probe)
                probe = os.path.dirname(probe)
            if new_dirs and not self.sec.require("mkdir", probe, "w",
                                                 target=os.path.relpath(probe) if probe != os.getcwd() else "."):
                status = 1
                continue
            try:
                if parents:
                    os.makedirs(name, exist_ok=True)
                else:
                    os.mkdir(name)
                for d in reversed(new_dirs):
                    self.sec.created(d, is_dir=True)
            except FileExistsError:
                status = self.err(f"mkdir: cannot create directory '{name}': File exists")
            except FileNotFoundError:
                status = self.err(f"mkdir: cannot create directory '{name}': No such file or directory")
            except PermissionError:
                status = self.err(f"mkdir: cannot create directory '{name}': Permission denied")
        return status

    def _can_remove(self, cmd, name):
        """Removing needs write access to the item and to its folder."""
        if not self.sec.require(cmd, name, "w"):
            return False
        parent = self.sec.parent(name)
        shown = os.path.relpath(parent) if parent != os.getcwd() else "."
        return self.sec.require(cmd, parent, "w", target=shown)

    def b_rmdir(self, args):
        if not args:
            return self.err("rmdir: missing operand")
        status = 0
        for name in args:
            if os.path.isdir(name) and not self._can_remove("rmdir", name):
                status = 1
                continue
            try:
                os.rmdir(name)
                self.sec.removed(name)
            except FileNotFoundError:
                status = self.err(f"rmdir: failed to remove '{name}': No such file or directory")
            except NotADirectoryError:
                status = self.err(f"rmdir: failed to remove '{name}': Not a directory")
            except PermissionError:
                status = self.err(f"rmdir: failed to remove '{name}': Permission denied")
            except OSError as e:  # ENOTEMPTY / EEXIST
                status = self.err(f"rmdir: failed to remove '{name}': {e.strerror}")
        return status

    def b_rm(self, args):
        recursive = force = False
        names = []
        for a in args:
            if a.startswith("-") and len(a) > 1:
                for ch in a[1:]:
                    if ch in "rR":
                        recursive = True
                    elif ch == "f":
                        force = True
                    else:
                        return self.err(f"rm: invalid option -- '{ch}'", 2)
            else:
                names.append(a)
        if not names:
            return self.err("rm: missing operand")
        status = 0
        for name in names:
            if os.path.lexists(name) and not self._can_remove("rm", name):
                status = 1
                continue
            try:
                if os.path.isdir(name) and not os.path.islink(name):
                    if not recursive:
                        status = self.err(f"rm: cannot remove '{name}': Is a directory")
                        continue
                    shutil.rmtree(name)
                else:
                    os.remove(name)
                self.sec.removed(name)
            except FileNotFoundError:
                if not force:
                    status = self.err(f"rm: cannot remove '{name}': No such file or directory")
            except PermissionError:
                status = self.err(f"rm: cannot remove '{name}': Permission denied")
        return status

    def b_touch(self, args):
        if not args:
            return self.err("touch: missing file operand")
        status = 0
        for name in args:
            exists = os.path.exists(name)
            if exists:
                ok = self.sec.require("touch", name, "w")
            else:
                parent = self.sec.parent(name)
                ok = not os.path.isdir(parent) or self.sec.require(
                    "touch", parent, "w",
                    target=os.path.relpath(parent) if parent != os.getcwd() else ".")
            if not ok:
                status = 1
                continue
            try:
                if exists:
                    os.utime(name, None)     # update the timestamps
                else:
                    open(name, "a").close()  # create an empty file
                    self.sec.created(name)
            except FileNotFoundError:
                status = self.err(f"touch: cannot touch '{name}': No such file or directory")
            except PermissionError:
                status = self.err(f"touch: cannot touch '{name}': Permission denied")
        return status

    # ------------------------------------------------------ job control
    @staticmethod
    def parse_signal(spec):
        if spec.isdigit():
            num = int(spec)
            return num if num in signal.valid_signals() else None
        name = spec.upper()
        if not name.startswith("SIG"):
            name = "SIG" + name
        sig = getattr(signal, name, None)
        return sig if isinstance(sig, signal.Signals) else None

    def b_kill(self, args):
        usage = "kill: usage: kill [-SIGNAL] pid | %job ..."
        if not args:
            return self.err(usage, 2)
        sig = signal.SIGTERM
        if args[0].startswith("-") and len(args[0]) > 1:
            sig = self.parse_signal(args[0][1:])
            if sig is None:
                return self.err(f"kill: {args[0][1:]}: invalid signal specification")
            args = args[1:]
            if not args:
                return self.err(usage, 2)

        terminating = sig in (signal.SIGTERM, signal.SIGHUP, signal.SIGINT,
                              signal.SIGQUIT)
        status = 0
        for target in args:
            try:
                if target.startswith("%"):
                    job = self.find_job(target)
                    if job is None:
                        status = self.err(f"kill: {target}: no such job")
                        continue
                    os.killpg(job.pgid, sig)
                    if job.status == "Stopped" and terminating:
                        os.killpg(job.pgid, signal.SIGCONT)  # let it see the signal
                else:
                    try:
                        pid = int(target)
                    except ValueError:
                        status = self.err(f"kill: {target}: arguments must be process or job IDs")
                        continue
                    if pid <= 0:
                        status = self.err(f"kill: {pid}: invalid process id")
                        continue
                    os.kill(pid, sig)
                    job = self.find_job_by_pid(pid)
                    if job and job.status == "Stopped" and terminating:
                        os.kill(pid, signal.SIGCONT)
                self.tmsg(f"kill({target}, {signal.Signals(sig).name}) sent")
            except ProcessLookupError:
                status = self.err(f"kill: ({target}) - No such process")
            except PermissionError:
                status = self.err(f"kill: ({target}) - Operation not permitted")
        time.sleep(0.05)  # give the target a moment so the notice shows next prompt
        return status

    def b_jobs(self, args):
        self.reap_jobs()
        for job_id in sorted(self.jobs):
            job = self.jobs[job_id]
            print(self.format_job(job))
            job.changed = False
        # Remove jobs that finished; they were just reported.
        for job_id in [j for j, job in self.jobs.items() if job.finished]:
            del self.jobs[job_id]
        return 0

    def b_fg(self, args):
        spec = args[0] if args else None
        job = self.find_job(spec)
        if job is None:
            return self.err(f"fg: {spec or 'current'}: no such job")
        print(job.command)
        job.background = False
        job.status = "Running"
        self.tmsg(f"fg: tcsetpgrp(tty, {job.pgid}) then killpg({job.pgid}, SIGCONT)")
        self.wait_foreground(job, send_cont=True)
        return self.last_status

    def b_bg(self, args):
        spec = args[0] if args else None
        job = self.find_job(spec)
        if job is None:
            return self.err(f"bg: {spec or 'current'}: no such job")
        if job.status == "Running":
            return self.err(f"bg: job {job.id} already in background")
        try:
            os.killpg(job.pgid, signal.SIGCONT)
        except ProcessLookupError:
            return self.err(f"bg: job {job.id} has terminated")
        job.status = "Running"
        job.background = True
        self.tmsg(f"bg: killpg({job.pgid}, SIGCONT), shell does not wait")
        print(f"[{job.id}]+ {job.command} &")
        return 0

    def b_exit(self, args):
        code = self.last_status
        if args:
            try:
                code = int(args[0]) & 0xFF
            except ValueError:
                return self.err(f"exit: {args[0]}: numeric argument required", 2)
        self.reap_jobs()
        stopped = [j for j in self.jobs.values() if j.status == "Stopped"]
        if stopped and not self.warned_stopped:
            self.warned_stopped = True
            return self.err("There are stopped jobs. Type 'exit' again to quit.")
        for job in self.jobs.values():   # clean up anything still running
            try:
                os.killpg(job.pgid, signal.SIGHUP)
                os.killpg(job.pgid, signal.SIGCONT)
            except ProcessLookupError:
                pass
        print("exit")
        sys.exit(code)


def main():
    if not hasattr(os, "fork"):
        print("mysh needs a Unix-like OS (Linux or macOS) because it uses os.fork().")
        sys.exit(1)
    Shell().run()


if __name__ == "__main__":
    main()
