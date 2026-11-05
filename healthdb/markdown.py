"""Markdown handling: strict front matter, display title, readable text.

Front matter format (documented in README.md):

    ---
    key: "double-quoted value"     # \\" and \\\\ escapes allowed
    key: 'single-quoted value'     # '' is a literal quote
    key: 42                        # bare integer
    key: bare text                 # bare scalar (must not start with YAML syntax)
    akteure: Tag A, Tag B          # comma-separated list; may be empty (Articles only)
    ---

The block must start on the first line of the file. Only the keys in
CANONICAL_KEYS (or their German aliases in KEY_ALIASES) are allowed. Any
violation raises FrontMatterError; the importer then rejects the file.
"""

import html
import re
import unicodedata
from dataclasses import dataclass, field
from datetime import date

# German keys used by the press-review export -> canonical property names.
KEY_ALIASES = {
    "titel": "title",
    "quelle": "publisher",
    "datum": "publication_date",
    "autor": "author",
    "typ": "media_type",
    "land": "country",
    "sprache": "language",
    "pressespiegel": "press_review",
    "pdf": "source_pdf",
    "pdf_seiten": "source_pages",
    "faksimile_seiten_ausgelassen": "omitted_facsimile_pages",
}

CANONICAL_KEYS = {
    "id", "title", "publisher", "publication_date", "source_version",
    "valid_from", "valid_until", "source_url", "author", "media_type",
    "country", "language", "press_review", "source_pdf", "source_pages",
    "omitted_facsimile_pages", "akteure",
}
DATE_KEYS = {"publication_date", "valid_from", "valid_until"}
INT_KEYS = {"omitted_facsimile_pages"}
PERSISTENT_ID_RE = re.compile(r"^[A-Za-z0-9._:-]+$")

TITLE_METADATA = "metadata"
TITLE_HEADING = "heading"
TITLE_FILENAME = "filename"


class FrontMatterError(ValueError):
    """Malformed or unsupported front matter."""


@dataclass
class ParsedMarkdown:
    metadata: dict
    has_front_matter: bool
    title: str
    title_origin: str
    text: str
    warnings: list = field(default_factory=list)


# --------------------------------------------------------------------------
# Front matter

_KEY_LINE = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*):(?:[ \t]+(.*))?$")
_DQ_ESCAPES = {'"': '"', "\\": "\\", "n": "\n", "t": "\t", "/": "/"}


def _parse_double_quoted(raw, lineno):
    out, i = [], 1
    while i < len(raw):
        ch = raw[i]
        if ch == "\\":
            if i + 1 >= len(raw) or raw[i + 1] not in _DQ_ESCAPES:
                raise FrontMatterError(f"line {lineno}: unsupported escape in {raw!r}")
            out.append(_DQ_ESCAPES[raw[i + 1]])
            i += 2
            continue
        if ch == '"':
            if raw[i + 1:].strip():
                raise FrontMatterError(f"line {lineno}: text after closing quote in {raw!r}")
            return "".join(out)
        out.append(ch)
        i += 1
    raise FrontMatterError(f"line {lineno}: unterminated double-quoted value {raw!r}")


def _parse_single_quoted(raw, lineno):
    body = raw[1:]
    out, i = [], 0
    while i < len(body):
        if body[i] == "'":
            if body[i + 1:i + 2] == "'":
                out.append("'")
                i += 2
                continue
            if body[i + 1:].strip():
                raise FrontMatterError(f"line {lineno}: text after closing quote in {raw!r}")
            return "".join(out)
        out.append(body[i])
        i += 1
    raise FrontMatterError(f"line {lineno}: unterminated single-quoted value {raw!r}")


def _parse_scalar(raw, lineno):
    raw = raw.strip()
    if not raw:
        raise FrontMatterError(f"line {lineno}: empty value")
    if raw[0] == '"':
        return _parse_double_quoted(raw, lineno)
    if raw[0] == "'":
        return _parse_single_quoted(raw, lineno)
    if re.fullmatch(r"-?\d+", raw):
        return int(raw)
    if raw[0] in "[]{}>|&*!%@`#,":
        raise FrontMatterError(f"line {lineno}: unsupported YAML syntax {raw!r}")
    return raw


def _parse_akteure(raw, lineno):
    """Comma-separated tag list; an empty value means an empty list."""
    raw = raw.strip()
    if not raw:
        return []
    text = str(_parse_scalar(raw, lineno))
    values = [unicodedata.normalize("NFC", v.strip()) for v in text.split(",")]
    if not all(values):
        raise FrontMatterError(f"line {lineno}: empty entry in akteure list {raw!r}")
    if len(set(values)) != len(values):
        raise FrontMatterError(f"line {lineno}: duplicate entry in akteure list {raw!r}")
    return values


