"""Integration tests against a throwaway Neo4j (docker compose --profile test).

They are skipped unless NEO4J_TEST_URI / _USER / _PASSWORD are set (env or .env).
The test database is wiped before every test, so it must never point at the
main database.
"""

import pytest

from conftest import EXPECTED, EXPECTED_BY_ACTOR
from healthdb import config, graph, retrieval
from healthdb.config import Actor
from healthdb.importer import run_import


@pytest.fixture(scope="session")
def neo():
    try:
        settings = config.connection_settings(prefix="NEO4J_TEST")
    except config.ConfigError as exc:
        pytest.skip(f"integration tests need a test database: {exc}")
    main_uri = config.get_setting("NEO4J_URI")
    if main_uri and main_uri.rstrip("/") == settings.uri.rstrip("/"):
        pytest.skip("NEO4J_TEST_URI equals NEO4J_URI; refusing to wipe the main database")
    try:
        driver = graph.connect(settings)
    except Exception as exc:
        pytest.skip(f"test database not reachable at {settings.uri}: {exc}")
    yield driver, settings.database
    driver.close()


@pytest.fixture
def db(neo):
    driver, database = neo
    driver.execute_query("MATCH (n) DETACH DELETE n", database_=database)
    return driver, database


def do_import(db, data_tree, actors, options, **kw):
    driver, database = db
    return run_import(data_tree, actors, options, driver, database, **kw)


def q(db, query, **params):
    driver, database = db
    return graph.run_read(driver, database, query, **params)


def all_checks_pass(report):
    return {k: v["count"] for k, v in report["validation"].items() if not v["passed"]} == {}


# ----------------------------------------------------------------------------

def test_full_import_creates_expected_graph(db, data_tree, actors, options):
    rep = do_import(db, data_tree, actors, options)
    assert rep["status"] == "success", rep["issues"]
    assert all_checks_pass(rep)
    n = rep["counts"]["nodes"]
    assert n["Actor"] == 3 and n["Moderator"] == 3
    assert n["Document"] == sum(EXPECTED.values())
    for label, count in EXPECTED.items():
        assert n[label] == count
    assert rep["features"]["actor_article_matching"] == "active_from_akteure_tags"
    assert rep["counts"]["relationship_totals"]["MATCHES_KEYWORD"] == 3
    assert len(rep["committed"]) > 3     # batch_size=2 -> several batches


def test_actors_and_moderators_created_once_with_scopes(db, data_tree, actors, options):
    do_import(db, data_tree, actors, options)
    do_import(db, data_tree, actors, options)
    rows = q(db, "MATCH (n) WHERE n:Actor OR n:Moderator RETURN labels(n)[0] AS l, "
                 "coalesce(n.actor_id, n.moderator_id) AS id, n.name AS name, n.scope AS scope "
                 "ORDER BY l, id")
    assert rows == [
        {"l": "Actor", "id": "aerztekammer", "name": "Ärztekammer", "scope": "actor_truth"},
        {"l": "Actor", "id": "gesundheitsministerium", "name": "Gesundheitsministerium",
         "scope": "actor_truth"},
        {"l": "Actor", "id": "sozialversicherung", "name": "Sozialversicherung", "scope": "actor_truth"},
        {"l": "Moderator", "id": "ethicmind", "name": "EthicMind", "scope": "ethics"},
        {"l": "Moderator", "id": "legaleye", "name": "LegalEye", "scope": "legal"},
        {"l": "Moderator", "id": "truthseeker", "name": "TruthSeeker", "scope": "actor_truth"},
    ]


def test_actor_profile_sources_only_own_documents(db, data_tree, actors, options):
    do_import(db, data_tree, actors, options)
    driver, database = db
    for actor in actors:
        docs = retrieval.actor_profile_sources(driver, database, actor.actor_id)
        assert len(docs) == EXPECTED_BY_ACTOR[actor.actor_id]
        for d in docs:
            assert d["doc_type"] == "ActorData" and d["actor_id"] == actor.actor_id
            assert d["source_path"].startswith(f"ActorData/{actor.folder}/")
    with pytest.raises(LookupError):
        retrieval.actor_profile_sources(driver, database, "unknown")


