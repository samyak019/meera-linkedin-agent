"""The 4-stage pipeline: Evaluate & Score -> Gatekeeper -> Refine & Research -> Synthesize & Draft.

The model does the judgement inside each stage; this code owns the control flow, so the tool rules
hold no matter what the model says:
  - google_rss_search runs only in Stage 3, only if the score is >= 7, at most twice (the second
    time only if the first result set was empty or irrelevant, with a reformulated query).
  - send_telegram_message runs exactly once per run, at whichever stage ends it.
  - Stage 1's score and justification are recorded before any tool is called.
"""
import os
import re
import uuid

import lint
import llm
import rss

HERE = os.path.dirname(os.path.abspath(__file__))


def _load_voice_guide():
    """The real guide is private: it comes from the VOICE_GUIDE env var (Vercel) or a gitignored
    voice_guide.txt (local). The public repo only ships voice_guide.example.txt."""
    if os.environ.get("VOICE_GUIDE", "").strip():
        return os.environ["VOICE_GUIDE"]
    for name in ("voice_guide.txt", "voice_guide.example.txt"):
        path = os.path.join(HERE, name)
        if os.path.exists(path):
            with open(path, encoding="utf-8") as f:
                return f.read()
    return ""


VOICE_GUIDE = _load_voice_guide()

ABOUT_MEERA = (
    "Meera Pillai is the founder of Skinstinct, an Indian skincare brand with its own manufacturing "
    "unit. She spent 2 years in pharma before founding it. Her LinkedIn audience: other founders, "
    "formulators, D2C operators, and informed skincare buyers."
)

PASS_SCORE = 7
MAX_DRAFT_ATTEMPTS = 3

# ---------------------------------------------------------------- Stage 1

STAGE1_SYSTEM = f"""You are Meera's content strategist. Stage 1: Evaluate & Score.

{ABOUT_MEERA}

Score the transcribed input 1-10 for LinkedIn relevancy:
- 1-3: Personal/off-topic chatter, incomplete thoughts, nothing professional.
- 4-6: A professional topic is present, but the point is vague, generic, or already overdone \
("hard work pays off", "communication is key") with no personal angle or specific example.
- 7-10: A clear, specific professional insight, opinion, or update -- something a reader couldn't \
get from a generic listicle. Must include at least one concrete detail (an example, number, \
experience, or stance) that makes it Meera's.

If the score is borderline between two bands, round DOWN and say why in one sentence -- false \
positives waste Meera's time worse than an extra clarifying round.

Also prepare gatekeeper feedback in case the score is 6 or below (always fill these in). It goes \
straight to Meera, so address her as "you", keep it warm and specific, and don't mention the score:
- promising: what's promising about the idea (specific to her input, not generic praise)
- missing: exactly what's missing (a concrete example, a number, a stance, a "why now")
- question: ONE prompting question that helps her elaborate -- concrete, answerable in a line or \
two by voice note

Return ONLY JSON:
{{"score": 1-10, "justification": "2-3 sentences", "borderline": true|false, \
"rounding_note": "one sentence if borderline, else null", \
"promising": "...", "missing": "...", "question": "..."}}"""


def stage1_score(text):
    result, _ = llm.generate_json(STAGE1_SYSTEM, f"<transcribed_input>\n{text}\n</transcribed_input>", temperature=0.2)
    try:
        score = int(result.get("score"))
    except (TypeError, ValueError):
        score = 1
    result["score"] = max(1, min(10, score))
    return result


def stage2_feedback_message(s1):
    return (
        f"Score: {s1['score']}/10 (a note needs 7+ to become a draft)\n\n"
        f"{s1.get('promising', '').strip()}\n\n"
        f"What's missing: {s1.get('missing', '').strip()}\n\n"
        f"{s1.get('question', '').strip()}\n\n"
        "(Reply to this message with more and I'll take another look.)"
    )


# ---------------------------------------------------------------- Stage 3

