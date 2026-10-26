"""Edit a TOML file in place without reformatting the rest of it.

The stdlib can parse TOML and cannot write it. The dashboard changes a few
keys — a box size, a return address — and must leave comments, secrets, and
key order alone. A full rewrite would shuffle a file the shop has been
editing by hand.

This is a line editor, not a general formatter. It assumes table headers are
on their own lines, which is how config.toml is written. Values may span
lines (arrays); those are replaced with a single line.
"""

from __future__ import annotations

import re
from dataclasses import dataclass


class TomlEditError(ValueError):
    pass


_BARE_KEY = re.compile(r"[A-Za-z0-9_-]+")


def render_value(value) -> str:
    """Render a Python value as a TOML value on one line."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int) and not isinstance(value, bool):
        return str(value)
    if isinstance(value, float):
        return _format_float(value)
    if isinstance(value, str):
        return _quote(value)
    if isinstance(value, (list, tuple)):
        return "[" + ", ".join(render_value(item) for item in value) + "]"
    raise TomlEditError(f"cannot write a {type(value).__name__} into TOML")


def set_key(text: str, table: str | None, key: str, value) -> str:
    """Set ``key`` inside ``[table]`` (None is the top of the file).

    ``value is None`` deletes the key. A missing table is created at the
    end of the file. Existing comments on other lines are left where they
    are; an inline comment on the key's own line is kept.
    """
    lines = _lines(text)
    section = _find_section(lines, table)
    rendered = None if value is None else render_value(value)
    if section is None:
        if value is None:
            return text
        if not table:
            raise TomlEditError(f"no top-level key {key!r} to set")
        return _append_table(text, table, [(key, rendered)])

    found = _locate_key(lines, section.body_start, section.end, key)
    if found is None:
        if value is None:
            return text
        insert_at = _last_content(lines, section.body_start, section.end) + 1
        # A brand-new table has no body yet: body_start == end, and
        # _last_content returns body_start - 1, so insert_at is the header's
        # next line. For a table that already has keys, insert after them.
        if section.header_line is not None and insert_at < section.body_start:
            insert_at = section.body_start
        lines.insert(insert_at, f"{_format_key(key)} = {rendered}\n")
        return "".join(lines)

    start, end, comment = found
    if value is None:
        del lines[start:end]
        return "".join(lines)

    indent = re.match(r"[ \t]*", lines[start]).group(0)
    token = _key_token(lines[start], key)
    suffix = ""
    if comment is not None and end == start + 1:
        suffix = "  " + lines[start][comment:].rstrip("\r\n")
    newline = "\n" if lines[start].endswith("\n") or text.endswith("\n") or end > start else ""
    # Keep a trailing newline whenever the original line had one, and whenever
    # the value we replaced spanned more than that line.
    if lines[start].endswith("\n") or end > start + 1 or start < len(lines) - 1:
        newline = "\n"
    lines[start:end] = [f"{indent}{token} = {rendered}{suffix}{newline}"]
    return "".join(lines)


def delete_table(text: str, table: str) -> str:
    """Remove ``[table]`` and its keys. Surrounding tables stay put."""
    lines = _lines(text)
    section = _find_section(lines, table)
    if section is None or section.header_line is None:
        return text
    del lines[section.header_line:section.end]
    return "".join(lines)


def _format_float(number: float) -> str:
    if number != number or number in (float("inf"), float("-inf")):
        raise TomlEditError(f"cannot write {number!r} into TOML")
    text = f"{number:.6f}".rstrip("0").rstrip(".")
    if "." not in text:
        text += ".0"
    return text


def _quote(text: str) -> str:
    if "\n" in text or "\r" in text:
        raise TomlEditError("TOML strings written here must be a single line")
    escaped = text.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def _format_key(key: str) -> str:
    if _BARE_KEY.fullmatch(key):
        return key
    return _quote(key)


def _append_table(text: str, table: str, pairs: list[tuple[str, str]]) -> str:
    chunk = _format_header(table) + "\n"
    chunk += "".join(f"{_format_key(key)} = {rendered}\n" for key, rendered in pairs)
    if not text:
        return chunk
    if not text.endswith("\n"):
        text += "\n"
    if not text.endswith("\n\n"):
        text += "\n"
    return text + chunk


def _format_header(table: str) -> str:
    parts = []
    for part in table.split("."):
        parts.append(part if _BARE_KEY.fullmatch(part) else _quote(part))
    return "[" + ".".join(parts) + "]"


@dataclass
class _Section:
    parts: tuple[str, ...] | None
    header_line: int | None
    body_start: int
    end: int


def _lines(text: str) -> list[str]:
    if text == "":
        return []
    return text.splitlines(keepends=True)


def _sections(lines: list[str]) -> list[_Section]:
    headers: list[tuple[int, tuple[str, ...]]] = []
    for index, line in enumerate(lines):
        parts = _header_parts(line)
        if parts is not None:
            headers.append((index, parts))
    if not headers:
        return [_Section(None, None, 0, len(lines))]
    sections = [_Section(None, None, 0, headers[0][0])]
    for index, (line_no, parts) in enumerate(headers):
        end = headers[index + 1][0] if index + 1 < len(headers) else len(lines)
        sections.append(_Section(parts, line_no, line_no + 1, end))
    return sections


def _find_section(lines: list[str], table: str | None) -> _Section | None:
    want = None if not table else tuple(table.split("."))
    for section in _sections(lines):
        if section.parts == want:
            return section
    return None


def _header_parts(line: str) -> tuple[str, ...] | None:
    stripped = line.strip()
    if not stripped.startswith("["):
        return None
    # Array-of-tables. Nothing in this project uses them; keep their keys
    # out of the previous table by giving them a name nothing asks for.
    if stripped.startswith("[["):
        return ("[[", stripped)
    body, _comment = _split_comment(stripped)
    body = body.strip()
    if not (body.startswith("[") and body.endswith("]")):
        return None
    return tuple(_split_dotted(body[1:-1]))


def _split_dotted(body: str) -> list[str]:
    parts: list[str] = []
    buf: list[str] = []
    quote = ""
    i = 0
    while i < len(body):
        char = body[i]
        if quote:
            if char == quote:
                quote = ""
            else:
                buf.append(char)
            i += 1
            continue
        if char in "\"'":
            quote = char
            i += 1
            continue
        if char == ".":
            parts.append("".join(buf).strip())
            buf = []
            i += 1
            continue
        buf.append(char)
        i += 1
    parts.append("".join(buf).strip())
    return parts


def _locate_key(lines: list[str], start: int, end: int, key: str):
    """Return (first_line, last_line_exclusive, comment_index) or None.

    comment_index is an index into the first line, or None.
    """
    index = start
    while index < end:
        line = lines[index]
        stripped = line.lstrip()
        if not stripped or stripped.startswith("#"):
            index += 1
            continue
        match = _KEY_RE.match(line)
        if match is None or _decode_key(match.group("key")) != key:
            index += 1
            continue
        blob = "".join(lines[index:end])
        value_at = match.end()
        # match.end() is relative to this line; the blob starts at this line.
        consumed = _scan_value(blob[value_at:])
        if consumed < 0:
            raise TomlEditError(f"could not parse the value of {key!r}")
        absolute = value_at + consumed
        comment_at = _inline_comment_at(blob, absolute)
        # How many lines does the value occupy?
        head = blob[:absolute]
        extra = head.count("\n")
        return index, index + extra + 1, comment_at
    return None


_KEY_RE = re.compile(
    r"""^[ \t]*(?P<key>"(?:\\.|[^"\\])*"|'(?:[^'])*'|[A-Za-z0-9_-]+)[ \t]*="""
)