def test_truthseeker_sees_all_articles_and_all_actordata(db, data_tree, actors, options):
    do_import(db, data_tree, actors, options)
    driver, database = db
    docs = retrieval.truthseeker_sources(driver, database)
    assert len(docs) == EXPECTED["Article"] + EXPECTED["ActorData"]
    assert {d["actor_id"] for d in docs if d["doc_type"] == "ActorData"} == set(EXPECTED_BY_ACTOR)
    assert len(retrieval.truthseeker_sources(driver, database, ["Article"])) == EXPECTED["Article"]
    only = retrieval.truthseeker_sources(driver, database, ["ActorData"], ["aerztekammer"])
    assert {d["actor_id"] for d in only} == {"aerztekammer"} and len(only) == 2
    mixed = retrieval.truthseeker_sources(driver, database, institutions=["aerztekammer"])
    assert len(mixed) == EXPECTED["Article"] + 2
    with pytest.raises(ValueError):
        retrieval.truthseeker_sources(driver, database, ["LegalText"])
    # coverage check straight from the graph
    assert q(db, """MATCH (d) WHERE (d:Article OR d:ActorData)
                    AND NOT EXISTS { (:Moderator {moderator_id:'truthseeker'})-[:USES_SOURCE]->(d) }
                    RETURN count(d) AS n""")[0]["n"] == 0


def test_actordata_shared_by_actor_and_truthseeker_without_copy(db, data_tree, actors, options):
    do_import(db, data_tree, actors, options)
    rows = q(db, """MATCH (d:ActorData)
                    RETURN d.source_path AS p,
                           [(a:Actor)-[:HAS_PROFILE_SOURCE]->(d) | a.actor_id] AS actors,
                           [(m:Moderator)-[:USES_SOURCE]->(d) | m.moderator_id] AS mods""")
    assert len(rows) == EXPECTED["ActorData"]
    for r in rows:
        assert len(r["actors"]) == 1 and r["mods"] == ["truthseeker"]
    paths = [r["p"] for r in rows]
    assert len(paths) == len(set(paths))
    # identical file in two actor folders -> two nodes, each with its own actor
    shared = [r for r in rows if r["p"].endswith("Gemeinsamer_Vertrag.md")]
    assert sorted(r["actors"][0] for r in shared) == ["aerztekammer", "sozialversicherung"]


def test_new_actor_documents_reach_truthseeker(db, data_tree, actors, options):
    do_import(db, data_tree, actors, options)
    folder = data_tree / "ActorData" / "Patientenanwalt"
    folder.mkdir()
    (folder / "Profil.md").write_text("# Profil der Patientenanwaltschaft\n\nText.\n", "utf-8")
    actors = actors + [Actor("patientenanwalt", "Patientenanwaltschaft", "Patientenanwalt")]
    rep = do_import(db, data_tree, actors, options)
    assert rep["status"] == "success" and all_checks_pass(rep)
    driver, database = db
    own = retrieval.actor_profile_sources(driver, database, "patientenanwalt")
    assert [d["title"] for d in own] == ["Profil der Patientenanwaltschaft"]
    ts = retrieval.truthseeker_sources(driver, database, ["ActorData"], ["patientenanwalt"])
    assert [d["doc_id"] for d in ts] == [own[0]["doc_id"]]


def test_legaleye_and_ethicmind_sources(db, data_tree, actors, options):
    do_import(db, data_tree, actors, options)
    driver, database = db
    legal = retrieval.legaleye_sources(driver, database)
    ethics = retrieval.ethicmind_sources(driver, database)
    assert len(legal) == EXPECTED["LegalText"] and {d["scope"] for d in legal} == {"legal"}
    assert len(ethics) == EXPECTED["EthicalText"] and {d["scope"] for d in ethics} == {"ethics"}
    xml = next(d for d in legal if d["source_path"].endswith("PARA=1=10=2020-01-01=2020-02-01.xml"))
    assert xml["valid_from"] == "2020-02-01" and xml["valid_until"] is None


