import os
import re
import tempfile

from flask import Flask, jsonify, render_template, request

import config
import lint
import llm
import pipeline
import store
import telegram_ingest

app = Flask(__name__)
store.init_db()
pipeline.start_listener()  # idles until a bot token is connected


@app.route("/")
def index():
    return render_template(
        "index.html",
        runs=store.recent_runs(),
        pending_backlog=sum(1 for n in store.untriaged_notes() if n["source"] == "export"),
        posted_week=store.posts_this_week(),
        target=config.POSTS_PER_WEEK,
        has_key=llm.client is not None,
        has_bot=bool(telegram_ingest.BOT_TOKEN),
        owner=telegram_ingest.owner_chat_id(),
    )


def _err(exc, code=502):
    return jsonify({"error": str(exc)}), code


@app.post("/api/try")
def api_try():
    body = request.json or {}
    text = (body.get("text") or "").strip()
    if len(text) < 5:
        return _err("Type a note first.", 400)
    lines = []
    run = pipeline.try_text(text, deliver=bool(body.get("send")), log=lines.append)
    return jsonify({"outcome": run["outcome"], "score": run.get("score"), "log": lines})


@app.post("/api/run")
def api_run():
    lines = []
    try:
        result = pipeline.run(log=lines.append)
    except llm.LLMUnavailable as exc:
        return _err(exc)
    return jsonify({**result, "log": lines})


ENV_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")


def _set_env_value(key, value):
    lines = open(ENV_PATH).read().splitlines() if os.path.exists(ENV_PATH) else []
    out, found = [], False
    for line in lines:
        if line.split("=", 1)[0].strip() == key:
            out.append(f"{key}={value}")
            found = True
        else:
            out.append(line)
    if not found:
        out.append(f"{key}={value}")
    with open(ENV_PATH, "w") as f:
        f.write("\n".join(out) + "\n")
    os.chmod(ENV_PATH, 0o600)


@app.post("/api/settings/telegram")
def api_connect_telegram():
    token = re.sub(r"\s+", "", (request.json or {}).get("token", ""))
    if not re.fullmatch(r"\d+:[A-Za-z0-9_-]{30,}", token):
        return _err("That doesn't look like a bot token. It should look like 1234567890:AAF...", 400)
    previous = telegram_ingest.BOT_TOKEN
    telegram_ingest.BOT_TOKEN = token
    try:
        me = telegram_ingest._api("getMe")
    except Exception:
        telegram_ingest.BOT_TOKEN = previous
        return _err("Telegram rejected that token. Copy it again from BotFather.", 400)
    _set_env_value("TELEGRAM_BOT_TOKEN", token)
    return jsonify({"username": me["username"]})


@app.post("/api/settings/gemini")
def api_set_gemini():
    key = re.sub(r"\s+", "", (request.json or {}).get("key", ""))  # keys copied from chat often wrap
    if len(key) < 20:
        return _err("That doesn't look like a Gemini API key.", 400)
    try:
        llm.set_api_key(key)
    except llm.LLMUnavailable as exc:
        return _err(exc, 400)
    _set_env_value("GEMINI_API_KEY", key)
    return jsonify({"ok": True})


@app.post("/api/import")
def api_import():
    f = request.files.get("file")
    if not f:
        return _err("No file uploaded", 400)
    with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as tmp:
        f.save(tmp.name)
    try:
        added = telegram_ingest.import_export(tmp.name)
    except (ValueError, KeyError) as exc:
        return _err(f"Not a Telegram JSON export: {exc}", 400)
    finally:
        os.unlink(tmp.name)
    return jsonify({"added": added})


@app.post("/api/drafts/<draft_id>/save")
def api_save(draft_id):
    draft = store.get_draft(draft_id)
    body = (request.json or {}).get("body", "")
    sources = (draft["angle"] or {}).get("sources") or []
    source_input = draft["note"]["text"] + "\n" + (draft["note"].get("extra_context") or "")
    allowed = "\n".join([source_input] + [f"{e['title']} {e['published']}" for e in sources])
    report = lint.lint(body, allowed, source_input=source_input)
    store.update_draft_body(draft_id, body, report)
    return jsonify({"lint": report})


@app.post("/api/drafts/<draft_id>/posted")
def api_posted(draft_id):
    body = (request.json or {}).get("body", "").strip()
    draft = store.get_draft(draft_id)
    store.set_draft_status(draft_id, "posted", final_body=body or draft["body"])
    store.set_note_status(draft["note_id"], "used")
    return jsonify({"ok": True})


if __name__ == "__main__":
    app.run(port=config.PORT, debug=False)
