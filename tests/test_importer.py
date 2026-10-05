from pathlib import Path

from addie.categorize import match_category
from addie.importers import file_importer as fi

FIXTURE = Path(__file__).parent / "fixtures" / "bank_sample.csv"


def test_parse_amount_variants():
    assert fi.parse_amount("1,234.50") == 123450
    assert fi.parse_amount("₪ 99") == 9900
    assert fi.parse_amount("12.5-") == -1250
    assert fi.parse_amount("(40)") == -4000
    assert fi.parse_amount("-7") == -700
    assert fi.parse_amount("") is None
    assert fi.parse_amount("abc") is None


def test_parse_date_variants():
    assert fi.parse_date("05/09/2026") == "2026-09-05"
    assert fi.parse_date("5/9/26") == "2026-09-05"
    assert fi.parse_date("05.09.2026") == "2026-09-05"
    assert fi.parse_date("2026-09-05") == "2026-09-05"
    assert fi.parse_date("סה\"כ") is None


def test_detects_hebrew_headers_after_preamble_and_parses_debit_credit():
    rows = fi.read_rows("bank.csv", FIXTURE.read_bytes())
    m = fi.detect_mapping(rows)
    assert m.is_complete()
    assert m.header_row == 2
    assert m.debit is not None and m.credit is not None and m.amount is None
    txs, skipped = fi.parse(rows, m)
    assert [t.amount for t in txs] == [1250000, -41230, -1800, -1800, -200000]
    assert skipped == 1  # the totals row


def test_cp1255_encoding_and_card_inversion():
    content = "תאריך עסקה,שם בית העסק,סכום חיוב\n10/09/2026,NETFLIX,49.90\n".encode("cp1255")
    rows = fi.read_rows("card.csv", content)
    m = fi.detect_mapping(rows)
    m.invert = True
    txs, _ = fi.parse(rows, m)
    assert txs[0].description == "NETFLIX" and txs[0].amount == -4990


def test_longest_rule_wins():
    rules = [{"pattern": "סופר", "category_id": 1}, {"pattern": "סופר-פארם", "category_id": 2}]
    assert match_category("סופר-פארם רמת אביב", rules) == 2
    assert match_category("אחר", rules) is None