def keyword_links(db):
    rows = q(db, "MATCH (a:Actor)-[r:MATCHES_KEYWORD]->(d:Article) "
                 "RETURN a.actor_id AS a, d.source_path AS p, r.source AS s ORDER BY a, p")
    return [(r["a"], r["p"].rsplit("/", 1)[-1][:20]) for r in rows], {r["s"] for r in rows}


ARZT = "2025-01-02_01_Ärztem"
KASSE = "2025-01-03_01_Kassen"
TAGGED = [("aerztekammer", ARZT), ("gesundheitsministerium", KASSE), ("sozialversicherung", KASSE)]


def test_akteure_create_matches_keyword_links(db, data_tree, actors, options):
    rep = do_import(db, data_tree, actors, options)
    links, sources = keyword_links(db)
    assert links == TAGGED and sources == {"akteure_front_matter"}
    assert rep["planned"]["matches_keyword"] == {"create": 3, "delete": 0, "unchanged": 0}
    assert all_checks_pass(rep)


def test_list_only_akteure_are_stored_without_links(db, data_tree, actors, options):
    do_import(db, data_tree, actors, options)
    row = q(db, "MATCH (d:Article) WHERE d.source_path CONTAINS 'Ärztemangel' "
                "RETURN d.akteure AS akteure")[0]
    assert row["akteure"] == ["Ärztekammer", "Krankenhäuser"]
    assert q(db, "MATCH (a:Actor) WHERE a.name = 'Krankenhäuser' RETURN a") == []


def test_actor_articles_returns_only_tagged_articles(db, data_tree, actors, options):
    do_import(db, data_tree, actors, options)
    driver, database = db
    res = retrieval.actor_articles(driver, database, "sozialversicherung")
    assert res["status"] == "active_from_akteure_tags"
    assert [d["title"] for d in res["results"]] == ["Kassenreform beschlossen"]
    assert res["results"][0]["akteure"] == ["Dachverband SV", "Gesundheitsministerium"]
    assert "not tagged" in res["note"]
    with pytest.raises(LookupError):
        retrieval.actor_articles(driver, database, "unknown")


def test_actor_articles_newest_first_undated_last(db, data_tree, actors, options):
    write = lambda name, date: (data_tree / "Articles" / name).write_text(
        f"---\ntitel: \"{name}\"\n{date}akteure: Dachverband SV\n---\n\nText\n", "utf-8")
    write("alt.md", 'datum: "01.02.2024"\n')
    write("neu.md", 'datum: "01.02.2026"\n')
    write("ohne.md", "")
    do_import(db, data_tree, actors, options)
    driver, database = db
    titles = [d["title"] for d in retrieval.actor_articles(driver, database, "sozialversicherung")["results"]]
    assert titles == ["neu.md", "Kassenreform beschlossen", "alt.md", "ohne.md"]


def test_changing_tags_updates_links(db, data_tree, actors, options):
    do_import(db, data_tree, actors, options)
    p = data_tree / "Articles" / "2025" / "2025-01-03_01_Kassenreform.md"
    p.write_text(p.read_text("utf-8").replace("akteure: Dachverband SV, Gesundheitsministerium",
                                              "akteure: Ärztekammer"), "utf-8")
    rep = do_import(db, data_tree, actors, options)
    assert rep["planned"]["matches_keyword"] == {"create": 1, "delete": 2, "unchanged": 1}
    assert keyword_links(db)[0] == [("aerztekammer", ARZT), ("aerztekammer", KASSE)]
    assert all_checks_pass(rep)


