from abc import ABC, abstractmethod
from typing import Any


class Extractor(ABC):
    file_type: str = ""

    def __init__(self, file_type: str | None = None) -> None:
        if file_type is not None:
            self.file_type = file_type

    @abstractmethod
    def extract_elements(self, path: str) -> list[dict[str, Any]]:
        ...

    def extract(self, path: str, filename: str) -> dict[str, Any]:
        elements = self.extract_elements(path)
        return {
            "filename": filename,
            "file_type": self.file_type,
            "plain_text": elements_to_plain_text(elements),
        }


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
        inner = "\n".join(_element_text(item) for item in el.get("items", []))
        return f"[Slide {el.get('index')}]\n{inner}"
    if t == "page":
        return f"[Page {el.get('index')}]\n{el.get('text', '')}"
    return ""
