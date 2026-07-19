"""Small dependency-free TOML compatibility layer.

Python 3.11's :mod:`tomllib` is preferred when available.  The fallback
implements the TOML subset used by Chatmesh configuration: tables, arrays of
tables, dotted/quoted keys, strings, booleans, integers, floats, arrays, and
inline tables.  The writer deliberately supports the same data model and
emits stable, sorted output.
"""

from __future__ import annotations

import json
import math
import re
from typing import Any, Dict, Iterator, List, Mapping, MutableMapping, Sequence, Tuple

try:  # Python 3.11+
    import tomllib as _stdlib_toml
except ImportError:  # pragma: no cover - exercised by the Python 3.9 run
    _stdlib_toml = None


class TOMLError(ValueError):
    """Raised when fallback parsing or deterministic encoding fails."""


_BARE_KEY = re.compile(r"^[A-Za-z0-9_-]+$")
_DECIMAL_INT = re.compile(r"^[+-]?(?:0|[1-9](?:_?[0-9])*)$")
_BASE_INT = re.compile(r"^[+-]?0(?:x[0-9A-Fa-f](?:_?[0-9A-Fa-f])*|o[0-7](?:_?[0-7])*|b[01](?:_?[01])*)$")
_FLOAT = re.compile(
    r"^[+-]?(?:(?:0|[1-9](?:_?[0-9])*)\.(?:[0-9](?:_?[0-9])*)"
    r"(?:[eE][+-]?[0-9](?:_?[0-9])*)?|"
    r"(?:0|[1-9](?:_?[0-9])*)[eE][+-]?[0-9](?:_?[0-9])*)$"
)


def load(path: str, *, force_fallback: bool = False) -> Dict[str, Any]:
    """Read a UTF-8 TOML document from *path*."""
    with open(path, "rb") as handle:
        raw = handle.read()
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise TOMLError("TOML must be UTF-8: %s" % exc) from exc
    return loads(text, force_fallback=force_fallback)


def loads(text: str, *, force_fallback: bool = False) -> Dict[str, Any]:
    """Parse TOML, using the standard library unless fallback is requested."""
    if not isinstance(text, str):
        raise TypeError("TOML input must be text")
    if _stdlib_toml is not None and not force_fallback:
        try:
            return _stdlib_toml.loads(text)
        except Exception as exc:
            raise TOMLError(str(exc)) from exc
    return loads_fallback(text)


def loads_fallback(text: str) -> Dict[str, Any]:
    """Parse Chatmesh's supported TOML subset without third-party modules."""
    root: Dict[str, Any] = {}
    current: MutableMapping[str, Any] = root
    for line_no, statement in _statements(text):
        try:
            if statement.startswith("[["):
                if not statement.endswith("]]"):
                    raise TOMLError("unterminated array-of-tables header")
                path = _parse_key_path(statement[2:-2].strip())
                if not path:
                    raise TOMLError("empty array-of-tables header")
                current = _open_array_table(root, path)
            elif statement.startswith("["):
                if not statement.endswith("]"):
                    raise TOMLError("unterminated table header")
                path = _parse_key_path(statement[1:-1].strip())
                if not path:
                    raise TOMLError("empty table header")
                current = _open_table(root, path)
            else:
                split = _find_top_level(statement, "=")
                if split < 0:
                    raise TOMLError("expected key = value")
                key_text = statement[:split].strip()
                value_text = statement[split + 1 :].strip()
                if not key_text or not value_text:
                    raise TOMLError("expected key and value")
                path = _parse_key_path(key_text)
                value = _ValueParser(value_text).parse()
                _assign(current, path, value)
        except TOMLError as exc:
            raise TOMLError("line %d: %s" % (line_no, exc)) from exc
    return root


