# Security

User stories for workspace containment and path security. All stories use the actor "As a file operation" because these checks are enforced at the boundary of every filesystem access, regardless of which higher-level component initiated the access.

---

# Workspace Containment

## US-01: As a file operation, I need my target path verified against the list of allowed directories so that no tool can read or write files outside the designated workspace

Every file access must be checked against the configured allowed directories. This is the primary security boundary preventing the agent from accessing or modifying files it should not touch (system files, other projects, sensitive data outside the workspace).

### Acceptance Criteria
- Given a path that resolves to a location inside an allowed directory, when the path check runs, then the operation is permitted
- Given a path that resolves to a location outside all allowed directories, when the path check runs, then the operation is denied
- Given a path that equals an allowed directory exactly (not a child of it), when the path check runs, then the operation is permitted
- Given a path containing symlinks, when the path check runs, then symlinks are resolved to their real targets before the containment check, preventing symlink-escape attacks where a link inside the workspace points to a file outside it

## US-02: As a file operation, I need allowed paths outside the workspace to trigger an operator warning so that unintentional broad access is surfaced during configuration review

It is valid but unusual to configure allowed paths that extend beyond the workspace root. When this happens, a warning should be logged so operators can confirm the expanded access is intentional rather than a configuration mistake.

### Acceptance Criteria
- Given an allowed path that is not a child of the workspace directory, when the configuration is loaded, then a warning is logged naming both the allowed path and the workspace
- Given all allowed paths within the workspace, when the configuration is loaded, then no warning is emitted

## US-03: As a file operation, I need reads performed atomically with race-condition protection so that a symlink swapped between validation and reading cannot bypass the containment check

A time-of-check-to-time-of-use (TOCTOU) attack can swap a symlink target between the moment the path is validated and the moment the file is opened. Atomic reads eliminate this race window by opening the parent directory first, then opening the file relative to that directory descriptor, and verifying consistency throughout.

### Acceptance Criteria
- Given a file inside the workspace, when an atomic read is performed, then the parent directory is opened and its identity verified against expectations before the file is opened relative to it
- Given a file whose parent directory descriptor does not match the expected path (possible TOCTOU swap), when the consistency check runs, then the read is denied
- Given a file opened via atomic read, when the opened descriptor is verified, then its device and inode are compared against the expected file to detect last-moment swaps
- Given a file that is a symlink, when an atomic read is attempted, then the system attempts to open the file without following symlinks (O_NOFOLLOW), but falls back to following symlinks if the platform does not support it, then validates that the resolved path is still within the workspace — preventing redirect-to-target attacks even on the fallback path

## US-04: As a file operation, I need files with multiple hard links rejected so that a hard link inside an allowed directory pointing to a file outside cannot bypass containment

A hard link creates a second directory entry for the same underlying file. If an attacker creates a hard link inside the allowed directory that points to a sensitive file outside it, a naive containment check would pass because the link's path is inside the workspace, but the actual data resides outside.

### Acceptance Criteria
- Given an opened file whose inode has more than one hard link, when the link-count check runs, then the read is denied
- Given an opened file whose inode has exactly one hard link, when the link-count check runs, then the read proceeds normally

## US-05: As a file operation, I need the real path of the opened file verified against allowed directories after opening so that no indirection technique (symlink, hard link, mount bind) can bypass containment

Even after all pre-open checks, the definitive verification is comparing the file descriptor's real location against the allowed paths. This is the last line of defense using platform-specific mechanisms to determine where the open descriptor actually points.

### Acceptance Criteria
- Given a platform that supports resolving a file descriptor to its real path, when the post-open check runs, then the resolved real path is checked against allowed directories
- Given a platform where descriptor-to-path resolution is not available, when the post-open check runs, then a cross-platform fallback using device and inode comparison against all files under allowed directories is used
- Given a file that cannot be validated by any available method, when the post-open check runs, then access is denied by default (deny-by-default posture)

---

# Path Security

## US-06: As a file operation, I need paths containing null bytes rejected so that a null byte cannot truncate the path at the operating system boundary and bypass the prefix check

A null byte in a path string causes the OS to see a shorter path than the one validated by the application. The prefix check passes on the full Python string, but the OS operates on the truncated version, potentially accessing a completely different file.

### Acceptance Criteria
- Given a path containing a null byte at any position, when the path is validated, then the operation is denied with a clear error message

## US-07: As a file operation, I need paths containing control characters rejected so that invisible characters cannot cause unexpected behavior in path handling

Control characters (tabs, carriage returns, escape sequences, and other non-printing characters) in file paths can cause display confusion, log injection, or unexpected behavior in shell commands that process paths.

### Acceptance Criteria
- Given a path containing any ASCII control character, when the path is validated, then the operation is denied with an error message identifying the specific control character found

## US-08: As a file operation, I need paths containing Unicode homoglyphs rejected so that visually identical characters from non-Latin scripts cannot be used to spoof file paths

Characters from Cyrillic, Greek, and other scripts can look identical to ASCII characters but have different Unicode code points. An attacker could craft a path that looks like a legitimate workspace path but actually references a different location, or bypass allowlist matching that compares code points.

### Acceptance Criteria
- Given a path containing a character from the homoglyph blocklist, when the path is validated, then the operation is denied with an error message identifying the specific homoglyph and its description
- Given a path that changes under Unicode normalization (NFKC), when the path is validated, then the operation is denied because the path contains compatibility characters that could be used for visual spoofing
- Given a custom homoglyph blocklist configured by the operator, when homoglyph checking runs, then the operator's blocklist is used instead of the built-in default

## US-09: As a file operation, I need all path security checks executed in a fixed priority order so that the most critical and cheapest checks run first

Path security involves multiple independent checks, each catching a different attack vector. Running them in a consistent order (null bytes first, then control characters, then homoglyphs) ensures the most critical and cheapest checks execute first, and the first failure short-circuits the remaining checks.

### Acceptance Criteria
- Given a path that violates multiple security rules simultaneously, when the validation pipeline runs, then the error from the highest-priority check (null bytes) is returned
- Given a path that passes null-byte validation but contains control characters, when the validation pipeline runs, then the control-character error is returned
- Given a path that passes null-byte and control-character checks but contains homoglyphs, when the validation pipeline runs, then the homoglyph error is returned
- Given a path that passes all security checks, when the validation pipeline runs, then no error is returned

## US-10: As a file operation, I need filename components validated against path traversal sequences so that directory separators or parent-directory references embedded in a filename cannot escape the intended directory

A filename component (the last segment of a path) should never contain directory separators or parent-directory references. These could allow an attacker to escape the intended directory by embedding traversal sequences in what appears to be a simple filename.

### Acceptance Criteria
- Given a filename containing a forward slash, when the filename is validated, then the operation is denied
- Given a filename containing a backslash, when the filename is validated, then the operation is denied
- Given a filename component that is a parent-directory reference, when the filename is validated, then the operation is denied
