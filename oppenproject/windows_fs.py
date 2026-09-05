"""Windows file handles and private ACLs. Imported only on Windows.

Keep every ancestor handle open without write/delete sharing during access. This
prevents path components being replaced or converted to reparse points mid-read.
Only local drive paths are accepted; UNC, device namespaces and reparse points
(including junctions and cloud placeholders) fail closed.
"""

import ctypes
import errno
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


def _open(path: Path, *, directory: bool, write=False, rename=False):
    try:
        handle = win32file.CreateFile(
            str(path),
            win32con.GENERIC_READ
            | (win32con.GENERIC_WRITE if write else 0)
            | (win32con.DELETE if rename else 0),
            win32con.FILE_SHARE_READ if not (write or rename) else 0,
            None,
            win32con.OPEN_ALWAYS if write else win32con.OPEN_EXISTING,
            win32file.FILE_FLAG_OPEN_REPARSE_POINT | win32con.FILE_FLAG_BACKUP_SEMANTICS,
            None,
        )
    except pywintypes.error as error:
        if error.winerror in {2, 3}:
            raise FileNotFoundError(errno.ENOENT, "Windows file or directory is missing") from error
        raise OSError(errno.EACCES, "Windows file handle access denied") from error
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


def rename_in_place(source: Path, name: str, *, replace: bool, expected):
    """Use the source handle's parent, keeping every directory locked against replacement.

    MoveFileEx (os.rename/replace) reopens the target directory for writing, which
    conflicts with our held directory handles. Native FileRenameInformation with
    a simple name and NULL RootDirectory is a rename within the same parent.
    https://learn.microsoft.com/windows-hardware/drivers/ddi/ntifs/ns-ntifs-_file_rename_information
    """
    if not name or any(c in name for c in "/\\:\0"):
        raise ValueError("Rename requires one filename")

    class RenameInfo(ctypes.Structure):
        _fields_ = [
            ("ReplaceIfExists", ctypes.c_ubyte),
            ("RootDirectory", ctypes.c_void_p),
            ("FileNameLength", ctypes.c_ulong),
            ("FileName", ctypes.c_wchar * 1),
        ]

    class IOStatus(ctypes.Structure):
        _fields_ = [("StatusOrPointer", ctypes.c_void_p), ("Information", ctypes.c_size_t)]

    encoded = name.encode("utf-16-le")
    buffer = ctypes.create_string_buffer(ctypes.sizeof(RenameInfo) + len(encoded))
    info = RenameInfo.from_buffer(buffer)
    info.ReplaceIfExists = bool(replace)
    info.RootDirectory = None
    info.FileNameLength = len(encoded)
    ctypes.memmove(ctypes.addressof(buffer) + RenameInfo.FileName.offset, encoded, len(encoded))
    native = ctypes.WinDLL("ntdll")
    rename_file = native.NtSetInformationFile
    rename_file.argtypes = [
        ctypes.c_void_p,
        ctypes.POINTER(IOStatus),
        ctypes.c_void_p,
        ctypes.c_ulong,
        ctypes.c_int,
    ]
    rename_file.restype = ctypes.c_long
    fd = _open(source, directory=False, rename=True)
    try:
        actual = os.fstat(fd)
        fields = ("st_dev", "st_ino", "st_size", "st_mtime_ns")
        if any(getattr(actual, f) != getattr(expected, f) for f in fields):
            raise OSError("Temporary Discussion file was replaced or modified")
        result = rename_file(
            msvcrt.get_osfhandle(fd), ctypes.byref(IOStatus()), buffer, len(buffer), 10
        )  # FileRenameInformation
        if result < 0:
            native.RtlNtStatusToDosError.argtypes = [ctypes.c_long]
            native.RtlNtStatusToDosError.restype = ctypes.c_ulong
            raise ctypes.WinError(native.RtlNtStatusToDosError(result))
    finally:
        os.close(fd)


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
    token = win32security.OpenProcessToken(win32api.GetCurrentProcess(), win32con.TOKEN_QUERY)
    try:
        sid = win32security.GetTokenInformation(token, win32security.TokenUser)[0]
    finally:
        token.Close()
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