def test_emptying_tags_removes_links(db, data_tree, actors, options):
    do_import(db, data_tree, actors, options)
    p = data_tree / "Articles" / "2025" / "2025-01-03_01_Kassenreform.md"
    p.write_text(p.read_text("utf-8").replace("akteure: Dachverband SV, Gesundheitsministerium",
                                              "akteure:"), "utf-8")
    rep = do_import(db, data_tree, actors, options)
    assert keyword_links(db)[0] == [("aerztekammer", ARZT)] and all_checks_pass(rep)


def test_list_only_value_can_become_an_actor_later(db, data_tree, actors, options):
    do_import(db, data_tree, actors, options)
    folder = data_tree / "ActorData" / "Krankenhaeuser"
    folder.mkdir()
    (folder / "Profil.md").write_text("# Profil\n", "utf-8")
    options.akteure_list_only.remove("Krankenhäuser")
    actors = actors + [Actor("krankenhaeuser", "Krankenhäuser", "Krankenhaeuser", ("Krankenhäuser",))]
    rep = do_import(db, data_tree, actors, options)      # article files are unchanged
    assert ("krankenhaeuser", ARZT) in keyword_links(db)[0]
    assert rep["planned"]["matches_keyword"]["create"] == 1 and all_checks_pass(rep)


def test_repeated_import_keeps_links(db, data_tree, actors, options):
    do_import(db, data_tree, actors, options)
    rep = do_import(db, data_tree, actors, options)
    assert rep["planned"]["matches_keyword"] == {"create": 0, "delete": 0, "unchanged": 3}
    assert keyword_links(db)[0] == TAGGED


def test_validation_detects_links_that_differ_from_tags(db, data_tree, actors, options):
    do_import(db, data_tree, actors, options)
    driver, database = db
    driver.execute_query("MATCH (:Actor {actor_id: 'aerztekammer'})-[r:MATCHES_KEYWORD]->() DELETE r",
                         database_=database)
    check = graph.validate(driver, database)["matches_keyword_differs_from_akteure_tags"]
    assert check == {"count": 1, "passed": False}


def test_other_actor_article_relationships_fail_validation(db, data_tree, actors, options):
    do_import(db, data_tree, actors, options)
    driver, database = db
    driver.execute_query("MATCH (a:Actor {actor_id:'aerztekammer'}), (d:Article) "
                         "WITH a, d LIMIT 1 CREATE (a)-[:MENTIONS]->(d)", database_=database)
    check = graph.validate(driver, database)["actor_article_relationships_other_than_matches_keyword"]
    assert not check["passed"]


def test_no_relationships_across_scopes(db, data_tree, actors, options):
    do_import(db, data_tree, actors, options)
    assert q(db, "MATCH (x)-[r]->(y) WHERE x.scope <> y.scope RETURN count(r) AS n")[0]["n"] == 0
    assert q(db, "MATCH (x)-[r]->(y) WHERE x.scope IS NULL OR y.scope IS NULL "
                 "RETURN count(r) AS n")[0]["n"] == 0


def test_identical_content_stays_separate_across_scopes(db, data_tree, actors, options):
    do_import(db, data_tree, actors, options)
    rows = q(db, "MATCH (d:Document) WHERE d.source_path ENDS WITH 'Grundsaetze.md' "
                 "RETURN d.scope AS scope, d.content_hash AS h, d.doc_id AS id")
    assert sorted(r["scope"] for r in rows) == ["actor_truth", "ethics", "legal"]
    assert len({r["h"] for r in rows}) == 1 and len({r["id"] for r in rows}) == 3


def test_repeated_import_creates_no_duplicates(db, data_tree, actors, options):
    first = do_import(db, data_tree, actors, options)
    fp1 = graph.graph_fingerprint(*db)
    second = do_import(db, data_tree, actors, options)
    fp2 = graph.graph_fingerprint(*db)
    assert (fp1["nodes"], fp1["rels"]) == (fp2["nodes"], fp2["rels"])
    assert second["planned"].get("unchanged") == sum(EXPECTED.values())
    assert "create" not in second["planned"] and "update" not in second["planned"]
    created = sum(c.get("nodes_created", 0) + c.get("relationships_created", 0)
                  for c in second["committed"])
    assert created == 0
    assert first["counts"]["relationship_totals"] == second["counts"]["relationship_totals"]
    assert second["validation"]["duplicate_relationships"]["count"] == 0


