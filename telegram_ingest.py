"""Ways notes get in, none of which asks Meera to change her habit:

1. Live: a bot added as an admin of her private channel. We poll getUpdates for channel_post
   events. Telegram only holds undelivered updates for ~24h, so this needs to run at least
   daily (the scheduled pipeline does).
2. Backlog: the ~60 notes already sitting in the channel predate the bot, and the Bot API
   cannot read history. Telegram Desktop -> channel -> Export chat history -> JSON produces
   a result.json we can import once.
3. Direct messages to the bot, from its owner only. The owner is TELEGRAM_NOTIFY_CHAT_ID, or if
   that's unset, whoever sends /start first (claimed once, stored in the db). Anyone can message
   a bot by username, so everyone else is ignored.

Voice notes are transcribed with Gemini. Each note gets exactly one reply from the bot: either
feedback asking for more, or a finished draft. Replying to that message adds to the same note.
"""
import json
import os
import urllib.parse
import urllib.request
from datetime import datetime, timezone

import config
import store

BOT_TOKEN = config.env("TELEGRAM_BOT_TOKEN")
CHANNEL_ID = config.env("TELEGRAM_CHANNEL_ID")  # optional filter, e.g. -1001234567890
NOTIFY_CHAT_ID = config.env("TELEGRAM_NOTIFY_CHAT_ID")

MIN_CHARS = 15  # skips "ok", stray links with no thought attached, etc.


def _api(method, params=None, timeout=30):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/{method}"
    data = urllib.parse.urlencode(params or {}).encode()
    with urllib.request.urlopen(url, data=data, timeout=timeout) as resp:
        payload = json.loads(resp.read())
    if not payload.get("ok"):
        raise RuntimeError(f"Telegram {method} failed: {payload}")
    return payload["result"]


def remember_message(msg_id, kind, target_id):
    """Link a bot message to the note/draft it's about, so a reply to it lands in the right place."""
    if msg_id:
        store.kv_set(f"msg:{msg_id}", f"{kind}:{target_id}")


def _lookup_message(msg_id):
    ref = store.kv_get(f"msg:{msg_id}")
    if ref:
        kind, target = ref.split(":", 1)
        return kind, target
    note = store.note_by_question_msg(msg_id)  # questions sent before replies were generalised
    return ("note", note["id"]) if note else (None, None)


def poll_channel(wait=0):
    """Pull new channel posts and owner DMs since the last poll.

    wait > 0 long-polls: Telegram holds the request open up to `wait` seconds until something
    arrives, which is how the listener gets messages instantly without hammering the API.

    Returns {"notes": [note_id, ...], "replies": [{"kind", "target", "text"}, ...]}.
    A reply is the owner replying to one of the bot's messages (a draft, a question, a skip).
    """
    result = {"notes": [], "replies": []}
    if not BOT_TOKEN:
        return result
    offset = int(store.kv_get("tg_offset", "0"))
    updates = _api(
        "getUpdates",
        {"offset": offset, "timeout": wait, "allowed_updates": json.dumps(["channel_post", "message", "callback_query"])},
        timeout=wait + 30,
    )
    for upd in updates:
        offset = max(offset, upd["update_id"] + 1)
        if upd.get("callback_query"):
            _answer_why(upd["callback_query"])
            continue
        post = upd.get("channel_post")
        if post:
            chat_id = str(post["chat"]["id"])
            if CHANNEL_ID and chat_id != CHANNEL_ID:
                continue
        else:
            post = upd.get("message")
            if not post or post["chat"]["type"] != "private":
                continue
            chat_id = str(post["chat"]["id"])
            text = (post.get("text") or "").strip()
            if owner_chat_id() is None and text.startswith("/start"):
                store.kv_set("owner_chat_id", chat_id)
                send(
                    "Connected. Send me a note or a voice note about anything you might post about. "
                    "If it's ready I'll send back a LinkedIn draft; if it needs more I'll tell you what. "
                    "Reply to any of my messages to add to that note. Nothing gets posted without you."
                )
            if chat_id != owner_chat_id():
                continue
        text = (post.get("text") or post.get("caption") or "").strip()
        audio = post.get("voice") or post.get("audio")
        if audio and not text:
            typing()
            try:
                text = _transcribe(audio)
            except Exception as exc:  # noqa: BLE001 -- tell her rather than silently dropping the note
                send(f"Couldn't transcribe that voice note ({str(exc)[:120]}). Could you send it again, or type it?")
                continue
        if not text or text.startswith("/"):
            continue

        # A reply to one of the bot's messages is about that note/draft, not a new note -- even if short.
        replied = (post.get("reply_to_message") or {}).get("message_id")
        kind, target = _lookup_message(replied) if replied else (None, None)
        if kind:
            result["replies"].append({"kind": kind, "target": target, "text": text})
            typing()
            continue

        if len(text) < MIN_CHARS:
            continue
        captured = datetime.fromtimestamp(post["date"], tz=timezone.utc).isoformat()
        note_id = store.add_note("telegram", f"{chat_id}:{post['message_id']}", captured, text)
        if note_id:
            result["notes"].append(note_id)
            remember_message(post["message_id"], "note", note_id)
            typing()  # the run's one message is its result; "typing..." shows it's working
    store.kv_set("tg_offset", offset)
    return result


