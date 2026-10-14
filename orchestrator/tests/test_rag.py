"""RAG ueber Weaviate (4.8/4.9): Abfragefehler sichtbar statt still leer,
robuste Query, Tier-Routing, Diagnose und Pflege im Admin-Panel."""

import json
import logging
import re

import httpx
import pytest

from orchestrator.graph import _initial_messages, initial_state
from orchestrator.services import weaviate_client as weaviate_module
from orchestrator.services.weaviate_client import weaviate_client

_REAL_CLIENT = httpx.AsyncClient


class FakeWeaviate:
    """Nachbau der Weaviate-1.26-REST/GraphQL-Antworten (gegen eine echte
    1.26.4 abgeglichen): Abfragefehler kommen als HTTP 200 mit errors."""

    def __init__(self, classes: dict[str, list[tuple[str, float]]], prop: str = "content") -> None:
        self.classes = classes
        self.prop = prop
        self.queries: list[str] = []
        self.created: list[dict] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/v1/meta":
            return httpx.Response(200, json={"version": "1.26.4", "modules": {"text2vec-transformers": {}}})
        if path == "/v1/schema":
            return httpx.Response(200, json={"classes": [
                {"class": name, "vectorizer": "text2vec-transformers",
                 "properties": [{"name": self.prop, "dataType": ["text"]}]}
                for name in self.classes
            ]})
        if path == "/v1/objects":
            self.created.append(json.loads(request.content))
            return httpx.Response(200, json={"id": "abc"})
        if path == "/v1/graphql":
            return self._graphql(json.loads(request.content)["query"])
        return httpx.Response(404)

    def _graphql(self, query: str) -> httpx.Response:
        self.queries.append(query)
        name = re.search(r"(?:Get|Aggregate) \{\s*(\w+)", query).group(1)
        if name not in self.classes:
            return httpx.Response(200, json={"errors": [
                {"message": f'Cannot query field "{name}" on type "GetObjectsObj".'}]})
        if "Aggregate" in query:
            return httpx.Response(200, json={"data": {"Aggregate": {name: [{"meta": {"count": len(self.classes[name])}}]}}})
        if re.search(r"\b(\w+)\s+_additional", query).group(1) != self.prop:
            return httpx.Response(200, json={"errors": [
                {"message": f'Cannot query field "{re.search(r"(\w+)\s+_additional", query).group(1)}" on type "{name}".'}]})
        concept = re.search(r"concepts: \[(.*)\] \}", query).group(1)
        json.loads(concept)  # muss ein gueltiges Stringliteral sein
        hits = [{self.prop: content, "_additional": {"certainty": certainty}}
                for content, certainty in self.classes[name]]
        return httpx.Response(200, json={"data": {"Get": {name: hits}}})


def _mock(monkeypatch, handler) -> None:
    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(weaviate_module.httpx, "AsyncClient",
                        lambda *args, **kwargs: _REAL_CLIENT(*args, transport=transport, **kwargs))


async def test_graphql_errors_are_logged_and_other_collections_still_count(monkeypatch, caplog):
    fake = FakeWeaviate({"GeneralKnowledge": [("passage: Wasser kocht bei 100 Grad.", 0.9)]})
    _mock(monkeypatch, fake)
    with caplog.at_level(logging.WARNING):
        chunks = await weaviate_client.search("Wann kocht Wasser?", tier=1)
    assert chunks == ["Wasser kocht bei 100 Grad."]  # passage-Praefix entfernt
    assert any("WebKnowledge" in r.message and "Cannot query field" in r.message for r in caplog.records)


async def test_query_survives_quotes_backslashes_and_newlines(monkeypatch):
    fake = FakeWeaviate({"GeneralKnowledge": [("passage: x", 0.9)], "WebKnowledge": []})
    _mock(monkeypatch, fake)
    assert await weaviate_client.search('Pfad "C:\\temp"\nzweite Zeile', tier=1) == ["x"]