def dumps(document: Mapping[str, Any]) -> str:
    """Serialize a TOML-compatible mapping in deterministic key order."""
    if not isinstance(document, Mapping):
        raise TypeError("TOML document must be a mapping")
    lines: List[str] = []
    _emit_table(lines, (), document, include_header=False)
    return "\n".join(lines).rstrip() + "\n"


def dump(document: Mapping[str, Any], path: str) -> None:
    """Write deterministic UTF-8 TOML to *path*."""
    with open(path, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(dumps(document))


def _statements(text: str) -> Iterator[Tuple[int, str]]:
    parts: List[str] = []
    start_line = 1
    square = 0
    curly = 0
    for line_no, raw_line in enumerate(text.splitlines(), 1):
        clean = _strip_comment(raw_line).strip()
        if not clean:
            continue
        if not parts:
            start_line = line_no
        parts.append(clean)
        ds, dc = _nesting_delta(clean)
        square += ds
        curly += dc
        if square < 0 or curly < 0:
            raise TOMLError("line %d: unexpected closing delimiter" % line_no)
        if square == 0 and curly == 0:
            yield start_line, " ".join(parts)
            parts = []
    if parts:
        raise TOMLError("line %d: unterminated value" % start_line)


def _strip_comment(line: str) -> str:
    quote = ""
    escaped = False
    for index, char in enumerate(line):
        if quote == '"':
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                quote = ""
        elif quote == "'":
            if char == "'":
                quote = ""
        elif char in ('"', "'"):
            quote = char
        elif char == "#":
            return line[:index]
    if quote:
        raise TOMLError("unterminated string")
    return line


def _nesting_delta(text: str) -> Tuple[int, int]:
    square = 0
    curly = 0
    quote = ""
    escaped = False
    for char in text:
        if quote == '"':
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                quote = ""
        elif quote == "'":
            if char == "'":
                quote = ""
        elif char in ('"', "'"):
            quote = char
        elif char == "[":
            square += 1
        elif char == "]":
            square -= 1
        elif char == "{":
            curly += 1
        elif char == "}":
            curly -= 1
    return square, curly


def _find_top_level(text: str, needle: str) -> int:
    square = 0
    curly = 0
    quote = ""
    escaped = False
    for index, char in enumerate(text):
        if quote == '"':
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                quote = ""
        elif quote == "'":
            if char == "'":
                quote = ""
        elif char in ('"', "'"):
            quote = char
        elif char == "[":
            square += 1
        elif char == "]":
            square -= 1
        elif char == "{":
            curly += 1
        elif char == "}":
            curly -= 1
        elif char == needle and square == 0 and curly == 0:
            return index
    return -1


def _parse_key_path(text: str) -> List[str]:
    if not text:
        raise TOMLError("empty key")
    chunks: List[str] = []
    start = 0
    quote = ""
    escaped = False
    for index, char in enumerate(text):
        if quote == '"':
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                quote = ""
        elif quote == "'":
            if char == "'":
                quote = ""
        elif char in ('"', "'"):
            quote = char
        elif char == ".":
            chunks.append(text[start:index].strip())
            start = index + 1
    if quote:
        raise TOMLError("unterminated quoted key")
    chunks.append(text[start:].strip())
    path: List[str] = []
    for chunk in chunks:
        if not chunk:
            raise TOMLError("empty dotted key component")
        if chunk[0] in ('"', "'"):
            parser = _ValueParser(chunk)
            value = parser.parse()
            if not isinstance(value, str):
                raise TOMLError("quoted key must be a string")
            path.append(value)
        elif _BARE_KEY.match(chunk):
            path.append(chunk)
        else:
            raise TOMLError("invalid bare key %r" % chunk)
    return path


def _container_child(
    container: MutableMapping[str, Any], key: str
) -> MutableMapping[str, Any]:
    if key not in container:
        child: MutableMapping[str, Any] = {}
        container[key] = child
        return child
    value = container[key]
    if isinstance(value, dict):
        return value
    if isinstance(value, list) and value and isinstance(value[-1], dict):
        return value[-1]
    raise TOMLError("key %r is not a table" % key)


def _open_table(
    root: MutableMapping[str, Any], path: Sequence[str]
) -> MutableMapping[str, Any]:
    current = root
    for key in path:
        current = _container_child(current, key)
    return current


def _open_array_table(
    root: MutableMapping[str, Any], path: Sequence[str]
) -> MutableMapping[str, Any]:
    current = root
    for key in path[:-1]:
        current = _container_child(current, key)
    leaf = path[-1]
    if leaf not in current:
        current[leaf] = []
    value = current[leaf]
    if not isinstance(value, list):
        raise TOMLError("key %r is not an array of tables" % leaf)
    item: MutableMapping[str, Any] = {}
    value.append(item)
    return item


def _assign(
    current: MutableMapping[str, Any], path: Sequence[str], value: Any
) -> None:
    target = current
    for key in path[:-1]:
        target = _container_child(target, key)
    leaf = path[-1]
    if leaf in target:
        raise TOMLError("duplicate key %r" % leaf)
    target[leaf] = value


class _ValueParser:
    def __init__(self, text: str):
        self.text = text
        self.index = 0

    def parse(self) -> Any:
        value = self._value()
        self._space()
        if self.index != len(self.text):
            raise TOMLError("unexpected text after value")
        return value

    def _space(self) -> None:
        while self.index < len(self.text) and self.text[self.index].isspace():
            self.index += 1

    def _value(self) -> Any:
        self._space()
        if self.index >= len(self.text):
            raise TOMLError("missing value")
        char = self.text[self.index]
        if char == '"':
            return self._basic_string()
        if char == "'":
            return self._literal_string()
        if char == "[":
            return self._array()
        if char == "{":
            return self._inline_table()
        start = self.index
        while (
            self.index < len(self.text)
            and self.text[self.index] not in ",]}"
            and not self.text[self.index].isspace()
        ):
            self.index += 1
        token = self.text[start : self.index]
        if token == "true":
            return True
        if token == "false":
            return False
        normalized = token.replace("_", "")
        if _BASE_INT.match(token):
            sign = -1 if normalized.startswith("-") else 1
            unsigned = normalized[1:] if normalized[:1] in "+-" else normalized
            return sign * int(unsigned, 0)
        if _DECIMAL_INT.match(token):
            return int(normalized, 10)
        if _FLOAT.match(token):
            value = float(normalized)
            if not math.isfinite(value):
                raise TOMLError("non-finite floats are unsupported")
            return value
        raise TOMLError("unsupported value %r" % token)

    def _basic_string(self) -> str:
        start = self.index
        self.index += 1
        escaped = False
        while self.index < len(self.text):
            char = self.text[self.index]
            self.index += 1
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                token = self.text[start : self.index]
                try:
                    return json.loads(token)
                except (TypeError, ValueError) as exc:
                    raise TOMLError("invalid string escape") from exc
        raise TOMLError("unterminated string")

    def _literal_string(self) -> str:
        self.index += 1
        start = self.index
        end = self.text.find("'", self.index)
        if end < 0:
            raise TOMLError("unterminated literal string")
        self.index = end + 1
        return self.text[start:end]

    def _array(self) -> List[Any]:
        self.index += 1
        result: List[Any] = []
        self._space()
        if self._take("]"):
            return result
        while True:
            result.append(self._value())
            self._space()
            if self._take("]"):
                return result
            if not self._take(","):
                raise TOMLError("expected ',' or ']' in array")
            self._space()
            if self._take("]"):
                return result

    def _inline_table(self) -> Dict[str, Any]:
        self.index += 1
        result: Dict[str, Any] = {}
        self._space()
        if self._take("}"):
            return result
        while True:
            start = self.index
            split = -1
            quote = ""
            escaped = False
            while self.index < len(self.text):
                char = self.text[self.index]
                if quote == '"':
                    if escaped:
                        escaped = False
                    elif char == "\\":
                        escaped = True
                    elif char == '"':
                        quote = ""
                elif quote == "'":
                    if char == "'":
                        quote = ""
                elif char in ('"', "'"):
                    quote = char
                elif char == "=":
                    split = self.index
                    break
                self.index += 1
            if split < 0:
                raise TOMLError("expected '=' in inline table")
            path = _parse_key_path(self.text[start:split].strip())
            self.index = split + 1
            value = self._value()
            _assign(result, path, value)
            self._space()
            if self._take("}"):
                return result
            if not self._take(","):
                raise TOMLError("expected ',' or '}' in inline table")
            self._space()

    def _take(self, char: str) -> bool:
        if self.index < len(self.text) and self.text[self.index] == char:
            self.index += 1
            return True
        return False


def _format_key(key: str) -> str:
    if not isinstance(key, str) or not key:
        raise TOMLError("TOML keys must be nonempty strings")
    return key if _BARE_KEY.match(key) else json.dumps(key, ensure_ascii=False)


def _format_path(path: Sequence[str]) -> str:
    return ".".join(_format_key(part) for part in path)


def _is_table_array(value: Any) -> bool:
    return (
        isinstance(value, list)
        and bool(value)
        and all(isinstance(item, Mapping) for item in value)
    )


def _format_value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        if not math.isfinite(value):
            raise TOMLError("cannot encode non-finite float")
        return repr(value)
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, (list, tuple)):
        if any(isinstance(item, Mapping) for item in value):
            raise TOMLError("arrays may not mix tables and scalar values")
        return "[" + ", ".join(_format_value(item) for item in value) + "]"
    if isinstance(value, Mapping):
        fields = []
        for key in sorted(value):
            fields.append("%s = %s" % (_format_key(key), _format_value(value[key])))
        return "{ " + ", ".join(fields) + " }"
    raise TOMLError("cannot encode value of type %s" % type(value).__name__)


