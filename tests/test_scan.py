import json
import shutil
import unicodedata

import pytest

from conftest import EXPECTED, EXPECTED_BY_ACTOR
from healthdb import model
from healthdb.config import (Actor, ConfigError, akteure_mapping, load_actors,
                             load_import_options)
from healthdb.scan import default_doc_id, normalise_rel_path, scan


def codes(result, level=None):
    return [i.code for i in result.issues if level is None or i.level == level]


def by_path(result):
    return {e.source_path: e for e in result.entries}


def test_folder_mapping_types_and_scopes(data_tree, actors, options):
    r = scan(data_tree, actors, options)
    assert not codes(r, "error")
    counts = {}
    for e in r.entries:
        counts[e.doc_type] = counts.get(e.doc_type, 0) + 1
        assert e.scope == model.SCOPE_FOR_TYPE[e.doc_type]
        assert (e.actor_id is not None) == (e.doc_type == model.ACTOR_DATA)
    assert counts == EXPECTED
    assert r.complete_doc_types == {"Article", "LegalText", "EthicalText"}
    assert r.complete_actor_ids == set(EXPECTED_BY_ACTOR)


def test_actor_folders_map_to_actors(data_tree, actors, options):
    r = scan(data_tree, actors, options)
    per_actor = {}
    for e in r.entries:
        if e.actor_id:
            assert e.source_path.split("/")[1].casefold() == \
                next(a.folder for a in actors if a.actor_id == e.actor_id).casefold()
            per_actor[e.actor_id] = per_actor.get(e.actor_id, 0) + 1
    assert per_actor == EXPECTED_BY_ACTOR


def test_folder_names_are_case_insensitive(data_tree, actors, options):
    (data_tree / "Articles").rename(data_tree / "articles")
    (data_tree / "ActorData" / "Aerztekammer").rename(data_tree / "ActorData" / "aerztekammer")
    r = scan(data_tree, actors, options)
    assert sum(e.doc_type == "Article" for e in r.entries) == 2
    assert sum(e.actor_id == "aerztekammer" for e in r.entries) == 2


def test_no_truthseeker_folder_required(data_tree, actors, options):
    r = scan(data_tree, actors, options)
    assert "missing_folder" not in codes(r)


def test_compilation_is_excluded_and_reported(data_tree, actors, options):
    r = scan(data_tree, actors, options)
    assert [x["path"] for x in r.excluded] == ["Articles/2025-01-02_Pressespiegel_gesamt.md"]
    assert "Articles/2025-01-02_Pressespiegel_gesamt.md" in r.present_paths
    assert not any("Pressespiegel_gesamt" in e.source_path for e in r.entries)


def test_nested_article_folder_is_read(data_tree, actors, options):
    assert "Articles/2025/2025-01-03_01_Kassenreform.md" in by_path(scan(data_tree, actors, options))


def test_german_filename_and_utf8_metadata(data_tree, actors, options):
    e = by_path(scan(data_tree, actors, options))["Articles/2025-01-02_01_Ärztemangel-im-Bezirk.md"]
    assert unicodedata.is_normalized("NFC", e.source_path)
    assert e.title == 'Ärztemangel im Bezirk: "Wir brauchen Hausärztinnen"'
    assert e.title_origin == "metadata"


def test_nfd_path_normalises_to_same_id_as_nfc(data_tree):
    nfc_name = "Ärztemangel.md"
    nfd_name = unicodedata.normalize("NFD", nfc_name)
    assert nfd_name != nfc_name
    a = normalise_rel_path(data_tree / "Articles" / nfc_name, data_tree)
    b = normalise_rel_path(data_tree / "Articles" / nfd_name, data_tree)
    assert a == b == "Articles/Ärztemangel.md"
    assert default_doc_id("actor_truth", a) == default_doc_id("actor_truth", b)


def test_title_origins(data_tree, actors, options):
    r = by_path(scan(data_tree, actors, options))
    s = r["ActorData/Gesundheitsministerium/Strategie_2024.md"]
    assert (s.title, s.title_origin) == ("Gesundheitsstrategie 2024", "heading")
    j = r["ActorData/Sozialversicherung/Jahresbericht.md"]
    assert (j.title, j.title_origin) == ("Jahresbericht", "filename")


def test_ids_are_stable_and_scope_separated(data_tree, actors, options):
    a = by_path(scan(data_tree, actors, options))
    b = by_path(scan(data_tree, actors, options))
    assert {p: e.doc_id for p, e in a.items()} == {p: e.doc_id for p, e in b.items()}
    same_content = [a["ActorData/Aerztekammer/Grundsaetze.md"], a["EthicMind/Grundsaetze.md"],
                    a["LegalEye/Grundsaetze.md"]]
    assert len({e.content_hash for e in same_content}) == 1
    assert len({e.doc_id for e in same_content}) == 3
    assert len({e.scope for e in same_content}) == 3


