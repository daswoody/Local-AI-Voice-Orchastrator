def test_get_layouts_returns_all_bundled_templates_by_default(client):
    response = client.get("/v1/cards/layouts")
    assert response.status_code == 200
    body = response.json()
    card_types = {t["card_type"] for t in body["templates"]}
    assert card_types == {
        "generic",
        "weather",
        "list",
        "calendar",
        "navigation",
        "notifications_summary",
        "media",
    }
    assert body["layout_version"] == 1


def test_get_layouts_since_current_version_returns_nothing_new(client):
    all_layouts = client.get("/v1/cards/layouts").json()
    response = client.get(f"/v1/cards/layouts?since_version={all_layouts['layout_version']}")
    assert response.json()["templates"] == []
