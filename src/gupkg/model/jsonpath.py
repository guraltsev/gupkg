"""Evaluate the small JSONPath subset that the ``json`` provider and checksum discovery use.

A path is a dotted chain of steps, optionally starting with ``$``. A step is a key
(``.name`` or ``['name']``), an array position (``[0]``, ``[-1]``), every child
(``.*`` or ``[*]``), or a filter (``[?key==value]``, ``!=`` and ``=~`` too, and ``&&`` to require several conditions). A path may
end in ``~``, which yields the key or position of each node instead of its value.
Nothing else is supported: no recursion, slices or scripts, so a path can never run
code or take unbounded time.

Usage and API
-------------
``parse_path(text)`` returns a ``JsonPath`` or raises ``JsonPathError``.
``JsonPath.select(root)`` returns the matching ``Node`` list, ``JsonPath.values(root)``
the matching values, and ``condition_holds(text, node)`` evaluates a stand-alone
condition such as ``stable==true`` on a node. Evaluation is bounded by ``MAX_NODES``.

Implementation Approach
-----------------------
The path is parsed once into a tuple of steps. Evaluation keeps a list of
``(key, value)`` nodes and replaces it step by step, so a filter or wildcard multiplies
nodes only up to the cap. Filter conditions are themselves parsed as relative paths
compared with a literal.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from .patterns import PatternError, compile_pattern

MAX_NODES = 100_000
_NAME = re.compile(r"[A-Za-z0-9_\-]+")
_NUMBER = re.compile(r"-?\d+(\.\d+)?")


class JsonPathError(ValueError):
    """Report a path or condition that is not in the supported subset."""


@dataclass(frozen=True)
class Node:
    """One selected value together with the key or position it was found under."""

    key: Any
    value: Any


@dataclass(frozen=True)
class _Condition:
    """A filter ``<relative path> <operator> <literal>``."""

    path: "JsonPath"
    operator: str
    literal: Any
    pattern: Any = None


@dataclass(frozen=True)
class JsonPath:
    """A parsed path: steps and whether the result is the keys of the selected nodes."""

    steps: tuple[tuple[str, Any], ...]
    wants_key: bool = False

    def select(self, root: Any) -> list[Node]:
        """Return the nodes selected from *root*, in document order.

        Raises
        ------
        JsonPathError
            If more than ``MAX_NODES`` nodes would be produced.
        """
        return self.select_from(Node(None, root))

    def select_from(self, start: Node) -> list[Node]:
        """Return the nodes selected from the node *start*, whose key a ``~`` path can yield."""
        nodes = [start]
        for kind, argument in self.steps:
            following: list[Node] = []
            for node in nodes:
                following.extend(_apply(kind, argument, node))
                if len(following) > MAX_NODES:
                    raise JsonPathError(f"the path selects more than {MAX_NODES} nodes")
            nodes = following
        if self.wants_key:
            return [Node(node.key, node.key) for node in nodes]
        return nodes

    def values(self, root: Any) -> list[Any]:
        """Return the values selected from *root* (the keys when the path ends in ``~``)."""
        return [node.value for node in self.select(root)]


def parse_path(text: str) -> JsonPath:
    """Parse *text* into a ``JsonPath``.

    Parameters
    ----------
    text : str
        A path such as ``$.a.b[0]``, ``files[?os==winnt].url`` or ``~``.

    Raises
    ------
    JsonPathError
        If the text is empty or uses syntax outside the subset.
    """
    source = text.strip()
    if not source:
        raise JsonPathError("the path is empty")
    wants_key = False
    if source.endswith("~"):
        wants_key, source = True, source[:-1]
    position = 1 if source.startswith("$") else 0
    steps: list[tuple[str, Any]] = []
    first = True

    # Read one step at a time: a dotted name, a wildcard, or a bracket expression.
    while position < len(source):
        character = source[position]
        if character == ".":
            position += 1
            if source[position : position + 1] == "*":
                steps.append(("all", None))
                position += 1
                continue
            match = _NAME.match(source, position)
            if match is None:
                raise JsonPathError(f"{text!r}: expected a key after '.' at position {position}")
            steps.append(("key", match.group(0)))
            position = match.end()
        elif character == "[":
            end = _closing_bracket(source, position, text)
            steps.append(_bracket_step(source[position + 1 : end].strip(), text))
            position = end + 1
        elif first and _NAME.match(source, position):
            match = _NAME.match(source, position)
            steps.append(("key", match.group(0)))
            position = match.end()
        else:
            raise JsonPathError(f"{text!r}: unexpected {character!r} at position {position}")
        first = False
    if not steps and not wants_key and source not in ("", "$"):
        raise JsonPathError(f"{text!r} is not a valid path")
    return JsonPath(tuple(steps), wants_key)


def condition_holds(text: str, node: Any) -> bool:
    """Return whether the condition *text* (``path==literal``, ``!=`` or ``=~``) holds for the value *node*.

    A path that selects nothing makes ``==`` and ``=~`` false and ``!=`` true.

    Raises
    ------
    JsonPathError
        If *text* is not a valid condition.
    """
    return evaluate_condition(parse_condition(text), node)


def evaluate_condition(condition: "_Condition", node: Any) -> bool:
    """Evaluate a condition parsed by ``parse_condition`` on a node, so a loop parses it once."""
    return _evaluate(condition, node)


def parse_condition(text: str) -> _Condition:
    """Parse a stand-alone condition such as ``stable==true`` or ``~ =~ \\d+\\.\\d+``.

    Raises
    ------
    JsonPathError
        If there is no operator, the path is invalid, or a regular expression is unsafe.
    """
    split = _split_condition(text)
    if split is None:
        raise JsonPathError(f"{text!r} is not a condition: write <path>==<value>, <path>!=<value> or <path>=~<regex>")
    path_text, operator, literal = split
    path = parse_path(path_text)
    if operator == "=~":
        try:
            return _Condition(path, operator, literal, compile_pattern(literal))
        except PatternError as error:
            raise JsonPathError(str(error)) from error
    return _Condition(path, operator, _parse_literal(literal))


# ---------------------------------------------------------------------------
# Evaluation
# ---------------------------------------------------------------------------


def _split_condition(text: str) -> tuple[str, str, str] | None:
    """Split ``path op literal`` at the first operator that is outside brackets and quotes."""
    depth, quote = 0, ""
    for index, character in enumerate(text):
        if quote:
            quote = "" if character == quote else quote
        elif character in "'\"":
            quote = character
        elif character == "[":
            depth += 1
        elif character == "]":
            depth -= 1
        elif depth == 0 and text[index : index + 2] in ("==", "!=", "=~"):
            return text[:index].strip(), text[index : index + 2], text[index + 2 :].strip()
    return None


def _split_and(text: str) -> list[str]:
    """Split a filter body at every ``&&`` that is outside brackets and quotes."""
    parts, depth, quote, start, index = [], 0, "", 0, 0
    while index < len(text):
        character = text[index]
        if quote:
            quote = "" if character == quote else quote
        elif character in "'\"":
            quote = character
        elif character == "[":
            depth += 1
        elif character == "]":
            depth -= 1
        elif depth == 0 and text[index : index + 2] == "&&":
            parts.append(text[start:index])
            start, index = index + 2, index + 1
        index += 1
    parts.append(text[start:])
    return parts


def _closing_bracket(source: str, start: int, whole: str) -> int:
    """Return the index of the ``]`` that closes the bracket at *start*, skipping quoted text."""
    quote = ""
    for index in range(start + 1, len(source)):
        character = source[index]
        if quote:
            if character == quote:
                quote = ""
        elif character in "'\"":
            quote = character
        elif character == "]":
            return index
    raise JsonPathError(f"{whole!r}: a '[' is never closed")


def _bracket_step(inner: str, whole: str) -> tuple[str, Any]:
    """Return the step for the text between ``[`` and ``]``."""
    if inner == "*":
        return ("all", None)
    if re.fullmatch(r"-?\d+", inner):
        return ("index", int(inner))
    if len(inner) >= 2 and inner[0] == inner[-1] and inner[0] in "'\"":
        return ("key", inner[1:-1])
    if inner.startswith("?"):
        return ("filter", tuple(parse_condition(part) for part in _split_and(inner[1:])))
    raise JsonPathError(f"{whole!r}: unsupported bracket expression [{inner}]")


def _parse_literal(text: str) -> Any:
    """Return a condition literal: ``true``, ``false``, ``null``, a number, a quoted or a bare string."""
    if text in ("true", "false", "null"):
        return {"true": True, "false": False, "null": None}[text]
    if _NUMBER.fullmatch(text):
        return float(text) if "." in text else int(text)
    if len(text) >= 2 and text[0] == text[-1] and text[0] in "'\"":
        return text[1:-1]
    return text


def _children(node: Node) -> list[Node]:
    """Return the children of an object or array node; scalars have none."""
    value = node.value
    if isinstance(value, dict):
        return [Node(key, item) for key, item in value.items()]
    if isinstance(value, list):
        return [Node(index, item) for index, item in enumerate(value)]
    return []


def _apply(kind: str, argument: Any, node: Node) -> list[Node]:
    """Apply one step to one node."""
    value = node.value
    if kind == "key":
        if isinstance(value, dict) and argument in value:
            return [Node(argument, value[argument])]
        return []
    if kind == "index":
        if isinstance(value, list) and -len(value) <= argument < len(value):
            return [Node(argument % len(value), value[argument])]
        return []
    if kind == "all":
        return _children(node)
    return [child for child in _children(node) if all(_evaluate(condition, child) for condition in argument)]


def _same(value: Any, literal: Any) -> bool:
    """Compare a JSON value with a literal, treating ``1`` and ``"1"`` as equal."""
    if isinstance(value, bool) or isinstance(literal, bool):
        return value is literal
    if isinstance(value, str) and isinstance(literal, (int, float)):
        return value == str(literal)
    return value == literal


def _evaluate(condition: _Condition, node: Any) -> bool:
    """Evaluate a parsed condition on a node (or on a raw value)."""
    target = node if isinstance(node, Node) else Node(None, node)
    selected = [item.value for item in condition.path.select_from(target)]
    if condition.operator == "==":
        return any(_same(item, condition.literal) for item in selected)
    if condition.operator == "!=":
        return not any(_same(item, condition.literal) for item in selected)
    return any(isinstance(item, (str, int, float)) and condition.pattern.fullmatch(str(item)) for item in selected)
