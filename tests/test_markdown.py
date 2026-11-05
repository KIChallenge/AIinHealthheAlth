import unicodedata

import pytest

from healthdb.markdown import (FrontMatterError, parse_date, parse_markdown,
                               parse_front_matter, split_front_matter)


def fm(text):
    lines, _, _ = split_front_matter(text)
    return parse_front_matter(lines)


# --- front matter -----------------------------------------------------------

def test_german_press_keys_and_escapes():
    meta = fm('---\ntitel: "Er sagte \\"Nein\\" – klar"\nquelle: "Kurier"\n'
              'datum: "04.04.2025"\nfaksimile_seiten_ausgelassen: 0\n---\nbody')
    assert meta["title"] == 'Er sagte "Nein" – klar'
    assert meta["publisher"] == "Kurier"
    assert meta["publication_date"] == "2025-04-04"
    assert meta["publication_date_raw"] == "04.04.2025"
    assert meta["omitted_facsimile_pages"] == 0


def test_english_keys_and_iso_date():
    meta = fm("---\ntitle: Plain title\npublication_date: 2024-02-29\nsource_version: '3'\n---\n")
    assert meta == {"title": "Plain title", "publication_date": "2024-02-29", "source_version": "3"}


@pytest.mark.parametrize("block, message", [
    ("---\nfoo: 1\n---\n", "unknown key"),
    ("---\ntitle: a\ntitle: b\n---\n", "duplicates"),
    ("---\ntitel: a\ntitle: b\n---\n", "duplicates"),
    ("---\ndatum: \"32.01.2025\"\n---\n", "datum"),
    ("---\ndatum: \"2025/01/01\"\n---\n", "unrecognised date"),
    ("---\ntitle: \"open\n---\n", "unterminated"),
    ("---\ntitle: \"a\" trailing\n---\n", "after closing quote"),
    ("---\ntitle: [a, b]\n---\n", "unsupported YAML"),
    ("---\ntitle:\n---\n", "empty value"),
    ("---\njust text\n---\n", "expected 'key: value'"),
    ("---\nfaksimile_seiten_ausgelassen: \"x\"\n---\n", "non-negative integer"),
    ("---\nid: \"bad id!\"\n---\n", "must match"),
    ("---\nvalid_from: 2024-01-01\nvalid_until: 2023-01-01\n---\n", "before valid_from"),
    ("---\ntitle: \"bad \\q escape\"\n---\n", "unsupported escape"),
])
def test_malformed_front_matter_is_rejected(block, message):
    with pytest.raises(FrontMatterError, match=message):
        fm(block)


def test_akteure_list():
    assert fm("---\nakteure: Ärztekammer, Dachverband SV\n---\n")["akteure"] == \
        ["Ärztekammer", "Dachverband SV"]
    assert fm("---\nakteure: Krankenhäuser\n---\n")["akteure"] == ["Krankenhäuser"]
    assert fm('---\nakteure: "A, B"\n---\n')["akteure"] == ["A", "B"]


def test_empty_akteure_is_an_empty_list():
    assert fm("---\nakteure:\n---\n")["akteure"] == []
    assert fm("---\nakteure:   \n---\n")["akteure"] == []
    assert "akteure" not in fm("---\ntitle: x\n---\n")


def test_akteure_values_are_nfc_normalised():
    nfd = unicodedata.normalize("NFD", "Ärztekammer")
    assert fm(f"---\nakteure: {nfd}\n---\n")["akteure"] == ["Ärztekammer"]


@pytest.mark.parametrize("value, message", [
    ("A,,B", "empty entry"), ("A, A", "duplicate entry"), ("[A, B]", "unsupported YAML"),
])
def test_malformed_akteure_is_rejected(value, message):
    with pytest.raises(FrontMatterError, match=message):
        fm(f"---\nakteure: {value}\n---\n")


def test_unclosed_front_matter_is_rejected():
    with pytest.raises(FrontMatterError, match="no closing"):
        split_front_matter("---\ntitle: x\n\n# Heading\n")


def test_no_front_matter_is_not_an_error():
    parsed = parse_markdown("# Titel\n\nText", "datei")
    assert parsed.metadata == {} and parsed.has_front_matter is False


def test_parse_date():
    assert parse_date("1.2.2025") == "2025-02-01"
    with pytest.raises(ValueError):
        parse_date("2025-02-30")


# --- titles -----------------------------------------------------------------

def test_metadata_title_wins():
    p = parse_markdown('---\ntitel: "Aus Metadaten"\n---\n# Überschrift\n', "file")
    assert (p.title, p.title_origin) == ("Aus Metadaten", "metadata")


def test_first_heading_used():
    p = parse_markdown("Intro\n\n## **Echte Überschrift**\n\n# Zweite\n", "file")
    assert (p.title, p.title_origin) == ("Echte Überschrift", "heading")


def test_heading_equal_to_filename_is_treated_as_filename_artifact():
    body = "# OeAK_Resolution_2023\n\n<!-- Quelle: x.pdf -->\n# **Resolution der Kammer**\n\nText\n"
    p = parse_markdown(body, "OeAK_Resolution_2023")
    assert (p.title, p.title_origin) == ("Resolution der Kammer", "heading")
    assert "OeAK_Resolution_2023" not in p.text


def test_filename_fallback_is_marked_and_not_in_text():
    p = parse_markdown("Nur Text ohne Überschrift.\n", "Jahresbericht_2025")
    assert (p.title, p.title_origin) == ("Jahresbericht_2025", "filename")
    assert "Jahresbericht_2025" not in p.text


def test_only_artifact_heading_falls_back_to_filename():
    p = parse_markdown("# Bericht\n\nText\n", "Bericht")
    assert p.title_origin == "filename"


# --- readable text ------------------------------------------------------------

def test_readable_text_extraction():
    src = (
        "---\ntitle: T\n---\n"
        "# Haupt **Titel**\n"
        "<!-- versteckter Kommentar -->\n"
        "Ein [Link-Text](https://example.invalid/a_(b)) und ![Bildtext](img.png).\n"
        "Auto <https://example.invalid/auto> Link. Ref [Label][r].\n"
        "[r]: https://example.invalid/ref\n"
        "**fett** __unterstrichen__ *kursiv* _betont_ ~~weg~~ `code` <u>html</u> a&amp;b\n"
        "OeAK_Resolution_Gesundheit bleibt.\n"
        "> Zitat\n"
        "- Punkt\n"
        "1. Erstens\n"
        "| A | B |\n|---|:---:|\n| x | y |\n"
        "---\n"
        "```\nCode *bleibt*\n```\n"
    )
    text = parse_markdown(src, "f").text
    assert "Haupt Titel" in text
    assert "Link-Text" in text and "Bildtext" in text and "Label" in text
    assert "example.invalid" not in text and "img.png" not in text
    assert "versteckter Kommentar" not in text
    assert "title: T" not in text
    assert "fett unterstrichen kursiv betont weg code html a&b" in text
    assert "OeAK_Resolution_Gesundheit bleibt." in text
    assert "Zitat" in text and "> Zitat" not in text
    assert "Punkt" in text and "- Punkt" not in text
    assert "1. Erstens" in text
    assert "A\tB" in text and "x\ty" in text and "---" not in text
    assert "Code *bleibt*" in text
    for marker in ("**", "__", "<u>", "#", "|"):
        assert marker not in text


def test_unterminated_comment_is_reported():
    p = parse_markdown("Sichtbar\n<!-- offen\nunsichtbar", "f")
    assert "unsichtbar" not in p.text
    assert any("unterminated HTML comment" in w for w in p.warnings)