STAGE3_SYSTEM = f"""You are Meera's content strategist. Stage 3: Refine & Research.

{ABOUT_MEERA}

1. Distill her input into a one-sentence core thesis, in plain words.
2. Write ONE precise news search query for Google News (2-5 words -- short queries match headlines; \
long SEO strings return market-research spam). Use the nouns a news headline would use: industry, \
regulator, or policy terms, with "India" when it helps (e.g. "CDSCO cosmetics", "cosmetics \
manufacturing India quality", "sunscreen SPF labelling rules"), not consumer shopping terms ("best \
toners"). No competitor brand names.

Return ONLY JSON: {{"thesis": "...", "query": "..."}}"""

RELEVANCE_SYSTEM = """You pick news items to support or frame a LinkedIn post. You only see \
headline, publisher and date -- not the article text -- so judge on what the HEADLINE itself says.

Each usable item gets a role:
- "supports": the headline itself states something that backs up or directly bears on the thesis.
- "context": recent industry, regulatory or research news in the same area the post operates in \
(quality, manufacturing, regulation, ingredients, the Indian cosmetics market) that gives the post a \
genuine "why now", even if it doesn't mention the post's specific topic. The test: could a reader see \
the connection from one sentence? Examples for a post about in-house raw-material testing: "CDSCO's \
cosmetic regulations: the rules exist, the enforcement does not" is context (weak enforcement means \
manufacturers must police quality themselves); a large contract manufacturer acquiring a brand's \
factory is context (more brands outsourcing production is why "ask your manufacturer" matters). \
Don't be literal about keywords; do be strict that the connection is real.

Never usable: market-size / "market report" / forecast pages (MarketsandMarkets, Fact.MR, Fortune \
Business Insights, Grand View Research and similar), product roundups and shopping listicles, \
celebrity routines, and news about a competitor brand's products.

If nothing is usable, write a reformulated query that is BROADER, not a reshuffle: step up one \
level from the specific process to the industry or regulator (e.g. "cosmetic water testing" -> \
"cosmetics manufacturing India quality" or "CDSCO cosmetics"), 2-5 words, and don't reuse most of \
the previous query's words.

Return ONLY JSON: {"usable": true|false, "picked": [{"i": index, "role": "supports|context", \
"connection": "one sentence: how this headline relates to the post"}] (up to 3), "reason": "one \
sentence", "reformulated_query": "... or null"}"""


def _same_words(a, b):
    wa, wb = set(a.lower().split()), set(b.lower().split())
    return len(wa & wb) >= 0.75 * max(len(wa), len(wb), 1)


def _judge(thesis, results):
    listing = "\n".join(
        f"[{i}] {r['title']} -- {r['source']}, {r['published']}" for i, r in enumerate(results)
    )
    verdict, _ = llm.generate_json(
        RELEVANCE_SYSTEM, f"THESIS: {thesis}\n\nRESULTS:\n{listing}", temperature=0.1
    )
    picked, roles, connections = [], {}, {}
    for p in verdict.get("picked") or []:
        i, role = (p.get("i"), p.get("role")) if isinstance(p, dict) else (p, "supports")
        if isinstance(i, int) and 0 <= i < len(results) and i not in picked:
            picked.append(i)
            roles[str(i)] = role if role in ("supports", "context") else "context"
            connections[str(i)] = (p.get("connection") or "") if isinstance(p, dict) else ""
    verdict.update(picked=picked[:3], roles=roles, connections=connections,
                   usable=bool(verdict.get("usable")) and bool(picked))
    return verdict


