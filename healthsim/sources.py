"""Read-only source access with role permissions.

Role identity always comes from the controller (`Role`), never from model output.
The Neo4j graph is only read: every query runs in a read transaction, and the only
values placed into Cypher text are fixed patterns and identifiers from the validated
property mapping. Search is lexical (bounded substring matching) unless a suitable
fulltext index already exists. A missing hit is never proof that a source is absent.
"""

import hashlib
import json
import re
import xml.etree.ElementTree as ET
from collections import Counter
from dataclasses import dataclass

from neo4j import READ_ACCESS, AsyncGraphDatabase
from neo4j.exceptions import Neo4jError, ServiceUnavailable

from .errors import PermissionDeniedError, SourceError

ACTOR, TRUTHSEEKER, LEGALEYE, ETHICMIND, META = "actor", "truthseeker", "legaleye", "ethicmind", "meta"
REQUIRED_LABELS = {"Actor", "Moderator", "Document", "Article", "ActorData", "LegalText", "EthicalText"}
REQUIRED_RELATIONSHIPS = {"HAS_PROFILE_SOURCE", "MATCHES_KEYWORD", "USES_SOURCE"}
STOPWORDS = set("""aber alle allem allen aller alles auch auf aus bei beim bitte damit dann dass dabei
dazu denn der des dem den die das dies diese diesem diesen dieser dieses doch durch eine einem einen
einer eines entwickeln fuer für gegen gemeinsamen hier ihre ihrem ihren ihrer innerhalb ist jede jedem
jeden jeder kann kennzeichnen können mehr mit nach nicht noch nur oder ohne sich sie sind sowie über
unter und vom von vor wenn werden wie wird wurde zum zur zwischen berücksichtigen sollen soll muss
müssen haben hat eines welche welcher welches""".split())
BLOCK_TAGS = {"ueberschrift", "absatz", "listelem", "tr", "schluss"}
SKIP_TAGS = {"binary", "src"}


@dataclass(frozen=True)
class Role:
    kind: str
    actor_id: str | None = None

    @property
    def key(self):
        return f"{self.kind}:{self.actor_id}" if self.actor_id else self.kind


def extract_terms(text, limit=12):
    """Deterministic search terms: frequent content words, first occurrence as tie-break."""
    words = re.findall(r"[A-Za-zÄÖÜäöüß]{5,}", text)
    counts = Counter(w.casefold() for w in words)
    order = {}
    for i, w in enumerate(w.casefold() for w in words):
        order.setdefault(w, i)
    ranked = sorted((w for w in counts if w not in STOPWORDS), key=lambda w: (-counts[w], order[w]))
    return ranked[:limit]


def searchable_text(content):
    """Prose values of a JSON view; field names, ids and enum values are not search terms."""
    try:
        data = json.loads(content)
    except ValueError:
        return content
    parts = []

    def walk(node):
        if isinstance(node, dict):
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)
        elif isinstance(node, str) and len(node.split()) >= 3:
            parts.append(node)

    walk(data)
    return " ".join(parts)


def passage_key(doc_id, machine_locator, content_hash):
    digest = hashlib.sha256(f"{doc_id}\n{machine_locator}\n{content_hash}".encode("utf-8")).hexdigest()
    return f"P-{digest[:12]}"


def split_markdown(text, words):
    """Readable text -> [(block_index_label, machine, text)] of about `words` words."""
    chunks, current, start = [], [], 1
    paragraphs = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
    pieces = []
    for number, para in enumerate(paragraphs, 1):
        tokens = para.split()
        for i in range(0, len(tokens), words):
            pieces.append((number, " ".join(tokens[i:i + words])))
    for number, piece in pieces:
        if current and sum(len(c.split()) for c in current) + len(piece.split()) > words:
            chunks.append((f"Absatz {start}", f"text:absatz:{start}", " ".join(current)))
            current, start = [], number
        if not current:
            start = number
        current.append(piece)
    if current:
        chunks.append((f"Absatz {start}", f"text:absatz:{start}", " ".join(current)))
    return chunks