def parse_date(value):
    """Accept YYYY-MM-DD or DD.MM.YYYY; return ISO string or raise ValueError."""
    text = str(value).strip()
    m = re.fullmatch(r"(\d{4})-(\d{2})-(\d{2})", text)
    if m:
        y, mo, d = map(int, m.groups())
    else:
        m = re.fullmatch(r"(\d{1,2})\.(\d{1,2})\.(\d{4})", text)
        if not m:
            raise ValueError(f"unrecognised date {text!r}")
        d, mo, y = map(int, m.groups())
    return date(y, mo, d).isoformat()


def split_front_matter(text):
    """Return (raw_front_matter_lines or None, body, first_body_line_number)."""
    lines = text.split("\n")
    if not lines or lines[0].rstrip() != "---":
        return None, text, 1
    for i in range(1, len(lines)):
        if lines[i].rstrip() == "---":
            return lines[1:i], "\n".join(lines[i + 1:]), i + 2
    raise FrontMatterError("front matter starts with '---' but has no closing '---'")


def parse_front_matter(lines):
    """Parse front-matter lines into canonical metadata. Raises FrontMatterError."""
    metadata, seen_raw = {}, {}
    for offset, line in enumerate(lines, start=2):
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        m = _KEY_LINE.match(line.rstrip())
        if not m:
            raise FrontMatterError(f"line {offset}: expected 'key: value', got {line!r}")
        raw_key, raw_value = m.group(1), m.group(2) or ""
        key = KEY_ALIASES.get(raw_key, raw_key)
        if key not in CANONICAL_KEYS:
            raise FrontMatterError(f"line {offset}: unknown key {raw_key!r}")
        if key in seen_raw:
            raise FrontMatterError(
                f"line {offset}: key {raw_key!r} duplicates {seen_raw[key]!r} (both mean {key!r})")
        seen_raw[key] = raw_key
        if key == "akteure":
            metadata[key] = _parse_akteure(raw_value, offset)
            continue
        value = _parse_scalar(raw_value, offset)
        if key in DATE_KEYS:
            try:
                metadata[key] = parse_date(value)
            except ValueError as exc:
                raise FrontMatterError(f"line {offset}: {raw_key}: {exc}") from None
            if raw_key == "datum" or str(value) != metadata[key]:
                metadata[key + "_raw"] = str(value)
        elif key in INT_KEYS:
            if not isinstance(value, int) or value < 0:
                raise FrontMatterError(f"line {offset}: {raw_key} must be a non-negative integer")
            metadata[key] = value
        else:
            value = str(value).strip()
            if not value:
                raise FrontMatterError(f"line {offset}: {raw_key} is empty")
            if key == "id" and not PERSISTENT_ID_RE.match(value):
                raise FrontMatterError(
                    f"line {offset}: id {value!r} must match {PERSISTENT_ID_RE.pattern}")
            metadata[key] = value
    if "valid_from" in metadata and "valid_until" in metadata \
            and metadata["valid_until"] < metadata["valid_from"]:
        raise FrontMatterError("valid_until is before valid_from")
    return metadata


# --------------------------------------------------------------------------
# Inline cleanup and text extraction

_HEADING = re.compile(r"^ {0,3}(#{1,6})(?:[ \t]+(.*?))?[ \t]*$")
_FENCE = re.compile(r"^ {0,3}(`{3,}|~{3,})")
_TABLE_SEP = re.compile(r"^\s*\|?\s*:?-{3,}:?\s*(\|\s*:?-{3,}:?\s*)*\|?\s*$")
_HRULE = re.compile(r"^ {0,3}([-*_])(?:[ \t]*\1){2,}[ \t]*$")
_REF_DEF = re.compile(r"^ {0,3}\[[^\]]+\]:\s*\S+")
_BULLET = re.compile(r"^(\s*)[-*+][ \t]+")
_QUOTE = re.compile(r"^\s*(?:>[ \t]?)+")

