import pytest

from conftest import FIXTURE_DATA
from healthdb.legalxml import LegalXmlError, parse_legal_xml

LEGAL = FIXTURE_DATA / "LegalEye"


def load(name):
    path = LEGAL / name
    return parse_legal_xml(path.read_bytes(), path.stem)


def test_metadata_mapping_and_open_end_date():
    p = load("TESTG=PARA=1=10=2020-01-01=2020-02-01.xml")
    assert (p.title, p.title_origin) == ("TestG § 1", "metadata")
    m = p.metadata
    assert m["publisher"] == "Bund"
    assert m["publication_date"] == "2020-01-01"
    assert m["valid_from"] == "2020-02-01"
    assert "valid_until" not in m          # 8888-08-08 = no end date, not invented
    assert m["source_version"] == "2023-01-01"
    assert m["law_title"] == "Testgesetz 2020"
    assert p.ris_dokid == "TESTG=PARA=1=10=2020-01-01=2020-02-01"


def test_end_date_kept_as_published():
    p = load("TESTG=PARA=2=10=2020-01-01=2020-02-01.xml")
    assert p.metadata["valid_until"] == "2024-12-31"


def test_readable_text():
    text = load("TESTG=PARA=1=10=2020-01-01=2020-02-01.xml").text
    assert "Geltungsbereich" in text
    assert "§ 1. (1) Dieses Gesetz gilt für § 2 ASVG Versicherte." in text
    assert "1. Ärztinnen und Ärzte;" in text
    assert "BGBl. I Nr. 1/2020–Anlage." in text
    assert "example.invalid" not in text      # URL attribute excluded
    assert "QUJDREVGRw" not in text           # embedded image data excluded


def test_title_uses_section_metadata_not_cited_paragraph():
    # Amendment article 2 whose text starts by citing "§ 1" of another law.
    data = ('<sozdok><rvo><metadaten abkrvo="veraendW 2022" gldart="ART" gldnr="2"/>'
            '<nutzdaten><absatz>Auf Grund des <gldsym>§ 1.</gldsym> ASVG ...</absatz>'
            '</nutzdaten></rvo></sozdok>').encode()
    assert parse_legal_xml(data, "x").title == "veraendW 2022 Art. 2"


@pytest.mark.parametrize("gldart, gldnr, expected", [
    ("PARA", "9f", "L § 9f"), ("ZIFF", "IV", "L Z IV"), ("ANL", "", "L Anl."),
    ("KEIN", "0", "L § 3"),          # unstructured type: falls back to first gldsym
])
def test_section_labels(gldart, gldnr, expected):
    data = (f'<sozdok><rvo><metadaten abkrvo="L" gldart="{gldart}" gldnr="{gldnr}"/>'
            '<nutzdaten><absatz><gldsym>§ 3.</gldsym> Text</absatz></nutzdaten></rvo></sozdok>').encode()
    assert parse_legal_xml(data, "x").title == expected


@pytest.mark.parametrize("data, message", [
    (b"<sozdok><rvo><metadaten/>", "parse error"),
    (b'<?xml version="1.0"?><!DOCTYPE x [<!ENTITY a "b">]><sozdok/>', "DOCTYPE"),
    (b"<other/>", "unexpected root"),
    (b"<sozdok><rvo><nutzdaten/></rvo></sozdok>", "missing <metadaten>"),
    (b'<sozdok><rvo><metadaten pubdat="2020-13-01"/><nutzdaten/></rvo></sozdok>', "invalid date"),
])
def test_bad_xml_is_rejected(data, message):
    with pytest.raises(LegalXmlError, match=message):
        parse_legal_xml(data, "x")
