#!/usr/bin/env python3
"""
Shell I/O boundary for the Omarchy Bar Editor plugin (davidjm.bar-editor).

Everything that reads or writes ~/.config/omarchy config goes through this one
script, invoked from QML via Process. It is the plugin's config-write boundary
and refuses anything outside a well-defined window (marketplace-review style):

  - Every path component under the config root is walked from / with
    O_NOFOLLOW|O_DIRECTORY: a symlinked component anywhere is a hard refusal.
  - Reads open the target with O_NOFOLLOW and fstat-check it: regular file,
    owned by the euid, st_nlink == 1, bounded size (1 MiB).
  - Writes create a randomized O_CREAT|O_EXCL|O_NOFOLLOW temp in the same
    directory, fsync the file and the directory, then os.replace() so a
    planted name can never redirect the write. Original modes are preserved.

Commands (argv subcommand, payload on stdin, JSON on stdout):

  read
      Print {"cfg": <shell.json>, "toml": {"values": {...}, "exists": bool},
             "profiles": [names]}.
      Missing files come back as empty defaults, never errors.

  write                                stdin: {"shell": <obj>, "toml": {"values":...}}
      atomically write shell.json and/or shell.toml (each with a `.bak-editor`
      backup). Keys not present in the payload are left untouched.

  profiles list
  profiles save <name>                 stdin: profile object
  profiles load <name>
  profiles delete <name>
"""

import errno
import json
import os
import secrets
import stat
import sys

CONFIG_ROOT = os.path.expanduser("~/.config/omarchy")
SHELL_JSON = os.path.join(CONFIG_ROOT, "shell.json")
SHELL_TOML = os.path.join(CONFIG_ROOT, "shell.toml")
PROFILES_DIR = os.path.join(CONFIG_ROOT, "bar-profiles")
BACKUP_SUFFIX = ".bak-editor"
MAX_CONFIG_BYTES = 1 << 20  # 1 MiB


# ---------------------------------------------------------------------------
# verified (no-follow) filesystem helpers
# ---------------------------------------------------------------------------

