# Path Security Requirements

This document defines the requirements for path security: the mechanisms that prevent the LLM-driven agent from accessing, modifying, or exfiltrating files outside the designated workspace.

Path security answers one question: **can the agent touch only what it is allowed to touch, even if the model actively tries to escape?**

The threat model assumes the LLM is untrusted. It may produce arbitrary path strings, including crafted Unicode, null bytes, symlinks, hardlinks, and TOCTOU race attempts. The security layer must defend against all of these without relying on the model's good behavior.

---

## 1. Workspace Containment

### R-SEC-01: Path resolution before access control

All path access checks must resolve the path to its real, absolute filesystem location (following symlinks, resolving `.` and `..`) before comparing against the allowed-paths list. The comparison must never operate on the raw, user-supplied string.

**Why:** A raw string comparison allows trivial escapes. The path `./workspace/../../../etc/passwd` starts with `./workspace` and would pass a naive prefix check, but resolves to `/etc/passwd`. Symlinks inside the workspace can point anywhere. Only the resolved path reveals the true target.

**Acceptance criteria:**

- Given a path `workspace/../../../etc/passwd`, when access is checked, then it is rejected because the resolved path is outside the workspace.
- Given a symlink `workspace/link -> /etc/shadow`, when access is checked on `workspace/link`, then it is rejected because the resolved target is outside the workspace.
- Given a path `workspace/subdir/../file.py` where `file.py` exists in the workspace root, when access is checked, then it is allowed (the resolved path is inside the workspace).

### R-SEC-02: Null byte injection prevention

Path strings containing null bytes must be rejected before any OS-level operation is performed.

**Why:** A null byte (`\x00`) terminates C strings. A path like `/workspace/safe\x00/../../etc/passwd` passes the Python string prefix check (it starts with `/workspace/safe`) but the OS truncates at the null byte and operates on `/workspace/safe`, which may or may not be the intended target. On some systems, the behavior is undefined.

**Acceptance criteria:**

- Given a path containing a null byte at any position, when it is validated, then it is rejected with a clear error message before any file operation is attempted.

### R-SEC-03: Control character rejection

Path strings containing any ASCII control character (0x00-0x1F, 0x7F) must be rejected.

**Why:** Control characters in paths cause unpredictable behavior across operating systems and terminals. Tab characters can split tokens in shell commands, carriage returns can overwrite displayed paths in logs, and escape sequences can manipulate terminal output to hide the real path being accessed.

**Acceptance criteria:**

- Given a path containing a tab character, when it is validated, then it is rejected with an error identifying the specific control character.
- Given a path containing a carriage return, when it is validated, then it is rejected.
- Given a path containing only printable characters, when it is validated, then it passes (control character check does not reject normal paths).

---

## 2. Homoglyph and Unicode Spoofing Prevention

### R-SEC-04: Unicode homoglyph detection via NFKC normalization

Paths must be checked against their NFKC-normalized form. If the normalized form differs from the original, the path contains compatibility characters (superscripts, ligatures, full-width letters, combining diacritics) that could visually spoof a different path.

**Why:** The full-width solidus `\uFF0F` looks identical to ASCII `/` in many fonts but is a different Unicode codepoint. A path containing it would pass a prefix check (the Python string does not contain ASCII `/` at that position) but might be interpreted as a directory separator by some systems, or might confuse operators reading logs.

**Acceptance criteria:**

- Given a path containing a full-width letter (e.g., full-width `A`), when it is validated, then it is rejected because NFKC normalization produces a different string.
- Given a path containing only ASCII characters, when it is validated, then it passes the NFKC check.

### R-SEC-05: Explicit homoglyph blocklist

Beyond NFKC normalization, paths must be checked against an explicit blocklist of known dangerous look-alike characters: Cyrillic letters that resemble Latin letters, Greek letters, fraction slashes, and full-width separators.

**Why:** Some homoglyphs survive NFKC normalization (Cyrillic `a` normalizes to Cyrillic `a`, not Latin `a`). These are dangerous because they are visually indistinguishable from ASCII in most fonts. An operator reviewing a log would see `workspace/config.py` and not realize the `o` is Cyrillic, pointing to a different file.

**Acceptance criteria:**

- Given a path containing Cyrillic small `o` (U+043E), when it is validated, then it is rejected with an error naming the character and its Unicode codepoint.
- Given a path containing a fraction slash (U+2044), when it is validated, then it is rejected.
- Given a configurable blocklist, when a custom blocklist is provided, then it replaces the default (not merged).

---

## 3. Symlink Attack Prevention

### R-SEC-06: Atomic file reading with pre-open directory pinning

File reads must use a multi-step atomic protocol: open the parent directory first, validate the directory descriptor matches expectations, then open the target file relative to the pinned directory descriptor.

**Why:** A simple `open(path)` has a TOCTOU (time-of-check-time-of-use) race: between the moment the path is validated and the moment the file is opened, an attacker can replace a legitimate file or directory with a symlink to an arbitrary target. Opening the directory first and then using `openat()` semantics (via `dir_fd`) eliminates this race window.

**Acceptance criteria:**

- Given a file `workspace/data.txt`, when it is read atomically, then the parent directory is opened and validated before the file is opened.
- Given a parent directory that is replaced with a symlink between validation and opening, when the directory descriptor is validated, then the read fails (descriptor does not match expected device/inode).

### R-SEC-07: O_NOFOLLOW with fallback and post-open validation