_INLINE_RULES = [
    (re.compile(r"!\[([^\]]*)\]\((?:[^()]|\([^)]*\))*\)"), r"\1"),          # image -> alt
    (re.compile(r"\[([^\]]+)\]\((?:[^()]|\([^)]*\))*\)"), r"\1"),           # link -> label
    (re.compile(r"\[([^\]]+)\]\[[^\]]*\]"), r"\1"),                         # ref link -> label
    (re.compile(r"<(?:https?://|ftp://|mailto:)[^>\s]+>", re.I), ""),        # autolink URL
    (re.compile(r"<br\s*/?>", re.I), " "),
    (re.compile(r"</?[A-Za-z][A-Za-z0-9-]*(?:\s[^<>]*)?/?>"), ""),           # inline HTML tags
    (re.compile(r"`([^`]+)`"), r"\1"),                                      # inline code
    (re.compile(r"\*\*(?=\S)(.+?)(?<=\S)\*\*"), r"\1"),
    (re.compile(r"(?<!\w)__(?=\S)(.+?)(?<=\S)__(?!\w)"), r"\1"),
    (re.compile(r"~~(?=\S)(.+?)(?<=\S)~~"), r"\1"),
    (re.compile(r"(?<![\w*])\*(?=[^\s*])(.+?)(?<=[^\s*])\*(?![\w*])"), r"\1"),
    (re.compile(r"(?<!\w)_(?=[^\s_])(.+?)(?<=[^\s_])_(?!\w)"), r"\1"),
    (re.compile(r"\*\*|(?<!\w)__(?!\w)"), ""),                              # stray markers
    (re.compile(r"\\([\\`*_{}\[\]()#+\-.!|>~\"'<])"), r"\1"),               # escapes
]


def clean_inline(text):
    for pattern, repl in _INLINE_RULES:
        text = pattern.sub(repl, text)
    return html.unescape(text)


def _split_table_row(line):
    s = line.strip()
    if s.startswith("|"):
        s = s[1:]
    if s.endswith("|") and not s.endswith("\\|"):
        s = s[:-1]
    cells = re.split(r"(?<!\\)\|", s)
    return [clean_inline(c.strip().replace("\\|", "|")) for c in cells]


def _strip_comments(body, warnings):
    out, pos = [], 0
    while True:
        start = body.find("<!--", pos)
        if start < 0:
            out.append(body[pos:])
            break
        out.append(body[pos:start])
        end = body.find("-->", start + 4)
        if end < 0:
            warnings.append("unterminated HTML comment; text after '<!--' was excluded")
            break
        # keep line structure so headings stay on their own lines
        out.append("\n" * body.count("\n", start, end + 3))
        pos = end + 3
    return "".join(out)


def _normalise_for_compare(text):
    return unicodedata.normalize("NFC", text).casefold().strip()


def extract(body, filename_stem):
    """Return (headings, readable_text, warnings) for a Markdown body.

    A first heading whose text equals the filename stem is treated as a
    converter artifact: it is excluded from the readable text and never used
    as a genuine heading title.
    """
    warnings = []
    body = _strip_comments(body, warnings)
    stem_key = _normalise_for_compare(filename_stem)
    headings, out = [], []
    in_fence, fence_marker, first_heading_seen = False, "", False
    for line in body.split("\n"):
        fence = _FENCE.match(line)
        if in_fence:
            if fence and fence.group(1)[0] == fence_marker[0] \
                    and len(fence.group(1)) >= len(fence_marker):
                in_fence = False
            else:
                out.append(line)
            continue
        if fence:
            in_fence, fence_marker = True, fence.group(1)
            continue
        heading = _HEADING.match(line)
        if heading:
            text = re.sub(r"[ \t]+#+[ \t]*$", "", heading.group(2) or "")
            text = clean_inline(text).strip()
            if not first_heading_seen:
                first_heading_seen = True
                if text and _normalise_for_compare(text) == stem_key:
                    continue  # filename artifact heading: drop from text
            if text:
                headings.append(text)
                out.append(text)
            continue
        if "|" in line and _TABLE_SEP.match(line):
            continue
        if _HRULE.match(line) or _REF_DEF.match(line):
            continue
        line = _QUOTE.sub("", line)
        if line.lstrip().startswith("|"):
            out.append("\t".join(_split_table_row(line)))
            continue
        line = _BULLET.sub(r"\1", line)
        out.append(clean_inline(line))
    if in_fence:
        warnings.append("unterminated code fence")
    text = "\n".join(re.sub(r"[ ]{2,}", " ", l).strip() for l in out)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    return headings, text, warnings


def parse_markdown(text, filename_stem):
    """Parse a whole Markdown file. Raises FrontMatterError for bad metadata."""
    fm_lines, body, _ = split_front_matter(text)
    metadata = parse_front_matter(fm_lines) if fm_lines is not None else {}
    headings, readable, warnings = extract(body, filename_stem)
    if metadata.get("title"):
        title, origin = metadata["title"], TITLE_METADATA
    elif headings:
        title, origin = headings[0], TITLE_HEADING
    else:
        title, origin = filename_stem, TITLE_FILENAME
    return ParsedMarkdown(metadata, fm_lines is not None, title, origin, readable, warnings)
