"""Wires Telegram to the 4-stage pipeline in orchestrator.py.

CLI:
    python pipeline.py import path/to/result.json   # one-time backlog import (not sent to Telegram)
    python pipeline.py run                          # poll Telegram once, process what arrived + some backlog
    python pipeline.py listen                       # stay connected; handle notes as they arrive
    python pipeline.py try "some note text"         # run one input through the stages, print everything
"""
import sys
import threading
import time
from datetime import datetime, timezone

import orchestrator
import store
import telegram_ingest

BACKLOG_PER_RUN = 3  # imported notes are processed a few at a time so a 60-note import fits the quota

_listener = None
_run_lock = threading.Lock()  # the listener thread and the web page share state


def handle_live(events, log=print):
    """Notes and replies that just arrived in Telegram. Each one is a full pipeline run that ends in
    exactly one Telegram message."""
    for note_id in events["notes"]:
        orchestrator.run_pipeline(note_id, log=log)
    for r in events["replies"]:
        if r["kind"] == "draft":
            old = store.get_draft(r["target"])
            if not old:
                continue
            store.add_context(old["note_id"], r["text"])
            orchestrator.run_pipeline(old["note_id"], revision=r["text"], previous_draft=old, log=log)
        else:
            store.add_context(r["target"], r["text"])
            orchestrator.run_pipeline(r["target"], log=log)


def process_backlog(log=print):
    """Imported notes: run them through the stages but don't message her about each one; the
    results show up on the page."""
    pending = [n for n in store.untriaged_notes() if n["source"] == "export"][:BACKLOG_PER_RUN]
    for note in pending:
        orchestrator.run_pipeline(note["id"], deliver=False, log=log)
    return len(pending)


def run(log=print):
    with _run_lock:
        if _listener and _listener.is_alive() and telegram_ingest.BOT_TOKEN:
            events = {"notes": [], "replies": []}  # the listener already has the Telegram connection
        else:
            events = telegram_ingest.poll_channel()
            handle_live(events, log)
        log(f"Telegram: {len(events['notes'])} new note(s), {len(events['replies'])} repl(ies)")
        backlog = process_backlog(log)
        log(f"Backlog: processed {backlog} imported note(s)")
        return {"new_notes": len(events["notes"]), "replies": len(events["replies"]), "backlog": backlog}


def try_text(text, deliver=True, log=print):
    """Run an input typed on the page (or CLI) through the pipeline, as if she'd sent it."""
    now = datetime.now(timezone.utc)
    note_id = store.add_note("web", f"web:{now.timestamp()}", now.isoformat(), text)
    with _run_lock:
        return orchestrator.run_pipeline(note_id, deliver=deliver, log=log)


def start_listener(log=print):
    """Long-poll Telegram in the background so notes are handled as they arrive."""
    global _listener
    if _listener and _listener.is_alive():
        return

    def loop():
        while True:
            if not telegram_ingest.BOT_TOKEN:
                time.sleep(10)
                continue
            try:
                events = telegram_ingest.poll_channel(wait=50)
                if events["notes"] or events["replies"]:
                    with _run_lock:
                        handle_live(events, log)
            except Exception as exc:  # network blips, Telegram 5xx -- keep listening
                log(f"listener: {exc}")
                time.sleep(10)

    _listener = threading.Thread(target=loop, daemon=True, name="telegram-listener")
    _listener.start()


if __name__ == "__main__":
    store.init_db()
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    if cmd == "import" and len(sys.argv) > 2:
        print(f"Imported {telegram_ingest.import_export(sys.argv[2])} note(s)")
    elif cmd == "run":
        print(run())
    elif cmd == "listen":
        print(run())  # catch up first
        start_listener()
        _listener.join()
    elif cmd == "try" and len(sys.argv) > 2:
        result = try_text(" ".join(a for a in sys.argv[2:] if a != "--send"), deliver="--send" in sys.argv)
        print("\n--- message ---\n" + result["message"])
    else:
        print(__doc__)
