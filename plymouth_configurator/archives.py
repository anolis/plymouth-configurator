"""Extract untrusted theme archives without following links or escaping the cache."""

from pathlib import Path, PurePosixPath
import shutil
import stat
import subprocess
import tarfile
import tempfile
import zipfile


ARCHIVE_SUFFIXES = (".zip", ".tar", ".tar.gz", ".tgz", ".tar.xz", ".txz",
                    ".tar.bz2", ".tbz2", ".tar.zst")


def is_archive(path: Path) -> bool:
    return path.name.lower().endswith(ARCHIVE_SUFFIXES)


def _target(dest: Path, name: str) -> Path:
    path = PurePosixPath(name)
    if path.is_absolute() or ".." in path.parts or "\\" in name or "\x00" in name:
        raise ValueError(f"Unsafe archive path: {name!r}")
    return dest.joinpath(*path.parts)


def _write(dest: Path, name: str, directory: bool, stream=None) -> None:
    target = _target(dest, name)
    if directory:
        target.mkdir(parents=True, exist_ok=True)
    else:
        target.parent.mkdir(parents=True, exist_ok=True)
        # Reject duplicate entries and file/directory collisions.
        with target.open("xb") as output:
            shutil.copyfileobj(stream, output)


def _extract_tar(archive, dest: Path) -> None:
    for member in archive:
        if member.isdir():
            _write(dest, member.name, True)
        elif member.isreg():
            with archive.extractfile(member) as stream:
                _write(dest, member.name, False, stream)
        else:
            raise ValueError(f"Archive links and special files are not supported: {member.name}")


def extract_archive(archive: Path, cache_dir: Path) -> Path:
    """Extract regular files/directories into a fresh private directory.

    All tar variants use the same checks, including zstd on Python 3.11.
    Archive permissions and ownership are deliberately not restored.
    """
    base = cache_dir / "extract"
    base.mkdir(parents=True, exist_ok=True)
    dest = Path(tempfile.mkdtemp(prefix="theme-", dir=base))
    try:
        if archive.name.lower().endswith(".zip"):
            with zipfile.ZipFile(archive) as source:
                for member in source.infolist():
                    kind = stat.S_IFMT(member.external_attr >> 16)
                    if kind not in (0, stat.S_IFREG, stat.S_IFDIR):
                        raise ValueError(f"Archive links and special files are not supported: {member.filename}")
                    with source.open(member) as stream:
                        _write(dest, member.filename, member.is_dir(), stream)
        elif archive.name.lower().endswith(".zst"):
            with tempfile.TemporaryFile() as errors:
                with subprocess.Popen(["zstd", "-dc", "--", str(archive)],
                                      stdout=subprocess.PIPE, stderr=errors) as proc:
                    try:
                        with tarfile.open(fileobj=proc.stdout, mode="r|") as source:
                            _extract_tar(source, dest)
                    except BaseException:
                        proc.kill()
                        raise
                    finally:
                        proc.stdout.close()
                    if proc.wait() != 0:
                        errors.seek(0)
                        raise ValueError(errors.read().decode(errors="replace") or "zstd failed")
        else:
            with tarfile.open(archive, mode="r:*") as source:
                _extract_tar(source, dest)
        return dest
    except BaseException:
        shutil.rmtree(dest)
        raise
