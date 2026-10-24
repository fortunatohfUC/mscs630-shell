# mysh: Shell Simulation (Deliverable 1)

MSCS-630 Advanced Operating Systems, University of the Cumberlands
Fortunato Hernandez

A Unix-like shell written in Python. Deliverable 1 covers the basic shell,
process management with `fork()`, `execvp()` and `waitpid()`, foreground and
background execution, and job control.

## Running

```
python3 mysh.py
```

Requires Python 3.8+ on Linux or macOS (`os.fork()` is not available on Windows).
No third-party packages are needed.

## Features

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
