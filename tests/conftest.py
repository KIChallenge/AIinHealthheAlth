import shutil
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from healthdb.config import Actor, Exclusion, ImportOptions  # noqa: E402

FIXTURE_DATA = Path(__file__).resolve().parent / "fixtures" / "data"

DEFAULT_ACTORS = [
    Actor("gesundheitsministerium", "Gesundheitsministerium", "Gesundheitsministerium",
          ("Gesundheitsministerium",)),
    Actor("sozialversicherung", "Sozialversicherung", "Sozialversicherung", ("Dachverband SV",)),
    Actor("aerztekammer", "Ärztekammer", "Aerztekammer", ("Ärztekammer",)),
]

# Expected numbers for the unmodified fixture tree.
EXPECTED = {"Article": 2, "ActorData": 5, "LegalText": 3, "EthicalText": 2}
EXPECTED_BY_ACTOR = {"gesundheitsministerium": 1, "sozialversicherung": 2, "aerztekammer": 2}


@pytest.fixture
def data_tree(tmp_path):
    """A private, modifiable copy of the fixture data tree."""
    dest = tmp_path / "data"
    shutil.copytree(FIXTURE_DATA, dest)
    return dest


@pytest.fixture
def actors():
    return list(DEFAULT_ACTORS)


@pytest.fixture
def options():
    return ImportOptions(exclusions=[Exclusion("articles", "*_Pressespiegel_gesamt.md",
                                               "compilation_not_single_article")],
                         batch_size=2,
                         akteure_list_only=["Krankenhäuser", "Patientenvertretung"])