def test_rename_creates_new_identity(data_tree, actors, options):
    before = by_path(scan(data_tree, actors, options))["EthicMind/Patientencharta.md"]
    (data_tree / "EthicMind" / "Patientencharta.md").rename(data_tree / "EthicMind" / "Charta.md")
    after = by_path(scan(data_tree, actors, options))["EthicMind/Charta.md"]
    assert after.content_hash == before.content_hash and after.doc_id != before.doc_id


def test_editing_keeps_identity_changes_hash(data_tree, actors, options):
    before = by_path(scan(data_tree, actors, options))["EthicMind/Patientencharta.md"]
    p = data_tree / "EthicMind" / "Patientencharta.md"
    p.write_text(p.read_text(encoding="utf-8") + "\nNeuer Absatz.\n", encoding="utf-8")
    after = by_path(scan(data_tree, actors, options))["EthicMind/Patientencharta.md"]
    assert after.doc_id == before.doc_id and after.content_hash != before.content_hash


def test_identical_content_reported(data_tree, actors, options):
    r = scan(data_tree, actors, options)
    msgs = [i.reason for i in r.issues if i.code == "identical_content"]
    assert any("ActorData/Aerztekammer/Gemeinsamer_Vertrag.md" in m
               and "ActorData/Sozialversicherung/Gemeinsamer_Vertrag.md" in m for m in msgs)


def test_missing_expected_folder(data_tree, actors, options):
    shutil.rmtree(data_tree / "EthicMind")
    r = scan(data_tree, actors, options)
    assert "missing_folder" in codes(r, "warning")
    assert "EthicalText" not in r.complete_doc_types
    assert {"folder": "ethicmind", "status": "missing"}.items() <= \
        next(f for f in r.folders if f["folder"] == "ethicmind").items()


def test_empty_folder_distinct_from_missing(data_tree, actors, options):
    shutil.rmtree(data_tree / "EthicMind")
    (data_tree / "EthicMind").mkdir()
    r = scan(data_tree, actors, options)
    assert "empty_folder" in codes(r) and "missing_folder" not in codes(r)
    assert "EthicalText" in r.complete_doc_types


def test_unconfigured_actor_folder_is_reported(data_tree, actors, options):
    (data_tree / "ActorData" / "Patientenanwalt").mkdir()
    (data_tree / "ActorData" / "Patientenanwalt" / "x.md").write_text("# X\n", encoding="utf-8")
    r = scan(data_tree, actors, options)
    issue = next(i for i in r.issues if i.code == "unconfigured_actor_folder")
    assert "Patientenanwalt" in issue.path and "1 file(s)" in issue.reason
    assert not any("Patientenanwalt" in e.source_path for e in r.entries)


def test_missing_actor_folder_is_reported(data_tree, actors, options):
    shutil.rmtree(data_tree / "ActorData" / "Gesundheitsministerium")
    r = scan(data_tree, actors, options)
    assert "missing_actor_folder" in codes(r)
    assert "gesundheitsministerium" not in r.complete_actor_ids


def test_new_actor_configuration_is_picked_up(data_tree, actors, options):
    (data_tree / "ActorData" / "Patientenanwalt").mkdir()
    (data_tree / "ActorData" / "Patientenanwalt" / "Profil.md").write_text("# Profil\n", "utf-8")
    actors.append(Actor("patientenanwalt", "Patientenanwaltschaft", "Patientenanwalt"))
    r = scan(data_tree, actors, options)
    e = by_path(r)["ActorData/Patientenanwalt/Profil.md"]
    assert e.actor_id == "patientenanwalt" and e.doc_type == "ActorData"


def test_file_problems_are_distinguished(data_tree, actors, options):
    (data_tree / "EthicMind" / "leer.md").write_bytes(b"")
    (data_tree / "EthicMind" / "latin1.md").write_bytes("Grundsätze".encode("latin-1"))
    (data_tree / "EthicMind" / "bad_meta.md").write_text("---\nunbekannt: 1\n---\nText\n", "utf-8")
    (data_tree / "EthicMind" / "notes.txt").write_text("x", "utf-8")
    r = scan(data_tree, actors, options)
    found = {i.code: i.path for i in r.issues if i.level in ("error", "warning")}
    assert found["empty_file"].endswith("leer.md")
    assert found["utf8_decode_error"].endswith("latin1.md")
    assert found["invalid_metadata"].endswith("bad_meta.md")
    assert found["unsupported_extension"].endswith("notes.txt")
    assert [i.level for i in r.issues if i.code == "missing_optional_metadata"]  # info only
    assert all(i.level == "info" for i in r.issues if i.code == "missing_optional_metadata")
    paths = by_path(r)
    assert not any(p.endswith(("leer.md", "latin1.md", "bad_meta.md")) for p in paths)


def test_duplicate_persistent_ids_reject_both(data_tree, actors, options):
    for name in ("a.md", "b.md"):
        (data_tree / "EthicMind" / name).write_text(f"---\nid: charta-1\n---\n# {name}\n", "utf-8")
    r = scan(data_tree, actors, options)
    dup = [i for i in r.issues if i.code == "duplicate_document_id"]
    assert len(dup) == 2 and all(i.level == "error" for i in dup)
    assert "ethics:charta-1" not in {e.doc_id for e in r.entries}