def stage3_research(text):
    plan, _ = llm.generate_json(STAGE3_SYSTEM, f"<transcribed_input>\n{text}\n</transcribed_input>", temperature=0.2)
    thesis, query = plan.get("thesis", "").strip(), plan.get("query", "").strip()
    searches = []
    for attempt in range(2):
        try:
            results = rss.google_rss_search(query)
        except Exception as exc:  # network / feed errors count as an empty result set
            results, err = [], str(exc)[:120]
        else:
            err = None
        if attempt == 1:
            # Judge the second search together with the first one's headlines: a good item from either
            # counts, without spending a third search.
            seen = {r["url"] for r in results}
            results = results + [r for r in searches[0]["results"] if r["url"] not in seen]
        entry = {"query": query, "results": results, "usable": False, "picked": [], "reason": err or ""}
        if results:
            verdict = _judge(thesis, results)
            entry.update(usable=verdict["usable"], picked=verdict["picked"], roles=verdict["roles"],
                         connections=verdict["connections"], reason=verdict.get("reason", ""))
            next_query = verdict.get("reformulated_query")
        else:
            entry["reason"] = entry["reason"] or "No results."
            next_query = None
        searches.append(entry)
        if entry["usable"] or attempt == 1:
            break
        if not next_query or _same_words(next_query, query):
            # Reordering the same words finds the same headlines; ask for a genuinely broader query.
            retry, _ = llm.generate_json(
                STAGE3_SYSTEM,
                f"<transcribed_input>\n{text}\n</transcribed_input>\n\nThe query \"{query}\" found nothing "
                "usable. Write a BROADER query one level up (the industry or regulator, not the specific "
                "process), using mostly different words.",
                temperature=0.5,
            )
            next_query = retry.get("query")
        if not next_query or _same_words(next_query, query):
            break
        query = next_query.strip()
    evidence = []
    for s in searches:
        if s["usable"]:
            evidence = [
                dict(s["results"][i], role=s["roles"].get(str(i), "context"),
                     connection=s.get("connections", {}).get(str(i), ""))
                for i in s["picked"]
            ]
    if not evidence:
        adjacent = _adjacent_article(thesis, searches)
        evidence = [adjacent] if adjacent else []
    return thesis, searches, evidence


# Market-size/forecast pages and press-release mills: never worth linking under a post.
SPAM_RE = re.compile(
    r"market (size|share|report|analysis|forecast|growth)|\bcagr\b|\b20[3-4]\d\b.*report|"
    r"marketsandmarkets|fact\.mr|fortune business insights|grand view research|research and markets|"
    r"mordor intelligence|imarc|openpr|ein presswire|globenewswire|market\.us|precedence research",
    re.I,
)
FALLBACK_QUERIES = ["skincare industry India", "cosmetics industry India"]

ADJACENT_SYSTEM = """No headline directly supports this LinkedIn post, but the reader still gets one \
related news article under it. From the headlines below, pick the ONE most related or adjacent item: \
same industry, same kind of problem, same regulator, or the same audience's concerns. It must be real \
news or analysis -- not a market-size report, product roundup, or a competitor's product news.

Return ONLY JSON: {"i": index, "connection": "one honest sentence on how it relates (say plainly if \
it's only loosely related)"}"""


def _clean(results):
    seen, out = set(), []
    for r in results:
        if r["url"] in seen or SPAM_RE.search(f"{r['title']} {r['source']}"):
            continue
        seen.add(r["url"])
        out.append(r)
    return out


def _adjacent_article(thesis, searches):
    """Always come back with one article: the closest adjacent item from what was already searched,
    or from one broad industry search if those had nothing linkable."""
    pool = _clean([r for s in searches for r in s["results"]])
    if not pool:
        for q in FALLBACK_QUERIES:
            try:
                results = _clean(rss.google_rss_search(q))
            except Exception:  # noqa: BLE001 -- feed down; try the next broad query
                results = []
            searches.append({"query": q, "results": results, "usable": False, "picked": [],
                             "reason": "Broad fallback search for an adjacent article."})
            if results:
                pool = results
                break
    if not pool:
        return None  # Google News itself returned nothing -- the only case with no article
    listing = "\n".join(f"[{i}] {r['title']} -- {r['source']}, {r['published']}" for i, r in enumerate(pool))
    try:
        pick, _ = llm.generate_json(ADJACENT_SYSTEM, f"POST THESIS: {thesis}\n\nHEADLINES:\n{listing}", temperature=0.1)
        i = pick.get("i")
        connection = pick.get("connection") or ""
    except llm.LLMUnavailable:
        i, connection = None, ""
    if not isinstance(i, int) or not 0 <= i < len(pool):
        i, connection = 0, "Closest recent industry news found; only loosely related to the post."
    return dict(pool[i], role="adjacent", connection=connection)


