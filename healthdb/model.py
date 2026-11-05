"""Fixed vocabulary of the graph: labels, scopes, moderators and folder rules."""

IMPORTER = "svs_healthdb"

# Bump whenever parsing/extraction changes the stored title, text or metadata,
# so that unchanged source files are rewritten on the next import.
#   1: initial version
#   2: RIS XML titles built from gldart/gldnr instead of the first <gldsym>
#   3: `akteure` front-matter list stored on Article documents
PARSER_VERSION = 3

SCOPE_ACTOR_TRUTH = "actor_truth"
SCOPE_LEGAL = "legal"
SCOPE_ETHICS = "ethics"
SCOPES = (SCOPE_ACTOR_TRUTH, SCOPE_LEGAL, SCOPE_ETHICS)

# Document type labels (each Document node carries exactly one of these).
ARTICLE = "Article"
ACTOR_DATA = "ActorData"
LEGAL_TEXT = "LegalText"
ETHICAL_TEXT = "EthicalText"
DOC_TYPES = (ARTICLE, ACTOR_DATA, LEGAL_TEXT, ETHICAL_TEXT)

# Reviewer ("moderator") nodes. Stable ids, display names, scope.
TRUTHSEEKER = "truthseeker"
LEGALEYE = "legaleye"
ETHICMIND = "ethicmind"
MODERATORS = (
    {"moderator_id": TRUTHSEEKER, "name": "TruthSeeker", "scope": SCOPE_ACTOR_TRUTH},
    {"moderator_id": LEGALEYE, "name": "LegalEye", "scope": SCOPE_LEGAL},
    {"moderator_id": ETHICMIND, "name": "EthicMind", "scope": SCOPE_ETHICS},
)

# Which moderator uses which document type (USES_SOURCE).
MODERATOR_FOR_TYPE = {
    ARTICLE: TRUTHSEEKER,
    ACTOR_DATA: TRUTHSEEKER,
    LEGAL_TEXT: LEGALEYE,
    ETHICAL_TEXT: ETHICMIND,
}

SCOPE_FOR_TYPE = {
    ARTICLE: SCOPE_ACTOR_TRUTH,
    ACTOR_DATA: SCOPE_ACTOR_TRUTH,
    LEGAL_TEXT: SCOPE_LEGAL,
    ETHICAL_TEXT: SCOPE_ETHICS,
}

# Top-level source folders (matched case-insensitively) -> document type and
# accepted file extensions. There is intentionally no TruthSeeker folder.
SOURCE_FOLDERS = {
    "articles": {"doc_type": ARTICLE, "extensions": (".md",)},
    "actordata": {"doc_type": ACTOR_DATA, "extensions": (".md",)},
    "legaleye": {"doc_type": LEGAL_TEXT, "extensions": (".xml", ".md")},
    "ethicmind": {"doc_type": ETHICAL_TEXT, "extensions": (".md",)},
}

REL_HAS_PROFILE_SOURCE = "HAS_PROFILE_SOURCE"
REL_USES_SOURCE = "USES_SOURCE"
# Actor -> Article links, derived only from the `akteure` tags in the article
# front matter. The tags come from an external system; this importer does no
# matching of its own.
REL_MATCHES_KEYWORD = "MATCHES_KEYWORD"
KEYWORD_SOURCE = "akteure_front_matter"

ACTOR_ARTICLE_MATCHING_STATUS = "active_from_akteure_tags"
