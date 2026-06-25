"""Azure AI Search document-key generation for chunks.

Azure rejects keys containing characters outside [A-Za-z0-9_-=]. Client doc_ids
are often filenames (e.g. 'tefal-opti-grill-en.pdf') whose '.' breaks upload.
"""
import base64
import re

from app.pipeline.ingest import _chunk_key

_AZURE_KEY = re.compile(r"^[A-Za-z0-9_\-=]+$")


def test_key_with_dotted_doc_id_is_azure_safe():
    # The exact doc_id from the reported upload failure.
    key = _chunk_key("test-tefal-opti-grill-en.pdf", 0)
    assert _AZURE_KEY.match(key), key
    assert "." not in key


def test_key_is_unique_per_chunk_and_decodes_to_doc_id():
    doc_id = "test-tefal-opti-grill-en.pdf"
    k0 = _chunk_key(doc_id, 0)
    k1 = _chunk_key(doc_id, 1)
    assert k0 != k1
    # The encoded prefix (everything before the trailing _<index>) round-trips.
    token = k0.rsplit("_", 1)[0]
    assert base64.urlsafe_b64decode(token).decode("utf-8") == doc_id


def test_distinct_doc_ids_do_not_collide():
    # A naive sanitizer that replaced '.'/'-' with '_' would merge these.
    keys = {_chunk_key(d, 0) for d in ("a.b", "a-b", "a_b")}
    assert len(keys) == 3
