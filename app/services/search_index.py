import asyncio
import uuid
from collections.abc import Sequence
from typing import Any

from azure.core.credentials import AzureKeyCredential
from azure.search.documents.aio import SearchClient
from azure.search.documents.indexes.aio import SearchIndexClient
from azure.search.documents.indexes.models import (
    HnswAlgorithmConfiguration,
    HnswParameters,
    SearchableField,
    SearchField,
    SearchFieldDataType,
    SearchIndex,
    SemanticConfiguration,
    SemanticField,
    SemanticPrioritizedFields,
    SemanticSearch,
    SimpleField,
    VectorSearch,
    VectorSearchAlgorithmMetric,
    VectorSearchProfile,
)
from azure.search.documents.models import QueryType, VectorizedQuery

from app.errors import SearchIndexError
from app.settings import Settings


SEMANTIC_CONFIG_NAME = "default"
HNSW_CONFIG_NAME = "hnsw-cosine"
HNSW_PROFILE_NAME = "default-hnsw"


def build_index(name: str, enable_semantic: bool) -> SearchIndex:
    fields: list[Any] = [
        SimpleField(
            name="id", type=SearchFieldDataType.String, key=True, filterable=True
        ),
        SimpleField(
            name="doc_id",
            type=SearchFieldDataType.String,
            filterable=True,
            retrievable=True,
        ),
        SimpleField(
            name="doc_name",
            type=SearchFieldDataType.String,
            retrievable=True,
        ),
        SimpleField(
            name="dataset_id",
            type=SearchFieldDataType.String,
            filterable=True,
            retrievable=True,
        ),
        SimpleField(
            name="chunk_index",
            type=SearchFieldDataType.Int32,
            retrievable=True,
        ),
        SearchableField(
            name="content",
            type=SearchFieldDataType.String,
            analyzer_name="standard.lucene",
        ),
        SearchField(
            name="content_vector",
            type=SearchFieldDataType.Collection(SearchFieldDataType.Single),
            searchable=True,
            hidden=True,
            vector_search_dimensions=1536,
            vector_search_profile_name=HNSW_PROFILE_NAME,
        ),
        SimpleField(
            name="uploaded_at",
            type=SearchFieldDataType.DateTimeOffset,
            filterable=True,
            sortable=True,
        ),
    ]

    vector_search = VectorSearch(
        algorithms=[
            HnswAlgorithmConfiguration(
                name=HNSW_CONFIG_NAME,
                parameters=HnswParameters(
                    m=4,
                    ef_construction=400,
                    ef_search=500,
                    metric=VectorSearchAlgorithmMetric.COSINE,
                ),
            )
        ],
        profiles=[
            VectorSearchProfile(
                name=HNSW_PROFILE_NAME,
                algorithm_configuration_name=HNSW_CONFIG_NAME,
            )
        ],
    )

    semantic_search: SemanticSearch | None = None
    if enable_semantic:
        semantic_search = SemanticSearch(
            default_configuration_name=SEMANTIC_CONFIG_NAME,
            configurations=[
                SemanticConfiguration(
                    name=SEMANTIC_CONFIG_NAME,
                    prioritized_fields=SemanticPrioritizedFields(
                        title_field=SemanticField(field_name="doc_name"),
                        content_fields=[SemanticField(field_name="content")],
                    ),
                )
            ],
        )

    return SearchIndex(
        name=name,
        fields=fields,
        vector_search=vector_search,
        semantic_search=semantic_search,
    )