# ---------------------------------------------------------------- Stage 4

STAGE4_SYSTEM = f"""You are writing a LinkedIn post AS Meera Pillai. Stage 4: Synthesize & Draft.

{ABOUT_MEERA}

STRUCTURE (in this order):
1. Hook: 1-2 lines, specific, not generic. Open with one of her two patterns from the voice guide \
(a cold data/scene drop, or a meta-announcement). Never open with a question.
2. Her core insight, in her own words and tone. Preserve her original phrasing and specific \
details wherever possible -- refine grammar and flow, don't replace her language with generic \
corporate phrasing.
3. Supporting data or news from the provided sources, cited naturally in-line by publisher and \
month. A "supports" source can back up her point; a "context" source can only frame why this \
matters now ("This comes as CDSCO..."), never be presented as proof. Use ONLY what the headline \
states -- you have not read the articles, so do not attribute anything beyond the headline to \
them. Do not paste URLs into the post; links are attached separately. If no sources are provided, \
do not reference outside data at all -- the post stands on her experience.
4. One actionable takeaway, aimed outward (something the reader can ask, check, or do).
5. One open-ended question inviting comments.
Then a blank line and the sign-off: Meera

RULES:
- Length: 100-200 words (not counting the sign-off). Short paragraphs, line breaks for readability.
- At most 2 hashtags, on their own line before the sign-off, or none.
- No emoji unless her input has emoji. No exclamation points. No superlatives, urgency, sales CTAs, \
or competitor names.
- Conversational, authoritative, authentic. Not robotic, not salesy.
- Facts: only what's in her input and the provided headlines. Never invent numbers, events, steps, \
outcomes, or timing ("last month", "this week") that her input doesn't state. If something the post needs isn't there, write around it or insert [CHECK: ...].
- Plain text only, no markdown. Output the post and nothing else -- no preamble.

HER VOICE GUIDE (for tone, rhythm and word choice; where it conflicts with the structure above, the \
structure above wins):
{VOICE_GUIDE}"""


def _stage4_prompt(text, thesis, evidence, revision, previous_body, lint_feedback, examples=()):
    parts = [f"<transcribed_input>\n{text}\n</transcribed_input>", f"CORE THESIS: {thesis}"]
    if evidence:
        parts.append("SOURCES (headline only):\n" + "\n".join(
            f"- [{e.get('role', 'context')}] \"{e['title']}\" -- {e['source']}, {e['published']}"
            + (f"\n  How it connects: {e['connection']}" if e.get("connection") else "")
            for e in evidence
        ))
    else:
        parts.append("SOURCES: none found.")
    if examples:
        parts.append("POSTS SHE ACTUALLY PUBLISHED (her final edits -- match these closely):\n\n" + "\n\n---\n\n".join(examples))
    if previous_body:
        parts.append(f"YOUR PREVIOUS DRAFT:\n{previous_body}")
    if revision:
        parts.append(f"HER RESPONSE TO THE PREVIOUS DRAFT (apply it):\n{revision}")
    if lint_feedback:
        parts.append("YOUR LAST ATTEMPT BROKE THESE RULES. FIX ALL OF THEM:\n- " + "\n- ".join(lint_feedback))
    return "\n\n".join(parts)


