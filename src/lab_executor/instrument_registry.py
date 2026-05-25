from __future__ import annotations
import logging
from pathlib import Path

import yaml
from pydantic import ValidationError

from lab_executor.models.instrument_def import InstrumentDefinition
from lab_executor.utils.idn_matcher import match_definition

logger = logging.getLogger(__name__)


class InstrumentRegistry:
    def __init__(self, yaml_dir: str | Path) -> None:
        self._yaml_dir = Path(yaml_dir)
        self._definitions: list[InstrumentDefinition] = []
        self._load_all()

    def _load_all(self) -> None:
        self._definitions.clear()
        for path in sorted(self._yaml_dir.glob("*.yaml")):
            if path.name.startswith("_"):
                continue  # _template.yaml などをスキップ
            self._load_file(path)
        logger.info("%d 件の機器定義をロードしました。", len(self._definitions))

    def _load_file(self, path: Path) -> None:
        try:
            with path.open(encoding="utf-8") as f:
                data = yaml.safe_load(f)
            defn = InstrumentDefinition.model_validate(data)
            self._definitions.append(defn)
            logger.debug("ロード済み: %s (%s)", defn.display_name, path.name)
        except (yaml.YAMLError, ValidationError, Exception) as e:
            logger.warning("定義ファイルのロード失敗 %s: %s", path.name, e)

    def reload(self) -> int:
        """ホットリロード。再ロード後の定義件数を返す。"""
        self._load_all()
        return len(self._definitions)

    def match_idn(self, idn_response: str) -> InstrumentDefinition | None:
        return match_definition(idn_response, self._definitions)

    def list_definitions(self) -> list[dict]:
        return [
            {
                "manufacturer": d.metadata.manufacturer,
                "model": d.metadata.model,
                "description": d.metadata.description,
                "command_count": len(d.commands),
            }
            for d in self._definitions
        ]

    def get_definition(self, manufacturer: str, model: str) -> InstrumentDefinition | None:
        for d in self._definitions:
            if d.metadata.manufacturer == manufacturer and d.metadata.model == model:
                return d
        return None
