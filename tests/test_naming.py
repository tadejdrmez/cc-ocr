import pytest

from ccocr.naming import NamingError, build_name, sanitise_value, unique_path


def test_diacritics_kept_and_unsafe_replaced():
    name = build_name("{doctype}_{issuer}_{date}", {"doctype": "račun", "issuer": 'Mlekarna Celje: Šentjur "d.o.o."', "date": "2026-11-12"})
    assert name == "račun_Mlekarna-Celje-Šentjur-d.o.o_2026-11-12"


def test_nfc_normalisation():
    decomposed = "č"  # c + combining caron
    assert sanitise_value(decomposed) == "č"


def test_length_limit_keeps_number_and_date():
    name = build_name("{issuer}_{date}_{number}", {"issuer": "X" * 200, "date": "2026-11-12", "number": "2026-001"}, max_length=60)
    assert len(name) + 4 <= 60
    assert name.endswith("_2026-11-12_2026-001")


def test_missing_token_value_stops():
    with pytest.raises(NamingError):
        build_name("{doctype}_{number}", {"doctype": "invoice"})


def test_reserved_names_avoided():
    assert build_name("{x}", {"x": "CON"}) == "_CON"


def test_collision_case_insensitive(tmp_path):
    (tmp_path / "Invoice_1.pdf").write_bytes(b"x")
    assert unique_path(tmp_path, "invoice_1", (".pdf", ".xml")) == "invoice_1_2"
    assert unique_path(tmp_path, "invoice_9", (".pdf",)) == "invoice_9"
