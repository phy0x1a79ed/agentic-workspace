"""The view URL's parameters: parsing, canonical spelling, cache identity.

The failures worth pinning here are the quiet ones. A swap that chains
instead of applying simultaneously would produce a picture that looks
plausible and is wrong -- pinned in :mod:`test_svgpost` once that module
exists (T8); here we only pin that ``parse_swaps`` never lets one source
collapse two targets. A blank parameter that vanishes before anyone can
complain renders the plain board and reports success. A ``view_url`` built
from a non-UUID id would 404 for the wrong reason -- a path-shape mismatch
rather than "does not exist" -- so that is refused at build time too.
"""

from __future__ import annotations

import pytest

from awm.penpot_view import renderspec as R

FILE_ID = "0197f9d2-1a2b-73aa-8b9c-1234567890ab"
PAGE_ID = "0197f9d2-1a2b-73aa-8b9c-1234567890ac"
BOARD_ID = "0197f9d2-1a2b-73aa-8b9c-1234567890ad"


# --- canonical spelling ------------------------------------------------

@pytest.mark.parametrize("token", ["ff00ff", "#ff00ff", "%23FF00FF", "f0f",
                                   "#F0F", " ff00ff "])
def test_every_spelling_of_one_colour_canonicalises(token):
    assert R.canonical_colour(token) == "ff00ff"


def test_eight_digit_rgba_is_refused_not_truncated():
    with pytest.raises(R.SpecError, match="eight-digit"):
        R.canonical_colour("ff00ff80")


@pytest.mark.parametrize("token", ["red", "rgb(1,2,3)", "gg0000", "ff00f", ""])
def test_junk_colour_names_the_token(token):
    with pytest.raises(R.SpecError):
        R.canonical_colour(token)


def test_swap_without_a_colon_is_refused():
    with pytest.raises(R.SpecError, match="ff00ff00aa55"):
        R.parse_swap("ff00ff00aa55")


def test_one_source_may_not_be_swapped_twice():
    with pytest.raises(R.SpecError, match="swapped twice"):
        R.parse_swaps(["ff00ff:00aa55", "f0f:112233"])


def test_the_same_swap_repeated_is_not_an_error():
    assert R.parse_swaps(["ff00ff:00aa55", "f0f:00aa55"]) == (("ff00ff", "00aa55"),)


def test_too_many_swaps_is_refused():
    tokens = [f"{i:06x}:000000" for i in range(R.MAX_SWAPS + 1)]
    with pytest.raises(R.SpecError, match=str(R.MAX_SWAPS)):
        R.parse_swaps(tokens)


# --- the query round trip -----------------------------------------------

def test_parameter_order_does_not_change_the_spec_or_its_fingerprint():
    a = R.from_query({"swap": ["ff00ff:00aa55", "00ff00:333333"]})
    b = R.from_query({"swap": ["00ff00:333333", "ff00ff:00aa55"]})
    assert a == b
    assert R.to_query(a) == R.to_query(b)
    assert R.fingerprint(a) == R.fingerprint(b)


def test_a_blank_swap_is_an_error_not_a_shrug():
    """`parse_qs` drops blank values by default, which would render the plain
    board and call it success -- the handler keeps them precisely so this
    fires."""
    with pytest.raises(R.SpecError, match="empty swap"):
        R.from_query({"swap": [""]})


def test_a_blank_crop_is_an_error():
    with pytest.raises(R.SpecError, match="empty crop"):
        R.from_query({"crop": [""]})


def test_unknown_parameters_are_ignored():
    """A cache-buster or a revision selector doesn't describe a render, so
    neither may perturb the variant or its fingerprint."""
    spec = R.from_query({"bust": ["1758"], "rev": ["abc123"]})
    assert spec.is_plain and R.fingerprint(spec) == "__plain__"


def test_a_junk_scale_still_renders():
    assert R.from_query({"scale": ["wat"]}).scale == 1.0


def test_the_plain_spec_keeps_the_original_cache_identity():
    assert R.fingerprint(R.DEFAULT) == "__plain__"
    assert R.to_query(R.DEFAULT) == ""


def test_the_query_is_readable():
    spec = R.from_query({"swap": ["ff00ff:00aa55"], "crop": ["frame-a"]})
    assert R.to_query(spec) == "swap=ff00ff:00aa55&crop=frame-a"


# --- uuid validation -----------------------------------------------------

@pytest.mark.parametrize("token", [FILE_ID, FILE_ID.upper() and FILE_ID])
def test_a_canonical_uuid_is_accepted(token):
    assert R.is_uuid(token)


@pytest.mark.parametrize("token", ["not-a-uuid", "", FILE_ID.replace("-", ""),
                                   FILE_ID.upper()])
def test_a_non_canonical_id_is_rejected(token):
    assert not R.is_uuid(token)


# --- view_url --------------------------------------------------------------

def test_view_url_is_query_string_only_beyond_the_three_uuids():
    """httpsfront's edge forwards raw path bytes and 404s a path segment that
    re-segments when decoded -- so scale/swap/crop must never land in the
    path, only the query string."""
    spec = R.from_query({"swap": ["ff00ff:00aa55"], "crop": ["a/b"]})
    url = R.view_url(FILE_ID, PAGE_ID, BOARD_ID, spec)
    path, _, query = url.partition("?")
    assert path == f"{R.VIEW_PREFIX}/{FILE_ID}/{PAGE_ID}/{BOARD_ID}"
    assert "/" not in query.replace("crop=a%2Fb", "")


def test_view_url_with_no_params_has_no_trailing_question_mark():
    assert R.view_url(FILE_ID, PAGE_ID, BOARD_ID) == \
        f"{R.VIEW_PREFIX}/{FILE_ID}/{PAGE_ID}/{BOARD_ID}"


def test_view_url_refuses_a_non_uuid_id():
    with pytest.raises(R.SpecError, match="board-id"):
        R.view_url(FILE_ID, PAGE_ID, "not-a-uuid")


# --- cache_key -------------------------------------------------------------

def test_cache_key_is_scoped_to_exactly_the_one_board_and_variant():
    """A render of a composite board must not force a re-render of the child
    boards it embeds -- the key is per (file, page, board, variant), never
    coarser than that."""
    plain = R.cache_key(FILE_ID, PAGE_ID, BOARD_ID)
    swapped = R.cache_key(FILE_ID, PAGE_ID, BOARD_ID,
                           R.from_query({"swap": ["ff00ff:00aa55"]}))
    assert plain != swapped
    assert plain[:3] == swapped[:3] == (FILE_ID, PAGE_ID, BOARD_ID)
    assert plain[3] == "__plain__"


def test_cache_key_differs_for_a_different_board_on_the_same_file():
    a = R.cache_key(FILE_ID, PAGE_ID, BOARD_ID)
    b = R.cache_key(FILE_ID, PAGE_ID, PAGE_ID)  # any distinct uuid stands in
    assert a != b


# --- describe ----------------------------------------------------------

def test_describe_the_plain_spec():
    assert R.describe(R.DEFAULT) == "plain"


def test_describe_names_every_parameter():
    spec = R.from_query({"scale": ["2"], "swap": ["ff00ff:00aa55"],
                          "crop": ["frame-a"]})
    text = R.describe(spec)
    assert "scale 2" in text
    assert "#ff00ff->#00aa55" in text
    assert "crop 'frame-a'" in text
