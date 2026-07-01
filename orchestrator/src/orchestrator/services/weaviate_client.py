import logging

import httpx

from ..config import settings

logger = logging.getLogger(__name__)

_TIER1_COLLECTIONS = ["GeneralKnowledge", "WebKnowledge"]
_TIER2_PLUS_COLLECTIONS = _TIER1_COLLECTIONS + ["PrivateKnowledge"]


class WeaviateClient:
    """Fragt Weaviate direkt per GraphQL ueber den REST-Port (8080) ab, bewusst
    ohne den offiziellen v4-Client-Library: dieser braucht zusaetzlich einen
    erreichbaren gRPC-Port (50051), den 1.5a/1.5b nie eingerichtet oder
    getestet haben - nearText wurde dort nur ueber GraphQL/REST validiert.

    Tier-1-Anfragen werden hier, zusaetzlich zu eventuellen Filtern, gar nicht
    erst gegen PrivateKnowledge geroutet (Defense in Depth, 4.4)."""

    def __init__(self) -> None:
        self._base_url = settings.weaviate_url.rstrip("/")
        self._content_property = settings.weaviate_content_property
        self._top_k = settings.rag_top_k
        self._relative_margin = settings.rag_relative_margin

    def _collections_for_tier(self, tier: int) -> list[str]:
        return _TIER2_PLUS_COLLECTIONS if tier >= 2 else _TIER1_COLLECTIONS

    async def search(self, text: str, tier: int) -> list[str]:
        scored: list[tuple[str, float]] = []
        headers = {}
        if settings.weaviate_api_key:
            headers["Authorization"] = f"Bearer {settings.weaviate_api_key}"

        async with httpx.AsyncClient(base_url=self._base_url, timeout=10.0, headers=headers) as client:
            for collection in self._collections_for_tier(tier):
                scored.extend(await self._query_collection(client, collection, text))

        return self._filter_relative(scored)

    async def _query_collection(
        self, client: httpx.AsyncClient, collection: str, text: str
    ) -> list[tuple[str, float]]:
        query = self._build_query(collection, text)
        try:
            response = await client.post("/v1/graphql", json={"query": query})
            response.raise_for_status()
        except httpx.HTTPError:
            logger.warning(
                "Weaviate-Abfrage gegen %s fehlgeschlagen - RAG wird fuer diese Anfrage uebersprungen",
                collection,
            )
            return []

        results = response.json().get("data", {}).get("Get", {}).get(collection) or []
        return [
            (item[self._content_property], item["_additional"]["certainty"])
            for item in results
            if item.get(self._content_property) and item.get("_additional", {}).get("certainty") is not None
        ]

    def _build_query(self, collection: str, text: str) -> str:
        # e5-Praefix-Konvention aus 4.9: Suchen mit "query:", Schreiben (ausserhalb
        # von 1.7) mit "passage:".
        safe_text = text.replace('"', '\\"')
        return f"""
        {{
          Get {{
            {collection}(
              nearText: {{ concepts: ["query: {safe_text}"] }}
              limit: {self._top_k}
            ) {{
              {self._content_property}
              _additional {{ certainty }}
            }}
          }}
        }}
        """

    def _filter_relative(self, scored: list[tuple[str, float]]) -> list[str]:
        """Tuning-Pfad Punkt 2 aus 4.9: absolute Certainty-Schwellwerte sind wegen
        des Hochbias von e5-base unbrauchbar, daher nur relativ zum Top-K-
        Durchschnitt filtern."""
        if not scored:
            return []
        scored.sort(key=lambda pair: pair[1], reverse=True)
        top = scored[: self._top_k]
        avg_certainty = sum(certainty for _, certainty in top) / len(top)
        threshold = avg_certainty * (1 + self._relative_margin)
        filtered = [content for content, certainty in top if certainty >= threshold]
        return filtered or [top[0][0]]


weaviate_client = WeaviateClient()
