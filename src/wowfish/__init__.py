"""wowfish: a vision-based fishing bot for World of Warcraft: Forever."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("forever-fishing-zh")
except PackageNotFoundError:  # running from a source tree without install
    __version__ = "1.0.1"

APP_TITLE = "Forever 钓鱼助手"