def parse_sozdok(xml_text):
    """Explicit SOZDOK adapter. External entities are impossible: DOCTYPE is rejected.

    Returns (blocks, references); a block keeps heading, readable and machine locator.
    """
    upper = xml_text[:4096].upper()
    if "<!DOCTYPE" in upper or "<!ENTITY" in xml_text.upper():
        raise SourceError("SOZDOK-Dokument mit DOCTYPE/ENTITY wird nicht verarbeitet.")
    data = re.sub(r"^\s*<\?xml[^>]*\?>", "", xml_text)
    try:
        root = ET.fromstring(data)
    except ET.ParseError as exc:
        raise SourceError(f"SOZDOK-Dokument nicht lesbar: {exc}") from None
    body = root.find(".//nutzdaten")
    if body is None:
        raise SourceError("SOZDOK-Dokument ohne <nutzdaten>.")
    blocks, refs, heading = [], [], [None]

    def text_of(elem):
        parts = []

        def walk(node):
            if node.tag in SKIP_TAGS:
                return
            if node.text:
                parts.append(node.text)
            for child in node:
                if child.tag == "rvolink" and child.get("abkrvo"):
                    refs.append({"law": child.get("abkrvo"), "section_type": child.get("gldart"),
                                 "section_number": child.get("gldnr"),
                                 "label": re.sub(r"\s+", " ", "".join(child.itertext())).strip()})
                walk(child)
                if child.tail:
                    parts.append(child.tail)
        walk(elem)
        return re.sub(r"\s+", " ", "".join(parts)).strip()

    def visit(node, path):
        counts = Counter()
        for child in node:
            counts[child.tag] += 1
            step = f"{path}/{child.tag}[{counts[child.tag]}]"
            if child.tag in SKIP_TAGS:
                continue
            if child.tag in BLOCK_TAGS:
                text = text_of(child)
                if child.tag == "ueberschrift" and text:
                    heading[0] = text
                if text:
                    blocks.append({"heading": heading[0], "text": text, "machine": step})
            else:
                visit(child, step)

    visit(body, "sozdok/rvo/nutzdaten")
    return blocks, refs


def split_sozdok(xml_text, title, words):
    blocks, refs = parse_sozdok(xml_text)
    chunks, current = [], []

    def flush():
        if current:
            head = current[0]["heading"]
            label = f"{title}, {head}" if head else title
            chunks.append((label, current[0]["machine"], " ".join(b["text"] for b in current)))
            current.clear()

    for block in blocks:
        if current and sum(len(b["text"].split()) for b in current) + len(block["text"].split()) > words:
            flush()
        current.append(block)
    flush()
    return chunks, refs


def passages_from(row, terms, per_doc, words):
    """Best passages of one document row, with context and exact version reference."""
    refs = []
    if row.get("content_format") == "ris_xml" and row.get("original_content"):
        chunks, refs = split_sozdok(row["original_content"], row["title"], words)
    else:
        chunks = split_markdown(row.get("text") or "", words)
    scored = []
    for i, (label, machine, text) in enumerate(chunks):
        low = text.casefold()
        score = sum(low.count(t) for t in terms)
        if score:
            scored.append((-len({t for t in terms if t in low}), -score, i))
    out = []
    for _, _, i in sorted(scored)[:per_doc]:
        label, machine, text = chunks[i]
        before = chunks[i - 1][2] if i else ""
        after = chunks[i + 1][2] if i + 1 < len(chunks) else ""
        dated = row.get("publication_date") or row.get("valid_from")
        out.append({
            "passage_id": passage_key(row["doc_id"], machine, row["content_hash"]),
            "doc_id": row["doc_id"], "doc_type": row["doc_type"], "title": row["title"],
            "publisher": row.get("publisher"), "publication_date": row.get("publication_date"),
            "valid_from": row.get("valid_from"), "valid_until": row.get("valid_until"),
            "source_url": row.get("source_url"), "content_hash": row["content_hash"],
            "source_path": row.get("source_path"), "locator": label, "machine_locator": machine,
            "date_unknown": dated is None, "text": text, "context_before": before[-600:],
            "context_after": after[:600], "cross_references": refs,
        })
    return out


