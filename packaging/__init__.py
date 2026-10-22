"""Local application modules, sharing dependency submodules with PyPA packaging.

The project directory historically had no initializer; an installed regular package
otherwise masks ``python -m packaging.senses``. Extend the search path so imports
such as packaging.version keep resolving to the installed dependency.
"""

from importlib.metadata import version
from pkgutil import extend_path

__path__ = extend_path(__path__, __name__)
__version__ = version("packaging")


def self_evaluate() -> dict[str, object]:
    """Report the package path bridge; no product module is imported here.

    This package only preserves the dual-import layout. Runtime behaviour lives
    in the individual modules, so this audit must not be read as feature proof.
    """
    return {
        "module": "__init__",
        "domain": "package and PyPA join-path bridge",
        "package": __name__,
        "version": __version__,
        "path_entries": list(__path__),
        "known_limitations": [
            "A path bridge provides no resident behaviour, tools or data.",
            "Importing the package does not import or verify any product module.",
        ],
        "improvement_directions": [
            "Keep this file limited to import-path compatibility.",
        ],
    }
