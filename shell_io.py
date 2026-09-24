#!/usr/bin/python3
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

  Note: stdin is parsed incrementally with read_stdin_json() — the QML
  Process keeps the pipe open for the process lifetime, so payloads must
  never rely on EOF (json.load(sys.stdin) would block forever).

  write                                stdin: {"shell": <obj>, "toml": {"values":...}}
      atomically write shell.json and/or shell.toml (each with a `.bak-editor`
      backup). Keys not present in the payload are left untouched.

  profiles list
  profiles save <name>                 stdin: profile object
  profiles load <name>
  profiles delete <name>
"""

import base64
import copy
import errno
import fcntl
import json
import math
import os
import re
import secrets
import stat
import sys
from contextlib import contextmanager

CONFIG_ROOT = os.path.expanduser("~/.config/omarchy")
SHELL_JSON = os.path.join(CONFIG_ROOT, "shell.json")
SHELL_TOML = os.path.join(CONFIG_ROOT, "shell.toml")
PROFILES_DIR = os.path.join(CONFIG_ROOT, "bar-profiles")
BACKUP_SUFFIX = ".bak-editor"
LOCK_NAME = ".shell.json.lock"
JOURNAL_NAME = ".shell.json.transaction.json"
LEGACY_JOURNAL_NAME = ".bar-editor.transaction.json"
MAX_CONFIG_BYTES = 1 << 20
MAX_INPUT_BYTES = 2 << 20
MAX_JOURNAL_BYTES = 8 << 20
MAX_INPUT_DEPTH = 64
MAX_INPUT_NODES = 100000
MAX_INPUT_KEYS = 256
MAX_INPUT_STRING_BYTES = 1 << 20
MAX_TOML_LINES = 10000
MAX_TOML_LINE_CHARS = 65536
MAX_PROFILE_NAME = 64
PROFILE_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._ -]{0,63}$")
_MISSING = object()


def _reject_json_constant(value):
    raise ValueError(f"invalid JSON constant: {value}")


# ---------------------------------------------------------------------------
# verified (no-follow) filesystem helpers
# ---------------------------------------------------------------------------

def _absolute(path):
    value = os.path.abspath(os.fspath(path))
    if "\x00" in value:
        raise SystemExit("invalid config path")
    return value


def _inside_config(path):
    root = _absolute(CONFIG_ROOT)
    candidate = _absolute(path)
    return candidate == root or candidate.startswith(root + os.sep)


def _open_parent_dir(path, create=False):
    target = _absolute(path)
    if not _inside_config(target):
        raise SystemExit("refusing path outside config root")
    parent = os.path.dirname(target)
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
    current = os.open("/", flags)
    try:
        for component in parent.split(os.sep)[1:]:
            if not component or component in (".", ".."):
                raise SystemExit("refusing configdir")
            try:
                next_fd = os.open(component, flags, dir_fd=current)
            except OSError as error:
                if not create or error.errno != errno.ENOENT:
                    raise
                try:
                    os.mkdir(component, 0o700, dir_fd=current)
                except FileExistsError:
                    pass
                next_fd = os.open(component, flags, dir_fd=current)
            info = os.fstat(next_fd)
            mode = stat.S_IMODE(info.st_mode)
            if (not stat.S_ISDIR(info.st_mode) or info.st_uid not in (0, os.geteuid())
                    or (mode & 0o022 and not (info.st_uid == 0 and mode & 0o1000))):
                os.close(next_fd)
                raise SystemExit("refusing configdir")
            os.close(current)
            current = next_fd
        return current
    except SystemExit:
        os.close(current)
        raise
    except OSError as error:
        os.close(current)
        raise SystemExit(f"refusing configdir: {error.strerror}")


def verify_parent_dir(path, create=False):
    parent = _open_parent_dir(path, create)
    os.close(parent)


def _read_fd_checked(fd, path, limit=MAX_CONFIG_BYTES):
    before = os.fstat(fd)
    if not stat.S_ISREG(before.st_mode):
        raise SystemExit(f"refusing non-regular file: {path}")
    if before.st_uid != os.geteuid():
        raise SystemExit(f"refusing file owned by uid {before.st_uid}: {path}")
    if before.st_nlink != 1 or stat.S_IMODE(before.st_mode) & 0o022:
        raise SystemExit(f"refusing unsafe file: {path}")
    if before.st_size > limit:
        raise SystemExit(f"refusing oversized file: {path}")
    data = bytearray()
    while len(data) <= limit:
        chunk = os.read(fd, min(65536, limit + 1 - len(data)))
        if not chunk:
            break
        data.extend(chunk)
    after = os.fstat(fd)
    if (len(data) > limit or before.st_size != after.st_size
            or before.st_dev != after.st_dev or before.st_ino != after.st_ino
            or getattr(before, "st_mtime_ns", int(before.st_mtime * 1000000000))
            != getattr(after, "st_mtime_ns", int(after.st_mtime * 1000000000))):
        raise SystemExit(f"file changed while reading: {path}")
    try:
        return data.decode("utf-8"), True
    except UnicodeDecodeError:
        raise SystemExit(f"refusing non-UTF-8 file: {path}")


def _read_file_checked(path, limit=MAX_CONFIG_BYTES):
    target = _absolute(path)
    parent = _open_parent_dir(target)
    fd = None
    try:
        flags = os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_CLOEXEC", 0)
        try:
            fd = os.open(os.path.basename(target), flags, dir_fd=parent)
        except OSError as error:
            if error.errno == errno.ENOENT:
                return "", False
            raise SystemExit(f"cannot open {target}: {error.strerror}")
        return _read_fd_checked(fd, target, limit)
    finally:
        if fd is not None:
            os.close(fd)
        os.close(parent)


def read_file_checked(path):
    return _read_file_checked(path, MAX_CONFIG_BYTES)


def _existing_mode(path):
    target = _absolute(path)
    parent = _open_parent_dir(target)
    try:
        try:
            st = os.stat(os.path.basename(target), dir_fd=parent, follow_symlinks=False)
        except OSError as error:
            if error.errno == errno.ENOENT:
                return 0o600
            raise SystemExit(f"cannot stat {target}: {error.strerror}")
        if (not stat.S_ISREG(st.st_mode) or st.st_uid != os.geteuid()
                or st.st_nlink != 1 or stat.S_IMODE(st.st_mode) & 0o022):
            raise SystemExit(f"refusing unsafe existing file: {target}")
        return st.st_mode & 0o7777
    finally:
        os.close(parent)


def _write_bytes_atomic(path, data, mode=None, max_bytes=MAX_CONFIG_BYTES):
    target = _absolute(path)
    if not isinstance(data, bytes):
        data = bytes(data)
    if len(data) > max_bytes:
        raise SystemExit("refusing oversized write")
    parent = _open_parent_dir(target)
    base = os.path.basename(target)
    if not base or base in (".", ".."):
        os.close(parent)
        raise SystemExit("invalid config path")
    if mode is None:
        mode = _existing_mode(target)
    temporary = "." + base + "." + secrets.token_hex(8) + ".tmp"
    fd = None
    try:
        fd = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0),
            0o600,
            dir_fd=parent,
        )
        view = memoryview(data)
        while view:
            count = os.write(fd, view)
            if count <= 0:
                raise OSError("short write")
            view = view[count:]
        os.fchmod(fd, mode & 0o7777)
        os.fsync(fd)
        os.close(fd)
        fd = None
        os.replace(temporary, base, src_dir_fd=parent, dst_dir_fd=parent)
        os.fsync(parent)
    except Exception:
        if fd is not None:
            os.close(fd)
        try:
            os.unlink(temporary, dir_fd=parent)
        except OSError:
            pass
        raise
    finally:
        os.close(parent)


def write_bytes_atomic(path, data, mode=None):
    return _write_bytes_atomic(path, data, mode, MAX_CONFIG_BYTES)


def backup(path):
    content, exists = read_file_checked(path)
    if not exists:
        return
    write_bytes_atomic(path + BACKUP_SUFFIX, content.encode("utf-8"), _existing_mode(path))


@contextmanager
def config_lock():
    path = os.path.join(_absolute(CONFIG_ROOT), LOCK_NAME)
    parent = _open_parent_dir(path)
    fd = None
    try:
        fd = os.open(
            os.path.basename(path),
            os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0),
            0o600,
            dir_fd=parent,
        )
        st = os.fstat(fd)
        if (not stat.S_ISREG(st.st_mode) or st.st_uid != os.geteuid()
                or st.st_nlink != 1 or stat.S_IMODE(st.st_mode) & 0o022):
            raise SystemExit("refusing unsafe lock file")
        os.fchmod(fd, 0o600)
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        if fd is not None:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            except OSError:
                pass
            os.close(fd)
        os.close(parent)


@contextmanager
def instance_lock():
    path = os.path.join(_absolute(CONFIG_ROOT), ".bar-editor.instance.lock")
    parent = _open_parent_dir(path)
    fd = None
    try:
        fd = os.open(
            os.path.basename(path),
            os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0),
            0o600,
            dir_fd=parent,
        )
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid()
                or info.st_nlink != 1 or stat.S_IMODE(info.st_mode) & 0o022):
            raise SystemExit("refusing unsafe instance lock")
        os.fchmod(fd, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise SystemExit("Bar Editor is already running") from error
        yield
    finally:
        if fd is not None:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            except OSError:
                pass
            os.close(fd)
        os.close(parent)


def _secure_unlink(path):
    target = _absolute(path)
    parent = _open_parent_dir(target)
    try:
        try:
            os.unlink(os.path.basename(target), dir_fd=parent)
        except OSError as error:
            if error.errno != errno.ENOENT:
                raise
        os.fsync(parent)
    finally:
        os.close(parent)


def _journal_path(name=JOURNAL_NAME):
    return os.path.join(_absolute(CONFIG_ROOT), name)


def _transaction_journal_paths():
    return [_journal_path(JOURNAL_NAME), _journal_path(LEGACY_JOURNAL_NAME)]


def _allowed_transaction_target(target):
    target = _absolute(target)
    if target in (_absolute(SHELL_JSON), _absolute(SHELL_TOML)):
        return True
    root = _absolute(PROFILES_DIR)
    return target.startswith(root + os.sep) and target.endswith(".json") and len(target) <= 4096


def _encode_bytes(value):
    return base64.b64encode(value).decode("ascii")


def _decode_bytes(value, exists=True):
    if not exists:
        return b""
    if not isinstance(value, str) or len(value) > ((MAX_CONFIG_BYTES * 4) // 3 + 8):
        raise SystemExit("invalid transaction journal")
    try:
        raw = base64.b64decode(value.encode("ascii"), validate=True)
    except (ValueError, UnicodeError) as error:
        raise SystemExit("invalid transaction journal") from error
    if len(raw) > MAX_CONFIG_BYTES:
        raise SystemExit("invalid transaction journal")
    return raw


def _journal_operations(path=None):
    path = _journal_path() if path is None else _absolute(path)
    try:
        journal_info = os.lstat(path)
    except FileNotFoundError:
        return []
    except OSError as error:
        raise SystemExit("invalid transaction journal") from error
    if stat.S_IMODE(journal_info.st_mode) & 0o077:
        raise SystemExit("invalid transaction journal")
    content, exists = _read_file_checked(path, MAX_JOURNAL_BYTES)
    if not exists:
        return []
    try:
        data = json.loads(content, parse_constant=_reject_json_constant)
    except (json.JSONDecodeError, ValueError, RecursionError) as error:
        raise SystemExit("invalid transaction journal") from error
    if not isinstance(data, dict) or data.get("version") != 1 or not isinstance(data.get("operations"), list):
        raise SystemExit("invalid transaction journal")
    operations = []
    seen_targets = set()
    if len(data["operations"]) > 16:
        raise SystemExit("invalid transaction journal")
    for item in data["operations"]:
        if not isinstance(item, dict):
            raise SystemExit("invalid transaction journal")
        target = item.get("path")
        if not isinstance(target, str) or not _inside_config(target):
            raise SystemExit("invalid transaction journal")
        target = _absolute(target)
        if not _allowed_transaction_target(target):
            raise SystemExit("invalid transaction journal")
        if target in seen_targets:
            raise SystemExit("invalid transaction journal")
        seen_targets.add(target)
        if target in _transaction_journal_paths() or target == os.path.join(_absolute(CONFIG_ROOT), LOCK_NAME):
            raise SystemExit("invalid transaction journal")
        mode = item.get("mode")
        if isinstance(mode, bool) or not isinstance(mode, int) or mode < 0 or mode > 0o7777:
            raise SystemExit("invalid transaction journal")
        exists = item.get("exists")
        backup_exists = item.get("backup_exists")
        if not isinstance(exists, bool) or not isinstance(backup_exists, bool):
            raise SystemExit("invalid transaction journal")
        old = _decode_bytes(item.get("old"), exists)
        backup = _decode_bytes(item.get("backup"), backup_exists)
        operations.append({
            "path": target,
            "mode": mode,
            "exists": exists,
            "old": old,
            "backup_exists": backup_exists,
            "backup": backup,
        })
    return operations


def _restore_operations(operations):
    profile_root = _absolute(PROFILES_DIR) + os.sep
    for item in reversed(operations):
        target = item["path"]
        if target.startswith(profile_root):
            dirfd = _open_profiles_dir(True)
            os.close(dirfd)
        if item["exists"]:
            _write_bytes_atomic(target, item["old"], item["mode"], MAX_CONFIG_BYTES)
        else:
            _secure_unlink(target)
        backup = target + BACKUP_SUFFIX
        if item["backup_exists"]:
            _write_bytes_atomic(backup, item["backup"], item["mode"], MAX_CONFIG_BYTES)
        else:
            _secure_unlink(backup)


def recover_pending_transaction():
    recovered = False
    for journal in _transaction_journal_paths():
        operations = _journal_operations(journal)
        if operations:
            _restore_operations(operations)
            _secure_unlink(journal)
            recovered = True
        elif os.path.lexists(journal):
            _secure_unlink(journal)
    return recovered


def _make_journal(entries, journal=None):
    journal = _journal_path() if journal is None else _absolute(journal)
    try:
        os.lstat(journal)
    except FileNotFoundError:
        pass
    except OSError as error:
        raise SystemExit("transaction journal is unavailable") from error
    else:
        raise SystemExit("transaction journal already exists")
    operations = []
    for path, content in entries:
        target = _absolute(path)
        if not _inside_config(target) or not _allowed_transaction_target(target) or target == journal:
            raise SystemExit("invalid transaction target")
        if content is not None and (not isinstance(content, bytes) or len(content) > MAX_CONFIG_BYTES):
            raise SystemExit("invalid transaction content")
        old, exists = read_file_checked(target)
        mode = _existing_mode(target)
        backup, backup_exists = read_file_checked(target + BACKUP_SUFFIX)
        operations.append({
            "path": target,
            "mode": mode,
            "exists": exists,
            "old": old.encode("utf-8") if exists else None,
            "backup_exists": backup_exists,
            "backup": backup.encode("utf-8") if backup_exists else None,
        })
    payload_operations = []
    for item in operations:
        payload_item = dict(item)
        if item["exists"]:
            payload_item["old"] = _encode_bytes(item["old"])
        else:
            payload_item["old"] = None
        if item["backup_exists"]:
            payload_item["backup"] = _encode_bytes(item["backup"])
        else:
            payload_item["backup"] = None
        payload_operations.append(payload_item)
    payload = json.dumps({"version": 1, "operations": payload_operations},
                         ensure_ascii=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
    if len(payload) > MAX_JOURNAL_BYTES:
        raise SystemExit("transaction journal is too large")
    _write_bytes_atomic(journal, payload, 0o600, MAX_JOURNAL_BYTES)
    return operations


def _write_transaction(entries, journal=None):
    if not isinstance(entries, list) or not entries or len(entries) > 16:
        raise SystemExit("invalid transaction")
    normalized = []
    seen = set()
    for path, content in entries:
        target = _absolute(path)
        if target in seen or not _inside_config(target) or not _allowed_transaction_target(target):
            raise SystemExit("invalid transaction target")
        if content is not None and (not isinstance(content, bytes) or len(content) > MAX_CONFIG_BYTES):
            raise SystemExit("invalid transaction content")
        seen.add(target)
        normalized.append((target, content))
    journal = _journal_path() if journal is None else _absolute(journal)
    operations = _make_journal(normalized, journal)
    expected = {item["path"]: item for item in operations}
    try:
        for target, content in normalized:
            old, exists = read_file_checked(target)
            prior = expected[target]
            if exists != prior["exists"] or (exists and old.encode("utf-8") != prior["old"]):
                raise RuntimeError(f"transaction source changed: {target}")
            mode = _existing_mode(target)
            if mode != prior["mode"]:
                raise RuntimeError(f"transaction mode changed: {target}")
            if exists:
                write_bytes_atomic(target + BACKUP_SUFFIX, old.encode("utf-8"), mode)
            if content is None:
                _secure_unlink(target)
            else:
                write_bytes_atomic(target, content, mode)
        for target, content in normalized:
            actual, exists = read_file_checked(target)
            if content is None:
                if exists:
                    raise RuntimeError(f"delete verification failed: {target}")
            elif not exists or actual != content.decode("utf-8"):
                raise RuntimeError(f"write verification failed: {target}")
            elif _existing_mode(target) != expected[target]["mode"]:
                raise RuntimeError(f"write mode verification failed: {target}")
    except BaseException:
        try:
            _restore_operations(operations)
            _secure_unlink(journal)
        except BaseException:
            pass
        raise
    _secure_unlink(journal)


# ---------------------------------------------------------------------------
# shell.json / shell.toml
# ---------------------------------------------------------------------------

def load_json(path):
    text, exists = read_file_checked(path)
    if not exists:
        return {}, False
    try:
        obj = json.loads(text, parse_constant=_reject_json_constant)
        _validate_input_value(obj)
    except (json.JSONDecodeError, RecursionError, ValueError) as e:
        raise SystemExit(f"invalid JSON in {path}: {e}")
    if not isinstance(obj, dict):
        raise SystemExit(f"invalid JSON object in {path}")
    return obj, True


def _merge_shell_value(base, requested, current, path="shell"):
    if requested is _MISSING and current is _MISSING:
        return _MISSING
    if requested is _MISSING and base is _MISSING:
        return copy.deepcopy(current)
    if current is _MISSING and base is _MISSING:
        return copy.deepcopy(requested)
    if requested is _MISSING:
        if current == base:
            return _MISSING
        raise SystemExit(f"shell configuration changed while saving: {path}")
    if current is _MISSING:
        if requested == base:
            return _MISSING
        raise SystemExit(f"shell configuration changed while saving: {path}")
    if requested == base:
        return copy.deepcopy(current)
    if current == base:
        return copy.deepcopy(requested)
    if requested == current:
        return copy.deepcopy(requested)
    if isinstance(base, dict) and isinstance(requested, dict) and isinstance(current, dict):
        result = {}
        keys = list(base)
        keys.extend(key for key in requested if key not in base)
        keys.extend(key for key in current if key not in base and key not in requested)
        for key in keys:
            merged = _merge_shell_value(
                base.get(key, _MISSING),
                requested.get(key, _MISSING),
                current.get(key, _MISSING),
                f"{path}.{key}",
            )
            if merged is not _MISSING:
                result[key] = merged
        return result
    raise SystemExit(f"shell configuration changed while saving: {path}")


def merge_shell_config(base, requested, current):
    validate_shell_config(base)
    validate_shell_config(requested)
    validate_shell_config(current)
    return _merge_shell_value(base, requested, current)


def _strip_toml_comment(value):
    quote = None
    escaped = False
    for index, char in enumerate(value):
        if quote == '"':
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == quote:
                quote = None
        elif quote == "'":
            if char == quote:
                quote = None
        elif char in ("'", '"'):
            quote = char
        elif char == "#":
            return value[:index].rstrip()
    return value.rstrip()


def _toml_scalar(value):
    text = _strip_toml_comment(value.strip())
    if not text or len(text) > MAX_TOML_LINE_CHARS:
        raise ValueError("invalid TOML value")
    if any(ord(char) < 32 and char not in "\t" for char in text):
        raise ValueError("invalid TOML value")
    if text in ("nan", "+nan", "-nan", "inf", "+inf", "-inf"):
        raise ValueError("invalid TOML number")
    if text[0] == '"':
        try:
            result = json.loads(text)
        except json.JSONDecodeError as exc:
            raise ValueError("invalid TOML string") from exc
        if not isinstance(result, str):
            raise ValueError("invalid TOML string")
        return result
    if text[0] == "'" and text[-1] == "'":
        return text[1:-1]
    if text == "true":
        return True
    if text == "false":
        return False
    if re.fullmatch(r"[-+]?\d+", text):
        return int(text)
    if not re.fullmatch(r"[-+]?(?:\d+\.\d*|\.\d+)(?:[eE][-+]?\d+)?", text):
        raise ValueError("invalid TOML scalar")
    number = float(text)
    if not math.isfinite(number):
        raise ValueError("invalid TOML number")
    return number


def _toml_header(line):
    text = _strip_toml_comment(line.strip())
    array = text.startswith("[[") and text.endswith("]]" )
    if array:
        name = text[2:-2].strip()
    elif text.startswith("[") and text.endswith("]"):
        name = text[1:-1].strip()
    else:
        return False, None
    if not name or any(char in name for char in "[]"):
        return False, None
    if len(name) > 256:
        return False, None
    if name[0] in ("'", '"'):
        try:
            name = _toml_scalar(name)
        except ValueError:
            return False, None
    return True, name


def split_inline_comment(value):
    quote = None
    escaped = False
    for index, char in enumerate(value):
        if quote == '"':
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == quote:
                quote = None
        elif quote == "'":
            if char == quote:
                quote = None
        elif char in ("'", '"'):
            quote = char
        elif char == "#":
            return value[:index], value[index:]
    return value, ""


def load_toml_bar():
    values = {}
    text, exists = read_file_checked(SHELL_TOML)
    lines = text.splitlines()
    if len(lines) > MAX_TOML_LINES or any(len(line) > MAX_TOML_LINE_CHARS for line in lines):
        raise SystemExit("TOML configuration is too large")
    section = None
    bar_sections = 0
    for line in lines:
        if line.startswith("\ufeff"):
            line = line[1:]
        stripped_header = _strip_toml_comment(line.strip())
        if stripped_header.startswith("[["):
            _, array_name = _toml_header(line)
            if array_name == "bar":
                raise SystemExit("invalid TOML bar table")
        is_header, name = _toml_header(line)
        if is_header:
            if name == "bar":
                bar_sections += 1
                if bar_sections > 1:
                    raise SystemExit("duplicate TOML bar table")
            section = None if name is None else name.strip()
            continue
        if section != "bar":
            continue
        stripped = _strip_toml_comment(line.strip())
        if not stripped or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        key = key.strip().replace("-", "_")
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
            continue
        try:
            parsed = _toml_scalar(value)
            if key not in values and len(values) >= 256:
                continue
            values[key] = parsed
        except ValueError:
            continue
    try:
        validate_toml_values(values)
    except (ValueError, SystemExit) as error:
        raise SystemExit(f"invalid TOML configuration: {error}")
    return values, exists


def _toml_quote(value):
    return json.dumps(str(value), ensure_ascii=False, allow_nan=False)


def _toml_bool(value, default):
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        text = value.strip().lower()
        if text in ("true", "1", "yes", "on"):
            return True
        if text in ("false", "0", "no", "off"):
            return False
    raise ValueError("invalid TOML boolean")


def _toml_number(value, default, minimum, maximum, integer=False):
    if value is None:
        value = default
    if isinstance(value, bool):
        raise ValueError("invalid TOML number")
    try:
        number = float(value)
    except (TypeError, ValueError) as error:
        raise ValueError("invalid TOML number") from error
    if not math.isfinite(number):
        raise ValueError("non-finite TOML number")
    number = max(minimum, min(maximum, number))
    if integer:
        return int(round(number))
    return number


def _toml_color(value, default):
    text = default if value is None else value
    if not isinstance(text, str) or not re.fullmatch(r"#[0-9a-fA-F]{6}", text):
        raise ValueError("invalid color")
    return text


def _toml_value(values, key, default=None):
    if key in values:
        return values[key]
    dashed = key.replace("_", "-")
    return values.get(dashed, default)


def _managed_toml_values(values):
    background = _toml_color(_toml_value(values, "background", "#1a1b26"), "#1a1b26")
    text_color = _toml_color(_toml_value(values, "text", "#c0caf5"), "#c0caf5")
    active = _toml_color(_toml_value(values, "active", "#f7768e"), "#f7768e")
    alpha = _toml_number(_toml_value(values, "background_alpha", 1.0), 1.0, 0.0, 1.0)
    scale = "true" if _toml_bool(_toml_value(values, "scale_with_font", True), True) else "false"
    horizontal = _toml_number(_toml_value(values, "size_horizontal", 26), 26, 1, 1000, True)
    vertical = _toml_number(_toml_value(values, "size_vertical", 28), 28, 1, 1000, True)
    return {
        "background": _toml_quote(background),
        "background-alpha": repr(alpha),
        "text": _toml_quote(text_color),
        "active": _toml_quote(active),
        "scale-with-font": scale,
        "size-horizontal": str(horizontal),
        "size-vertical": str(vertical),
    }


def format_toml(values, original_content=""):
    validate_toml_values(values)
    if (not isinstance(values, dict) or len(values) > 64
            or not isinstance(original_content, str)
            or len(original_content.encode("utf-8")) > MAX_CONFIG_BYTES
            or "\x00" in original_content
            or len(original_content.splitlines()) > MAX_TOML_LINES
            or any(len(line) > MAX_TOML_LINE_CHARS for line in original_content.splitlines())):
        raise ValueError("invalid TOML input")
    managed = _managed_toml_values(values)
    key_order = (
        "background", "background-alpha", "text", "active",
        "scale-with-font", "size-horizontal", "size-vertical",
    )
    bar_lines = [f"[bar]\n"] + [f"{key} = {managed[key]}\n" for key in key_order]
    bar_block = "".join(bar_lines)
    if not original_content.strip():
        return "# Omarchy Bar Editor managed [bar] overrides.\n" + bar_block

    lines = original_content.splitlines(keepends=True)
    start_idx = -1
    end_idx = len(lines)
    bar_sections = 0
    for index, line in enumerate(lines):
        header_line = line[1:] if index == 0 and line.startswith("\ufeff") else line
        stripped_header = _strip_toml_comment(header_line.strip())
        if stripped_header.startswith("[["):
            _, array_name = _toml_header(header_line)
            if array_name == "bar":
                raise ValueError("invalid TOML bar table")
        is_header, name = _toml_header(header_line)
        if not is_header:
            continue
        if name == "bar":
            bar_sections += 1
            if bar_sections > 1:
                raise ValueError("duplicate TOML bar table")
            if start_idx < 0:
                start_idx = index
        elif start_idx >= 0 and index > start_idx:
            end_idx = index
            break

    if start_idx < 0:
        separator = "" if original_content.endswith("\n") else "\n"
        return original_content + separator + "\n" + bar_block

    output = []
    seen = set()
    for line in lines[start_idx:end_idx]:
        if "=" not in line or line.lstrip().startswith("#"):
            output.append(line)
            continue
        key_text, value_text = line.split("=", 1)
        key = key_text.strip().replace("_", "-")
        if key not in managed:
            output.append(line)
            continue
        newline = ""
        for candidate in ("\r\n", "\n", "\r"):
            if value_text.endswith(candidate):
                newline = candidate
                break
        comment_source = value_text[:-len(newline)] if newline else value_text
        without_comment, comment = split_inline_comment(comment_source)
        trailing = without_comment[len(without_comment.rstrip(" \t")):]
        output.append(f"{key:<17}= {managed[key]}{trailing}{comment}{newline}")
        seen.add(key)

    for key in key_order:
        if key not in seen:
            output.append(f"{key} = {managed[key]}\n")
    return "".join(lines[:start_idx] + output + lines[end_idx:])


def cmd_read():
    with config_lock():
        recover_pending_transaction()
        cfg, _ = load_json(SHELL_JSON)
        validate_shell_config(cfg)
        toml_values, toml_exists = load_toml_bar()
        profiles = list_profiles()
    out = {
        "cfg": cfg,
        "toml": {"values": toml_values, "exists": toml_exists},
        "profiles": profiles,
    }
    print(json.dumps(out, ensure_ascii=False, allow_nan=False))


def _validate_input_value(value, depth=0, counter=None):
    if counter is None:
        counter = [0]
    if depth > MAX_INPUT_DEPTH:
        raise SystemExit("input nesting is too deep")
    counter[0] += 1
    if counter[0] > MAX_INPUT_NODES:
        raise SystemExit("input has too many values")
    if value is None or isinstance(value, bool):
        return
    if isinstance(value, int):
        if value.bit_length() > 128:
            raise SystemExit("input integer is too large")
        return
    if isinstance(value, float):
        if not math.isfinite(value):
            raise SystemExit("input contains a non-finite number")
        return
    if isinstance(value, str):
        if len(value.encode("utf-8")) > MAX_INPUT_STRING_BYTES:
            raise SystemExit("input string is too large")
        return
    if isinstance(value, list):
        if len(value) > MAX_INPUT_KEYS:
            raise SystemExit("input list is too large")
        for item in value:
            _validate_input_value(item, depth + 1, counter)
        return
    if isinstance(value, dict):
        if len(value) > MAX_INPUT_KEYS:
            raise SystemExit("input object is too large")
        for key, item in value.items():
            if not isinstance(key, str) or len(key.encode("utf-8")) > 1024:
                raise SystemExit("input object key is invalid")
            _validate_input_value(item, depth + 1, counter)
        return
    raise SystemExit("input contains an unsupported value")


def _valid_text(value, limit, allow_empty=False):
    if not isinstance(value, str) or len(value.encode("utf-8")) > limit:
        return False
    if not allow_empty and not value:
        return False
    return not any(ord(char) < 32 or ord(char) == 127 for char in value)


def _valid_config_id(value):
    return (isinstance(value, str) and 0 < len(value) <= 128
            and value[0].isascii() and value[0].isalnum()
            and value not in ("constructor", "prototype", "__proto__")
            and all(char.isascii() and (char.isalnum() or char in "._-") for char in value))


def validate_shell_config(value):
    if not isinstance(value, dict):
        raise SystemExit("shell configuration must be an object")
    _validate_input_value(value)
    bar = value.get("bar")
    if bar is not None:
        if not isinstance(bar, dict):
            raise SystemExit("bar configuration must be an object")
        position = bar.get("position")
        if position is not None and position not in ("top", "bottom", "left", "right"):
            raise SystemExit("invalid bar position")
        for key in ("transparent", "scaleWithFont"):
            if key in bar and not isinstance(bar[key], bool):
                raise SystemExit("invalid bar boolean")
        for key, limit in (("centerAnchor", 256), ("fontFamily", 256), ("host", 128)):
            if key in bar and not _valid_text(bar[key], limit, True):
                raise SystemExit("invalid bar text")
        layout = bar.get("layout")
        if layout is not None:
            if not isinstance(layout, dict):
                raise SystemExit("bar layout must be an object")
            total = 0
            for section in ("left", "center", "right"):
                entries = layout.get(section, [])
                if not isinstance(entries, list) or len(entries) > 1000:
                    raise SystemExit("invalid bar layout")
                for entry in entries:
                    total += 1
                    if total > 3000:
                        raise SystemExit("bar layout is too large")
                    if isinstance(entry, str):
                        if not _valid_config_id(entry):
                            raise SystemExit("invalid widget id")
                    elif isinstance(entry, dict):
                        if not _valid_config_id(entry.get("id")):
                            raise SystemExit("invalid widget id")
                    else:
                        raise SystemExit("invalid widget entry")
    idle = value.get("idle")
    if idle is not None:
        if not isinstance(idle, dict):
            raise SystemExit("idle configuration must be an object")
        for key in ("screensaver", "lock"):
            if key in idle:
                number = idle[key]
                if isinstance(number, bool) or not isinstance(number, (int, float)) or not math.isfinite(float(number)):
                    raise SystemExit("invalid idle timeout")
                if number < 0 or number > 86400:
                    raise SystemExit("invalid idle timeout")
    plugins = value.get("plugins")
    if plugins is not None:
        if not isinstance(plugins, list) or len(plugins) > 10000:
            raise SystemExit("invalid plugins configuration")
        for item in plugins:
            if isinstance(item, str):
                if not _valid_config_id(item):
                    raise SystemExit("invalid plugin id")
            elif isinstance(item, dict):
                if not _valid_config_id(item.get("id")):
                    raise SystemExit("invalid plugin id")
            else:
                raise SystemExit("invalid plugin entry")


def validate_toml_values(values):
    if not isinstance(values, dict) or len(values) > 64:
        raise SystemExit("invalid TOML values")
    _validate_input_value(values)
    try:
        for key in ("background", "text", "active"):
            if key in values:
                if values[key] is None:
                    raise ValueError("invalid TOML color")
                _toml_color(values[key], "#000000")
        alpha_key = "background_alpha" if "background_alpha" in values else "background-alpha"
        if alpha_key in values:
            if values[alpha_key] is None:
                raise ValueError("invalid TOML alpha")
            number = _toml_number(values[alpha_key], 1.0, 0.0, 1.0)
            if not math.isfinite(number):
                raise ValueError("invalid TOML alpha")
        scale_key = "scale_with_font" if "scale_with_font" in values else "scale-with-font"
        if scale_key in values:
            if values[scale_key] is None:
                raise ValueError("invalid TOML boolean")
            _toml_bool(values[scale_key], True)
        for key, default in (("size_horizontal", 26), ("size_vertical", 28)):
            dashed = key.replace("_", "-")
            actual = key if key in values else dashed
            if actual in values:
                if values[actual] is None:
                    raise ValueError("invalid TOML size")
                number = _toml_number(values[actual], default, 1, 1000, True)
                if not math.isfinite(number):
                    raise ValueError("invalid TOML size")
    except (TypeError, ValueError, OverflowError) as error:
        raise SystemExit(f"invalid TOML values: {error}") from error


def read_stdin_json():
    decoder = json.JSONDecoder(parse_constant=_reject_json_constant)
    stream = getattr(sys.stdin, "buffer", sys.stdin)
    buf = ""
    total = 0
    while True:
        remaining = MAX_INPUT_BYTES - total
        if remaining <= 0:
            raise SystemExit("input too large")
        chunk = stream.read(min(65536, remaining + 1))
        if not chunk:
            raise SystemExit("stdin closed before complete JSON")
        if isinstance(chunk, bytes):
            total += len(chunk)
            try:
                chunk = chunk.decode("utf-8")
            except UnicodeDecodeError as error:
                raise SystemExit("input is not UTF-8") from error
        else:
            try:
                total += len(chunk.encode("utf-8"))
            except UnicodeEncodeError as error:
                raise SystemExit("input is not UTF-8") from error
        if total > MAX_INPUT_BYTES:
            raise SystemExit("input too large")
        buf += chunk
        try:
            value, end = decoder.raw_decode(buf)
        except (json.JSONDecodeError, RecursionError, ValueError):
            continue
        if buf[end:].strip():
            raise SystemExit("trailing data after JSON")
        _validate_input_value(value)
        return value


def cmd_write():
    payload = read_stdin_json()
    if not isinstance(payload, dict):
        raise SystemExit("invalid write payload")
    _validate_input_value(payload)
    if any(key not in ("shell", "base", "toml") for key in payload):
        raise SystemExit("unknown write payload key")
    if "base" in payload and ("shell" not in payload or payload["shell"] is None):
        raise SystemExit("base requires shell")
    entries = []
    written_shell = None
    with config_lock():
        recover_pending_transaction()
        if "shell" in payload and payload["shell"] is not None:
            if "base" not in payload:
                raise SystemExit("shell write requires base configuration")
            current, _ = load_json(SHELL_JSON)
            cfg = merge_shell_config(payload["base"], payload["shell"], current)
            data = json.dumps(cfg, indent=2, ensure_ascii=False, allow_nan=False) + "\n"
            encoded = data.encode("utf-8")
            if len(encoded) > MAX_CONFIG_BYTES:
                raise SystemExit("shell configuration is too large")
            entries.append((SHELL_JSON, encoded))
            written_shell = cfg
        if "toml" in payload and payload["toml"] is not None:
            toml = payload["toml"]
            if not isinstance(toml, dict) or not isinstance(toml.get("values", {}), dict):
                raise SystemExit("toml values must be an object")
            validate_toml_values(toml.get("values", {}))
            current, _ = read_file_checked(SHELL_TOML)
            new_content = format_toml(toml.get("values", {}), current)
            encoded = new_content.encode("utf-8")
            if len(encoded) > MAX_CONFIG_BYTES:
                raise SystemExit("TOML configuration is too large")
            entries.append((SHELL_TOML, encoded))
        if entries:
            _write_transaction(entries)
    output = {"ok": True}
    if written_shell is not None:
        output["shell"] = written_shell
    print(json.dumps(output, ensure_ascii=False, allow_nan=False))


# ---------------------------------------------------------------------------
# profiles
# ---------------------------------------------------------------------------

def safe_profile_name(name):
    value = str(name).strip()
    if (not value or len(value) > MAX_PROFILE_NAME or value.startswith(".") or
            "/" in value or "\\" in value or value in (".", "..") or
            any(ord(char) < 32 or ord(char) == 127 for char in value) or
            not PROFILE_NAME_RE.fullmatch(value)):
        raise SystemExit(f"bad profile name: {value!r}")
    return value


def _open_profiles_dir(create=False):
    target = _absolute(PROFILES_DIR)
    parent = _open_parent_dir(target)
    name = os.path.basename(target)
    if create:
        try:
            os.mkdir(name, 0o700, dir_fd=parent)
        except FileExistsError:
            pass
        except OSError as error:
            os.close(parent)
            raise SystemExit(f"cannot create profiles directory: {error.strerror}")
    try:
        fd = os.open(
            name,
            os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0),
            dir_fd=parent,
        )
    except OSError as error:
        os.close(parent)
        if error.errno == errno.ENOENT:
            return None
        raise SystemExit(f"cannot open profiles directory: {error.strerror}")
    finally:
        try:
            os.close(parent)
        except OSError:
            pass
    st = os.fstat(fd)
    if (not stat.S_ISDIR(st.st_mode) or st.st_uid != os.geteuid()
            or stat.S_IMODE(st.st_mode) & 0o022):
        os.close(fd)
        raise SystemExit("refusing unsafe profiles directory")
    return fd


def list_profiles():
    fd = _open_profiles_dir(False)
    if fd is None:
        return []
    names = []
    try:
        for entry in os.listdir(fd):
            if not entry.endswith(".json"):
                continue
            name = entry[:-5]
            try:
                name = safe_profile_name(name)
                st = os.stat(name + ".json", dir_fd=fd, follow_symlinks=False)
            except (OSError, SystemExit):
                continue
            if (stat.S_ISREG(st.st_mode) and st.st_uid == os.geteuid()
                    and st.st_nlink == 1 and not stat.S_IMODE(st.st_mode) & 0o022):
                names.append(name)
    finally:
        os.close(fd)
    return sorted(names)


def cmd_profiles(args):
    if not args:
        raise SystemExit("profiles requires a subcommand")
    cmd = args[0]
    if cmd == "list":
        with config_lock():
            recover_pending_transaction()
            names = list_profiles()
        print(json.dumps(names, ensure_ascii=False, allow_nan=False))
        return
    if len(args) < 2:
        raise SystemExit(f"profiles {cmd} requires a name")
    name = safe_profile_name(args[1])
    path = os.path.join(PROFILES_DIR, name + ".json")
    if cmd == "save":
        payload = read_stdin_json()
        if not isinstance(payload, dict):
            raise SystemExit("profile must be an object")
        _validate_input_value(payload)
        if any(key not in ("shell", "toml") for key in payload):
            raise SystemExit("unknown profile key")
        if "shell" in payload:
            validate_shell_config(payload["shell"])
        if "toml" in payload:
            toml = payload["toml"]
            if not isinstance(toml, dict) or not isinstance(toml.get("values", {}), dict):
                raise SystemExit("invalid profile TOML")
            validate_toml_values(toml.get("values", {}))
        encoded = (json.dumps(payload, indent=2, ensure_ascii=False, allow_nan=False) + "\n").encode("utf-8")
        if len(encoded) > MAX_CONFIG_BYTES:
            raise SystemExit("profile is too large")
        with config_lock():
            recover_pending_transaction()
            dirfd = _open_profiles_dir(True)
            os.close(dirfd)
            _write_transaction([(path, encoded)])
        print(json.dumps({"ok": True}, ensure_ascii=False, allow_nan=False))
    elif cmd == "load":
        with config_lock():
            recover_pending_transaction()
            content, exists = read_file_checked(path)
            if not exists:
                raise SystemExit(f"no profile named {name}")
            try:
                profile = json.loads(content, parse_constant=_reject_json_constant)
                _validate_input_value(profile)
                if not isinstance(profile, dict):
                    raise ValueError
                if "shell" in profile:
                    validate_shell_config(profile["shell"])
                if "toml" in profile:
                    toml = profile["toml"]
                    if not isinstance(toml, dict) or not isinstance(toml.get("values", {}), dict):
                        raise ValueError("invalid profile TOML")
                    validate_toml_values(toml.get("values", {}))
            except (json.JSONDecodeError, RecursionError, ValueError) as exc:
                raise SystemExit("corrupt profile") from exc
        print(content, end="")
    elif cmd == "delete":
        with config_lock():
            recover_pending_transaction()
            fd = _open_profiles_dir(False)
            if fd is None:
                raise SystemExit(f"no profile named {name}")
            try:
                st = os.stat(name + ".json", dir_fd=fd, follow_symlinks=False)
                if (not stat.S_ISREG(st.st_mode) or st.st_uid != os.geteuid()
                        or st.st_nlink != 1 or stat.S_IMODE(st.st_mode) & 0o022):
                    raise SystemExit("refusing unsafe profile")
            except FileNotFoundError:
                raise SystemExit(f"no profile named {name}")
            finally:
                os.close(fd)
            _write_transaction([(path, None)])
        print(json.dumps({"ok": True}, ensure_ascii=False, allow_nan=False))
    else:
        raise SystemExit(f"unknown profiles subcommand: {cmd}")


# ---------------------------------------------------------------------------

def main():
    if len(sys.argv) < 2:
        raise SystemExit("usage: shell_io.py <read|write|profiles ...>")
    cwd = os.getcwd()
    os.chdir("/")  # never run relative to a caller-controlled directory
    root = os.path.abspath(CONFIG_ROOT)
    verify_parent_dir(os.path.join(root, "x"), create=True)
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