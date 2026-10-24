"""Cover the JSONPath subset: keys, positions, wildcards, filters, key selection and the refusals.

Pure functions over parsed JSON, no boundary. Out of scope: the ``json`` provider that uses
the paths (``test_track_providers``).
"""

from __future__ import annotations

import pytest

from gupkg.model.jsonpath import MAX_NODES, JsonPathError, Node, condition_holds, parse_path

DOCUMENT = {
    "latest": "1.5.0",
    "releases": [
        {"name": "one", "stable": True, "assets": [{"os": "win", "arch": "x64", "url": "u1"}, {"os": "mac", "url": "u2"}]},
        {"name": "two", "stable": False, "assets": []},
    ],
    "by_version": {"1.0.0": {"stable": True}, "2.0.0-rc1": {"stable": False}},
}


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        pytest.param("$.latest", ["1.5.0"], id="dotted-key"),
        pytest.param("latest", ["1.5.0"], id="no-dollar"),
        pytest.param("$['latest']", ["1.5.0"], id="quoted-key"),
        pytest.param("$.releases[0].name", ["one"], id="position"),
        pytest.param("$.releases[-1].name", ["two"], id="negative-position"),
        pytest.param("$.releases[5].name", [], id="position-out-of-range"),
        pytest.param("$.releases[*].name", ["one", "two"], id="every-element"),
        pytest.param("$.releases.*.name", ["one", "two"], id="dot-star"),
        pytest.param("$.releases[?stable==true].name", ["one"], id="filter-equals-true"),
        pytest.param("$.releases[?stable!=true].name", ["two"], id="filter-not-equals"),
        pytest.param("$.releases[0].assets[?os==win && arch==x64].url", ["u1"], id="filter-with-two-conditions"),
        pytest.param("$.releases[?name =~ t.*].name", ["two"], id="filter-regex"),
        pytest.param("$.by_version.*~", ["1.0.0", "2.0.0-rc1"], id="keys-of-an-object"),
        pytest.param("$.missing.deeper", [], id="absent-key"),
    ],
)
def test_paths_select_the_expected_values(path: str, expected: list) -> None:
    """Each supported step selects what JSONPath users expect, and an absent path selects nothing."""
    assert parse_path(path).values(DOCUMENT) == expected


def test_a_tilde_path_yields_the_key_of_the_record_it_starts_from() -> None:
    """`~` on a record node is the record's own key, which is how a versions object names its versions."""
    records = parse_path("$.by_version.*").select(DOCUMENT)

    assert [parse_path("~").select_from(record)[0].value for record in records] == ["1.0.0", "2.0.0-rc1"]


@pytest.mark.parametrize(
    ("condition", "holds"),
    [
        pytest.param("stable==true", True, id="equals"),
        pytest.param("stable==false", False, id="equals-false"),
        pytest.param("name==one", True, id="bare-string"),
        pytest.param("name=='one'", True, id="quoted-string"),
        pytest.param("missing!=x", True, id="absent-is-not-equal"),
        pytest.param("missing==x", False, id="absent-is-not-equal-to"),
        pytest.param("name =~ o.e", True, id="regex-full-match"),
        pytest.param("name =~ on", False, id="regex-must-match-whole-value"),
    ],
)
def test_conditions_are_evaluated_on_a_record(condition: str, holds: bool) -> None:
    """The `filter` conditions of the json provider compare a record's values with a literal."""
    record = Node("k", {"name": "one", "stable": True})

    assert condition_holds(condition, record) is holds


@pytest.mark.parametrize(
    "path",
    [
        pytest.param("", id="empty"),
        pytest.param("$..deep", id="recursive-descent"),
        pytest.param("$.a[1:3]", id="slice"),
        pytest.param("$.a[(@.length-1)]", id="script"),
        pytest.param("$.a[", id="unclosed-bracket"),
        pytest.param("$.", id="dangling-dot"),
        pytest.param("$.a[?x=~(a+)+]", id="unsafe-regex-in-a-filter"),
    ],
)
def test_syntax_outside_the_subset_is_refused(path: str) -> None:
    """Anything beyond the documented subset fails at parse time instead of being interpreted."""
    with pytest.raises(JsonPathError):
        parse_path(path)


def test_a_path_cannot_select_an_unbounded_number_of_nodes() -> None:
    """Nested wildcards over a wide document stop at the node cap instead of exhausting memory."""
    wide = [[0] * 1000 for _ in range(1000)]

    with pytest.raises(JsonPathError, match=str(MAX_NODES)):
        parse_path("$[*][*]").select(wide)