class Neo4jSources:
    """Read-only adapter for the existing graph."""

    def __init__(self, settings):
        self.database = settings.neo4j_database
        self.m = settings.graph_mapping
        self.fulltext_index = None
        self.search_mode = "lexical_fallback"
        self.driver = AsyncGraphDatabase.driver(
            settings.neo4j_uri, auth=(settings.neo4j_username, settings.neo4j_password),
            notifications_min_severity="OFF")

    async def close(self):
        await self.driver.close()

    async def _read(self, query, **params):
        async def work(tx):
            result = await tx.run(query, params)
            return [r.data() async for r in result]
        try:
            async with self.driver.session(database=self.database, default_access_mode=READ_ACCESS) as s:
                return await s.execute_read(work)
        except (Neo4jError, ServiceUnavailable, OSError) as exc:
            raise SourceError(f"Neo4j-Lesezugriff fehlgeschlagen: {type(exc).__name__}") from None

    async def list_actors(self):
        m = self.m
        return await self._read(f"MATCH (a:Actor) RETURN a.{m['actor_id']} AS actor_id, "
                                f"a.{m['actor_name']} AS name LIMIT 1000")

    async def verify_schema(self):
        labels = {r["label"] for r in await self._read("CALL db.labels() YIELD label RETURN label")}
        rels = {r["relationshipType"] for r in await self._read(
            "CALL db.relationshipTypes() YIELD relationshipType RETURN relationshipType")}
        problems = [f"Label fehlt: {x}" for x in sorted(REQUIRED_LABELS - labels)]
        problems += [f"Beziehung fehlt: {x}" for x in sorted(REQUIRED_RELATIONSHIPS - rels)]
        mods = {r["id"] for r in await self._read(
            f"MATCH (m:Moderator) RETURN m.{self.m['moderator_id']} AS id LIMIT 100")}
        problems += [f"Moderator fehlt: {x}" for x in sorted({TRUTHSEEKER, LEGALEYE, ETHICMIND} - mods)]
        sample = await self._read("MATCH (d:Document) RETURN keys(d) AS keys LIMIT 1")
        if sample:
            for key in ("doc_id", "text", "content_hash", "title"):
                if self.m[key] not in sample[0]["keys"]:
                    problems.append(f"Dokumenteigenschaft fehlt: {self.m[key]}")
        for row in await self._read("SHOW FULLTEXT INDEXES YIELD name, labelsOrTypes, properties, state "
                                    "RETURN name, labelsOrTypes, properties, state"):
            if "Document" in row["labelsOrTypes"] and self.m["text"] in row["properties"] \
                    and row["state"] == "ONLINE":
                self.fulltext_index, self.search_mode = row["name"], "fulltext_index"
        return {"ok": not problems, "problems": problems, "search_mode": self.search_mode}

    def _pattern(self, role, doc_type):
        """Fixed permission pattern; the document filter happens before any model sees text."""
        m = self.m
        if role.kind == ACTOR:
            if doc_type == "ActorData":
                return (f"(o:Actor {{{m['actor_id']}: $owner}})-[:HAS_PROFILE_SOURCE]->(d:Document:ActorData)",
                        role.actor_id)
            return (f"(o:Actor {{{m['actor_id']}: $owner}})-[:MATCHES_KEYWORD]->(d:Document:Article)",
                    role.actor_id)
        labels = {TRUTHSEEKER: doc_type or "Article", LEGALEYE: "LegalText", ETHICMIND: "EthicalText"}
        if role.kind not in labels:
            raise PermissionDeniedError("Diese Rolle darf keine Quellen durchsuchen.")
        if role.kind == TRUTHSEEKER and labels[role.kind] not in ("Article", "ActorData"):
            raise PermissionDeniedError("TruthSeeker durchsucht nur Artikel und Institutionsprofile.")
        return (f"(o:Moderator {{{m['moderator_id']}: $owner}})-[:USES_SOURCE]->(d:Document:{labels[role.kind]})",
                role.kind)

    def _columns(self):
        m = self.m
        names = ("doc_id", "doc_type", "title", "text", "original_content", "content_format", "content_hash",
                 "source_path", "publisher", "publication_date", "valid_from", "valid_until", "source_url")
        return ", ".join(f"d.{m[n]} AS {n}" for n in names)

    def _date_filter(self, doc_type):
        m = self.m
        pub, start, end = m["publication_date"], m["valid_from"], m["valid_until"]
        if doc_type == "LegalText" or doc_type is None:
            return (f"(d.{start} IS NULL OR d.{start} <= $ref) AND (d.{end} IS NULL OR d.{end} >= $ref) "
                    f"AND (d.{pub} IS NULL OR d.{pub} <= $ref)")
        return f"(d.{pub} IS NULL OR d.{pub} <= $ref)"

    async def search(self, role, terms, ref_date, limit, doc_type=None):
        pattern, owner = self._pattern(role, doc_type)
        kind = doc_type or {LEGALEYE: "LegalText", ETHICMIND: "EthicalText"}.get(role.kind)
        m = self.m
        if self.fulltext_index:
            query = (f"CALL db.index.fulltext.queryNodes($index, $phrase, {{limit: 500}}) "
                     f"YIELD node AS d, score MATCH {pattern} WHERE {self._date_filter(kind)} "
                     f"WITH d, score ORDER BY score DESC, d.{m['publication_date']} DESC LIMIT $limit "
                     f"RETURN {self._columns()}")
            return await self._read(query, index=self.fulltext_index, owner=owner, ref=ref_date, limit=limit,
                                    phrase=" OR ".join(re.sub(r"\W", "", t) for t in terms))
        query = (f"MATCH {pattern} WHERE {self._date_filter(kind)} "
                 f"WITH d, toLower(d.{m['text']}) AS low "
                 "WITH d, size([t IN $terms WHERE low CONTAINS t]) AS hits WHERE hits > 0 "
                 f"WITH d, hits ORDER BY hits DESC, d.{m['publication_date']} DESC LIMIT $limit "
                 f"RETURN {self._columns()}")
        return await self._read(query, owner=owner, ref=ref_date, limit=limit, terms=terms)

    async def resolve_reference(self, reference, ref_date):
        """A cited provision in force at the reference date, or None."""
        m = self.m
        rows = await self._read(
            f"MATCH (:Moderator {{{m['moderator_id']}: 'legaleye'}})-[:USES_SOURCE]->(d:Document:LegalText) "
            f"WHERE d.{m['law_abbreviation']} = $law AND d.{m['section_type']} = $kind "
            f"AND d.{m['section_number']} = $number AND {self._date_filter('LegalText')} "
            f"RETURN {self._columns()} ORDER BY d.{m['valid_from']} DESC LIMIT 1",
            law=reference["law"], kind=reference["section_type"], number=reference["section_number"],
            ref=ref_date)
        return rows[0] if rows else None

    async def document(self, doc_id):
        """Readable text of one document as it is in the graph now, or None."""
        m = self.m
        rows = await self._read(f"MATCH (d:Document {{{m['doc_id']}: $id}}) "
                                f"RETURN d.{m['text']} AS text, d.{m['content_hash']} AS content_hash LIMIT 1", id=doc_id)
        return rows[0] if rows else None

    async def current_hash(self, doc_id):
        rows = await self._read(f"MATCH (d:Document {{{self.m['doc_id']}: $id}}) "
                                f"RETURN d.{self.m['content_hash']} AS h LIMIT 1", id=doc_id)
        return rows[0]["h"] if rows else None


