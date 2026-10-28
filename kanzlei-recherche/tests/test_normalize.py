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
        # ohne Trennzeichen (tel:-Links): Vorwahlverzeichnis aus phonenumbers
        ("+49431123456", "+49 431 123456"),
        ("0431123456", "+49 431 123456"),
        ("+494614808625", "+49 461 4808625"),
        ("04102123456", "+49 4102 123456"),
        ("+4940123456", "+49 40 123456"),
        ("+491701234567", "+49 170 1234567"),
        ("+49 4 61480862", "+49 461 480862"),  # einstellige „Vorwahl" ist falsch getrennt
        ("+45 12 34 56 78", None),  # nicht deutsch
        ("123", None),
        ("info@a.example", None),
        ("", None),
        (None, None),
    ],
)
def test_normalize_telefon(roh, erwartet):
    assert normalize_telefon(roh) == erwartet
