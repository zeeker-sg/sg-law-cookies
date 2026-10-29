"""Discord payload/version tests only; no Discord messages are sent."""

import importlib.util
import json
from pathlib import Path

from sg_law_cookies import db
from sg_law_cookies.models import Cookie


def load_script(name):
    path = Path(__file__).resolve().parents[1] / "scripts" / name
    spec = importlib.util.spec_from_file_location(name.replace("-", "_"), path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def pending(tmp_path):
    path = tmp_path / "reviews.db"
    conn = db.init_db(path)
    item = Cookie(headline="Draft", summary="Summary", why_it_matters="Why", significance="low")
    db.save_pending_cookie(conn, item, "judgment")
    conn.close()
    return path, item


def test_modal_stale_edit_cannot_overwrite_newer_draft(tmp_path, monkeypatch):
    path, item = pending(tmp_path)
    server = load_script("cookies-approval-server.py")
    monkeypatch.setattr(server, "get_db_conn", lambda: (db.init_db(path), db))
    monkeypatch.setattr(server, "_get_discord_token", lambda: None)
    modal = server._modal_response(item.id, "edit")
    custom_id = modal["data"]["custom_id"]
    assert len(custom_id) <= 100
    conn = db.init_db(path)
    db.update_pending_cookie_text(conn, item.id, summary="Newer edit")
    response, status = server.handle_modal_submit({"data": {"custom_id": custom_id, "components": [
        {"components": [{"custom_id": "edit_summary", "value": "Stale edit"}]}]}}, "reviewer")
    assert status == 200
    assert "draft changed" in json.loads(response)["data"]["content"]
    assert db.get_pending_cookie(conn, item.id)["summary"] == "Newer edit"
    conn.close()


def test_new_buttons_preserve_cookie_id_with_full_revision_token(tmp_path):
    path, item = pending(tmp_path)
    posting = load_script("post_pending_cookies.py")
    conn = db.init_db(path)
    token = db.pending_fingerprint(db.get_pending_cookie(conn, item.id))
    components = posting.build_components(item.id, token)
    for button in components[0]["components"]:
        assert len(button["custom_id"]) <= 100
        assert button["custom_id"].split(":")[1:] == [item.id, token]
    conn.close()
