import pytest

import rotation


@pytest.mark.parametrize(
    ("bucket", "expected"),
    [
        ("2023-W29", "2023-W28"),
        ("2024-W01", "2023-W52"),
        ("2021-W01", "2020-W53"),
        ("2016-W01", "2015-W53"),
    ],
)
def test_prev_week_follows_iso_calendar(bucket, expected):
    assert rotation._prev_week(bucket) == expected


def test_prev_week_rejects_invalid_iso_week():
    with pytest.raises(ValueError, match="not a valid ISO weekly bucket"):
        rotation._prev_week("2021-W53")
