"""Recipes an operator wrote, kept as files rather than inside a definition.

An instrument definition describes a model of instrument and is replaced
wholesale when it is updated; a procedure someone wrote for their experiment
must not live there. These tests pin what makes the second location safe: a
name is never a path, an invalid recipe never lands, and a library file can
never change what an existing definition recipe name means.
"""

from __future__ import annotations

import textwrap

import pytest
import yaml

from lab_executor.models.instrument_def import InstrumentDefinition
from lab_executor.recipe_library import (
    RecipeLibrary,
    RecipeLibraryError,
    parse_recipe,
    resolve_recipe,
)

SIMPLE = textwrap.dedent("""
    description: 電圧をかけて測る
    parameters:
      - {name: target_v, type: float, required: true, range: [0, 30]}
    steps:
      - {command: set_voltage, args: {voltage: "${params.target_v}"}}
      - {command: measure_voltage, result_as: measured}
""")

DEFINITION_YAML = textwrap.dedent("""
    metadata:
      manufacturer: Kikusui
      model: PMX35-3A
      category: power_supply
      support_level: experimental
      definition_version: "0.1.0"
    commands:
      set_voltage:
        scpi: "VOLT {voltage}"
        type: write
        description: 電圧設定
        parameters:
          - {name: voltage, type: float, range: [0, 36.75]}
      measure_voltage:
        scpi: "MEAS:VOLT?"
        type: query
        description: 電圧測定
        returns: {type: float, unit: V}
    recipes:
      warm_up:
        description: 定義側のレシピ
        steps:
          - {command: measure_voltage}
""")


@pytest.fixture
def library(tmp_path):
    return RecipeLibrary(tmp_path)


def test_a_saved_recipe_can_be_read_back_as_written(library):
    library.save("iv_sweep", SIMPLE)
    assert library.read_text("iv_sweep") == SIMPLE
    recipe = library.get("iv_sweep")
    assert recipe.description == "電圧をかけて測る"
    assert len(recipe.steps) == 2


def test_an_invalid_recipe_never_lands(library):
    """A save that would leave an unrunnable file is refused instead."""
    with pytest.raises(RecipeLibraryError):
        library.save("broken", "steps: [{no_such_field: 1}]")
    assert library.list() == []


def test_a_save_does_not_destroy_the_previous_version_on_failure(library):
    library.save("keep", SIMPLE)
    with pytest.raises(RecipeLibraryError):
        library.save("keep", "steps: [[[")
    assert library.read_text("keep") == SIMPLE


@pytest.mark.parametrize(
    "name",
    ["../escape", "sub/dir", "with space", ".hidden", "", "x" * 65, "1st/../2nd"],
)
def test_a_name_is_never_a_path(library, name):
    with pytest.raises(RecipeLibraryError):
        library.save(name, SIMPLE)
    with pytest.raises(RecipeLibraryError):
        library.read_text(name)


def test_a_library_recipe_may_not_shadow_a_definition_recipe(library):
    """Otherwise which recipe a name refers to depends on where we looked."""
    with pytest.raises(RecipeLibraryError, match="機器定義"):
        library.save("warm_up", SIMPLE, reserved={"warm_up"})


def test_the_definition_wins_when_both_have_the_name(library, tmp_path):
    """A library must never change what an existing name already meant."""
    definition = InstrumentDefinition(**yaml.safe_load(DEFINITION_YAML))
    # Written directly, bypassing the save-time guard, to prove resolution
    # order holds even if a file appears by other means.
    (tmp_path / "warm_up.yaml").write_text(SIMPLE, encoding="utf-8")
    resolved = resolve_recipe("warm_up", definition, library)
    assert resolved.description == "定義側のレシピ"


def test_a_library_recipe_is_found_when_the_definition_has_none(library):
    definition = InstrumentDefinition(**yaml.safe_load(DEFINITION_YAML))
    library.save("iv_sweep", SIMPLE)
    assert resolve_recipe("iv_sweep", definition, library).description == "電圧をかけて測る"
    assert resolve_recipe("absent", definition, library) is None


def test_listing_reports_what_each_recipe_is(library):
    library.save("iv_sweep", SIMPLE)
    entry = library.list()[0]
    assert entry.name == "iv_sweep"
    assert entry.description == "電圧をかけて測る"
    assert entry.step_count == 2
    assert entry.parameters == ("target_v",)
    assert entry.error == ""


def test_a_file_that_has_become_unreadable_is_listed_with_its_reason(library, tmp_path):
    """Hiding it would look like the recipe was deleted."""
    library.save("good", SIMPLE)
    (tmp_path / "bad.yaml").write_text("steps: [[[", encoding="utf-8")
    by_name = {e.name: e for e in library.list()}
    assert by_name["good"].error == ""
    assert by_name["bad"].error


def test_an_absent_library_directory_is_empty_not_an_error(tmp_path):
    assert RecipeLibrary(tmp_path / "not-created-yet").list() == []


def test_a_missing_recipe_says_so(library):
    with pytest.raises(RecipeLibraryError, match="見つかりません"):
        library.read_text("absent")


def test_deleting_reports_whether_there_was_anything_to_delete(library):
    library.save("temporary", SIMPLE)
    assert library.delete("temporary") is True
    assert library.delete("temporary") is False


def test_a_parse_error_says_where_it_is():
    with pytest.raises(RecipeLibraryError, match="書き方に誤り"):
        parse_recipe("steps: [{command: 1}]\nparameters: not-a-list")


def test_empty_content_is_refused_rather_than_saved_as_nothing():
    with pytest.raises(RecipeLibraryError, match="中身がありません"):
        parse_recipe("")
