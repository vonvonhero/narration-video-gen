"""Minimal YAML subset loader (stdlib only).

The CLI must run before any container or virtualenv exists, so it cannot depend
on PyYAML. ``narration_video_gen.compat.load_yaml`` prefers PyYAML when it is importable
and falls back to this module otherwise.

Supported subset -- deliberately small, because every file in this repository is
written against it:

* block mappings and block sequences, nested by indentation
* ``- `` sequence items holding a scalar, a nested mapping, or a nested sequence
* flow sequences on one line: ``[a, b, c]`` (scalar items only)
* flow mappings on one line: ``{a: 1, b: 2}`` (scalar values only)
* JSON documents (JSON is a YAML 1.2 subset)
* scalars: int, float, ``true``/``false``, ``null``/``~``, quoted strings
* literal block scalars ``|`` and folded ``>``
* ``#`` comments outside quotes, and ``---`` document start

Not supported: anchors, aliases, tags, multiple documents, complex keys.
Anything unsupported raises :class:`YamlError` rather than silently mis-parsing.
"""

from __future__ import annotations

import json
import re

__all__ = ["YamlError", "safe_load"]


class YamlError(ValueError):
    """Raised when the input uses YAML features outside the supported subset."""


_INT_RE = re.compile(r"^[+-]?\d+$")
# A block scalar header is the last thing on its line: "|", ">", optionally
# followed by a chomping indicator ("-" or "+").
_BLOCK_SCALAR_RE = re.compile(r"(?P<style>[|>])(?P<chomp>[-+]?)$")
_FLOAT_RE = re.compile(r"^[+-]?(\d+\.\d*|\.\d+|\d+)([eE][+-]?\d+)?$")


def safe_load(text):
    """Parse ``text`` and return the resulting Python object (or ``None``)."""
    stripped = text.lstrip()
    if stripped.startswith(("{", "[")):
        try:
            return json.loads(text)
        except json.JSONDecodeError as exc:
            raise YamlError("invalid JSON document: %s" % exc) from exc
    lines = _tokenize(text)
    if not lines:
        return None
    value, index = _parse_block(lines, 0, lines[0][0])
    if index != len(lines):
        raise YamlError("line %d: unexpected indentation" % lines[index][2])
    return value


# --------------------------------------------------------------------------
# tokenizing
# --------------------------------------------------------------------------

def _tokenize(text):
    """Return ``[(indent, content, lineno), ...]`` with blanks/comments removed.

    Literal block scalars are folded into the token that introduces them, so the
    block parser never sees their raw lines.
    """
    raw = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    tokens = []
    i = 0
    while i < len(raw):
        line = raw[i]
        lineno = i + 1
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or stripped in ("---", "..."):
            i += 1
            continue
        indent = len(line) - len(line.lstrip(" "))
        if "\t" in line[:indent]:
            raise YamlError("line %d: tabs are not allowed for indentation" % lineno)
        content = _strip_comment(stripped)
        block_match = _BLOCK_SCALAR_RE.search(content)
        if block_match:
            style = block_match.group("style")
            chomp = block_match.group("chomp")
            head = content[: block_match.start()].rstrip()
            body, i = _consume_block_scalar(raw, i + 1, indent)
            if style == ">":
                # Folded: blank lines separate paragraphs, everything else joins.
                joined = _fold(body)
            else:
                joined = "\n".join(body)
            if chomp != "-" and joined:
                joined += "\n"
            tokens.append((indent, head, lineno, joined))
            continue
        tokens.append((indent, content, lineno, None))
        i += 1
    return tokens


def _consume_block_scalar(raw, start, parent_indent):
    """Collect the indented body of a block scalar starting at ``start``."""
    body = []
    i = start
    block_indent = None
    while i < len(raw):
        line = raw[i]
        if not line.strip():
            body.append("")
            i += 1
            continue
        indent = len(line) - len(line.lstrip(" "))
        if indent <= parent_indent:
            break
        if block_indent is None:
            block_indent = indent
        body.append(line[block_indent:])
        i += 1
    while body and not body[-1]:
        body.pop()
    return body, i


def _fold(body):
    """Join a folded block scalar: blank lines become paragraph breaks."""
    paragraphs = []
    current = []
    for line in body:
        if line.strip():
            current.append(line.strip())
        elif current:
            paragraphs.append(" ".join(current))
            current = []
    if current:
        paragraphs.append(" ".join(current))
    return "\n".join(paragraphs)


def _strip_comment(text):
    """Drop a trailing ``#`` comment that is not inside quotes."""
    quote = None
    for pos, ch in enumerate(text):
        if quote:
            if ch == quote:
                quote = None
        elif ch in ("'", '"'):
            quote = ch
        elif ch == "#" and (pos == 0 or text[pos - 1] in " \t"):
            return text[:pos].rstrip()
    return text.rstrip()


# --------------------------------------------------------------------------
# block parsing
# --------------------------------------------------------------------------

def _parse_block(tokens, index, indent):
    if tokens[index][1].startswith("- ") or tokens[index][1] == "-":
        return _parse_sequence(tokens, index, indent)
    return _parse_mapping(tokens, index, indent)


