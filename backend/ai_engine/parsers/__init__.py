"""Public parser interface: document decoding always uses a child process."""

from .core import MIME_TYPES, ParseError
from .core import validate_file as _validate_file
from .isolation import parse

__all__ = ["MIME_TYPES", "ParseError", "parse", "validate_file"]


def validate_file(name, raw):
    # ZIP directory/XML inspection belongs to the resource-limited parser child.
    return _validate_file(name, raw, inspect_archive=False)
