"""Unit tests for addon version handling.

Covers ``_semver_tuple`` (core/addons/manifest.py) and the github tag
sorter + charset gate (core/addons/installer.py). No I/O.
"""

from __future__ import annotations

import pytest

from core.addons.installer import _TAG_RE, _tag_sort_key
from core.addons.manifest import _semver_tuple


class TestSemverTuple:
    def test_plain_triple(self) -> None:
        assert _semver_tuple("1.2.3") == (1, 2, 3)

    def test_leading_triple_of_dev_version(self) -> None:
        # Used for min_core comparison: only MAJOR.MINOR.PATCH matters.
        assert _semver_tuple("1.0.2.dev9") == (1, 0, 2)

    def test_prerelease_suffix_ignored(self) -> None:
        assert _semver_tuple("2.0.0-rc.1") == (2, 0, 0)

    def test_v_prefix_rejected(self) -> None:
        with pytest.raises(ValueError):
            _semver_tuple("v1.2.3")

    def test_garbage_rejected(self) -> None:
        with pytest.raises(ValueError):
            _semver_tuple("soon")


class TestTagSortKey:
    def test_numeric_order_beats_lexicographic(self) -> None:
        assert max(["v1.9.0", "v1.10.0"], key=_tag_sort_key) == "v1.10.0"

    def test_major_version_numeric(self) -> None:
        assert max(["v9.0.0", "v10.0.0"], key=_tag_sort_key) == "v10.0.0"

    def test_semver_sorts_above_non_semver(self) -> None:
        assert max(["nightly", "v0.0.1"], key=_tag_sort_key) == "v0.0.1"
        assert max(["latest", "2024-01-01"], key=_tag_sort_key) == "2024-01-01"

    def test_non_semver_falls_back_to_raw_order(self) -> None:
        assert max(["alpha", "beta"], key=_tag_sort_key) == "beta"

    def test_v_prefix_stripped_before_parse(self) -> None:
        assert _tag_sort_key("v2.0.0")[1] == (2, 0, 0)


class TestTagCharset:
    @pytest.mark.parametrize("tag", ["v1.2.3", "1.0.0", "release-1", "feature/x", "v1.0_rc1", "a"])
    def test_accepted(self, tag: str) -> None:
        assert _TAG_RE.match(tag) is not None

    @pytest.mark.parametrize(
        "tag", ["-evil", "--branch=x", "", "v1.0;rm", "a b", "v1.0`id`", "v1.0$HOME"]
    )
    def test_rejected(self, tag: str) -> None:
        # Leading dash rejected: option injection via `git clone --branch=`.
        assert _TAG_RE.match(tag) is None