async def test_tier1_never_queries_private_knowledge(monkeypatch):
    fake = FakeWeaviate({"GeneralKnowledge": [], "WebKnowledge": [],
                         "PrivateKnowledge": [("passage: geheim", 0.95)]})
    _mock(monkeypatch, fake)
    assert await weaviate_client.search("geheim", tier=1) == []
    assert not any("PrivateKnowledge" in q for q in fake.queries)
    assert await weaviate_client.search("geheim", tier=2) == ["geheim"]


async def test_wrong_content_property_is_reported(monkeypatch):
    fake = FakeWeaviate({name: [("passage: a", 0.9)] for name in
                         ("GeneralKnowledge", "WebKnowledge", "PrivateKnowledge")}, prop="text")
    _mock(monkeypatch, fake)
    assert await weaviate_client.search("a", tier=3) == []
    status = await weaviate_client.status()
    assert status["reachable"]
    assert any("WEAVIATE_CONTENT_PROPERTY" in p for p in status["problems"])
    assert status["collections"][0]["text_properties"] == ["text"]


async def test_status_names_missing_and_empty_collections(monkeypatch):
    _mock(monkeypatch, FakeWeaviate({"GeneralKnowledge": [], "Conversations": []}))
    status = await weaviate_client.status()
    by_name = {c["name"]: c for c in status["collections"]}
    assert by_name["GeneralKnowledge"]["count"] == 0
    assert not by_name["WebKnowledge"]["exists"]
    assert status["other_collections"] == ["Conversations"]
    assert "Collection WebKnowledge fehlt in Weaviate." in status["problems"]
    assert "GeneralKnowledge ist leer." in status["problems"]


async def test_status_unreachable(monkeypatch):
    def refuse(request):
        raise httpx.ConnectError("Name or service not known", request=request)
    _mock(monkeypatch, refuse)
    status = await weaviate_client.status()
    assert not status["reachable"]
    assert "WEAVIATE_URL" in status["problems"][0]
    # Ein Chat-Turn laeuft trotzdem weiter, nur ohne Kontext.
    assert await weaviate_client.search("egal", tier=3) == []


def test_admin_rag_endpoints(monkeypatch, client, admin_headers):
    fake = FakeWeaviate({"GeneralKnowledge": [("passage: Muell kommt dienstags.", 0.93)],
                         "WebKnowledge": [], "PrivateKnowledge": []})
    _mock(monkeypatch, fake)

    status = client.get("/v1/admin/rag", headers=admin_headers).json()
    assert status["reachable"] and status["version"] == "1.26.4"

    result = client.post("/v1/admin/rag/search", headers=admin_headers,
                         json={"text": "Wann kommt der Muell?", "tier": 1}).json()
    assert [c["name"] for c in result["collections"]] == ["GeneralKnowledge", "WebKnowledge"]
    assert result["context"] == ["Muell kommt dienstags."]

    response = client.post("/v1/admin/rag/entries", headers=admin_headers,
                           json={"collection": "PrivateKnowledge", "content": " WLAN: Sonnenblume42 "})
    assert response.status_code == 201
    assert fake.created == [{"class": "PrivateKnowledge",
                             "properties": {"content": "passage: WLAN: Sonnenblume42"}}]

    assert client.post("/v1/admin/rag/entries", headers=admin_headers,
                       json={"collection": "Conversations", "content": "x"}).status_code == 422


def test_rag_endpoints_require_admin(client):
    assert client.get("/v1/admin/rag").status_code in (401, 403)


@pytest.mark.parametrize("chunks", [["Muell kommt dienstags."]])
def test_context_lands_in_system_prompt(chunks):
    state = initial_state("Wann kommt der Muell?", 1)
    state["context_chunks"] = chunks
    system = _initial_messages(state)[0]["content"]
    assert "Wissensdatenbank" in system and "- Muell kommt dienstags." in system