def _key_token(line: str, key: str) -> str:
    match = _KEY_RE.match(line)
    if match and _decode_key(match.group("key")) == key:
        return match.group("key")
    return _format_key(key)


def _decode_key(token: str) -> str:
    if len(token) >= 2 and token[0] == token[-1] and token[0] in "\"'":
        inner = token[1:-1]
        if token[0] == '"':
            return inner.replace('\\"', '"').replace("\\\\", "\\")
        return inner
    return token


def _inline_comment_at(blob: str, value_end: int) -> int | None:
    """Index of '#' that comments the value, if it sits on the value's last line."""
    line_start = blob.rfind("\n", 0, value_end) + 1
    i = value_end
    while i < len(blob) and blob[i] in " \t":
        i += 1
    if i < len(blob) and blob[i] == "#" and "\n" not in blob[line_start:i]:
        return i
    return None


def _split_comment(text: str) -> tuple[str, str]:
    """Split a header line into code and comment. '#' inside quotes stays."""
    quote = ""
    i = 0
    while i < len(text):
        char = text[i]
        if quote:
            if char == "\\":
                i += 2
                continue
            if char == quote:
                quote = ""
            i += 1
            continue
        if char in "\"'":
            quote = char
            i += 1
            continue
        if char == "#":
            return text[:i], text[i:]
        i += 1
    return text, ""


