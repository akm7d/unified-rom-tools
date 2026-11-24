"""romtools - Linux-native ROM verification and renaming tools."""

__version__ = "1.17.2"
APP_ORG = "romtools"

from .compat import hide_console_windows as _hide, restore_environment as _restore  # noqa: E402

_hide()
_restore()
