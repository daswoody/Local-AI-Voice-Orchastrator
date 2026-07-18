def test_get_layouts_returns_all_bundled_templates_by_default(client):
    response = client.get("/v1/cards/layouts")
    assert response.status_code == 200
    body = response.json()
    card_types = {t["card_type"] for t in body["layouts"]}
    assert card_types == {
        "generic",
        "weather",
        "list",
        "calendar",
        "navigation",
        "notifications_summary",
        "media",
    }
    assert body["version"] == 1


def test_get_layouts_since_current_version_returns_nothing_new(client):
    all_layouts = client.get("/v1/cards/layouts").json()
    response = client.get(f"/v1/cards/layouts?since_version={all_layouts['version']}")
    assert response.json()["layouts"] == []


# ---- HTML-Karten (4.12 v1.12) --------------------------------------------------


def test_html_layout_roundtrip_with_generic_fallback_root(client, admin_headers):
    """HTML-Layouts werden mit format+html ausgeliefert; als root liegt das
    generic-Fallback bei, damit Alt-Clients (Android) etwas rendern."""
    created = client.post(
        "/v1/admin/cards",
        json={"card_type": "fancy", "format": "html",
              "html": "<div><h3>{{data.headline}}</h3><p>{{data.body}}</p></div>"},
        headers=admin_headers,
    )
    assert created.status_code == 201

    layouts = client.get("/v1/cards/layouts").json()["layouts"]
    fancy = next(entry for entry in layouts if entry["card_type"] == "fancy")
    assert fancy["format"] == "html"
    assert "{{data.headline}}" in fancy["html"]
    # Fallback-root fuer Clients ohne HTML-Renderer
    texts = [child["text"] for child in fancy["root"]["children"]]
    assert texts == ["{{data.headline}}", "{{data.body}}"]

    # Bestehende JSON-Layouts sind unveraendert als format json markiert
    generic = next(entry for entry in layouts if entry["card_type"] == "generic")
    assert generic["format"] == "json"
    assert generic["html"] is None


def test_html_layout_requires_html_content(client, admin_headers):
    response = client.post(
        "/v1/admin/cards",
        json={"card_type": "leer", "format": "html", "html": "  "},
        headers=admin_headers,
    )
    assert response.status_code == 400


def test_json_layout_requires_root(client, admin_headers):
    response = client.post(
        "/v1/admin/cards",
        json={"card_type": "leer", "format": "json", "root": {}},
        headers=admin_headers,
    )
    assert response.status_code == 400
