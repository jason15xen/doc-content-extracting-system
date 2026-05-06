from __future__ import annotations

import logging
import time
from typing import Any

from azure.ai.documentintelligence import DocumentIntelligenceClient
from azure.ai.documentintelligence.models import AnalyzeDocumentRequest
from azure.core.credentials import AzureKeyCredential

from app.errors import ExtractionError
from app.extraction.extractors.base import Extractor

_LOG = logging.getLogger("app.extract")

# Formats Document Intelligence accepts directly. Anything outside this set
# would need a conversion step that the local pipeline doesn't apply either,
# so we just reject up front and keep the comparison apples-to-apples.
DI_SUPPORTED_EXTS = {
    ".pdf", ".docx", ".xlsx", ".pptx", ".html",
    ".jpeg", ".jpg", ".png", ".bmp", ".tiff", ".tif", ".heif",
}


class DocumentIntelligenceExtractor(Extractor):
    """Extract document text via Azure Document Intelligence.

    Synchronous (the SDK exposes a poller); call from a threadpool. Returns
    one element per page so the downstream `plain_text` join behaves exactly
    like the PyMuPDF extractor — keeping `/extract` and `/extract-di` output
    shapes identical for A/B comparison.
    """

    def __init__(
        self,
        file_type: str | None = None,
        *,
        endpoint: str,
        api_key: str,
        model_id: str = "prebuilt-read",
    ) -> None:
        super().__init__(file_type=file_type)
        self._endpoint = endpoint
        self._api_key = api_key
        self._model_id = model_id

    def extract_elements(self, path: str) -> list[dict[str, Any]]:
        if not self._endpoint or not self._api_key:
            raise ExtractionError("Azure Document Intelligence not configured")

        client = DocumentIntelligenceClient(
            endpoint=self._endpoint,
            credential=AzureKeyCredential(self._api_key),
        )
        try:
            with open(path, "rb") as f:
                payload = f.read()
            t0 = time.perf_counter()
            poller = client.begin_analyze_document(
                self._model_id,
                AnalyzeDocumentRequest(bytes_source=payload),
            )
            result = poller.result()
            _LOG.info(
                "di: %s analyzed in %.1fms (model=%s, %d bytes)",
                path,
                (time.perf_counter() - t0) * 1000.0,
                self._model_id,
                len(payload),
            )
        finally:
            client.close()

        elements: list[dict[str, Any]] = []
        pages = result.pages or []
        if pages:
            for i, page in enumerate(pages, start=1):
                lines = page.lines or []
                text = "\n".join(line.content for line in lines if line.content)
                if text.strip():
                    elements.append({"type": "page", "index": i, "text": text})
        elif result.content:
            elements.append({"type": "page", "index": 1, "text": result.content})
        return elements