def test_persistent_id_is_scoped(data_tree, actors, options):
    (data_tree / "EthicMind" / "p.md").write_text("---\nid: shared-1\n---\n# P\n", "utf-8")
    (data_tree / "LegalEye" / "p.md").write_text("---\nid: shared-1\n---\n# P\n", "utf-8")
    ids = {e.doc_id for e in scan(data_tree, actors, options).entries}
    assert {"ethics:shared-1", "legal:shared-1"} <= ids


def test_akteure_map_to_actors(data_tree, actors, options):
    r = by_path(scan(data_tree, actors, options))
    tagged = r["Articles/2025/2025-01-03_01_Kassenreform.md"]
    assert tagged.matched_actor_ids == ("gesundheitsministerium", "sozialversicherung")
    list_only = r["Articles/2025-01-02_01_Ärztemangel-im-Bezirk.md"]
    assert list_only.matched_actor_ids == ("aerztekammer",)   # Krankenhäuser gets no link


def write_article(data_tree, name, akteure_line):
    (data_tree / "Articles" / name).write_text(f"---\ntitel: \"T\"\n{akteure_line}---\n\nText\n", "utf-8")


def test_empty_and_absent_akteure_are_accepted(data_tree, actors, options):
    write_article(data_tree, "leer.md", "akteure:\n")
    write_article(data_tree, "ohne.md", "")
    r = scan(data_tree, actors, options)
    paths = by_path(r)
    assert paths["Articles/leer.md"].matched_actor_ids == ()
    assert paths["Articles/ohne.md"].matched_actor_ids == ()
    assert [i.path for i in r.issues if i.code == "missing_akteure"] == ["Articles/ohne.md"]
    assert not codes(r, "error")


def test_unknown_akteur_rejects_the_file(data_tree, actors, options):
    write_article(data_tree, "tippfehler.md", "akteure: Aerztekammer\n")
    r = scan(data_tree, actors, options)
    issue = next(i for i in r.issues if i.code == "unknown_akteur")
    assert issue.level == "error" and "Aerztekammer" in issue.reason
    assert "Articles/tippfehler.md" not in by_path(r)
    assert "Articles/tippfehler.md" in r.present_paths        # not reported as missing


def test_akteure_only_allowed_on_articles(data_tree, actors, options):
    (data_tree / "EthicMind" / "x.md").write_text("---\nakteure: Ärztekammer\n---\n# X\n", "utf-8")
    r = scan(data_tree, actors, options)
    assert any(i.code == "invalid_metadata" and i.path == "EthicMind/x.md" for i in r.issues)


def test_akteure_value_mapped_and_list_only_is_fatal(data_tree, actors, options):
    options.akteure_list_only.append("Ärztekammer")
    r = scan(data_tree, actors, options)
    assert r.fatal and "akteure_config_conflict" in codes(r, "error")


def test_missing_data_dir_is_fatal(tmp_path, actors, options):
    r = scan(tmp_path / "nope", actors, options)
    assert r.fatal and "missing_data_directory" in codes(r, "error")


def test_actor_config_validation(tmp_path):
    p = tmp_path / "actors.json"
    p.write_text(json.dumps([{"id": "a", "name": "A", "folder": "X"},
                             {"id": "a", "name": "B", "folder": "Y"}]), "utf-8")
    with pytest.raises(ConfigError, match="duplicate actor id"):
        load_actors(p)
    p.write_text(json.dumps([{"id": "a", "name": "A", "folder": "X"},
                             {"id": "b", "name": "B", "folder": "x"}]), "utf-8")
    with pytest.raises(ConfigError, match="duplicate actor folder"):
        load_actors(p)
    p.write_text(json.dumps([{"id": "Bad Id", "name": "A", "folder": "X"}]), "utf-8")
    with pytest.raises(ConfigError, match="must match"):
        load_actors(p)
    p.write_text(json.dumps([{"id": "a", "name": "A", "folder": "X", "akteure_labels": ["L"]},
                             {"id": "b", "name": "B", "folder": "Y", "akteure_labels": ["L"]}]),
                 "utf-8")
    with pytest.raises(ConfigError, match="mapped twice"):
        load_actors(p)


def test_shipped_configuration_is_valid():
    actors = load_actors()
    assert [a.actor_id for a in actors] == ["gesundheitsministerium", "sozialversicherung",
                                            "aerztekammer"]
    assert next(a for a in actors if a.actor_id == "aerztekammer").name == "Ärztekammer"
    assert akteure_mapping(actors) == {"Gesundheitsministerium": "gesundheitsministerium",
                                       "Dachverband SV": "sozialversicherung",
                                       "Ärztekammer": "aerztekammer"}
    options = load_import_options()
    assert options.exclusions[0].pattern == "*_Pressespiegel_gesamt.md"
    assert options.akteure_list_only == ["Krankenhäuser", "Patientenvertretung", "Bundesländer", "Finanzministerium", "WK & AK", "Gesetzgeber", "Industrie", "Landeswirtschaftskammer"]
