"""Vercel entry point: the public mini page. Stateless -- it runs the 4-stage pipeline on whatever
is typed and returns every stage's output. No database, no Telegram (the bot runs from the local app)."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from flask import Flask, jsonify, render_template, request  # noqa: E402

import llm  # noqa: E402
import orchestrator  # noqa: E402

app = Flask(__name__, template_folder=os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "templates"))

MAX_INPUT_CHARS = 4000


# Vercel's rewrite hands every request to this function, and the path Flask sees depends on the
# runtime version, so route on method rather than path: GET serves the page, POST runs a note.
@app.get("/", defaults={"_path": ""})
@app.get("/<path:_path>")
def page(_path):
    return render_template("web.html", has_key=llm.client is not None)


@app.post("/", defaults={"_path": ""})
@app.post("/<path:_path>")
def run(_path):
    text = ((request.json or {}).get("text") or "").strip()
    if len(text) < 5:
        return jsonify({"error": "Type a note first."}), 400
    if len(text) > MAX_INPUT_CHARS:
        return jsonify({"error": f"Keep it under {MAX_INPUT_CHARS} characters."}), 400
    if llm.client is None:
        return jsonify({"error": "This deployment has no GEMINI_API_KEY set."}), 503
    log = []
    result = orchestrator.process_text(text, log=log.append)
    result["log"] = log
    return jsonify(result)
