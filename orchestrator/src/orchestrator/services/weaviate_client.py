import json
import logging
from typing import Any

import httpx

from ..config import settings

logger = logging.getLogger(__name__)

_TIER1_COLLECTIONS = ["GeneralKnowledge", "WebKnowledge"]
_TIER2_PLUS_COLLECTIONS = _TIER1_COLLECTIONS + ["PrivateKnowledge"]

# e5-Praefixe (4.9): Inhalte liegen als "passage: ..." in Weaviate, gesucht
# wird mit "query: ...".
_PASSAGE_PREFIX = "passage: "


class WeaviateError(Exception):
    pass


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

    def _client(self, timeout: float = 10.0) -> httpx.AsyncClient:
        headers = {}
        if settings.weaviate_api_key:
            headers["Authorization"] = f"Bearer {settings.weaviate_api_key}"
        return httpx.AsyncClient(base_url=self._base_url, timeout=timeout, headers=headers)

    async def search(self, text: str, tier: int) -> list[str]:
        scored: list[tuple[str, float]] = []
        async with self._client() as client:
            for collection in self._collections_for_tier(tier):
                hits, _ = await self._query_collection(client, collection, text)
                scored.extend(hits)

        chunks = self._filter_relative(scored)
        logger.info("RAG: %d Treffer, %d davon im Kontext (Tier %d)", len(scored), len(chunks), tier)
        return chunks

    async def _query_collection(
        self, client: httpx.AsyncClient, collection: str, text: str
    ) -> tuple[list[tuple[str, float]], str | None]:
        """(Treffer, Fehlertext). Fehler brechen den Turn nie ab - RAG wird
        fuer diese Collection nur uebersprungen."""
        try:
            data = await self._graphql(client, self._build_query(collection, text))
        except WeaviateError as exc:
            logger.warning(
                "Weaviate-Abfrage gegen %s fehlgeschlagen - RAG wird fuer diese Anfrage uebersprungen: %s",
                collection, exc,
            )
            return [], str(exc)

        results = (data.get("Get") or {}).get(collection) or []
        hits = [
            (_strip_prefix(item[self._content_property]), item["_additional"]["certainty"])
            for item in results
            if item.get(self._content_property)
            and (item.get("_additional") or {}).get("certainty") is not None
        ]
        return hits, None

    async def _graphql(self, client: httpx.AsyncClient, query: str) -> dict:
        """Weaviate meldet Abfragefehler (unbekannte Collection, falsches
        Property, kein Vectorizer) mit HTTP 200 und einem errors-Feld - die
        muessen hier explizit zu Fehlern werden, sonst sieht RAG nur leer aus."""
        try:
            response = await client.post("/v1/graphql", json={"query": query})
            response.raise_for_status()
            payload = response.json()
        except httpx.HTTPStatusError as exc:
            raise WeaviateError(f"HTTP {exc.response.status_code}: {exc.response.text[:300]}") from exc
        except (httpx.HTTPError, ValueError) as exc:
            raise WeaviateError(f"{type(exc).__name__}: {exc}") from exc
        errors = payload.get("errors")
        if errors:
            raise WeaviateError("; ".join(str(error.get("message", error)) for error in errors)[:500])
        return payload.get("data") or {}

    def _build_query(self, collection: str, text: str) -> str:
        # e5-Praefix-Konvention aus 4.9: Suchen mit "query:", Schreiben mit
        # "passage:". JSON-Stringliterale sind gueltige GraphQL-Strings - so
        # brechen Anfuehrungszeichen, Backslashes und Zeilenumbrueche die
        # Abfrage nicht mehr.
        concept = json.dumps(f"query: {text}", ensure_ascii=False)
        return f"""
        {{
          Get {{
            {collection}(
              nearText: {{ concepts: [{concept}] }}
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

    # ---- Diagnose & Pflege (Admin-Panel "Wissen") ----------------------------------

    async def status(self) -> dict[str, Any]:
        """Was der Orchestrator von Weaviate sieht: erreichbar?, welche
        Collections, Vectorizer, Inhalts-Property, Anzahl Eintraege - und
        daraus abgeleitet, warum RAG gerade nichts liefert."""
        result: dict[str, Any] = {
            "url": self._base_url,
            "api_key_set": bool(settings.weaviate_api_key),
            "content_property": self._content_property,
            "top_k": self._top_k,
            "relative_margin": self._relative_margin,
            "reachable": False,
            "version": None,
            "modules": [],
            "error": None,
            "collections": [],
            "other_collections": [],
            "problems": [],
        }
        problems: list[str] = result["problems"]
        try:
            async with self._client(timeout=5.0) as client:
                meta = await client.get("/v1/meta")
                meta.raise_for_status()
                meta_json = meta.json()
                result["reachable"] = True
                result["version"] = meta_json.get("version")
                result["modules"] = sorted((meta_json.get("modules") or {}).keys())
                schema = await client.get("/v1/schema")
                schema.raise_for_status()
                classes = {cls["class"]: cls for cls in schema.json().get("classes") or []}
                for name in _TIER2_PLUS_COLLECTIONS:
                    result["collections"].append(
                        await self._collection_status(client, name, classes.get(name))
                    )
        except httpx.HTTPStatusError as exc:
            result["error"] = f"HTTP {exc.response.status_code}: {exc.response.text[:300]}"
            if exc.response.status_code in (401, 403):
                problems.append("Weaviate lehnt den Zugriff ab - WEAVIATE_API_KEY pruefen.")
            else:
                problems.append("Weaviate antwortet mit einem Fehler (siehe oben).")
            return result
        except (httpx.HTTPError, ValueError) as exc:
            result["error"] = f"{type(exc).__name__}: {exc}"
            problems.append(
                f"Weaviate ist unter {self._base_url} nicht erreichbar - WEAVIATE_URL pruefen "
                "(im ai-lab-Netzwerk z. B. http://weaviate:8080) und ob der Orchestrator im "
                "selben Docker-Netzwerk haengt."
            )
            return result

        result["other_collections"] = sorted(set(classes) - set(_TIER2_PLUS_COLLECTIONS))
        for collection in result["collections"]:
            name = collection["name"]
            if not collection["exists"]:
                problems.append(f"Collection {name} fehlt in Weaviate.")
                continue
            if not collection["has_content_property"]:
                text_props = ", ".join(collection["text_properties"]) or "keine"
                problems.append(
                    f"{name} hat kein Property '{self._content_property}' (Text-Properties: "
                    f"{text_props}) - WEAVIATE_CONTENT_PROPERTY anpassen."
                )
            if collection["vectorizer"] in (None, "none"):
                problems.append(f"{name} hat keinen Vectorizer - nearText-Suche ist so nicht moeglich.")
            if collection["count"] == 0:
                problems.append(f"{name} ist leer.")
        if not any(c["count"] for c in result["collections"] if c["exists"]):
            problems.append("Keine Collection enthaelt Eintraege - es gibt nichts, was die KI finden koennte.")
        return result

    async def _collection_status(
        self, client: httpx.AsyncClient, name: str, schema: dict | None
    ) -> dict[str, Any]:
        info: dict[str, Any] = {
            "name": name,
            "min_tier": 2 if name not in _TIER1_COLLECTIONS else 1,
            "exists": schema is not None,
            "vectorizer": None,
            "text_properties": [],
            "has_content_property": False,
            "count": None,
            "error": None,
        }
        if schema is None:
            return info
        properties = schema.get("properties") or []
        info["vectorizer"] = schema.get("vectorizer")
        info["text_properties"] = [
            prop["name"] for prop in properties if "text" in (prop.get("dataType") or [])
        ]
        info["has_content_property"] = any(prop["name"] == self._content_property for prop in properties)
        try:
            data = await self._graphql(client, f"{{ Aggregate {{ {name} {{ meta {{ count }} }} }} }}")
            info["count"] = ((data.get("Aggregate") or {}).get(name) or [{}])[0].get("meta", {}).get("count")
        except WeaviateError as exc:
            info["error"] = str(exc)
        return info

    async def debug_search(self, text: str, tier: int) -> dict[str, Any]:
        """Testsuche fuers Panel: alle Treffer je Collection mit Certainty und
        Fehler, dazu das, was nach dem Relativ-Filter in den Kontext ginge."""
        collections = []
        scored: list[tuple[str, float]] = []
        async with self._client() as client:
            for collection in self._collections_for_tier(tier):
                hits, error = await self._query_collection(client, collection, text)
                scored.extend(hits)
                collections.append({
                    "name": collection,
                    "error": error,
                    "hits": [{"content": content, "certainty": certainty} for content, certainty in hits],
                })
        return {"collections": collections, "context": self._filter_relative(list(scored))}

    async def add_entry(self, collection: str, content: str) -> str:
        """Eintrag mit e5-passage-Praefix ablegen (4.9) - den Vektor rechnet
        Weaviate selbst ueber den Vectorizer der Collection."""
        if collection not in _TIER2_PLUS_COLLECTIONS:
            raise WeaviateError(f"Unbekannte Collection '{collection}'")
        body = {
            "class": collection,
            "properties": {self._content_property: _PASSAGE_PREFIX + content.strip()},
        }
        try:
            async with self._client() as client:
                response = await client.post("/v1/objects", json=body)
                response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            raise WeaviateError(f"HTTP {exc.response.status_code}: {exc.response.text[:300]}") from exc
        except httpx.HTTPError as exc:
            raise WeaviateError(f"{type(exc).__name__}: {exc}") from exc
        return response.json().get("id", "")


def _strip_prefix(content: str) -> str:
    return content[len(_PASSAGE_PREFIX):] if content.startswith(_PASSAGE_PREFIX) else content


weaviate_client = WeaviateClient()
