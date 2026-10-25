"""
security.py - User authentication and file permissions for mysh
Deliverable 4: Security

MSCS-630 Advanced Operating Systems

Authentication: users log in with a username and password before they get a
prompt. Passwords are never stored in plain text. Each one is stored as a
salted PBKDF2-SHA256 hash, and checked with a constant-time comparison.

Roles: every user has a role (admin or standard) and a group (admin or users).

Authorization: files inside the shell's workspace (the folder mysh was
started in) have a simulated owner, group and Unix-style mode such as
rw-r--r--. Before every protected action the shell checks the current user
against the owner / group / others bits for read (r), write (w) or execute
(x). Admin can read and write anything, like root, but can only execute a
file that has at least one x bit. Files the shell did not create are
treated as system files owned by admin (rw-r--r--).

Denied actions and logins are recorded in an audit log that admin can view.
"""

import getpass
import hashlib
import hmac
import json
import os
import secrets
import time

USERS_FILE = ".mysh_users.json"
META_FILE = ".mysh_meta.json"

ROLE_GROUP = {"admin": "admin", "standard": "users"}

DEFAULT_USERS = [  # name, password, role  (demo accounts, listed in the README)
    ("admin", "admin123", "admin"),
    ("fortunato", "mysh2026", "standard"),
    ("maria", "maria2026", "standard"),
]

APP_LOG = """\
2026-10-24 09:14 INFO  scheduler started (round-robin, q=2)
2026-10-24 09:15 error page fault limit reached for process C
2026-10-24 09:15 INFO  user fortunato logged in
2026-10-24 09:16 error deadlock check: producer waiting on full buffer
2026-10-24 09:17 INFO  memory policy changed to lru
2026-10-24 09:18 error authentication failed for user guest
2026-10-24 09:19 INFO  backup finished
"""

SEED_FILES = {
    # path: (is_dir, owner, group, mode, content)
    USERS_FILE: (False, "admin", "admin", 0o600, None),
    META_FILE: (False, "admin", "admin", 0o600, None),
    "system": (True, "admin", "admin", 0o755, None),
    "system/config.txt": (False, "admin", "admin", 0o644,
                          "frames=4\npolicy=fifo\nquantum=2\nmax_users=3\n"),
    "system/app.log": (False, "admin", "admin", 0o644, APP_LOG),
    "system/secret.txt": (False, "admin", "admin", 0o600,
                          "backup key: only admin should be able to read this\n"),
    "scripts": (True, "admin", "users", 0o755, None),
    "scripts/report.sh": (False, "admin", "admin", 0o644,
                          '#!/bin/sh\necho "Daily report run by $MYSH_USER on $(date +%m/%d/%Y)"\n'
                          'echo "Uptime check: OK"\n'),
}


def hash_password(password, salt=None):
    salt = salt or secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), 100_000)
    return salt, digest.hex()


def mode_string(mode, is_dir=False):
    """0o644 -> '-rw-r--r--'"""
    out = "d" if is_dir else "-"
    for shift in (6, 3, 0):
        bits = (mode >> shift) & 7
        out += ("r" if bits & 4 else "-") + ("w" if bits & 2 else "-") + ("x" if bits & 1 else "-")
    return out


class User:
    def __init__(self, name, role, salt, pw_hash):
        self.name = name
        self.role = role
        self.group = ROLE_GROUP[role]
        self.salt = salt
        self.pw_hash = pw_hash

    @property
    def is_admin(self):
        return self.role == "admin"


