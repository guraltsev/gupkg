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
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any

from .patterns import PatternError, compile_pattern

# Upper bound on the nodes one evaluation step may produce, so a wildcard chain cannot exhaust memory.
MAX_NODES = 100_000

# Decides ``=~`` conditions for a batch of strings: (compiled pattern, texts) -> one verdict per text.
# The default runs ``fullmatch`` in this process, which is only right for text you trust; services
# pass ``RegexRunner.fullmatches`` so that the match runs in a worker with a deadline.
Matcher = Callable[[Any, Sequence[str]], Sequence[bool]]


# A bare key name in a dotted step.
_NAME = re.compile(r"[A-Za-z0-9_\-]+")
# A numeric literal in a condition.
_NUMBER = re.compile(r"-?\d+(\.\d+)?")


class JsonPathError(ValueError):
    """Report a path or condition that is not in the supported subset."""


@dataclass(frozen=True)
class Node:
    """One selected value together with the key or position it was found under.

    Attributes
    ----------
    key : Any
        The key or index the value was found under.
    value : Any
        The selected value.
    """

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
    """A parsed path: steps and whether the result is the keys of the selected nodes.

    Methods
    -------
    select
        Return the nodes selected from *root*, in document order.
    select_from
        Return the nodes selected from the node *start*, whose key a ``~`` path can yield.
    values
        Return the values selected from *root* (the keys when the path ends in ``~``).

    Attributes
    ----------
    steps : tuple[tuple[str, Any], ...]
        The parsed steps of the path.
    wants_key : bool
        Whether the result is the keys of the selected nodes.
    """

    steps: tuple[tuple[str, Any], ...]
    wants_key: bool = False

    def select(self, root: Any, matcher: "Matcher | None" = None) -> list[Node]:
        """Return the nodes selected from *root*, in document order.

        Services pass a *matcher* that runs ``=~`` patterns in a worker process,
        because the values come from the network.

        Parameters
        ----------
        root : Any
            The parsed JSON document to select from.
        matcher : Matcher or None
            Decides ``=~`` conditions (see ``Matcher``); ``None`` matches in process.

        Returns
        -------
        list of Node
            The matching nodes; for a ``~`` path, nodes whose value is their key.

        Raises
        ------
        JsonPathError
            If more than ``MAX_NODES`` nodes would be produced.
        """
        return self.select_from(Node(None, root), matcher)

    def select_from(self, start: Node, matcher: "Matcher | None" = None) -> list[Node]:
        """Return the nodes selected from the node *start*, whose key a ``~`` path can yield.

        Parameters
        ----------
        start : Node
            The node the path starts from.
        matcher : Matcher or None
            Decides ``=~`` conditions; ``None`` matches in process.

        Returns
        -------
        list of Node
            The matching nodes, in document order.

        Raises
        ------
        JsonPathError
            If more than ``MAX_NODES`` nodes would be produced.
        """
        # Replace the working list of nodes step by step, enforcing the node cap as it grows.
        nodes = [start]
        for kind, argument in self.steps:
            following: list[Node] = []
            for node in nodes:
                following.extend(_apply(kind, argument, node, matcher or _in_process))
                if len(following) > MAX_NODES:
                    raise JsonPathError(f"the path selects more than {MAX_NODES} nodes")
            nodes = following
        # A trailing ``~`` yields each node's key in place of its value.
        if self.wants_key:
            return [Node(node.key, node.key) for node in nodes]
        return nodes

    def values(self, root: Any, matcher: "Matcher | None" = None) -> list[Any]:
        """Return the values selected from *root* (the keys when the path ends in ``~``).

        Parameters
        ----------
        root : Any
            The parsed JSON document to select from.
        matcher : Matcher or None
            Decides ``=~`` conditions; ``None`` matches in process.

        Returns
        -------
        list
            The selected values.

        Raises
        ------
        JsonPathError
            If more than ``MAX_NODES`` nodes would be produced.

        Examples
        --------
        Basic usage:

        >>> document = {"files": [{"os": "winnt", "url": "a.zip"}, {"os": "linux", "url": "b.tgz"}]}
        >>> parse_path("files[0].url").values(document)
        ['a.zip']
        >>> parse_path("files[*].os").values(document)
        ['winnt', 'linux']

        Filters and keys:

        >>> parse_path("files[?os==winnt].url").values(document)
        ['a.zip']
        >>> parse_path("tags~").values({"tags": {"v1": 1, "v2": 2}})
        ['tags']
        """
        return [node.value for node in self.select(root, matcher)]