def _parse_mapping(tokens, index, indent):
    result = {}
    while index < len(tokens):
        cur_indent, content, lineno, block = tokens[index]
        if cur_indent < indent:
            break
        if cur_indent > indent:
            raise YamlError("line %d: unexpected indentation in mapping" % lineno)
        key, sep, rest = _split_key(content, lineno)
        if not sep:
            raise YamlError("line %d: expected 'key: value'" % lineno)
        rest = rest.strip()
        index += 1
        if block is not None:
            result[key] = block
        elif rest:
            result[key] = _parse_scalar(rest, lineno)
        elif index < len(tokens) and tokens[index][0] > indent:
            result[key], index = _parse_block(tokens, index, tokens[index][0])
        elif (index < len(tokens) and tokens[index][0] == indent
                and (tokens[index][1] == "-" or tokens[index][1].startswith("- "))):
            # sequence written at the same indentation as its key
            result[key], index = _parse_sequence(tokens, index, indent)
        else:
            result[key] = None
    return result, index


def _parse_sequence(tokens, index, indent):
    result = []
    while index < len(tokens):
        cur_indent, content, lineno, block = tokens[index]
        if cur_indent < indent:
            break
        if cur_indent > indent:
            raise YamlError("line %d: unexpected indentation in sequence" % lineno)
        if not (content == "-" or content.startswith("- ")):
            break
        item = content[1:].strip()
        index += 1
        if block is not None:
            result.append(block)
            continue
        if not item:
            if index < len(tokens) and tokens[index][0] > cur_indent:
                value, index = _parse_block(tokens, index, tokens[index][0])
                result.append(value)
            else:
                result.append(None)
            continue
        key, sep, rest = _split_key(item, lineno)
        if sep:
            # "- key: value" starts a mapping whose columns line up after "- "
            inner_indent = cur_indent + 2
            synthetic = [(inner_indent, item, lineno, None)]
            while index < len(tokens) and tokens[index][0] >= inner_indent:
                synthetic.append(tokens[index])
                index += 1
            value, consumed = _parse_mapping(synthetic, 0, inner_indent)
            if consumed != len(synthetic):
                raise YamlError("line %d: unparsed content in sequence item" % lineno)
            result.append(value)
        else:
            result.append(_parse_scalar(item, lineno))
    return result, index


def _split_key(content, lineno):
    """Split ``content`` into ``(key, found_separator, rest)``."""
    quote = None
    for pos, ch in enumerate(content):
        if quote:
            if ch == quote:
                quote = None
        elif ch in ("'", '"'):
            quote = ch
        elif ch in "[{":
            break
        elif ch == ":" and (pos + 1 == len(content) or content[pos + 1] in " \t"):
            key = content[:pos].strip()
            if len(key) >= 2 and key[0] == key[-1] and key[0] in ("'", '"'):
                key = key[1:-1]
            return key, True, content[pos + 1:]
    return content, False, ""


# --------------------------------------------------------------------------
# scalars
# --------------------------------------------------------------------------

def _parse_scalar(text, lineno):
    text = text.strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in ("'", '"'):
        body = text[1:-1]
        if text[0] == '"':
            body = body.replace('\\"', '"').replace("\\n", "\n").replace("\\\\", "\\")
        else:
            body = body.replace("''", "'")
        return body
    if text.startswith("[") and text.endswith("]"):
        inner = text[1:-1].strip()
        if not inner:
            return []
        return [_parse_scalar(part, lineno) for part in _split_flow(inner, lineno)]
    if text.startswith("{") and text.endswith("}"):
        inner = text[1:-1].strip()
        if not inner:
            return {}
        out = {}
        for part in _split_flow(inner, lineno):
            key, sep, rest = _split_key(part, lineno)
            if not sep:
                raise YamlError("line %d: expected 'key: value' in flow mapping" % lineno)
            out[key] = _parse_scalar(rest, lineno)
        return out
    lowered = text.lower()
    if lowered in ("true", "yes", "on"):
        return True
    if lowered in ("false", "no", "off"):
        return False
    if lowered in ("null", "~", ""):
        return None
    if _INT_RE.match(text):
        return int(text)
    if _FLOAT_RE.match(text):
        return float(text)
    return text


def _split_flow(text, lineno):
    """Split a flow collection body on commas that are not inside quotes/brackets."""
    parts = []
    depth = 0
    quote = None
    current = []
    for ch in text:
        if quote:
            current.append(ch)
            if ch == quote:
                quote = None
            continue
        # A quote character only opens a quoted scalar at the start of an item.
        # Mid-item it is an ordinary apostrophe -- "the author's own work" is a
        # plain scalar, not an unterminated string.
        if ch in ("'", '"') and not current:
            quote = ch
            current.append(ch)
        elif ch in "[{":
            depth += 1
            current.append(ch)
        elif ch in "]}":
            depth -= 1
            current.append(ch)
        elif ch == "," and depth == 0:
            parts.append("".join(current).strip())
            current = []
        else:
            current.append(ch)
    if quote:
        raise YamlError("line %d: unterminated quote in flow collection" % lineno)
    tail = "".join(current).strip()
    if tail:
        parts.append(tail)
    return parts