def stage4_draft(text, thesis, evidence, revision=None, previous_body=None, examples=()):
    allowed = "\n".join([text, revision or ""] + [f"{e['title']} {e['published']}" for e in evidence])
    best, feedback = None, None
    for _ in range(MAX_DRAFT_ATTEMPTS):
        body, _ = llm.generate(
            STAGE4_SYSTEM, _stage4_prompt(text, thesis, evidence, revision, previous_body, feedback, examples)
        )
        body = body.strip().strip("`").strip()
        report = lint.lint(body, allowed, source_input=text)
        if best is None or report["fails"] < best[1]["fails"]:
            best = (body, report)
        if report["fails"] == 0:
            break
        feedback = [f"{c['rule']}: {c['detail']}" for c in report["checks"] if c["level"] == "fail"]
    return best


GRADE_SYSTEM = f"""You grade a finished LinkedIn draft for Meera Pillai, strictly, out of 10. A 10 is rare: \
a post she'd publish untouched. A generic post that could be anyone's is a 4 at best.

{ABOUT_MEERA}

Score these five, 0-2 each, then total them:
- hook: specific and scroll-stopping, in one of her two opening patterns (naming her own batch or \
process is fine; only naming the Skinstinct brand/product line in the first line is a fault)
- insight: her concrete detail and stance come through, not generic advice
- voice: sounds like her guide (rhythm, clarifying hedge, no hype), not corporate
- evidence: every claim is grounded in her input or a cited headline; nothing invented (a cited \
headline's publisher and date are grounded, not invented)
- takeaway: an outward, actionable takeaway plus a real open question

Return ONLY JSON: {{"hook": 0-2, "insight": 0-2, "voice": 0-2, "evidence": 0-2, "takeaway": 0-2, \
"reason": "one sentence: the main thing holding it back, or why it's strong"}}

HER VOICE GUIDE:
{VOICE_GUIDE}"""


def grade_post(body, text, cited, report):
    grade, _ = llm.generate_json(
        GRADE_SYSTEM,
        f"HER INPUT:\n{text}\n\nCITED HEADLINES:\n"
        + ("\n".join(f"- {e['title']} ({e['source']}, published {e['published']})" for e in cited) or "none")
        + f"\n\nDRAFT:\n{body}",
        temperature=0.1,
    )
    parts = ("hook", "insight", "voice", "evidence", "takeaway")
    score = sum(max(0, min(2, int(grade.get(k) or 0))) for k in parts)
    # The grader can be generous; the mechanical checks can't. Unfixed rule breaks or open [CHECK]s cap it.
    if report["fails"]:
        score = min(score, 6)
    elif lint.CHECK_RE.search(body):
        score = min(score, 8)
    return {"score": score, "reason": grade.get("reason", ""), **{k: grade.get(k) for k in parts}}


def sources_block(evidence, searches):
    if not evidence:
        tried = ", ".join(f'"{s["query"]}"' for s in searches) or "none"
        return f"Related news: Google News returned nothing at all (searched {tried})."
    labels = {"supports": "supports the point", "context": "context / why now",
              "adjacent": "adjacent: related reading, not cited in the post"}
    lines = ["Related news:"]
    for n, e in enumerate(evidence, 1):
        why = f"\n   Why: {e['connection']}" if e.get("connection") else ""
        lines.append(f"{n}. {e['title']} -- {e['source']}, {e['published']} ({labels.get(e.get('role'), 'context')}){why}\n   {e['url']}")
    lines.append("Headlines only: open the link and check it before posting.")
    return "\n".join(lines)


def draft_message(post_grade, body, evidence, searches):
    """Score on top, the post between dividers (copy just that part), related news underneath."""
    return (
        f"Post score: {post_grade['score']}/10 -- {post_grade['reason']}\n"
        "———\n"
        f"{body}\n"
        "———\n"
        f"{sources_block(evidence, searches)}"
    )


# ---------------------------------------------------------------- run