def test_editing_a_file_updates_the_existing_record(db, data_tree, actors, options):
    do_import(db, data_tree, actors, options)
    path = data_tree / "EthicMind" / "Patientencharta.md"
    before = q(db, "MATCH (d:Document {source_path: 'EthicMind/Patientencharta.md'}) "
                   "RETURN d.doc_id AS id, d.content_hash AS h, d.first_imported_at AS f")[0]
    path.write_text(path.read_text("utf-8").replace("Recht auf Information.",
                                                    "Recht auf Information und Würde."), "utf-8")
    rep = do_import(db, data_tree, actors, options)
    assert rep["planned"].get("update") == 1
    after = q(db, "MATCH (d:Document {source_path: 'EthicMind/Patientencharta.md'}) "
                  "RETURN d.doc_id AS id, d.content_hash AS h, d.first_imported_at AS f, "
                  "d.text AS text")
    assert len(after) == 1
    after = after[0]
    assert after["id"] == before["id"] and after["f"] == before["f"]
    assert after["h"] != before["h"] and "Würde" in after["text"]


def test_parser_version_change_rewrites_unchanged_files(db, data_tree, actors, options):
    do_import(db, data_tree, actors, options)
    driver, database = db
    driver.execute_query("MATCH (d:LegalText) SET d.parser_version = 1, d.title = 'stale'",
                         database_=database)
    rep = do_import(db, data_tree, actors, options)
    assert rep["planned"]["by_type"]["LegalText"] == {"update": EXPECTED["LegalText"]}
    titles = {r["t"] for r in q(db, "MATCH (d:LegalText) RETURN d.title AS t")}
    assert "stale" not in titles and "TestG § 1" in titles


def test_missing_file_reported_not_deleted_then_pruned_on_request(db, data_tree, actors, options):
    do_import(db, data_tree, actors, options)
    (data_tree / "EthicMind" / "Patientencharta.md").unlink()
    rep = do_import(db, data_tree, actors, options)
    assert [m["source_path"] for m in rep["missing_from_source"]] == ["EthicMind/Patientencharta.md"]
    driver, database = db
    docs = retrieval.ethicmind_sources(driver, database)
    assert len(docs) == 2
    gone = next(d for d in docs if d["source_path"] == "EthicMind/Patientencharta.md")
    assert gone["source_present"] is False and gone["missing_since"]
    diag = retrieval.import_diagnostics(driver, database)
    assert [m["source_path"] for m in diag["missing_from_source"]] == ["EthicMind/Patientencharta.md"]

    rep = do_import(db, data_tree, actors, options, prune=True)
    assert [p["source_path"] for p in rep["pruned"]] == ["EthicMind/Patientencharta.md"]
    assert len(retrieval.ethicmind_sources(driver, database)) == 1


def test_incomplete_scan_does_not_mark_missing(db, data_tree, actors, options):
    import shutil
    do_import(db, data_tree, actors, options)
    shutil.rmtree(data_tree / "EthicMind")                       # whole folder gone
    shutil.rmtree(data_tree / "ActorData" / "Aerztekammer")      # one actor folder gone
    rep = do_import(db, data_tree, actors, options)
    codes = {i["code"] for i in rep["issues"]}
    assert {"missing_folder", "missing_actor_folder"} <= codes
    assert rep["missing_from_source"] == []
    assert q(db, "MATCH (d:Document) WHERE d.source_present = false RETURN count(d) AS n")[0]["n"] == 0