class Security:
    def __init__(self, root, err):
        self.root = os.path.realpath(root)
        self.err = err
        self.user = None
        self.audit_log = []
        self.users = {}
        self.meta = {}
        self._load()

    # ------------------------------------------------------------------ storage
    def _path(self, name):
        return os.path.join(self.root, name)

    def _load(self):
        try:
            with open(self._path(USERS_FILE)) as f:
                data = json.load(f)
            self.users = {u["name"]: User(u["name"], u["role"], u["salt"], u["hash"]) for u in data}
        except (OSError, ValueError, KeyError):
            self.users = {}
            for name, pw, role in DEFAULT_USERS:
                salt, h = hash_password(pw)
                self.users[name] = User(name, role, salt, h)
            self._save_users()
        try:
            with open(self._path(META_FILE)) as f:
                raw = json.load(f)
            self.meta = {k: {"owner": v["owner"], "group": v["group"], "mode": int(v["mode"], 8)}
                         for k, v in raw.items()}
        except (OSError, ValueError, KeyError):
            self.meta = {}
        self._seed()

    def _save_users(self):
        data = [{"name": u.name, "role": u.role, "salt": u.salt, "hash": u.pw_hash}
                for u in self.users.values()]
        with open(self._path(USERS_FILE), "w") as f:
            json.dump(data, f, indent=2)

    def _save_meta(self):
        raw = {k: {"owner": v["owner"], "group": v["group"], "mode": f"{v['mode']:04o}"}
               for k, v in sorted(self.meta.items())}
        with open(self._path(META_FILE), "w") as f:
            json.dump(raw, f, indent=2)

    def _seed(self):
        """Create the demo system files the first time the shell runs."""
        changed = False
        for rel, (is_dir, owner, group, mode, content) in SEED_FILES.items():
            full = self._path(rel)
            if is_dir:
                os.makedirs(full, exist_ok=True)
            elif content is not None and not os.path.exists(full):
                with open(full, "w") as f:
                    f.write(content)
                if rel.endswith(".sh"):
                    os.chmod(full, 0o755)   # real OS bit; mysh checks its own bits
            if rel not in self.meta:
                self.meta[rel] = {"owner": owner, "group": group, "mode": mode}
                changed = True
        if changed:
            self._save_meta()

    # ---------------------------------------------------------- authentication
    def verify(self, name, password):
        user = self.users.get(name)
        if user is None:
            hash_password(password)             # same work either way
            return None
        _, h = hash_password(password, user.salt)
        return user if hmac.compare_digest(h, user.pw_hash) else None

    def login(self, banner=True):
        """Ask for a username and password. Three tries, then give up."""
        if banner:
            print("mysh login (demo accounts are listed in the README)")
        for attempt in range(1, 4):
            try:
                name = input("username: ").strip()
                password = getpass.getpass("password: ")
            except (EOFError, KeyboardInterrupt):
                print()
                return False
            user = self.verify(name, password)
            if user:
                self.user = user
                os.environ["MYSH_USER"] = user.name
                self.audit("login", "-", "ok")
                print(f"Welcome, {user.name}. Role: {user.role}, group: {user.group}.")
                return True
            self.audit("login", name or "-", "FAILED", who=name or "?")
            left = 3 - attempt
            print(f"Login incorrect. {left} attempt{'s' if left != 1 else ''} left." if left
                  else "Login incorrect. Too many failed attempts.")
            time.sleep(1)                        # slow down guessing
        return False

    # ----------------------------------------------------------- authorization
    def managed(self, path):
        full = os.path.realpath(path)
        return full == self.root or full.startswith(self.root + os.sep)

    def rel(self, path):
        return os.path.relpath(os.path.realpath(path), self.root)

    def info(self, path):
        """Owner, group and mode for a path in the workspace (or None)."""
        if not self.managed(path):
            return None
        rel = self.rel(path)
        if rel in self.meta:
            return self.meta[rel]
        if os.path.isdir(path):                  # shared workspace folders
            return {"owner": "admin", "group": "users", "mode": 0o775}
        return {"owner": "admin", "group": "admin", "mode": 0o644}   # system file

    def can(self, path, op):
        """op is 'r', 'w' or 'x'."""
        m = self.info(path)
        if m is None:
            return True                          # outside the workspace
        u = self.user
        if u.is_admin:
            return op != "x" or bool(m["mode"] & 0o111) or os.path.isdir(path)
        if u.name == m["owner"]:
            bits = (m["mode"] >> 6) & 7
        elif u.group == m["group"]:
            bits = (m["mode"] >> 3) & 7
        else:
            bits = m["mode"] & 7
        return bool(bits & {"r": 4, "w": 2, "x": 1}[op])

    def require(self, cmd, path, op, target=None, action=None):
        """Check one permission; print a clear error and return False if denied."""
        if self.can(path, op):
            return True
        m = self.info(path)
        need = {"r": "read", "w": "write", "x": "execute"}[op]
        shown = target or path
        why = (f"{mode_string(m['mode'], os.path.isdir(path))[1:]} {m['owner']}:{m['group']}, "
               f"you are {self.user.name}:{self.user.group}")
        self.err(f"{cmd}: permission denied: '{shown}' needs {need} access ({why})")
        self.audit(action or cmd, shown, f"DENIED ({need})")
        return False

    def parent(self, path):
        return os.path.dirname(os.path.abspath(path)) or "/"

    # ------------------------------------------------- bookkeeping for changes
    def created(self, path, is_dir=False):
        if self.managed(path):
            self.meta[self.rel(path)] = {"owner": self.user.name, "group": self.user.group,
                                         "mode": 0o755 if is_dir else 0o644}
            self._save_meta()

    def removed(self, path):
        if self.managed(path):
            rel = self.rel(path)
            for key in [k for k in self.meta if k == rel or k.startswith(rel + os.sep)]:
                del self.meta[key]
            self._save_meta()

    def ls_columns(self, path):
        """mode, owner, group for ls -l, or None outside the workspace."""
        m = self.info(path)
        if m is None:
            return None
        return mode_string(m["mode"], os.path.isdir(path)), m["owner"], m["group"]

    # ------------------------------------------------------------------ audit
    def audit(self, action, target, result, who=None):
        stamp = time.strftime("%H:%M:%S")
        self.audit_log.append((stamp, who or (self.user.name if self.user else "?"),
                               action, target, result))

    # ---------------------------------------------------------- built-ins
    def b_whoami(self, args):
        u = self.user
        print(f"{u.name} (role: {u.role}, group: {u.group})")
        return 0

    def b_users(self, args):
        if not self.user.is_admin:
            return self._admin_only("users")
        print(f"  {'User':<12}{'Role':<10}{'Group':<8}")
        for u in self.users.values():
            print(f"  {u.name:<12}{u.role:<10}{u.group:<8}")
        return 0

    def b_useradd(self, args):
        if not self.user.is_admin:
            return self._admin_only("useradd")
        if len(args) != 2 or args[1] not in ROLE_GROUP:
            return self.err("useradd: usage: useradd NAME admin|standard", 2)
        name = args[0]
        if name in self.users:
            return self.err(f"useradd: user '{name}' already exists")
        pw = getpass.getpass(f"password for {name}: ")
        if len(pw) < 6:
            return self.err("useradd: password must be at least 6 characters")
        if getpass.getpass("confirm password: ") != pw:
            return self.err("useradd: passwords do not match")
        salt, h = hash_password(pw)
        self.users[name] = User(name, args[1], salt, h)
        self._save_users()
        print(f"user {name} added (role {args[1]}, group {ROLE_GROUP[args[1]]})")
        return 0

    def b_chmod(self, args):
        if len(args) != 2:
            return self.err("chmod: usage: chmod MODE FILE  (e.g. 600, 755, +x, g-r, o+w)", 2)
        spec, path = args
        if not os.path.exists(path):
            return self.err(f"chmod: cannot access '{path}': No such file or directory")
        m = self.info(path)
        if m is None:
            return self.err(f"chmod: '{path}' is outside the mysh workspace")
        if not (self.user.is_admin or self.user.name == m["owner"]):
            self.audit("chmod", path, "DENIED (not owner)")
            return self.err(f"chmod: permission denied: only the owner ({m['owner']}) "
                            f"or admin can change '{path}'")
        try:
            new = self._apply_mode(m["mode"], spec)
        except ValueError:
            return self.err(f"chmod: invalid mode '{spec}'", 2)
        old = m["mode"]
        self.meta[self.rel(path)] = {"owner": m["owner"], "group": m["group"], "mode": new}
        self._save_meta()
        isdir = os.path.isdir(path)
        print(f"mode of '{path}' changed from {mode_string(old, isdir)[1:]} to "
              f"{mode_string(new, isdir)[1:]}")
        return 0

    @staticmethod
    def _apply_mode(mode, spec):
        if spec.isdigit() and len(spec) == 3 and all(c in "01234567" for c in spec):
            return int(spec, 8)
        for part in spec.split(","):
            who = ""
            i = 0
            while i < len(part) and part[i] in "ugoa":
                who += part[i]
                i += 1
            who = who or "a"
            if i >= len(part) or part[i] not in "+-=" or len(part) == i + 1:
                raise ValueError
            op, perms = part[i], part[i + 1:]
            if any(c not in "rwx" for c in perms):
                raise ValueError
            bits = sum({"r": 4, "w": 2, "x": 1}[c] for c in perms)
            for w in ("ugo" if "a" in who else who):
                shift = {"u": 6, "g": 3, "o": 0}[w]
                if op == "+":
                    mode |= bits << shift
                elif op == "-":
                    mode &= ~(bits << shift)
                else:
                    mode = (mode & ~(7 << shift)) | (bits << shift)
        return mode & 0o777

    def b_chown(self, args):
        if not self.user.is_admin:
            return self._admin_only("chown")
        if len(args) != 2:
            return self.err("chown: usage: chown USER FILE", 2)
        name, path = args
        if name not in self.users:
            return self.err(f"chown: no user named '{name}'")
        if not os.path.exists(path):
            return self.err(f"chown: cannot access '{path}': No such file or directory")
        m = self.info(path)
        if m is None:
            return self.err(f"chown: '{path}' is outside the mysh workspace")
        self.meta[self.rel(path)] = {"owner": name, "group": self.users[name].group,
                                     "mode": m["mode"]}
        self._save_meta()
        print(f"owner of '{path}' changed to {name}:{self.users[name].group}")
        return 0

    def b_audit(self, args):
        if not self.user.is_admin:
            return self._admin_only("audit")
        if not self.audit_log:
            print("(audit log is empty)")
        for stamp, who, action, target, result in self.audit_log[-20:]:
            print(f"  {stamp}  {who:<10}{action:<8} {target:<22} {result}")
        return 0

    def _admin_only(self, cmd):
        self.audit(cmd, "-", "DENIED (admin only)")
        return self.err(f"{cmd}: permission denied: only admin can run {cmd} "
                        f"(you are {self.user.name}, role {self.user.role})")