def process_text(text, revision=None, previous_body=None, examples=(), log=print):
    """All four stages on one input, with no storage or Telegram -- the Vercel page calls this
    directly. Returns everything each stage produced, plus the message the bot would send."""
    out = {"input_text": text, "score": None, "stage1": None, "thesis": None, "searches": [],
           "evidence": [], "body": None, "report": None, "post_grade": None, "error": None}
    try:
        # Stage 1 -- reasoning is recorded before any tool call.
        s1 = stage1_score(text)
        out.update(score=s1["score"], stage1=s1)
        log(f"  Stage 1: {s1['score']}/10 -- {s1.get('justification')}"
            + (f" (rounded down: {s1['rounding_note']})" if s1.get("borderline") and s1.get("rounding_note") else ""))

        # Stage 2 -- gatekeeper.
        if s1["score"] < PASS_SCORE:
            log("  Stage 2: below 7, feedback only")
            return dict(out, outcome="feedback", message=stage2_feedback_message(s1))

        # Stage 3 -- thesis + research.
        thesis, searches, evidence = stage3_research(text)
        out.update(thesis=thesis, searches=searches, evidence=evidence)
        log(f"  Stage 3: thesis: {thesis}")
        for s in searches:
            log(f"    search \"{s['query']}\": {len(s['results'])} result(s), usable={s['usable']} -- {s['reason']}")

        # Stage 4 -- draft. Only supporting/context sources are cited in the post; an adjacent
        # article is attached underneath as related reading.
        cited = [e for e in evidence if e.get("role") in ("supports", "context")]
        body, report = stage4_draft(text, thesis, cited, revision=revision, previous_body=previous_body,
                                    examples=examples)
        post_grade = grade_post(body, text, cited, report)
        out.update(body=body, report=report, post_grade=post_grade)
        log(f"  Stage 4: drafted ({report['words']} words, {report['fails']} rule fail(s)), "
            f"post score {post_grade['score']}/10 -- {post_grade['reason']}")
        for e in evidence:
            log(f"    article [{e['role']}]: {e['title']} ({e['source']})")
        return dict(out, outcome="draft", message=draft_message(post_grade, body, evidence, searches))

    except llm.LLMUnavailable as exc:
        log(f"  run failed: {exc}")
        return dict(out, outcome="error", error=str(exc),
                    message=f"Couldn't process that note right now: {str(exc)[:200]}")


def run_pipeline(note_id, revision=None, previous_draft=None, deliver=True, log=print):
    """process_text plus the local app's storage and the one Telegram message per run.
    deliver=False (backlog imports) records the run without sending it."""
    import store  # local imports: the Vercel page uses process_text only, with no database or bot
    import telegram_ingest

    note = store.get_note(note_id)
    text = note["text"] + (f"\n\n{note['extra_context']}" if note.get("extra_context") else "")
    result = process_text(text, revision=revision, examples=store.posted_examples(), log=log,
                          previous_body=previous_draft["body"] if previous_draft else None)
    run = {k: result[k] for k in ("input_text", "score", "stage1", "thesis", "searches",
                                  "post_grade", "outcome", "message", "error")}
    run.update(id=uuid.uuid4().hex[:12], note_id=note_id, delivered=0)

    if result["outcome"] == "feedback":
        store.set_note_verdict(note_id, "needs_detail")
    elif result["outcome"] == "draft":
        angle = {"found": any(e.get("role") != "adjacent" for e in result["evidence"]),
                 "sources": result["evidence"], "thesis": result["thesis"], "post_grade": result["post_grade"]}
        run["draft_id"] = store.add_draft(note_id, result["body"], angle, result["report"])
        store.set_note_verdict(note_id, "develop")
        store.set_note_status(note_id, "drafted")
        if previous_draft and previous_draft.get("status") == "ready":
            store.set_draft_status(previous_draft["id"], "superseded")

    if deliver:
        msg_id = telegram_ingest.send(  # the one send_telegram_message call for this run
            run["message"], why_run_id=run["id"] if run.get("score") is not None else None
        )
        run["delivered"] = int(msg_id is not None)
        if msg_id:
            kind, target = ("draft", run["draft_id"]) if run["outcome"] == "draft" else ("note", note_id)
            telegram_ingest.remember_message(msg_id, kind, target)
    store.save_run(run)
    return run