def parse_path(text: str) -> JsonPath:
    """Parse *text* into a ``JsonPath``.

    Parameters
    ----------
    text : str
        A path such as ``$.a.b[0]``, ``files[?os==winnt].url`` or ``~``.

    Returns
    -------
    JsonPath
        The parsed steps and whether the path yields keys.

    Raises
    ------
    JsonPathError
        If the text is empty or uses syntax outside the subset.

    Examples
    --------
    Basic usage:

    >>> path = parse_path("$.files[0].url")
    >>> path.steps
    (('key', 'files'), ('index', 0), ('key', 'url'))
    >>> path.wants_key
    False

    Edge cases:

    >>> parse_path("tags~").wants_key
    True
    >>> parse_path("a..b")
    Traceback (most recent call last):
        ...
    gupkg.model.jsonpath.JsonPathError: 'a..b': expected a key after '.' at position 2
    >>> parse_path("  ")
    Traceback (most recent call last):
        ...
    gupkg.model.jsonpath.JsonPathError: the path is empty
    """
    # Normalise the text: reject the empty path, peel a trailing ``~`` and skip a leading ``$``.
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
            # A dotted key must follow the dot.
            match = _NAME.match(source, position)
            if match is None:
                raise JsonPathError(f"{text!r}: expected a key after '.' at position {position}")
            steps.append(("key", match.group(0)))
            position = match.end()
        elif character == "[":
            # A bracket expression: find its closing bracket (outside quotes) and classify the inside.
            end = _closing_bracket(source, position, text)
            steps.append(_bracket_step(source[position + 1 : end].strip(), text))
            position = end + 1
        elif first and _NAME.match(source, position):
            # The first key may omit its leading dot (``files[0]``).
            match = _NAME.match(source, position)
            steps.append(("key", match.group(0)))
            position = match.end()
        else:
            raise JsonPathError(f"{text!r}: unexpected {character!r} at position {position}")
        first = False
    # Text that produced no step and is not a bare ``$`` or ``~`` is not a path.
    if not steps and not wants_key and source not in ("", "$"):
        raise JsonPathError(f"{text!r} is not a valid path")
    return JsonPath(tuple(steps), wants_key)


def condition_holds(text: str, node: Any, matcher: Matcher | None = None) -> bool:
    """Return whether the condition *text* (``path==literal``, ``!=`` or ``=~``) holds for the value *node*.

    A path that selects nothing makes ``==`` and ``=~`` false and ``!=`` true.

    Parameters
    ----------
    text : str
        A condition such as ``stable==true``.
    node : Any
        The JSON value the condition's path starts from.
    matcher : Matcher or None
        Decides ``=~`` conditions; ``None`` matches in process.

    Returns
    -------
    bool
        Whether the condition holds.

    Raises
    ------
    JsonPathError
        If *text* is not a valid condition.

    Examples
    --------
    Basic usage:

    >>> condition_holds("stable==true", {"stable": True})
    True
    >>> condition_holds("stable!=true", {"stable": True})
    False
    >>> condition_holds("name=~^to+l$", {"name": "tool"})
    True

    Edge cases:

    >>> condition_holds("missing==1", {})
    False
    >>> condition_holds("missing!=1", {})
    True
    """
    return evaluate_condition(parse_condition(text), node, matcher)


def evaluate_condition(condition: "_Condition", node: Any, matcher: Matcher | None = None) -> bool:
    """Evaluate a condition parsed by ``parse_condition`` on a node, so a loop parses it once.

    Parameters
    ----------
    condition : _Condition
        The result of ``parse_condition``.
    node : Any
        The JSON value the condition's path starts from.
    matcher : Matcher or None
        Decides ``=~`` conditions; ``None`` matches in process.

    Returns
    -------
    bool
        Whether the condition holds.

    Examples
    --------
    Basic usage:

    >>> condition = parse_condition("n==2")
    >>> [evaluate_condition(condition, item) for item in ({"n": 2}, {"n": 3})]
    [True, False]
    """
    return _evaluate_many(condition, [node], matcher or _in_process)[0]


