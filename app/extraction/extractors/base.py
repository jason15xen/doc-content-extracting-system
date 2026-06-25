from abc import ABC, abstractmethod
from typing import Any


class Extractor(ABC):
    file_type: str = ""

    def __init__(self, file_type: str | None = None) -> None:
        if file_type is not None:
            self.file_type = file_type
        # Page indices (1-based) that were detected as scanned and dropped
        # during extraction. Populated by extractors that do scan detection
        # (currently PDF); stays empty for all others.
        self.skipped_pages: list[int] = []
        # Page indices (1-based) detected as scanned (page-filling raster),
        # whether or not their text was kept. Used to label the document as
        # containing scanned content.
        self.scanned_pages: list[int] = []

    @abstractmethod
    def extract_elements(self, path: str) -> list[dict[str, Any]]:
        ...

    def extract(self, path: str, filename: str) -> dict[str, Any]:
        elements = self.extract_elements(path)
        out: dict[str, Any] = {
            "filename": filename,
            "file_type": self.file_type,
            "plain_text": elements_to_plain_text(elements),
        }
        # Only surface these keys when relevant, so the common response shape
        # is unchanged for ordinary documents.
        if self.skipped_pages:
            out["skipped_pages"] = list(self.skipped_pages)
        if self.scanned_pages:
            out["scanned_pages"] = list(self.scanned_pages)
        return out


def elements_to_plain_text(elements: list[dict[str, Any]]) -> str:
    parts = [_element_text(el) for el in elements]
    return "\n".join(p for p in parts if p)


def _element_text(el: dict[str, Any]) -> str:
    t = el.get("type")
    if t == "paragraph":
        return el.get("text", "")
    if t == "table":
        return "\n".join("\t".join(row) for row in el.get("rows", []))
    if t == "sheet":
        header = el.get("name", "")
        body = "\n".join("\t".join(row) for row in el.get("rows", []))
        return f"{header}\n{body}" if header else body
    if t == "slide":
        return "\n".join(_element_text(item) for item in el.get("items", []))
    if t == "page":
        return el.get("text", "")
    return ""
