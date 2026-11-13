"""Public parser interface: document decoding always uses a child process."""

from .core import MIME_TYPES, ParseError, validate_file
from .isolation import parse

__all__ = ["MIME_TYPES", "ParseError", "parse", "validate_file"]
