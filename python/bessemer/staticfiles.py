"""``StaticFiles`` for FastAPI-style ``app.mount("/static", StaticFiles(...))``."""

from __future__ import annotations

import os


class StaticFiles:
    """A directory to serve natively when mounted on an App.

    ``app.mount(path, StaticFiles(directory=..., html=...), name=...)`` is
    ``app.static(path, directory, html=..., name=...)``: Rust serves the files
    with validators, ranges and precompressed variants, never calling Python.
    Symlinks that leave the directory are refused, so ``follow_symlink`` and
    ``packages`` are not supported.
    """

    def __init__(
        self,
        *,
        directory: str | os.PathLike[str] | None = None,
        packages: object = None,
        html: bool = False,
        check_dir: bool = True,
        follow_symlink: bool = False,
    ) -> None:
        if directory is None:
            raise ValueError("StaticFiles needs a directory")
        if packages is not None:
            raise ValueError("StaticFiles packages= is not supported; give a directory")
        if follow_symlink:
            raise ValueError("StaticFiles never follows symlinks out of its directory")
        if type(html) is not bool:
            raise TypeError("html must be a boolean")
        if check_dir and not os.path.isdir(directory):
            raise RuntimeError(f"Directory '{directory}' does not exist")
        self.directory = directory
        self.html = html


__all__ = ["StaticFiles"]