def import_export(path):
    """Import a Telegram Desktop JSON export (result.json). Safe to re-run; dedupes on message id."""
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    chat_id = str(data.get("id", "export"))
    added = 0
    for msg in data.get("messages", []):
        if msg.get("type") != "message":
            continue
        text = _flatten_text(msg.get("text", "")).strip()
        if len(text) < MIN_CHARS:
            continue
        if store.add_note("export", f"{chat_id}:{msg['id']}", msg.get("date", ""), text):
            added += 1
    return added


def _flatten_text(t):
    # Exports store formatted text as a list of plain strings and {"type": ..., "text": ...} entities.
    if isinstance(t, str):
        return t
    return "".join(part if isinstance(part, str) else part.get("text", "") for part in t)


def owner_chat_id():
    return NOTIFY_CHAT_ID or store.kv_get("owner_chat_id")


def _transcribe(audio):
    import llm  # local import: llm pulls in the Gemini SDK, which ingest-only callers don't need
    path = _api("getFile", {"file_id": audio["file_id"]})["file_path"]
    with urllib.request.urlopen(f"https://api.telegram.org/file/bot{BOT_TOKEN}/{path}", timeout=60) as resp:
        data = resp.read()
    return llm.transcribe(data, audio.get("mime_type") or "audio/ogg")


def typing():
    chat_id = owner_chat_id()
    if BOT_TOKEN and chat_id:
        try:
            _api("sendChatAction", {"chat_id": chat_id, "action": "typing"})
        except Exception:  # noqa: BLE001 -- cosmetic only
            pass


def _answer_why(cq):
    """'Why this score?' button: show the Stage 1 reasoning as a popup. A popup isn't a message, so the
    run still sends exactly one, and the draft stays clean to copy."""
    text = "That run isn't available anymore."
    if str(cq.get("from", {}).get("id")) == owner_chat_id() and (cq.get("data") or "").startswith("why:"):
        run = store.get_run(cq["data"][4:])
        g = run.get("post_grade") if run else None
        if g:
            text = (f"Post {g['score']}/10: hook {g.get('hook')}/2, insight {g.get('insight')}/2, "
                    f"voice {g.get('voice')}/2, evidence {g.get('evidence')}/2, takeaway {g.get('takeaway')}/2. "
                    f"Idea scored {run['score']}/10 at the gate.")
        elif run and run.get("score") is not None:
            s1 = run["stage1"] or {}
            verdict = "passed the gatekeeper" if run["score"] >= 7 else "below 7, so feedback instead of a draft"
            text = f"Scored {run['score']}/10 ({verdict}). {s1.get('justification', '')}"
            if s1.get("borderline") and s1.get("rounding_note"):
                text += f" Rounded down: {s1['rounding_note']}"
    try:
        # Telegram caps popup text at 200 characters.
        _api("answerCallbackQuery", {"callback_query_id": cq["id"], "text": text[:200], "show_alert": "true"})
    except Exception:  # noqa: BLE001 -- the button just stops spinning
        pass


def send(text, reply_to=None, why_run_id=None):
    """Message the owner. Returns the sent message id, or None if there's no one to message yet.
    why_run_id adds a 'Why this score?' button for that pipeline run."""
    chat_id = owner_chat_id()
    if not (BOT_TOKEN and chat_id):
        return None
    params = {"chat_id": chat_id, "text": text[:4096], "disable_web_page_preview": "true"}
    if why_run_id:
        params["reply_markup"] = json.dumps(
            {"inline_keyboard": [[{"text": "Why this score?", "callback_data": f"why:{why_run_id}"}]]}
        )
    if reply_to:
        params["reply_to_message_id"] = reply_to
        params["allow_sending_without_reply"] = "true"
    return _api("sendMessage", params)["message_id"]


def notify(message):
    return send(message) is not None