When opening a target file relative to a pinned directory, the system must first attempt the open with the `O_NOFOLLOW` flag. If the target is a symlink, the kernel returns `ELOOP`. On receiving `ELOOP`, the system falls back to opening without `O_NOFOLLOW` (allowing the kernel to follow the symlink) and then performs post-open path validation (R-SEC-10) to ensure the resolved path is still within the allowed workspace.

**Why:** Even with directory pinning (R-SEC-06), if the file itself is a symlink, `openat()` would follow it to an arbitrary target. `O_NOFOLLOW` is attempted first as the preferred defense. However, some legitimate workspace layouts use symlinks, so a hard rejection would break valid use cases. The fallback open combined with post-open path validation provides the safety net: symlinks that resolve within the workspace are allowed; symlinks that escape the workspace are caught by the path validation and rejected.

**Acceptance criteria:**

- Given a symlink `workspace/evil.txt -> /etc/shadow`, when it is opened, then `O_NOFOLLOW` triggers `ELOOP`, the system retries without `O_NOFOLLOW`, and post-open path validation rejects the read because the resolved path is outside the workspace.
- Given a symlink `workspace/link.txt -> workspace/real.txt` (target within workspace), when it is opened, then `O_NOFOLLOW` triggers `ELOOP`, the system retries without `O_NOFOLLOW`, and post-open path validation allows the read because the resolved path is inside the workspace.
- Given a regular file `workspace/normal.txt`, when it is opened with `O_NOFOLLOW`, then it opens normally without triggering the fallback path.

### R-SEC-08: Directory descriptor consistency validation

After opening a directory descriptor, the system must verify that the descriptor's device and inode match the expected path's device and inode. A mismatch indicates the directory was swapped (symlink race).

**Why:** If an attacker replaces the parent directory with a symlink between `os.open(parent)` and the subsequent file open, the descriptor would point to a different directory. The device/inode check detects this substitution.

**Acceptance criteria:**

- Given a directory descriptor whose device/inode matches the expected path, when validation runs, then it passes.
- Given a directory descriptor whose device/inode does NOT match (directory was swapped), when validation runs, then it fails and the file read is aborted.

---

## 4. Hardlink Escape Prevention

### R-SEC-09: Hardlink count check

Files with multiple hardlinks must be rejected. A file with more than one directory entry (hardlink count > 1) could be accessible from a location outside the allowed paths.

**Why:** An attacker can create a hardlink inside the workspace that points to the same inode as a sensitive file outside the workspace (e.g., `ln /etc/shadow workspace/shadow`). The file's content is the same regardless of which path is used. Since it is impractical to enumerate all hardlink locations, rejecting multi-linked files is the safe default.

**Acceptance criteria:**

- Given a file with exactly 1 hardlink (the normal case), when it is validated, then it passes.
- Given a file with 2 or more hardlinks, when it is validated, then it is rejected.

### R-SEC-10: Post-open containment validation

After a file is opened (descriptor obtained), the system must verify that the opened file's real path is within the allowed paths, using platform-specific mechanisms.

**Why:** Pre-open path validation and `O_NOFOLLOW` close most attack vectors, but the definitive check is: "where does this file descriptor actually point?" On Linux, `/proc/self/fd/N` reveals the real path; on macOS, `fcntl(F_GETPATH)` does the same. This final check catches any attack that manipulates the path between validation and opening.

**Acceptance criteria:**

- Given a file descriptor that resolves to a path inside the workspace, when post-open validation runs, then it passes.
- Given a file descriptor that resolves to a path outside the workspace (attack succeeded at the OS level), when post-open validation runs, then the descriptor is closed and the read is aborted.
- Given a platform where neither `/proc/self/fd` nor `fcntl(F_GETPATH)` is available, when post-open validation falls back to device/inode walking, then it still produces a correct accept/reject decision.

---

## 5. Path Traversal Prevention

### R-SEC-11: Filename component validation

Individual filename components (the last segment of a path) must not contain path separators (`/`, `\`) or traversal sequences (`..`).

**Why:** A tool might construct a path by joining a base directory with a filename from the model. If the filename is `../../etc/passwd`, a naive join produces a path outside the workspace. Validating the filename component in isolation catches this before the join.

**Acceptance criteria:**

- Given a filename component `../../etc/passwd`, when it is validated, then it is rejected.
- Given a filename component `normal_file.py`, when it is validated, then it passes.
- Given a filename component containing a backslash (`foo\bar`), when it is validated, then it is rejected (backslash is a path separator on Windows and could be used for traversal on cross-platform code).

---

## 6. Layered Defense Summary

The security requirements above form a defense-in-depth chain. No single check is sufficient alone:

1. **Null bytes and control characters** (R-SEC-02, R-SEC-03) -- reject obviously malformed input before any interpretation.
2. **Unicode spoofing** (R-SEC-04, R-SEC-05) -- reject visually deceptive characters that could fool human review.
3. **Path traversal** (R-SEC-11) -- reject structural attacks in path components.
4. **Workspace containment** (R-SEC-01) -- reject resolved paths outside allowed directories.
5. **Atomic open with directory pinning** (R-SEC-06, R-SEC-07, R-SEC-08) -- eliminate TOCTOU races at the syscall level.
6. **Hardlink detection** (R-SEC-09) -- reject files reachable from outside the workspace.
7. **Post-open validation** (R-SEC-10) -- final confirmation that the opened file is where it should be.

A path must pass ALL layers to be accessed. Failure at any layer aborts the operation.
