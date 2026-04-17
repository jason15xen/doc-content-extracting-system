from typing import Any

from docx import Document
from docx.oxml.ns import qn
from docx.table import Table, _Cell
from docx.text.paragraph import Paragraph

from app.extraction.extractors.base import Extractor


class DocxExtractor(Extractor):
    file_type = "docx"

    def extract_elements(self, path: str) -> list[dict[str, Any]]:
        doc = Document(path)
        elements: list[dict[str, Any]] = []
        for section in doc.sections:
            elements.extend(_walk(section.header))
            elements.extend(_walk(section.footer))
        elements.extend(_walk_body(doc.element.body, doc))
        return elements


def _walk(part) -> list[dict[str, Any]]:
    """Walk a header/footer or other body-like object in document order."""
    return _walk_body(part._element, part)


def _walk_body(body_el, parent) -> list[dict[str, Any]]:
    elements: list[dict[str, Any]] = []
    for child in body_el.iterchildren():
        if child.tag == qn("w:p"):
            text = Paragraph(child, parent).text
            if text.strip():
                elements.append({"type": "paragraph", "text": text})
        elif child.tag == qn("w:tbl"):
            elements.append({"type": "table", "rows": _table_rows(Table(child, parent))})
    return elements


def _table_rows(tbl: Table) -> list[list[str]]:
    return [[_cell_text(cell) for cell in row.cells] for row in tbl.rows]


def _cell_text(cell: _Cell) -> str:
    return "\n".join(p.text for p in cell.paragraphs)
