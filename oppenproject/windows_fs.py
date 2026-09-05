"""Windows file handles and private ACLs. Imported only on Windows.

Keep every ancestor handle open without write/delete sharing during access. This
prevents path components being replaced or converted to reparse points mid-read.
Only local drive paths are accepted; UNC, device namespaces and reparse points
(including junctions and cloud placeholders) fail closed.
"""

import msvcrt
import os
import re
from contextlib import contextmanager
from pathlib import Path

import ntsecuritycon
import pywintypes
import win32api
import win32con
import win32file
import win32security


def _open(path: Path, *, directory: bool, write=False):
    handle = win32file.CreateFile(
        str(path),
        win32con.GENERIC_READ | (win32con.GENERIC_WRITE if write else 0),
        win32con.FILE_SHARE_READ if not write else 0,
        None,
        win32con.OPEN_ALWAYS if write else win32con.OPEN_EXISTING,
        win32con.FILE_FLAG_OPEN_REPARSE_POINT | win32con.FILE_FLAG_BACKUP_SEMANTICS,
        None,
    )
    try:
        info = win32file.GetFileInformationByHandle(handle)
        attributes, links = info[0], info[7]
        if (
            win32file.GetFileType(handle) != win32con.FILE_TYPE_DISK
            or attributes & win32con.FILE_ATTRIBUTE_REPARSE_POINT
            or bool(attributes & win32con.FILE_ATTRIBUTE_DIRECTORY) != directory
            or (not directory and links != 1)
        ):
            raise OSError("Only ordinary local files and directories are supported")
        raw = handle.Detach()
        handle = None
        try:
            return msvcrt.open_osfhandle(raw, os.O_BINARY | (os.O_RDWR if write else os.O_RDONLY))
        except BaseException:
            win32api.CloseHandle(raw)
            raise
    finally:
        if handle is not None:
            handle.Close()


@contextmanager
def open_beneath(root: Path, parts: tuple[str, ...], directory=False, expected_root=None, write=False):
    if not re.fullmatch(r"[A-Za-z]:", root.drive) or not root.is_absolute():
        raise OSError("Use an absolute local Windows drive path")
    if not parts and not directory:
        raise OSError("A project root is a directory, not a readable file")
    fds = []
    try:
        current = Path(root.anchor)
        fds.append(_open(current, directory=True))
        for part in root.parts[1:]:
            current /= part
            fds.append(_open(current, directory=True))
        st = os.fstat(fds[-1])
        if not st.st_ino or (expected_root is not None and (st.st_dev, st.st_ino) != expected_root):
            raise OSError("Project root identity changed or is unavailable")
        for i, part in enumerate(parts):
            current /= part
            final = i == len(parts) - 1
            fds.append(_open(current, directory=directory or not final, write=write and final))
        yield fds[-1]
    except pywintypes.error as error:
        raise OSError("Windows file handle access denied") from error
    finally:
        for fd in reversed(fds):
            os.close(fd)


def make_private(path: Path, *, directory=False):
    """Replace the runtime object's DACL with current-user access, without inheritance."""
    with win32security.OpenProcessToken(win32api.GetCurrentProcess(), win32con.TOKEN_QUERY) as token:
        sid = win32security.GetTokenInformation(token, win32security.TokenUser)[0]
    acl = win32security.ACL()
    flags = win32con.OBJECT_INHERIT_ACE | win32con.CONTAINER_INHERIT_ACE if directory else 0
    acl.AddAccessAllowedAceEx(win32security.ACL_REVISION, flags, ntsecuritycon.FILE_ALL_ACCESS, sid)
    win32security.SetNamedSecurityInfo(
        str(path),
        win32security.SE_FILE_OBJECT,
        win32security.DACL_SECURITY_INFORMATION | win32security.PROTECTED_DACL_SECURITY_INFORMATION,
        None,
        None,
        acl,
        None,
    )
