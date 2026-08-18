"""User-authored recipes, kept as files the serve process owns.

Until now a recipe could only live inside an instrument definition. Those files
are shipped artifacts: they describe what a model of instrument can do, they are
reviewed, and they are replaced wholesale when a definition is updated. A
procedure someone writes for their own experiment does not belong there.

So this is a second place a recipe can come from -- a directory of standalone
YAML files. The runtime resolves a recipe from the definition first and from the
library second, and a library file may not take a definition recipe's name, so
which one runs is never ambiguous.

The library is owned by the serve process rather than by whatever is editing it.
That is what makes a save meaningful: the same runtime that will execute the
recipe is the one that parses and validates it, so a file that lands here is one
the runtime has already agreed it can run.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError

from lab_executor.models.instrument_def import RecipeDefinition

#: Recipe names, which are also file names. Deliberately narrow: a name is not a
#: path, and nothing outside the configured directory is ever written or read.
_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}\Z")

#: Refuse to read a file large enough that something else is going on.
MAX_BYTES = 1_000_000


class RecipeLibraryError(ValueError):
    """A recipe could not be read, written, or understood."""


def default_recipe_dir() -> Path:
    """Where recipes live unless the serve process was told otherwise."""
    import os

    raw = os.environ.get("LAB_EXECUTOR_RECIPE_DIR", "").strip()
    if raw:
        return Path(raw).expanduser()
    return Path.home() / ".lab-executor" / "recipes"


@dataclass(frozen=True)
class RecipeSummary:
    """One entry in the library listing."""

    name: str
    description: str = ""
    step_count: int = 0
    parameters: tuple[str, ...] = ()
    #: Set when the file is present but does not parse. The listing still shows
    #: it: a recipe that has become unreadable is something to fix, not to hide.
    error: str = ""


def _check_name(name: str) -> str:
    if not isinstance(name, str) or _NAME.fullmatch(name) is None:
        raise RecipeLibraryError(
            f"レシピ名として扱えません: {name!r}。"
            "英数字で始まり、英数字・アンダースコア・ハイフンのみ使えます。"
        )
    return name


def parse_recipe(text: str) -> RecipeDefinition:
    """Read recipe YAML, reporting what is wrong rather than raising raw errors."""
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise RecipeLibraryError(f"YAML として読めません: {exc}") from exc
    if data is None:
        raise RecipeLibraryError("中身がありません。")
    if not isinstance(data, dict):
        raise RecipeLibraryError(
            "レシピは description / parameters / steps を持つ mapping で書きます。"
        )
    try:
        return RecipeDefinition.model_validate(data)
    except ValidationError as exc:
        raise RecipeLibraryError(_readable(exc)) from exc


def _readable(exc: ValidationError) -> str:
    """Turn pydantic's report into something an operator can act on."""
    lines = []
    for error in exc.errors()[:5]:
        where = ".".join(str(p) for p in error.get("loc", ())) or "(全体)"
        lines.append(f"{where}: {error.get('msg', '')}")
    return "レシピの書き方に誤りがあります — " + " / ".join(lines)


class RecipeLibrary:
    """A directory of standalone recipe files."""

    def __init__(self, directory: Path | str | None = None) -> None:
        self._dir = Path(directory) if directory is not None else default_recipe_dir()

    @property
    def directory(self) -> Path:
        return self._dir

    def _path(self, name: str) -> Path:
        return self._dir / f"{_check_name(name)}.yaml"

    def list(self) -> list[RecipeSummary]:
        """Every recipe in the library, unreadable ones included."""
        if not self._dir.is_dir():
            return []
        summaries: list[RecipeSummary] = []
        for path in sorted(self._dir.glob("*.yaml")):
            name = path.stem
            if _NAME.fullmatch(name) is None:
                continue
            try:
                recipe = parse_recipe(path.read_text(encoding="utf-8"))
            except (RecipeLibraryError, OSError, UnicodeDecodeError) as exc:
                summaries.append(RecipeSummary(name=name, error=str(exc)))
                continue
            summaries.append(
                RecipeSummary(
                    name=name,
                    description=recipe.description,
                    step_count=len(recipe.steps),
                    parameters=tuple(p.name for p in recipe.parameters),
                )
            )
        return summaries

    def read_text(self, name: str) -> str:
        """The file as written, so an editor shows what the author wrote."""
        path = self._path(name)
        if not path.is_file():
            raise RecipeLibraryError(f"レシピが見つかりません: {name}")
        if path.stat().st_size > MAX_BYTES:
            raise RecipeLibraryError(f"レシピの大きさが上限を超えています: {name}")
        return path.read_text(encoding="utf-8")

    def get(self, name: str) -> RecipeDefinition:
        return parse_recipe(self.read_text(name))

    def save(self, name: str, text: str, *, reserved: set[str] | None = None) -> None:
        """Validate, then write. An invalid recipe never reaches the directory.

        ``reserved`` are names the instrument definitions already use. A library
        file may not take one of them, because then which recipe a name refers
        to would depend on where the runtime looked first.
        """
        _check_name(name)
        if reserved and name in reserved:
            raise RecipeLibraryError(
                f"'{name}' は機器定義のレシピと同じ名前です。別の名前にしてください。"
            )
        if len(text.encode("utf-8")) > MAX_BYTES:
            raise RecipeLibraryError("レシピの大きさが上限を超えています。")
        parse_recipe(text)
        self._dir.mkdir(parents=True, exist_ok=True)
        # Write beside the target and replace, so an interrupted save cannot
        # leave a half-written recipe where a whole one used to be.
        temporary = self._dir / f".{name}.yaml.tmp"
        temporary.write_text(text, encoding="utf-8")
        temporary.replace(self._path(name))

    def delete(self, name: str) -> bool:
        path = self._path(name)
        if not path.is_file():
            return False
        path.unlink()
        return True


def resolve_recipe(
    name: str,
    definition: Any,
    library: RecipeLibrary | None,
) -> RecipeDefinition | None:
    """Find a recipe by name: the instrument's own first, the library second.

    Definition recipes win, so adding a library never changes what an existing
    name means.
    """
    recipes = getattr(definition, "recipes", None) or {}
    found = recipes.get(name)
    if found is not None:
        return found
    if library is None:
        return None
    try:
        return library.get(name)
    except RecipeLibraryError:
        return None


__all__ = [
    "MAX_BYTES",
    "RecipeLibrary",
    "RecipeLibraryError",
    "RecipeSummary",
    "default_recipe_dir",
    "parse_recipe",
    "resolve_recipe",
]
