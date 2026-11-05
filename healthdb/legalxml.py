"""Parsing of RIS "sozdok" XML legal documents (LegalEye folder).

Metadata mapping (attributes of <metadaten>):
    title           abkrvo (or titrvo) + section label from gldart/gldnr
                    (PARA -> "§ n", ART -> "Art. n", ZIFF -> "Z n", ANL -> "Anl. n",
                    ANH -> "Anh. n"); other types: first <gldsym> text
    publisher       urheber
    publication_date pubdat
    valid_from      ikra   (Inkrafttreten)
    valid_until     akra   (Ausserkrafttreten); RIS uses 8888-08-08 for "no end date"
    source_version  fassung
Validity dates are copied as published. Nothing is inferred about whether a
provision is in force today.
"""

import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field

from .markdown import TITLE_METADATA, TITLE_FILENAME, parse_date

NO_END_DATE = "8888-08-08"

# Elements whose content starts on a new line.
_BLOCK = {
    "abschnitt", "ueberschrift", "absatz", "liste", "listelem", "ziffernliste",
    "literaliste", "subliteraliste", "strichliste", "aufzaehlung", "betragliste",
    "erlliste", "table", "tr", "schluss", "schlussteil", "inhaltsvz", "fntext",
    "unterschrift", "wechsel", "abstand",
}
_CELL = {"td", "h-td"}
# Empty marker elements that stand for characters.
_CHAR = {"nbsp": " ", "amp": "&", "lt": "<", "gt": ">", "gdash": "–",
         "bdash": "—", "tab": "\t", "br": "\n"}
# Subtrees that carry no readable text (embedded images).
_SKIP = {"binary", "src"}


class LegalXmlError(ValueError):
    """Malformed or unsupported legal XML."""


@dataclass
class ParsedLegalXml:
    metadata: dict
    title: str
    title_origin: str
    text: str
    ris_dokid: str = ""
    warnings: list = field(default_factory=list)


def _collect(elem, parts):
    tag = elem.tag
    if tag in _SKIP:
        if elem.tail:
            parts.append(elem.tail)
        return
    if tag in _CHAR:
        parts.append(_CHAR[tag])
    else:
        if tag in _BLOCK:
            parts.append("\n")
        elif tag in _CELL:
            parts.append("\t")
        if elem.text:
            parts.append(elem.text)
        for child in elem:
            _collect(child, parts)
        if tag == "symbol" or tag == "gldsym":
            parts.append(" ")
        if tag in _BLOCK:
            parts.append("\n")
    if elem.tail:
        parts.append(elem.tail)


def _readable_text(nutzdaten):
    parts = []
    _collect(nutzdaten, parts)
    lines = []
    for line in "".join(parts).split("\n"):
        cells = [re.sub(r"[ \r ]+", " ", c).strip() for c in line.split("\t")]
        line = "\t".join(c for c in cells if c)
        lines.append(line)
    text = "\n".join(lines)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


# RIS section types (metadaten@gldart) whose number (gldnr) identifies the
# section itself. The first <gldsym> in the text is not reliable for these:
# amendment articles often start by citing another law's paragraph.
_SECTION_PREFIX = {"PARA": "§", "ART": "Art.", "ZIFF": "Z", "ANL": "Anl.", "ANH": "Anh."}


def section_label(section_type, section_number):
    """Label such as '§ 31' or 'Art. 2' from metadata, or None if not derivable."""
    prefix = _SECTION_PREFIX.get(section_type or "")
    if prefix is None:
        return None
    return f"{prefix} {section_number}".strip() if section_number else prefix


def parse_legal_xml(data: bytes, filename_stem: str) -> ParsedLegalXml:
    """Parse one RIS XML file given as bytes. Raises LegalXmlError."""
    head = data[:4096].upper()
    if b"<!DOCTYPE" in head or b"<!ENTITY" in data.upper():
        raise LegalXmlError("DOCTYPE/ENTITY declarations are not accepted")
    try:
        root = ET.fromstring(data)
    except ET.ParseError as exc:
        raise LegalXmlError(f"XML parse error: {exc}") from None
    if root.tag != "sozdok":
        raise LegalXmlError(f"unexpected root element <{root.tag}> (expected <sozdok>)")
    meta_el = root.find("./rvo/metadaten")
    if meta_el is None:
        meta_el = root.find(".//metadaten")
    body_el = root.find(".//nutzdaten")
    if meta_el is None:
        raise LegalXmlError("missing <metadaten>")
    if body_el is None:
        raise LegalXmlError("missing <nutzdaten>")
    a = meta_el.attrib
    warnings = []
    metadata = {}

    def put_date(key, attr):
        value = a.get(attr, "").strip()
        if not value:
            return
        if key == "valid_until" and value == NO_END_DATE:
            return
        try:
            metadata[key] = parse_date(value)
        except ValueError:
            raise LegalXmlError(f"metadaten@{attr}: invalid date {value!r}") from None

    put_date("publication_date", "pubdat")
    put_date("valid_from", "ikra")
    put_date("valid_until", "akra")
    if "valid_from" in metadata and "valid_until" in metadata \
            and metadata["valid_until"] < metadata["valid_from"]:
        warnings.append(f"valid_until {metadata['valid_until']} is before valid_from "
                        f"{metadata['valid_from']} (kept as published)")
    for key, attr in (("publisher", "urheber"), ("source_version", "fassung"),
                      ("law_title", "titrvo"), ("law_abbreviation", "abkrvo"),
                      ("section_type", "gldart"), ("section_number", "gldnr"),
                      ("amendment_title", "titaen")):
        value = a.get(attr, "").strip()
        if value:
            metadata[key] = value

    law = metadata.get("law_abbreviation") or metadata.get("law_title")
    label = section_label(metadata.get("section_type"), metadata.get("section_number"))
    if label is None:
        # Unstructured section types: use the first section symbol in the text.
        gldsym = body_el.find(".//gldsym")
        label = ""
        if gldsym is not None:
            label = re.sub(r"\s+", " ", "".join(gldsym.itertext())).strip().rstrip(".")
    if law:
        title, origin = (f"{law} {label}".strip(), TITLE_METADATA)
    else:
        title, origin = filename_stem, TITLE_FILENAME
        warnings.append("no law title in metadata; filename used as display title")

    text = _readable_text(body_el)
    if not text:
        warnings.append("no readable text in <nutzdaten>")
    return ParsedLegalXml(metadata, title, origin, text, root.get("dokid", ""), warnings)
