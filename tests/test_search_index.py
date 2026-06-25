"""Unit tests for SearchGateway helpers — pure, no Azure connection needed."""
from app.services.search_index import _odata_quote, build_index


def test_odata_quote_escapes_single_quotes():
    # A quote is escaped by doubling it (OData filter literal rule).
    assert _odata_quote("plain") == "plain"
    assert _odata_quote("a'b") == "a''b"
    assert _odata_quote("o'neill's") == "o''neill''s"


def test_build_index_respects_given_dimensions():
    idx = build_index("rag", enable_semantic=False, dimensions=3072)
    vec = next(f for f in idx.fields if f.name == "content_vector")
    assert vec.vector_search_dimensions == 3072


def test_build_index_default_dimensions():
    idx = build_index("rag", enable_semantic=True)
    vec = next(f for f in idx.fields if f.name == "content_vector")
    assert vec.vector_search_dimensions == 1536