def verify_parent_dir(path):
    """Walk the parent directory of `path` component-by-component from /,
    refusing any symlink along the way."""
    parent = os.path.dirname(os.path.abspath(path))
    rel_parts = [p for p in parent.replace("//", "/").split("/") if p]
    cur = "/"
    for part in rel_parts:
        cur = os.path.join(cur, part)
        try:
            fd = os.open(cur, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            os.close(fd)
        except OSError as e:
            raise SystemExit(f"refusing configdir: {cur}: {e.strerror}")


def read_file_checked(path):
    """Read a regular, euid-owned, single-hardlink, bounded file. Returns
    (contents, exists). Raises SystemExit on anything suspicious."""
    verify_parent_dir(path)
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except OSError as e:
        if e.errno == errno.ENOENT:
            return "", False
        raise SystemExit(f"cannot open {path}: {e.strerror}")
    st = os.fstat(fd)
    if not stat.S_ISREG(st.st_mode):
        os.close(fd)
        raise SystemExit(f"refusing non-regular file: {path}")
    if st.st_uid != os.geteuid():
        os.close(fd)
        raise SystemExit(f"refusing file owned by uid {st.st_uid}: {path}")
    if st.st_nlink != 1:
        os.close(fd)
        raise SystemExit(f"refusing hard-linked file: {path}")
    if st.st_size > MAX_CONFIG_BYTES:
        os.close(fd)
        raise SystemExit(f"refusing oversized file: {path}")
    data = b""
    while len(data) <= MAX_CONFIG_BYTES:
        chunk = os.read(fd, 65536)
        if not chunk:
            break
        data += chunk
    os.close(fd)
    if len(data) > MAX_CONFIG_BYTES:
        raise SystemExit(f"refusing oversized file: {path}")
    try:
        return data.decode("utf-8"), True
    except UnicodeDecodeError:
        raise SystemExit(f"refusing non-UTF-8 file: {path}")


def write_bytes_atomic(path, data, mode=None):
    """Atomically write bytes to `path` via a randomized same-dir temp, fsync'd
    and os.replace()'d. Preserves `mode` when given, else keeps the original
    mode or defaults to 0600."""
    verify_parent_dir(path)
    parent = os.path.dirname(os.path.abspath(path))
    base = os.path.basename(path)
    tmp = os.path.join(parent, "." + base + "." + secrets.token_hex(8) + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        view = memoryview(data)
        while view:
            n = os.write(fd, view)
            view = view[n:]
        os.fsync(fd)
    finally:
        os.close(fd)

    if mode is None:
        try:
            existing = os.stat(path).st_mode & 0o7777
        except OSError:
            existing = 0o600
        mode = existing
    os.chmod(tmp, mode)
    os.replace(tmp, path)

    dfd = os.open(parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(dfd)
    finally:
        os.close(dfd)


def backup(path):
    """Copy an existing file to `<path>.bak-editor`, atomically."""
    content, exists = read_file_checked(path)
    if not exists:
        return
    try:
        mode = os.stat(path).st_mode & 0o7777
    except OSError:
        mode = 0o600
    write_bytes_atomic(path + BACKUP_SUFFIX, content.encode("utf-8"), mode)


# ---------------------------------------------------------------------------
# shell.json / shell.toml
# ---------------------------------------------------------------------------

def load_json(path):
    text, exists = read_file_checked(path)
    if not exists:
        return {}, False
    try:
        obj = json.loads(text)
    except json.JSONDecodeError as e:
        raise SystemExit(f"invalid JSON in {path}: {e}")
    return obj if isinstance(obj, dict) else {}, True


def load_toml_bar():
    """Parse the [bar] section values from shell.toml (mirrors the GTK app)."""
    values = {}
    text, exists = read_file_checked(SHELL_TOML)
    section = None
    for line in text.splitlines():
        line_str = line.strip()
        if line_str.startswith("[") and line_str.endswith("]"):
            section = line_str.strip("[]").strip()
            continue
        if section != "bar":
            continue
        if not line_str or line_str.startswith("#") or "=" not in line_str:
            continue
        key, _, val = line_str.partition("=")
        key = key.strip().replace("-", "_")
        values[key] = val.strip().strip("'\"")
    return values, exists


def format_toml(values, original_content=""):
    """Replace or append the [bar] block, preserving other sections."""
    bar_block = (
        "[bar]\n"
        f'background       = "{values.get("background", "#1a1b26")}"\n'
        f'background-alpha = {values.get("background_alpha", "1.0")}\n'
        f'text             = "{values.get("text", "#c0caf5")}"\n'
        f'active           = "{values.get("active", "#f7768e")}"\n'
        f'scale-with-font  = {str(values.get("scale_with_font", "true")).lower()}\n'
        f'size-horizontal  = {values.get("size_horizontal", "26")}\n'
        f'size-vertical    = {values.get("size_vertical", "28")}\n'
    )
    if not original_content.strip():
        return "# Omarchy Bar Editor managed [bar] overrides.\n" + bar_block
    lines = original_content.splitlines(keepends=True)
    in_bar = False
    start_idx = -1
    end_idx = -1
    for i, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            sec = stripped[1:-1].strip()
            if sec == "bar":
                in_bar = True
                start_idx = i
            elif in_bar:
                end_idx = i
                break
    if in_bar:
        if end_idx == -1:
            end_idx = len(lines)
        return "".join(lines[:start_idx] + [bar_block] + lines[end_idx:])
    return original_content.rstrip() + "\n\n" + bar_block


def cmd_read():
    cfg, _ = load_json(SHELL_JSON)
    toml_values, toml_exists = load_toml_bar()
    profiles = list_profiles()
    out = {
        "cfg": cfg,
        "toml": {"values": toml_values, "exists": toml_exists},
        "profiles": profiles,
    }
    print(json.dumps(out, ensure_ascii=False))


def cmd_write():
    payload = json.load(sys.stdin)
    if not isinstance(payload, dict):
        raise SystemExit("invalid write payload")

    cfg = payload.get("shell")
    if cfg is not None:
        if not isinstance(cfg, dict):
            raise SystemExit("shell must be an object")
        backup(SHELL_JSON)
        data = json.dumps(cfg, indent=2, ensure_ascii=False) + "\n"
        write_bytes_atomic(SHELL_JSON, data.encode("utf-8"))

    toml = payload.get("toml")
    if toml is not None:
        if not isinstance(toml, dict):
            raise SystemExit("toml must be an object")
        backup(SHELL_TOML)
        existing_content, _ = read_file_checked(SHELL_TOML)
        new_content = format_toml(toml.get("values", {}), existing_content)
        write_bytes_atomic(SHELL_TOML, new_content.encode("utf-8"))

    print(json.dumps({"ok": True}, ensure_ascii=False))


# ---------------------------------------------------------------------------
# profiles
# ---------------------------------------------------------------------------

def safe_profile_name(name):
    name = str(name).strip()
    if (not name or name.startswith(".") or "/" in name or "\\" in name
            or name in ("..",) or any(c in name for c in ":\x00")):
        raise SystemExit(f"bad profile name: {name!r}")
    return name


def list_profiles():
    verify_parent_dir(PROFILES_DIR)
    if not os.path.isdir(PROFILES_DIR):
        return []
    names = []
    try:
        for entry in os.listdir(PROFILES_DIR):
            if not entry.endswith(".json"):
                continue
            p = os.path.join(PROFILES_DIR, entry)
            try:
                st = os.lstat(p)
            except OSError:
                continue
            if stat.S_ISREG(st.st_mode):
                names.append(entry[:-5])
    except OSError:
        return []
    return sorted(names)


def cmd_profiles(args):
    if not args:
        raise SystemExit("profiles requires a subcommand")
    cmd = args[0]
    if cmd == "list":
        print(json.dumps(list_profiles(), ensure_ascii=False))
        return
    if len(args) < 2:
        raise SystemExit(f"profiles {cmd} requires a name")
    name = safe_profile_name(args[1])
    path = os.path.join(PROFILES_DIR, name + ".json")

    if cmd == "save":
        payload = json.load(sys.stdin)
        if not isinstance(payload, dict):
            raise SystemExit("profile must be an object")
        verify_parent_dir(path)
        os.makedirs(PROFILES_DIR, exist_ok=True)
        data = json.dumps(payload, indent=2, ensure_ascii=False) + "\n"
        write_bytes_atomic(path, data.encode("utf-8"))
        print(json.dumps({"ok": True}, ensure_ascii=False))
    elif cmd == "load":
        content, exists = read_file_checked(path)
        if not exists:
            raise SystemExit(f"no profile named {name}")
        print(content, end="")
    elif cmd == "delete":
        _, exists = read_file_checked(path)
        if not exists:
            raise SystemExit(f"no profile named {name}")
        os.remove(path)
        dfd = os.open(PROFILES_DIR, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            os.fsync(dfd)
        finally:
            os.close(dfd)
        print(json.dumps({"ok": True}, ensure_ascii=False))
    else:
        raise SystemExit(f"unknown profiles subcommand: {cmd}")


# ---------------------------------------------------------------------------

def main():
    if len(sys.argv) < 2:
        raise SystemExit("usage: shell_io.py <read|write|profiles ...>")
    cwd = os.getcwd()
    os.chdir("/")  # never run relative to a caller-controlled directory
    root = os.path.abspath(CONFIG_ROOT)
    verify_parent_dir(os.path.join(root, "x"))
    os.chdir(cwd)

    cmd = sys.argv[1]
    if cmd == "read":
        cmd_read()
    elif cmd == "write":
        cmd_write()
    elif cmd == "profiles":
        cmd_profiles(sys.argv[2:])
    else:
        raise SystemExit(f"unknown command: {cmd}")


if __name__ == "__main__":
    try:
        main()
    except SystemExit as e:
        if e.code:
            print(str(e.code), file=sys.stderr)
        sys.exit(1 if e.code else 0)
    except Exception as e:
        print(f"shell_io error: {e}", file=sys.stderr)
        sys.exit(1)