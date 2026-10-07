# FUSE Mount

PICARD can be mounted as a real filesystem using FUSE, exposing all stored files
through standard filesystem operations. Read, write, mkdir, symlink, and more
work as expected from any program.

## Prerequisites

FUSE support requires a platform-specific library plus the `refuse` Python package
(already declared in `pyproject.toml`).

| Platform | Requirement | Install |
|----------|-------------|---------|
| Linux | libfuse / FUSE 2/3 | `sudo apt install fuse` or `sudo dnf install fuse` |
| macOS | macFUSE | [macfuse.io](https://osxfuse.github.io/) |
| Windows | WinFSP | [winfsp.dev](https://winfsp.dev/) |

## Mounting

```bash
# Linux / macOS
picard mount /mnt/picard

# Windows (drive letter)
picard mount P:

# Read-only mode
picard mount /mnt/picard --readonly

# Foreground (for debugging)
picard mount /mnt/picard --foreground
```

You will be prompted for a passphrase. The passphrase is held in memory for
the duration of the mount and cleared on unmount.

## Unmounting

```bash
# Linux
fusermount -u /mnt/picard

# macOS
umount /mnt/picard

# Windows
# Close all programs using the drive, then the mount process exits
```

## Supported Operations

| Operation | FUSE Method | Notes |
|-----------|------------|-------|
| Read file | `read` | Uses `read_range` for partial reads (block cache) |
| Write file | `write` | Buffered in memory, flushed on close or fsync |
| Create file | `create` | Allocates write buffer |
| Open file | `open` | Loads existing content for write mode |
| Close file | `release` | Flushes write buffer via `put()` |
| Sync file | `fsync` | Flushes write buffer without closing |
| Get attributes | `getattr` | Returns manifest metadata as stat struct |
| List directory | `readdir` | Lists children via index |
| Make directory | `mkdir` | Creates directory manifest |
| Remove directory | `rmdir` | Checks empty, removes manifest |
| Delete file | `unlink` | Removes chunks and manifest |
| Rename | `rename` | Updates manifest path |
| Symlink | `symlink` | Creates symlink manifest |
| Readlink | `readlink` | Returns symlink target |
| Chmod | `chmod` | Updates manifest mode |
| Chown | `chown` | Updates manifest uid/gid |
| Set timestamps | `utimens` | Updates manifest atime/mtime |
| Get xattr | `getxattr` | Reads from manifest xattrs |
| Set xattr | `setxattr` | Updates manifest xattrs |
| List xattrs | `listxattr` | Returns xattr keys |
| Remove xattr | `removexattr` | Deletes xattr key |
| Filesystem stats | `statfs` | Synthetic stats from du() |
| Truncate | `truncate` | Read, truncate, re-put |

## Write Buffering

Writes are accumulated in an in-memory `BytesIO` buffer per open file handle.
The buffer is flushed (encrypted, chunked, scattered) when:

1. The file is closed (`release`)
2. `fsync` is called explicitly
3. The filesystem is unmounted (`destroy`)

This means the entire file must fit in memory during a write session.
For files larger than available RAM, use the CLI's streaming put instead.

## Performance Tuning

### Block Cache

The FUSE mount automatically uses a block cache for read performance.
Decrypted plaintext blocks are cached in memory to avoid redundant
Argon2id key derivation (which takes ~100ms per block).

Default settings:
- Max entries: 1024
- Max bytes: 64 MiB
- TTL: 300 seconds (5 minutes)

The cache is cleared on unmount.

### Read Performance

Reads use `read_range()` which only decrypts the blocks overlapping
the requested byte range. Combined with the block cache, sequential
reads of a file decrypt each block exactly once.

### Write Performance

Writes are fast (just `BytesIO.write()`), but the flush on close
encrypts and scatters the entire file. Large files will have a
noticeable pause on close.

## Threading Model

The FUSE mount runs an asyncio event loop in a background daemon thread.
FUSE callbacks use `run_coroutine_threadsafe` to bridge synchronous FUSE
calls to the async orchestrator. The mount itself runs with `nothreads=False`,
allowing multiple FUSE operations in flight.

## Read-Only Mode

Pass `--readonly` to prevent all write operations. Any write attempt
returns `EROFS` (read-only filesystem). Useful for safely browsing
stored data without risk of accidental modification.

## Known Limitations

- **Memory**: Write buffering holds the entire file in memory. Use CLI for large files.
- **Hard links**: Not supported (the index tracks paths, not inodes).
- **Concurrent writes**: Multiple processes writing to the same file may conflict.
  The last close wins.
- **No fsnotify**: Changes made via CLI or web dashboard are visible on next
  read (the index is shared), but no filesystem change notifications are sent.
- **Argon2id cost**: First read of each block takes ~100ms for key derivation.
  The block cache mitigates this for repeated reads.
- **Windows**: Drive letter mount only. Some POSIX operations (uid/gid, chmod)
  are no-ops on Windows. `os.getuid()`/`os.getgid()` are shimmed to return 0.
