"""Rich-based console output and tqdm compatibility for VoxCPM."""

from __future__ import annotations

import sys
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from typing import Any

from rich.console import Console
from rich.progress import (
    BarColumn,
    MofNCompleteColumn,
    Progress,
    SpinnerColumn,
    TaskProgressColumn,
    TextColumn,
    TimeElapsedColumn,
    TimeRemainingColumn,
)

_console = Console(stderr=False)
_progress_stack: list[Progress] = []
_installed = False


def get_console() -> Console:
    return _console


def log(message: str, *, style: str | None = None) -> None:
    if style:
        _console.print(message, style=style)
    else:
        _console.print(message)


def log_error(message: str) -> None:
    Console(stderr=True).print(message, style="bold red", markup=False)


@contextmanager
def progress_session(**kwargs: Any) -> Iterator[Progress]:
    """Own a shared Progress so nested rich-tqdm tasks attach to the same bar."""
    progress = Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        TaskProgressColumn(),
        MofNCompleteColumn(),
        TimeElapsedColumn(),
        TimeRemainingColumn(),
        console=_console,
        transient=False,
        **kwargs,
    )
    _progress_stack.append(progress)
    progress.start()
    try:
        yield progress
    finally:
        progress.stop()
        if _progress_stack and _progress_stack[-1] is progress:
            _progress_stack.pop()
        elif progress in _progress_stack:
            _progress_stack.remove(progress)


class RichTqdm:
    """Minimal tqdm-compatible wrapper backed by rich.progress.

    Used only inside VoxCPM model modules (do not replace global tqdm.tqdm —
    huggingface_hub relies on the real tqdm API).
    """

    def __init__(
        self,
        iterable: Iterable[Any] | None = None,
        *args: Any,
        total: float | None = None,
        desc: str | None = None,
        disable: bool = False,
        leave: bool = True,  # noqa: ARG002 - tqdm compat
        **kwargs: Any,
    ):
        _ = args, kwargs
        self.iterable = iterable
        self.disable = bool(disable)
        self.desc = desc or ""
        if total is None and iterable is not None:
            try:
                total = len(iterable)  # type: ignore[arg-type]
            except TypeError:
                total = None
        self.total = total
        self.n = 0
        self._owns_progress = False
        self._progress: Progress | None = None
        self._task_id = None

        if self.disable:
            return

        active = _progress_stack[-1] if _progress_stack else None
        if active is not None:
            self._progress = active
            self._owns_progress = False
        else:
            self._progress = Progress(
                TextColumn("[progress.description]{task.description}"),
                BarColumn(),
                TaskProgressColumn(),
                MofNCompleteColumn(),
                TimeRemainingColumn(),
                console=_console,
                transient=True,
            )
            self._progress.start()
            self._owns_progress = True

        self._task_id = self._progress.add_task(self.desc or "generate", total=self.total)

    def __iter__(self) -> Iterator[Any]:
        if self.iterable is None:
            return iter(())
        try:
            for item in self.iterable:
                yield item
                self.update(1)
        finally:
            self.close()

    def __enter__(self) -> RichTqdm:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def update(self, n: float = 1) -> None:
        if self.disable or self._progress is None or self._task_id is None:
            self.n += n
            return
        self.n += n
        self._progress.update(self._task_id, advance=n)

    def set_description(self, desc: str | None = None, refresh: bool = True) -> None:  # noqa: ARG002
        self.desc = desc or ""
        if self._progress is not None and self._task_id is not None:
            self._progress.update(self._task_id, description=self.desc or "generate")

    def close(self) -> None:
        if self._progress is None:
            return
        if self._task_id is not None:
            try:
                self._progress.remove_task(self._task_id)
            except Exception:  # noqa: BLE001
                pass
            self._task_id = None
        if self._owns_progress:
            self._progress.stop()
        self._progress = None


def _patch_voxcpm_modules() -> None:
    for mod_name in ("voxcpm.model.voxcpm", "voxcpm.model.voxcpm2"):
        mod = sys.modules.get(mod_name)
        if mod is not None and getattr(mod, "tqdm", None) is not RichTqdm:
            setattr(mod, "tqdm", RichTqdm)


def install_rich_tqdm() -> None:
    """Patch VoxCPM's bound `tqdm` to RichTqdm (leaves global tqdm intact)."""
    global _installed
    _patch_voxcpm_modules()
    _installed = True


def ensure_rich_tqdm() -> None:
    """Call after `import voxcpm` so model modules exist, then patch them."""
    install_rich_tqdm()
