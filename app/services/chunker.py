import tiktoken

_enc: tiktoken.Encoding | None = None


def _encoding() -> tiktoken.Encoding:
    global _enc
    if _enc is None:
        _enc = tiktoken.get_encoding("cl100k_base")
    return _enc


def chunk_text(text: str, *, tokens: int, overlap: int) -> list[str]:
    if not text or not text.strip():
        return []
    if tokens <= 0:
        raise ValueError("tokens must be > 0")
    if overlap < 0 or overlap >= tokens:
        raise ValueError("overlap must be in [0, tokens)")

    enc = _encoding()
    ids = enc.encode(text)
    if not ids:
        return []

    step = tokens - overlap
    chunks: list[str] = []
    start = 0
    while start < len(ids):
        window = ids[start : start + tokens]
        chunks.append(enc.decode(window))
        if start + tokens >= len(ids):
            break
        start += step
    return chunks
