from __future__ import annotations

import importlib
import pathlib
import re
from pkgutil import extend_path

__path__ = extend_path(__path__, __name__)

_SYMBOL_TO_MODULE: dict[str, str] = {}


def _build_symbol_index() -> None:
    if _SYMBOL_TO_MODULE:
        return
    for root_text in __path__:
        root = pathlib.Path(root_text)
        if not root.exists():
            continue
        for path in root.rglob("*.py"):
            if path.name == "__init__.py":
                continue
            module_name = f"{__name__}.{'.'.join(path.relative_to(root).with_suffix('').parts)}"
            try:
                text = path.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue
            for match in re.finditer(r"^class\s+([A-Za-z_][A-Za-z0-9_]*)\b", text, re.MULTILINE):
                _SYMBOL_TO_MODULE.setdefault(match.group(1), module_name)


def __getattr__(name: str) -> object:
    _build_symbol_index()
    module_name = _SYMBOL_TO_MODULE.get(name)
    if module_name is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    module = importlib.import_module(module_name)
    value = getattr(module, name)
    globals()[name] = value
    return value
