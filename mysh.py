#!/usr/bin/env python3
"""
mysh - Shell Simulation with Integrated OS Concepts
Deliverable 1: Basic Shell Implementation and Process Management
Deliverable 2: Process Scheduling (see scheduler.py and the `sched` built-in)

MSCS-630 Advanced Operating Systems

What this deliverable covers
  * A read-parse-execute loop that accepts user commands.
  * Built-in commands: cd, pwd, exit, echo, clear, ls, cat, mkdir, rmdir,
    rm, touch, kill (plus jobs, fg, bg, help, trace).
  * External programs are started with the real Unix system calls
    os.fork(), os.execvp(), os.waitpid() and os._exit().
  * Foreground and background execution ("&"), a job table that tracks
    the state of every child, and job control (jobs, fg, bg, Ctrl+Z, Ctrl+C).
  * Deliverable 2: a `sched` built-in that runs a simulated workload through
    Round-Robin (configurable time slice) and preemptive Priority scheduling
    and reports waiting, turnaround and response time.

Run it with:   python3 mysh.py
It needs a Unix-like OS (Linux or macOS) because os.fork() does not exist
on Windows. No third-party packages are required.
"""

import os
import shlex
import shutil
import signal
import stat
import sys
import time

from scheduler import SchedShell

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

# Operators that later deliverables will add (pipes, redirection, lists).
UNSUPPORTED_OPERATORS = {"|", "||", "&&", ";", "<", ">", ">>", "(", ")"}

# Built-ins that can safely run inside a forked child when followed by "&".
FORKABLE_BUILTINS = {"echo", "pwd", "ls", "cat"}