def _last_content(lines: list[str], start: int, end: int) -> int:
    """Index of the last non-blank line in [start, end), or start - 1."""
    index = end - 1
    while index >= start:
        if lines[index].strip():
            return index
        index -= 1
    return start - 1


def _scan_value(text: str) -> int:
    """Length of a TOML value at the start of ``text``.

    Leading spaces on the first line are included. A value that begins on
    the next line (a multiline array) is included too. -1 if it never closes
    or there is no value.
    """
    i = 0
    while i < len(text) and text[i] in " \t":
        i += 1
    if i < len(text) and text[i] in "\r\n":
        j = i
        while j < len(text) and text[j] in " \t\r\n":
            j += 1
        # Only treat a following container as this value. A following key
        # means this key's value was empty, which is not TOML we can edit.
        if j < len(text) and text[j] in "[{":
            i = j
        else:
            return -1
    if i >= len(text) or text[i] in "\r\n#":
        return -1
    end = _scan_at(text, i)
    if end < 0:
        return -1
    return end


def _scan_at(text: str, i: int) -> int:
    char = text[i]
    if char == '"':
        return _scan_string(text, i, '"', '"""')
    if char == "'":
        return _scan_string(text, i, "'", "'''")
    if char == "[":
        return _scan_container(text, i, "[", "]")
    if char == "{":
        return _scan_container(text, i, "{", "}")
    j = i
    while j < len(text) and text[j] not in " \t\r\n#,]}":
        j += 1
    return j if j > i else -1


def _scan_string(text: str, i: int, quote: str, multi: str) -> int:
    if text.startswith(multi, i):
        j = i + 3
        while j < len(text):
            if quote == '"' and text[j] == "\\":
                j += 2
                continue
            if text.startswith(multi, j):
                return j + 3
            j += 1
        return -1
    j = i + 1
    while j < len(text):
        if quote == '"' and text[j] == "\\":
            j += 2
            continue
        if text[j] == quote:
            return j + 1
        if text[j] == "\n":
            return -1
        j += 1
    return -1


def _scan_container(text: str, i: int, open_c: str, close_c: str) -> int:
    j = i + 1
    depth = 1
    while j < len(text):
        if text.startswith('"""', j) or text.startswith("'''", j):
            end = _scan_string(text, j, text[j], text[j] * 3)
            if end < 0:
                return -1
            j = end
            continue
        if text[j] in "\"'":
            end = _scan_string(text, j, text[j], text[j] * 3)
            if end < 0:
                return -1
            j = end
            continue
        if text[j] == open_c:
            depth += 1
        elif text[j] == close_c:
            depth -= 1
            if depth == 0:
                return j + 1
        j += 1
    return -1