class FixtureSources:
    """In-memory adapter with the same permissions, for offline tests and `--sources fixture`."""

    search_mode = "fixture"

    def __init__(self, actors, documents):
        self.actors, self.documents = actors, documents

    async def close(self):
        pass

    async def list_actors(self):
        return [{"actor_id": a["actor_id"], "name": a["name"]} for a in self.actors]

    async def verify_schema(self):
        return {"ok": True, "problems": [], "search_mode": self.search_mode}

    @staticmethod
    def _permitted(role, doc, doc_type):
        if role.kind == ACTOR:
            if doc_type == "ActorData":
                return doc["doc_type"] == "ActorData" and doc.get("owner") == role.actor_id
            return doc["doc_type"] == "Article" and role.actor_id in doc.get("matched", ())
        if role.kind == TRUTHSEEKER:
            return doc["doc_type"] == (doc_type or "Article") and doc["doc_type"] in ("Article", "ActorData")
        if role.kind == LEGALEYE:
            return doc["doc_type"] == "LegalText"
        if role.kind == ETHICMIND:
            return doc["doc_type"] == "EthicalText"
        raise PermissionDeniedError("Diese Rolle darf keine Quellen durchsuchen.")

    async def search(self, role, terms, ref_date, limit, doc_type=None):
        hits = []
        for doc in self.documents:
            if not self._permitted(role, doc, doc_type):
                continue
            if any((doc.get(k) or "") > ref_date for k in ("publication_date", "valid_from")):
                continue
            if doc.get("valid_until") and doc["valid_until"] < ref_date:
                continue
            low = doc["text"].casefold()
            count = sum(1 for t in terms if t in low)
            if count:
                hits.append((count, doc.get("publication_date") or "", doc))
        hits.sort(key=lambda h: (-h[0], h[1]), reverse=False)
        return [dict(h[2]) for h in hits[:limit]]

    async def resolve_reference(self, reference, ref_date):
        for doc in self.documents:
            if doc["doc_type"] == "LegalText" and doc.get("law_abbreviation") == reference["law"] \
                    and doc.get("section_type") == reference["section_type"] \
                    and doc.get("section_number") == reference["section_number"] \
                    and (doc.get("valid_from") or "") <= ref_date \
                    and (not doc.get("valid_until") or doc["valid_until"] >= ref_date):
                return dict(doc)
        return None

    async def current_hash(self, doc_id):
        return next((d["content_hash"] for d in self.documents if d["doc_id"] == doc_id), None)