def _emit_table(
    lines: List[str],
    path: Sequence[str],
    table: Mapping[str, Any],
    *,
    include_header: bool,
) -> None:
    scalar_keys = [
        key
        for key, value in table.items()
        if not isinstance(value, Mapping) and not _is_table_array(value)
    ]
    child_tables = [
        key for key, value in table.items() if isinstance(value, Mapping)
    ]
    table_arrays = [
        key for key, value in table.items() if _is_table_array(value)
    ]
    if include_header:
        if lines and lines[-1] != "":
            lines.append("")
        lines.append("[%s]" % _format_path(path))
    for key in sorted(scalar_keys):
        lines.append("%s = %s" % (_format_key(key), _format_value(table[key])))
    for key in sorted(child_tables):
        _emit_table(
            lines,
            tuple(path) + (key,),
            table[key],
            include_header=True,
        )
    for key in sorted(table_arrays):
        array_path = tuple(path) + (key,)
        for item in table[key]:
            if lines and lines[-1] != "":
                lines.append("")
            lines.append("[[%s]]" % _format_path(array_path))
            _emit_array_item(lines, array_path, item)


def _emit_array_item(
    lines: List[str], path: Sequence[str], item: Mapping[str, Any]
) -> None:
    for key in sorted(item):
        value = item[key]
        if isinstance(value, Mapping) or _is_table_array(value):
            continue
        lines.append("%s = %s" % (_format_key(key), _format_value(value)))
    for key in sorted(item):
        value = item[key]
        if isinstance(value, Mapping):
            _emit_table(
                lines, tuple(path) + (key,), value, include_header=True
            )
        elif _is_table_array(value):
            nested = tuple(path) + (key,)
            for child in value:
                if lines and lines[-1] != "":
                    lines.append("")
                lines.append("[[%s]]" % _format_path(nested))
                _emit_array_item(lines, nested, child)