def parse_condition(text: str) -> _Condition:
    """Parse a stand-alone condition such as ``stable==true`` or ``~ =~ \\d+\\.\\d+``.

    Parameters
    ----------
    text : str
        The condition: ``<path>==<value>``, ``<path>!=<value>`` or ``<path>=~<regex>``.

    Returns
    -------
    _Condition
        The parsed condition, for ``evaluate_condition``.

    Raises
    ------
    JsonPathError
        If there is no operator, the path is invalid, or a regular expression is unsafe.

    Examples
    --------
    Basic usage:

    >>> condition = parse_condition("stable==true")
    >>> condition.operator
    '=='
    >>> evaluate_condition(condition, {"stable": True})
    True

    Edge cases:

    >>> parse_condition("stable")
    Traceback (most recent call last):
        ...
    gupkg.model.jsonpath.JsonPathError: 'stable' is not a condition: write <path>==<value>, <path>!=<value> or <path>=~<regex>
    """
    # Locate the operator, then parse the path on its left.
    split = _split_condition(text)
    if split is None:
        raise JsonPathError(f"{text!r} is not a condition: write <path>==<value>, <path>!=<value> or <path>=~<regex>")
    path_text, operator, literal = split
    path = parse_path(path_text)
    # A regular expression is compiled (and vetted) once here; other literals are typed.
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
    # Track bracket depth and quoting so operators inside filters or strings are skipped.
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
        # Only an operator at depth zero outside quotes ends the path part.
        elif depth == 0 and text[index : index + 2] in ("==", "!=", "=~"):
            return text[:index].strip(), text[index : index + 2], text[index + 2 :].strip()
    return None


def _split_and(text: str) -> list[str]:
    """Split a filter body at every ``&&`` that is outside brackets and quotes."""
    # Scan once, tracking bracket depth and quoting, and cut at each top-level ``&&``.
    parts, depth, quote, start, index = [], 0, "", 0, 0
    while index < len(text):
        character = text[index]
        # Quotes and brackets change what counts as top level.
        if quote:
            quote = "" if character == quote else quote
        elif character in "'\"":
            quote = character
        elif character == "[":
            depth += 1
        elif character == "]":
            depth -= 1
        # A top-level ``&&`` closes the current part; skip its second character.
        elif depth == 0 and text[index : index + 2] == "&&":
            parts.append(text[start:index])
            start, index = index + 2, index + 1
        index += 1
    # The text after the last separator is the final part.
    parts.append(text[start:])
    return parts


def _closing_bracket(source: str, start: int, whole: str) -> int:
    """Return the index of the ``]`` that closes the bracket at *start*, skipping quoted text."""
    # Scan forward from the opening bracket, ignoring any ``]`` inside quotes.
    quote = ""
    for index in range(start + 1, len(source)):
        character = source[index]
        if quote:
            if character == quote:
                quote = ""
        elif character in "'\"":
            quote = character
        # The first unquoted ``]`` closes the bracket.
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


def _apply(kind: str, argument: Any, node: Node, matcher: Matcher) -> list[Node]:
    """Apply one step to one node."""
    # Key and index steps pick one child (or none); a wildcard takes them all.
    value = node.value
    if kind == "key":
        if isinstance(value, dict) and argument in value:
            return [Node(argument, value[argument])]
        return []
    # A negative index counts from the end; out-of-range positions select nothing.
    if kind == "index":
        if isinstance(value, list) and -len(value) <= argument < len(value):
            return [Node(argument % len(value), value[argument])]
        return []
    if kind == "all":
        return _children(node)
    # Filter step: each condition is decided for every remaining child at once, so a worker process (see
    # ``Matcher``) is asked once per condition, not once per child.
    kept = _children(node)
    for condition in argument:
        if not kept:
            break
        verdicts = _evaluate_many(condition, kept, matcher)
        kept = [child for child, holds in zip(kept, verdicts) if holds]
    return kept


def _same(value: Any, literal: Any) -> bool:
    """Compare a JSON value with a literal, treating ``1`` and ``"1"`` as equal."""
    if isinstance(value, bool) or isinstance(literal, bool):
        return value is literal
    if isinstance(value, str) and isinstance(literal, (int, float)):
        return value == str(literal)
    return value == literal


def _evaluate_many(condition: _Condition, nodes: Sequence[Any], matcher: Matcher) -> list[bool]:
    """Evaluate a parsed condition on each node (or raw value); the ``=~`` texts go to *matcher* in one batch."""
    # Select the condition's path from every target, then judge each result list by the operator.
    targets = [node if isinstance(node, Node) else Node(None, node) for node in nodes]
    selected = [[item.value for item in condition.path.select_from(target, matcher)] for target in targets]
    if condition.operator == "==":
        return [any(_same(item, condition.literal) for item in items) for items in selected]
    if condition.operator == "!=":
        return [not any(_same(item, condition.literal) for item in items) for items in selected]
    # For ``=~``, send every candidate text to the matcher in one batch and regroup its verdicts per target.
    texts = [str(item) for items in selected for item in items if isinstance(item, (str, int, float))]
    verdicts = iter(matcher(condition.pattern, texts) if texts else [])
    return [any([next(verdicts) for item in items if isinstance(item, (str, int, float))]) for items in selected]


# --- supporting helpers ---


def _in_process(pattern: Any, texts: Sequence[str]) -> list[bool]:
    """Return whether *pattern* matches the whole of each text, in process and with no deadline."""
    return [pattern.fullmatch(text) is not None for text in texts]
