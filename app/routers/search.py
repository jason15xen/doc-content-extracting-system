import uuid
from collections import defaultdict
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.deps import get_chatter, get_embedder, get_pipeline_context, get_search, get_session
from app.pipeline.context import PipelineContext
from app.repositories import datasets as datasets_repo
from app.schemas.search import SearchRequest, SearchResponse, SearchSource
from app.services.chat import Chatter
from app.services.embeddings import Embedder
from app.services.search_index import SearchGateway

router = APIRouter(tags=["search"])


def _score(row: dict[str, Any]) -> float:
    reranker = row.get("@search.reranker_score")
    if isinstance(reranker, (int, float)):
        return float(reranker)
    score = row.get("@search.score", 0.0)
    try:
        return float(score)
    except (TypeError, ValueError):
        return 0.0


@router.post("/search", response_model=SearchResponse)
async def search(
    body: SearchRequest,
    session: AsyncSession = Depends(get_session),
    ctx: PipelineContext = Depends(get_pipeline_context),
    embedder: Embedder = Depends(get_embedder),
    chatter: Chatter = Depends(get_chatter),
    search_gw: SearchGateway = Depends(get_search),
) -> SearchResponse:
    if body.dataset_id is not None:
        ds = await datasets_repo.get(session, body.dataset_id)
        if ds is None:
            raise HTTPException(status_code=404, detail="dataset not found")

    vectors = await embedder.embed_many([body.query])
    if not vectors:
        raise HTTPException(status_code=500, detail="failed to embed query")
    query_vec = vectors[0]

    rows = await search_gw.hybrid_search(
        body.query,
        query_vec,
        top_k=ctx.settings.search_top_k_chunks,
        dataset_id=body.dataset_id,
    )

    if not rows:
        return SearchResponse(answer="No matching documents.", sources=[])

    # Collapse chunks into docs, sum contribution scores.
    by_doc: dict[str, dict[str, Any]] = defaultdict(
        lambda: {"doc_name": "", "score_sum": 0.0, "chunks": []}
    )
    for row in rows:
        did = row["doc_id"]
        s = _score(row)
        by_doc[did]["doc_name"] = row["doc_name"]
        by_doc[did]["score_sum"] += s
        by_doc[did]["chunks"].append(
            {
                "chunk_index": row["chunk_index"],
                "content": row.get("content", ""),
                "score": s,
            }
        )

    ranked_doc_ids = sorted(
        by_doc.keys(), key=lambda d: by_doc[d]["score_sum"], reverse=True
    )[: body.top_k]

    # Build chat context across the selected docs, highest-scoring chunks first.
    all_chunks: list[dict[str, Any]] = []
    for did in ranked_doc_ids:
        for c in by_doc[did]["chunks"]:
            all_chunks.append(
                {
                    "doc_id": did,
                    "doc_name": by_doc[did]["doc_name"],
                    "chunk_index": c["chunk_index"],
                    "content": c["content"],
                    "score": c["score"],
                }
            )
    all_chunks.sort(key=lambda c: c["score"], reverse=True)
    contexts = all_chunks[: ctx.settings.chat_max_context_chunks]

    answer = await chatter.answer(body.query, contexts)

    sources = [
        SearchSource(
            doc_id=uuid.UUID(did),
            doc_name=by_doc[did]["doc_name"],
            score=float(by_doc[did]["score_sum"]),
        )
        for did in ranked_doc_ids
    ]

    return SearchResponse(answer=answer, sources=sources)