# --------------------------------------------------------------------------
# Job table
# --------------------------------------------------------------------------
class Job:
    """One entry in the shell's job table (one child process for now)."""

    def __init__(self, job_id, pid, command, background):
        self.id = job_id
        self.pid = pid
        self.pgid = pid              # each job gets its own process group
        self.command = command       # command text without a trailing "&"
        self.background = background
        self.status = "Running"      # Running | Stopped | Done | Exit N | ...
        self.returncode = None
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
        self.trace = False           # print fork/exec/wait events when True
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
            "sched": self.b_sched,
        }
        self.sched = SchedShell(self.err)   # Deliverable 2 scheduler

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
        if not USE_COLOR:
            return f"mysh:{cwd}$ "
        # \001 and \002 tell readline the color codes take up no width.
        s, e = ("\001", "\002") if HAVE_READLINE else ("", "")
        return (f"{s}\033[1;32m{e}mysh{s}\033[0m{e}:"
                f"{s}\033[1;34m{e}{cwd}{s}\033[0m{e}$ ")

    # ------------------------------------------------------------- main loop
    def run(self):
        print(color("mysh - MSCS-630 shell simulation (Deliverables 1-2). "
                    "Type 'help' for commands.", CYAN))
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
        tokens, so "sleep 5&" is read the same way as "sleep 5 &".
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
                    f"unsupported operator '{tok}' (pipes and redirection "
                    "are planned for a later deliverable)", 2)
                return

        argv = [self.expand(t) for t in tokens]
        name, args = argv[0], argv[1:]
        cmdline = " ".join(tokens)
        if name != "exit":
            self.warned_stopped = False

        if name in self.builtins:
            if background and name in FORKABLE_BUILTINS:
                self.launch(argv, True, cmdline, builtin=True)
            else:
                if background:
                    self.err(f"{name}: shell built-in runs in the foreground")
                self.last_status = self.builtins[name](args)
        else:
            self.launch(argv, background, cmdline)

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

    def launch(self, argv, background, cmdline, builtin=False):
        """Create a child with fork() and run the command in it."""
        if not builtin:
            problem = self.resolve_program(argv[0])
            if problem:
                code = 127 if "not found" in problem or "No such" in problem else 126
                self.last_status = self.err(problem, code)
                return

        sys.stdout.flush()
        sys.stderr.flush()
        try:
            pid = os.fork()
        except OSError as e:
            self.last_status = self.err(f"fork failed: {e.strerror}")
            return

        if pid == 0:
            # ---------------- child process ----------------
            self._setup_child(background)
            if builtin:
                status = self.builtins[argv[0]](argv[1:])
                sys.stdout.flush()
                os._exit(status)
            self.tmsg(f"child {os.getpid()}: execvp('{argv[0]}', {argv})")
            try:
                os.execvp(argv[0], argv)
            except OSError as e:
                # Only reached if exec fails; the child must not return
                # to the shell loop, so it exits right away.
                self.err(f"{argv[0]}: {e.strerror}")
                os._exit(126)

        # ---------------- parent (the shell) ----------------
        try:
            os.setpgid(pid, pid)  # done in both processes to avoid a race
        except OSError:
            pass
        job = self.add_job(pid, cmdline, background)
        self.tmsg(f"fork(): shell pid {os.getpid()} created child pid {pid} "
                  f"in process group {pid}")
        if background:
            self.tmsg(f"background job [{job.id}]: shell does not wait, "
                      "returns to the prompt")
            if self.trace:
                # Parent and child write to the same terminal at the same
                # time; a short pause keeps the child's trace line from
                # landing on top of the next prompt.
                time.sleep(0.05)
            print(f"[{job.id}] {pid}")
            self.last_status = 0
        else:
            self.wait_foreground(job)

    def _setup_child(self, background):
        """Runs in the child right after fork(), before exec()."""
        pid = os.getpid()
        try:
            os.setpgid(0, 0)  # new process group with pgid == pid
        except OSError:
            pass
        if self.interactive and not background:
            try:
                os.tcsetpgrp(self.tty_fd, pid)  # child owns the terminal
            except OSError:
                pass
        for sig in CHILD_RESET_SIGNALS:
            signal.signal(sig, signal.SIG_DFL)

    # ------------------------------------------------------------ job table
    def add_job(self, pid, command, background):
        job_id = max(self.jobs, default=0) + 1
        job = Job(job_id, pid, command, background)
        self.jobs[job_id] = job
        return job

    def find_job_by_pid(self, pid):
        for job in self.jobs.values():
            if job.pid == pid:
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

    def apply_status(self, job, status):
        """Translate a raw waitpid() status into the job's state."""
        if os.WIFSTOPPED(status):
            job.status = "Stopped"
        elif os.WIFCONTINUED(status):
            job.status = "Running"
        elif os.WIFEXITED(status):
            code = os.WEXITSTATUS(status)
            job.returncode = code
            job.status = "Done" if code == 0 else f"Exit {code}"
            job.finished = True
        elif os.WIFSIGNALED(status):
            sig = os.WTERMSIG(status)
            job.returncode = 128 + sig
            names = {signal.SIGTERM: "Terminated", signal.SIGKILL: "Killed",
                     signal.SIGINT: "Interrupt", signal.SIGHUP: "Hangup"}
            job.status = names.get(sig, signal.Signals(sig).name)
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
                self.apply_status(job, status)
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
        """Give the job the terminal and block in waitpid() until it exits
        or is stopped (WUNTRACED catches Ctrl+Z)."""
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
        self.tmsg(f"foreground job: shell blocks in waitpid({job.pid}, WUNTRACED)")
        try:
            _, status = os.waitpid(job.pid, os.WUNTRACED)
            self.apply_status(job, status)
        except ChildProcessError:
            job.status, job.finished, job.returncode = "Done", True, 0
        finally:
            if self.interactive:
                # Take the terminal back and restore its settings, in case
                # the program (an editor, for example) changed them.
                os.tcsetpgrp(self.tty_fd, self.shell_pgid)
                if termios and self.shell_tmodes:
                    termios.tcsetattr(self.tty_fd, termios.TCSADRAIN,
                                      self.shell_tmodes)

        self.tmsg(f"waitpid({job.pid}) returned: {job.status}")
        if job.status == "Stopped":
            job.background = True
            print()
            print(self.format_job(job))
            self.last_status = 148
        else:
            if job.status in ("Interrupt",):
                print()
            elif job.finished and job.status not in ("Done",) \
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
            ("cat file..", "show file contents"),
            ("mkdir [-p] dir..", "create directories"),
            ("rmdir dir..", "remove empty directories"),
            ("rm [-r] [-f] file..", "remove files (or trees with -r)"),
            ("touch file..", "create a file or update its timestamp"),
            ("kill [-SIG] pid|%job", "send a signal (default SIGTERM)"),
            ("jobs", "list background and stopped jobs"),
            ("fg [%job]", "bring a job to the foreground"),
            ("bg [%job]", "resume a stopped job in the background"),
            ("trace on|off", "show fork/exec/wait events as they happen"),
            ("sched help", "process scheduling (Round-Robin, Priority)"),
            ("exit [n]", "leave the shell"),
            ("cmd &", "run any command in the background"),
        ]
        for usage, desc in rows:
            print(f"  {usage:<24} {desc}")
        return 0

    def b_sched(self, args):
        """Deliverable 2: simulated process scheduling."""
        return self.sched.run(args)

    def b_trace(self, args):
        if args and args[0] in ("on", "off"):
            self.trace = args[0] == "on"
            print(f"trace {'enabled' if self.trace else 'disabled'}")
            return 0
        if not args:
            print(f"trace is {'on' if self.trace else 'off'}")
            return 0
        return self.err("trace: usage: trace on|off", 2)

    def b_cd(self, args):
        if len(args) > 1:
            return self.err("cd: too many arguments")
        target = args[0] if args else os.path.expanduser("~")
        if target == "-":
            target = os.environ.get("OLDPWD")
            if not target:
                return self.err("cd: OLDPWD not set")
            print(target)
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
        if os.access(full, os.X_OK):
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
                print(f"{stat.filemode(st.st_mode)} {st.st_nlink:>2} "
                      f"{st.st_size:>8} {mtime} {self._styled_name(name, full)}")
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
            return self.err("cat: missing file operand")
        status = 0
        for name in args:
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
            try:
                if parents:
                    os.makedirs(name, exist_ok=True)
                else:
                    os.mkdir(name)
            except FileExistsError:
                status = self.err(f"mkdir: cannot create directory '{name}': File exists")
            except FileNotFoundError:
                status = self.err(f"mkdir: cannot create directory '{name}': No such file or directory")
            except PermissionError:
                status = self.err(f"mkdir: cannot create directory '{name}': Permission denied")
        return status

    def b_rmdir(self, args):
        if not args:
            return self.err("rmdir: missing operand")
        status = 0
        for name in args:
            try:
                os.rmdir(name)
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
            try:
                if os.path.isdir(name) and not os.path.islink(name):
                    if not recursive:
                        status = self.err(f"rm: cannot remove '{name}': Is a directory")
                        continue
                    shutil.rmtree(name)
                else:
                    os.remove(name)
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
            try:
                if os.path.exists(name):
                    os.utime(name, None)     # update the timestamps
                else:
                    open(name, "a").close()  # create an empty file
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