class SearchGateway:
    """Azure AI Search gateway: schema, upsert, delete, hybrid search."""

    UPLOAD_BATCH = 1000

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        cred = AzureKeyCredential(settings.azure_search_api_key)
        self._index_name = settings.azure_search_index
        self._endpoint = settings.azure_search_endpoint
        self._cred = cred
        self._enable_semantic = settings.enable_semantic_ranking
        self._search_client: SearchClient | None = None
        self._index_client: SearchIndexClient | None = None

    def _search(self) -> SearchClient:
        if self._search_client is None:
            self._search_client = SearchClient(
                endpoint=self._endpoint,
                index_name=self._index_name,
                credential=self._cred,
            )
        return self._search_client

    def _index(self) -> SearchIndexClient:
        if self._index_client is None:
            self._index_client = SearchIndexClient(
                endpoint=self._endpoint, credential=self._cred
            )
        return self._index_client

    async def ensure_index(self) -> None:
        index = build_index(self._index_name, enable_semantic=self._enable_semantic)
        await self._index().create_or_update_index(index)

    async def upsert_chunks(self, docs: list[dict[str, Any]]) -> None:
        if not docs:
            return
        client = self._search()
        batches = [
            docs[i : i + self.UPLOAD_BATCH]
            for i in range(0, len(docs), self.UPLOAD_BATCH)
        ]
        sem = asyncio.Semaphore(4)

        async def upload(batch: list[dict[str, Any]]) -> None:
            async with sem:
                results = await client.upload_documents(documents=batch)
            failures = [r for r in results if not r.succeeded]
            if failures:
                msgs = "; ".join(
                    f"{r.key}: {r.error_message}" for r in failures[:5]
                )
                raise SearchIndexError(
                    f"upload failed for {len(failures)} rows: {msgs}"
                )

        await asyncio.gather(*(upload(b) for b in batches))

    async def delete_by_doc_ids(
        self, doc_ids: Sequence[uuid.UUID | str]
    ) -> int:
        if not doc_ids:
            return 0
        client = self._search()
        deleted = 0
        for raw_id in doc_ids:
            did = str(raw_id)
            while True:
                keys: list[str] = []
                result = await client.search(
                    search_text="*",
                    filter=f"doc_id eq '{did}'",
                    select=["id"],
                    top=1000,
                )
                async for row in result:
                    keys.append(row["id"])
                if not keys:
                    break
                outcomes = await client.delete_documents(
                    documents=[{"id": k} for k in keys]
                )
                failed = [r for r in outcomes if not r.succeeded]
                if failed:
                    raise SearchIndexError(
                        f"delete failed for {len(failed)} rows in doc {did}"
                    )
                deleted += len(outcomes) - len(failed)
                if len(keys) < 1000:
                    break
        return deleted

    async def hybrid_search(
        self,
        query: str,
        query_vector: list[float],
        *,
        top_k: int,
        dataset_id: uuid.UUID | None,
    ) -> list[dict[str, Any]]:
        client = self._search()
        vector_q = VectorizedQuery(
            vector=query_vector,
            k_nearest_neighbors=top_k,
            fields="content_vector",
        )
        filter_expr: str | None = None
        if dataset_id is not None:
            filter_expr = f"dataset_id eq '{dataset_id}'"
        kwargs: dict[str, Any] = {
            "search_text": query,
            "vector_queries": [vector_q],
            "filter": filter_expr,
            "select": ["id", "doc_id", "doc_name", "chunk_index", "content"],
            "top": top_k,
        }
        if self._enable_semantic:
            kwargs["query_type"] = QueryType.SEMANTIC
            kwargs["semantic_configuration_name"] = SEMANTIC_CONFIG_NAME
        result = await client.search(**kwargs)
        rows: list[dict[str, Any]] = []
        async for row in result:
            rows.append(dict(row))
        return rows

    async def list_distinct_doc_ids(self) -> list[str]:
        client = self._search()
        seen: set[str] = set()
        result = await client.search(
            search_text="*", select=["doc_id"], top=100000
        )
        async for row in result:
            did = row.get("doc_id")
            if did:
                seen.add(did)
        return list(seen)

    async def aclose(self) -> None:
        if self._search_client is not None:
            await self._search_client.close()
            self._search_client = None
        if self._index_client is not None:
            await self._index_client.close()
            self._index_client = None