class SourceService:
    """Retrieval for one process: permission-filtered, cached, snapshotted."""

    def __init__(self, adapter, events, settings):
        self.adapter, self.events = adapter, events
        self.cfg = settings.retrieval
        self._cache = {}

    def _words(self):
        return self.cfg.get("passage_words", 160)

    async def retrieve(self, debate_id, role, query_text, ref_date, documents, doc_type=None):
        """Return (passages, warnings). Passages are permitted for `role` and saved locally."""
        terms = extract_terms(query_text, self.cfg.get("max_terms", 12))
        key = (debate_id, role.key, tuple(terms), doc_type)
        if key in self._cache:
            return self._cache[key]
        warnings = []
        if not terms:
            return [], ["Keine Suchbegriffe ableitbar; es wurden keine Quellen abgerufen."]
        rows = await self.adapter.search(role, terms, ref_date, documents, doc_type)
        passages, seen_refs = [], []
        for row in rows:
            found = passages_from(row, terms, self.cfg.get("passages_per_document", 2), self._words())
            passages += found
            if found:
                seen_refs += found[0]["cross_references"]
        passages += await self._follow_references(seen_refs, terms, ref_date, warnings)
        for p in passages:
            self.events.save_passage(debate_id, p)
            if p["date_unknown"]:
                warnings.append(f"Quelle {p['passage_id']} ist undatiert; ihr Verhältnis zum Stichtag "
                                "ist unbekannt.")
        result = (passages, warnings)
        self._cache[key] = result
        return result

    async def _follow_references(self, references, terms, ref_date, warnings):
        """Resolve referenced provisions deterministically; report what cannot be found."""
        out, done = [], set()
        for ref in references:
            if len(done) >= self.cfg.get("max_cross_references", 3):
                break
            ident = (ref.get("law"), ref.get("section_type"), ref.get("section_number"))
            if ident in done or not all(ident):
                continue
            done.add(ident)
            row = await self.adapter.resolve_reference(ref, ref_date)
            if row is None:
                warnings.append(f"Verweis {ref.get('label') or ident} konnte zum Stichtag nicht "
                                "aufgelöst werden; die Anwendbarkeit bleibt unsicher.")
                continue
            out += passages_from(row, terms or ["§"], 1, self._words()) or \
                passages_from(row, [row["title"].split()[0].casefold()], 1, self._words())
        return out

    async def citation_context(self, debate_id, passage_id, cited_ids):
        """Controlled lookup: only passages already cited in this debate can be opened."""
        if passage_id not in cited_ids:
            raise PermissionDeniedError("Dieser Beleg gehört nicht zu dieser Debatte.")
        passage = self.events.passage(debate_id, passage_id)
        if passage is None:
            raise SourceError(f"Beleg {passage_id} ist lokal nicht gespeichert.")
        current = await self.adapter.current_hash(passage["doc_id"])
        return passage, current == passage["content_hash"]
