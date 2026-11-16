# Security Policy

## Supported Versions

| Version | Supported          |
| ------- | ------------------ |
| 1.0.x   | :white_check_mark: |

---

## Auto-Updater Security Architecture

**Iconverter** includes a built-in GitHub Releases updater (`check_for_updates`, `download_update_asset`, and `replace_executable_in_place` in `icon_converter.py`). To protect users during update checks and installations:

1. **HTTPS-Only Transport**:
   - `is_allowed_download_url()` enforces `https://` URLs with a valid hostname before any asset download begins. Plain `http://`, `file://`, or custom schemes are rejected.
2. **Atomic Temp-File Staging & Verification**:
   - Updates are streamed to a temporary file (`update_<version>.tmp_download`) and are **never** moved into place unless:
     - Total downloaded bytes are non-zero and match both the HTTP `Content-Length` header and the release asset's `expected_size`.
     - When GitHub's release asset metadata includes a `digest` (`sha256:<hex>`), the streamed SHA-256 digest matches the expected 64-character hex digest.
   - Any incomplete or mismatched download is immediately unlinked in a `finally:` block.
3. **Transactional In-Place NTFS Replacement**:
   - On Windows, `replace_executable_in_place()` renames the running executable to `<name>.exe.old` and moves the verified update into `<name>.exe`. If moving the new executable fails for any reason, it automatically rolls back `<name>.exe.old` -> `<name>.exe`.
4. **Isolated Subprocess IPC**:
   - The `--native-svg-worker` child process communicates exclusively over anonymous parent-child `stdin`/`stdout` pipes (`subprocess.PIPE`) with no network sockets or listening ports.

---

## Reporting a Vulnerability

If you discover a security issue in Iconverter or `ModernTtk`, please report it by opening a security advisory on GitHub at:

- https://github.com/asteng88/Iconverter/security/advisories

Please include steps to reproduce, affected operating system / version, and any relevant logs.