def test_dry_run_makes_no_changes(db, data_tree, actors, options):
    rep = do_import(db, data_tree, actors, options, dry_run=True)
    assert rep["planned"]["create"] == sum(EXPECTED.values())
    assert graph.graph_fingerprint(*db)["nodes"] == 0
    do_import(db, data_tree, actors, options)
    fp = graph.graph_fingerprint(*db)
    p = data_tree / "EthicMind" / "Patientencharta.md"
    p.write_text(p.read_text("utf-8") + "\nNeu.\n", "utf-8")
    (data_tree / "EthicMind" / "Grundsaetze.md").unlink()
    rep = do_import(db, data_tree, actors, options, dry_run=True, prune=True)
    assert rep["planned"]["update"] == 1 and rep["planned"]["to_prune"] == 1
    assert graph.graph_fingerprint(*db) == fp


def test_invalid_metadata_and_conflicting_ids_are_visible(db, data_tree, actors, options):
    (data_tree / "EthicMind" / "kaputt.md").write_text("---\ntitel: \"offen\n---\nx\n", "utf-8")
    for name in ("a.md", "b.md"):
        (data_tree / "EthicMind" / name).write_text("---\nid: gleich\n---\n# T\n", "utf-8")
    rep = do_import(db, data_tree, actors, options)
    assert rep["status"] == "completed_with_errors"
    errors = {(i["code"], i["path"].rsplit("/", 1)[-1]) for i in rep["issues"] if i["level"] == "error"}
    assert ("invalid_metadata", "kaputt.md") in errors
    assert {("duplicate_document_id", "a.md"), ("duplicate_document_id", "b.md")} <= errors
    assert q(db, "MATCH (d:Document {doc_id: 'ethics:gleich'}) RETURN count(d) AS n")[0]["n"] == 0


def test_persistent_id_preserves_identity_across_rename(db, data_tree, actors, options):
    p = data_tree / "EthicMind" / "Kodex.md"
    p.write_text("---\nid: kodex-1\n---\n# Kodex\n", "utf-8")
    do_import(db, data_tree, actors, options)
    p.rename(data_tree / "EthicMind" / "Kodex_neu.md")
    rep = do_import(db, data_tree, actors, options)
    assert rep["status"] == "success", rep["issues"]
    rows = q(db, "MATCH (d:Document {doc_id: 'ethics:kodex-1'}) RETURN d.source_path AS p")
    assert rows == [{"p": "EthicMind/Kodex_neu.md"}]
    assert rep["missing_from_source"] == []


def test_foreign_nodes_are_not_modified(db, data_tree, actors, options):
    driver, database = db
    driver.execute_query("CREATE (:Actor {actor_id: 'aerztekammer', name: 'fremd'})",
                         database_=database)
    rep = do_import(db, data_tree, actors, options)
    assert rep["status"] == "failed"
    assert any(i["code"] == "node_owned_by_other" for i in rep["issues"])
    assert graph.graph_fingerprint(*db)["nodes"] == 1


def test_get_document_returns_content_metadata_provenance(db, data_tree, actors, options):
    do_import(db, data_tree, actors, options)
    driver, database = db
    doc_id = q(db, "MATCH (d:Article) WHERE d.source_path CONTAINS 'Ärztemangel' "
                   "RETURN d.doc_id AS id")[0]["id"]
    doc = retrieval.get_document(driver, database, doc_id)
    assert doc["title"] == 'Ärztemangel im Bezirk: "Wir brauchen Hausärztinnen"'
    assert doc["title_origin"] == "metadata"
    assert doc["publisher"] == "Testzeitung" and doc["publication_date"] == "2024-12-31"
    assert doc["press_review"] == "Pressespiegel vom 02.01.2025"
    assert doc["original_content"].startswith("---\ntitel:")
    assert "Kassenstelle" in doc["text"] and "example.invalid" not in doc["text"]
    assert doc["type_labels"] == ["Article"]
    incoming = {(i["relationship"], i["from_id"]) for i in doc["provenance"]["incoming_relationships"]}
    assert incoming == {("USES_SOURCE", "truthseeker"), ("MATCHES_KEYWORD", "aerztekammer")}
    assert doc["first_imported_at"] and doc["updated_at"] and doc["importer"] == "svs_healthdb"
    assert retrieval.get_document(driver, database, "nope") is None
