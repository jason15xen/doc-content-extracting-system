from typing import Any

from app.extractors.base import Extractor

# Strict encodings tried in order; latin-1 is the unconditional catch-all
# because it decodes any byte sequence.
_STRICT_ENCODINGS = ("utf-8-sig", "utf-8", "utf-16")


class TextExtractor(Extractor):
    file_type = "txt"

    def extract_elements(self, path: str) -> list[dict[str, Any]]:
        with open(path, "rb") as f:
            raw = f.read()
        return [{"type": "paragraph", "text": _decode(raw)}]


def _decode(raw: bytes) -> str:
    for enc in _STRICT_ENCODINGS:
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("latin-1")
