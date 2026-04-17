from typing import Any

import pymupdf4llm

from app.extraction.extractors.base import Extractor


class PdfExtractor(Extractor):
    file_type = "pdf"

    def extract_elements(self, path: str) -> list[dict[str, Any]]:
        chunks = pymupdf4llm.to_markdown(path, page_chunks=True)
        return [
            {"type": "page", "index": i, "text": chunk.get("text", "")}
            for i, chunk in enumerate(chunks, start=1)
        ]
