# Content Desk

**Live page:** https://meera-linkedin-agent-lac.vercel.app · **Repo:** https://github.com/samyak019/meera-linkedin-agent

Meera sends the bot a note (typed or a voice note). The note goes through a 4-stage pipeline and
she gets back exactly one Telegram message: either feedback asking for more, or a LinkedIn draft
ready to paste. **Nothing is ever posted automatically.**

```
note / voice note ─> Stage 1: score 1-10 ─> Stage 2: gatekeeper
                                              ├─ ≤6 → feedback: what's promising, what's missing, one question → STOP
                                              └─ ≥7 → Stage 3: thesis + google_rss_search (retry once if empty/irrelevant)
                                                        └─> Stage 4: draft (100-200 words) → checks → Telegram
```

Reply to any bot message to add to that note. The pipeline runs again on the combined input.
A reply to a draft is also treated as edit instructions.

## How the rules are enforced

The model makes the judgement calls inside each stage, but `orchestrator.py` owns the control flow,
so the tool rules hold whatever the model outputs:

- `google_rss_search` runs only in Stage 3, only if the score is 7 or higher, at most twice. The
  second search uses a reformulated query and runs only if the first returned nothing usable. A
  separate relevance check rejects shopping roundups and listicles. If nothing usable turns up, the
  draft says so rather than inventing data.
- `send_telegram_message` runs exactly once per run. While it works, the bot shows "typing…"
  instead of sending acknowledgements.
- Every stage's reasoning (score, justification, rounding note, thesis, each query and why its
  results were or weren't used) is stored and shown on the web page.
- `lint.py` checks each draft mechanically: 100-200 words, at most 2 hashtags, no emoji unless her
  input had them, no invented numbers, no exclamation points, superlatives or sales CTAs, a closing
  question, and the "Meera" sign-off. Drafts that fail are rewritten, up to 3 attempts.
- Her voice guide shapes tone and rhythm. Where it conflicts with the spec's structure (the spec
  ends on a question for comments), the spec wins.

Google News RSS returns headline, publisher and date only, so drafts can only cite what a
headline says, and they link the item. The feed's terms describe it as for personal,
non-commercial use.

## Public web page (Vercel)

`api/index.py` is a small, stateless page: type a note, and the same 4 stages run and show on the
page. You see the idea score, the post score, the draft, the related news with links, and how
each stage decided. It has no database and no Telegram, since the bot runs from the local app below.

Deploy: import this repo in Vercel and set the environment variable `GEMINI_API_KEY`. Optionally
set `GEMINI_MODEL`, and `VOICE_GUIDE` to override `voice_guide.txt`. `.vercelignore` keeps the
local-only modules (SQLite, Telegram listener) out of the deployment.

The page is open to anyone with the link, so anyone can spend the Gemini key's quota.

## Setup (local app + Telegram bot)

```bash
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env        # fill in keys
python app.py               # http://localhost:5003
```

**Backlog (the ~60 existing notes):** the Bot API can't read channel history. In Telegram Desktop,
open the channel → ⋮ → Export chat history → format JSON, then upload `result.json` with "Import
Telegram export" (or run `python pipeline.py import result.json`). Re-importing is safe; it
dedupes.

**Direct messages:** notes can also be sent straight to the bot in a private chat. Only the
bot's owner is listened to: whoever sends `/start` first (or `TELEGRAM_NOTIFY_CHAT_ID` if set).
That chat also receives the "drafts ready" message.

**Ongoing:** Telegram only keeps undelivered bot updates for about 24h, so run the pipeline at
least daily:

```cron
0 7 * * *  cd /path/to/content-desk && venv/bin/python pipeline.py run
```

Each run polls Telegram, triages new notes, and tops the queue back up to 3. If
`TELEGRAM_NOTIFY_CHAT_ID` is set, Meera gets a Telegram message when drafts are waiting.

## Cost / quotas

Each note is triaged once (1 call). Each draft costs 1 grounded search call plus 1–3 drafting
calls. Three posts a week comes to roughly 10–15 calls. The free Gemini tier (5/min, 20/day,
limited search grounding) is enough to try it, but importing the 60-note backlog will hit
the daily cap. When search is unavailable the draft says so and offers "Retry search &
redraft". It doesn't silently skip the angle.

`sample/result.json` is made-up test data, not Meera's notes. Delete `content_desk.db` to reset.
