"""romtools - Linux-native ROM verification and renaming tools."""

__version__ = "1.11.4"
APP_ORG = "romtools"

from .compat import hide_console_windows as _hide  # noqa: E402

_hide()
