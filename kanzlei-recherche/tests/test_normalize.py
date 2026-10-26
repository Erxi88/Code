import pytest

from recherche.normalize import normalize_telefon


@pytest.mark.parametrize(
    ("roh", "erwartet"),
    [
        ("0431 / 12 34 56", "+49 431 123456"),
        ("0431-123456", "+49 431 123456"),
        ("(0431) 123456", "+49 431 123456"),
        ("Tel.: 04841 66 90 25", "+49 4841 669025"),
        ("+49 (0)431 123456", "+49 431 123456"),
        ("+49 431 123456", "+49 431 123456"),
        ("0049 40 52990331", "+49 40 52990331"),
        ("+49431123456", "+49431123456"),
        ("0431123456", "+49431123456"),
        ("+45 12 34 56 78", None),  # nicht deutsch
        ("123", None),
        ("info@a.example", None),
        ("", None),
        (None, None),
    ],
)
def test_normalize_telefon(roh, erwartet):
    assert normalize_telefon(roh) == erwartet
