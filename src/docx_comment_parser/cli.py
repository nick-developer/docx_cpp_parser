"""Entry point for the ``docx-comments`` command.

The console script is registered by every install, but Typer and Rich are only
pulled in by the ``[cli]`` extra.  This module therefore keeps the import of the
real application lazy: if the extra is missing the user gets a one-line
instruction instead of a traceback.
"""

from __future__ import annotations

import sys
from typing import NoReturn

__all__ = ["main"]

_MISSING_DEPS_MESSAGE = """\
The 'docx-comments' command needs Typer and Rich, which are not installed.

Install them with:

    pip install docx-comment-parser[cli]

The Python API (import docx_comment_parser) works without them.
"""


def main() -> NoReturn:
    """Run the CLI, or explain how to install it."""
    try:
        from ._cli_app import run
    except ImportError as exc:
        # Only the missing optional CLI dependencies should be swallowed here.
        # An ImportError from the compiled core, or from a genuine bug in the
        # app module, is a real failure and must surface with its traceback.
        missing = getattr(exc, "name", "") or ""
        if missing.split(".")[0] not in {"typer", "rich", "click"}:
            raise
        sys.stderr.write(_MISSING_DEPS_MESSAGE)
        raise SystemExit(1)

    raise SystemExit(run())


if __name__ == "__main__":  # pragma: no cover
    main()
