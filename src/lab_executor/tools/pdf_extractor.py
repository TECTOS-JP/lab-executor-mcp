from __future__ import annotations
import logging
from fastmcp import FastMCP

logger = logging.getLogger(__name__)

try:
    import pdfplumber
    _PDF_AVAILABLE = True
except ImportError:
    _PDF_AVAILABLE = False


def register_tools(mcp: FastMCP) -> None:

    @mcp.tool()
    async def extract_pdf_commands(
        pdf_path: str,
        manufacturer: str,
        model: str,
        page_range: str = "",
    ) -> dict:
        """
        プログラマーズマニュアルの PDF から SCPI コマンドを抽出し、
        YAML 定義ファイルの草案テキストを生成する。
        生成結果は instruments/ に自動保存されないため、
        内容を確認してから手動で保存すること。
        pdf_path: PDF ファイルの絶対パス
        manufacturer: メーカー名（YAML メタデータ用）
        model: モデル名（YAML メタデータ用）
        page_range: 抽出対象ページ範囲（例: "10-50"、空文字で全ページ）
        """
        if not _PDF_AVAILABLE:
            return {
                "success": False,
                "error": "DependencyMissing",
                "message": "pdfplumber がインストールされていません。`pip install pdfplumber` を実行してください。",
            }

        import asyncio
        loop = asyncio.get_event_loop()

        def _extract():
            from pathlib import Path
            path = Path(pdf_path)
            if not path.exists():
                raise FileNotFoundError(f"ファイルが見つかりません: {pdf_path}")

            pages_to_read = _parse_page_range(page_range)

            with pdfplumber.open(str(path)) as pdf:
                total_pages = len(pdf.pages)
                if pages_to_read:
                    indices = [p - 1 for p in pages_to_read if 1 <= p <= total_pages]
                else:
                    indices = list(range(total_pages))

                texts = []
                for i in indices:
                    t = pdf.pages[i].extract_text()
                    if t:
                        texts.append(t)

            full_text = "\n".join(texts)
            return full_text, total_pages

        try:
            full_text, total_pages = await loop.run_in_executor(None, _extract)
        except FileNotFoundError as e:
            return {"success": False, "error": "FileNotFound", "message": str(e)}
        except Exception as e:
            return {"success": False, "error": "PDFReadError", "message": str(e)}

        # 簡易 SCPI コマンド候補の抽出（正規表現ベース）
        candidates = _extract_scpi_candidates(full_text)

        # YAML 草案テキストを生成
        yaml_draft = _build_yaml_draft(manufacturer, model, pdf_path, candidates)

        return {
            "success": True,
            "data": {
                "total_pages": total_pages,
                "extracted_pages": len(full_text.splitlines()),
                "candidate_count": len(candidates),
                "yaml_draft": yaml_draft,
                "note": (
                    "この YAML 草案は自動抽出のため不完全な場合があります。"
                    "コマンド構文・パラメータ範囲を必ずマニュアルと照合してから "
                    "instruments/<メーカー>_<モデル>.yaml として保存してください。"
                ),
            },
        }


def _parse_page_range(page_range: str) -> list[int]:
    if not page_range.strip():
        return []
    pages = []
    for part in page_range.split(","):
        part = part.strip()
        if "-" in part:
            lo, hi = part.split("-", 1)
            pages.extend(range(int(lo), int(hi) + 1))
        else:
            pages.append(int(part))
    return pages


def _extract_scpi_candidates(text: str) -> list[dict]:
    import re
    candidates = []
    seen = set()

    # パターン1: コロンを含む大文字のSCPI構文（例: MEASure:VOLTage?）
    pattern = re.compile(
        r'\b([A-Z][A-Z0-9]{1,}(?::[A-Z][A-Z0-9?{}\[\]]*)+)\b'
    )
    for m in pattern.finditer(text):
        cmd = m.group(1)
        if cmd in seen:
            continue
        seen.add(cmd)
        # 前後の文脈（説明文候補）を取得
        start = max(0, m.start() - 5)
        end = min(len(text), m.end() + 120)
        context = text[start:end].replace("\n", " ").strip()
        candidates.append({"command": cmd, "context": context})

    return candidates[:200]  # 上限200件


def _build_yaml_draft(
    manufacturer: str,
    model: str,
    pdf_path: str,
    candidates: list[dict],
) -> str:
    import re
    from pathlib import Path

    lines = [
        f"metadata:",
        f'  manufacturer: "{manufacturer}"',
        f'  model: "{model}"',
        f"  description: \"\"",
        f'  manual_ref: "{Path(pdf_path).name}"',
        f"",
        f"identification:",
        f'  manufacturer_match: "{manufacturer.upper()}"',
        f'  model_regex: "{re.escape(model)}"',
        f"",
        f"connection:",
        f"  default_timeout_ms: 5000",
        f'  read_termination: "\\n"',
        f'  write_termination: "\\n"',
        f"",
        f"commands:",
    ]

    if not candidates:
        lines.append("  # コマンド候補が抽出できませんでした。手動で追加してください。")
    else:
        for c in candidates:
            raw = c["command"]
            # snake_case のキーを生成
            key = raw.lower().replace(":", "_").replace("?", "_query").replace(" ", "_")
            key = re.sub(r"[^a-z0-9_]", "", key).strip("_")
            cmd_type = "query" if raw.endswith("?") else "write"
            lines += [
                f"  {key}:",
                f'    scpi: "{raw}"',
                f'    type: "{cmd_type}"',
                f'    description: "# TODO: {c["context"][:80]}"',
                f"    parameters: []",
                f"",
            ]

    return "\n".join(lines)
