"""Deterministic checks against the voice guide's publishing checklist.

The model is told the rules, but it can't be trusted to grade itself, so everything that
can be checked mechanically is checked here. 'fail' blocks the draft and triggers a rewrite.
'warn' goes to Meera as a flag she should look at.
"""
import re

LINKEDIN_LIMIT = 3000

SUPERLATIVES = [
    "best", "revolutionary", "powerful", "game-changer", "game changer", "ultimate", "amazing",
    "incredible", "unmatched", "world-class", "cutting-edge", "groundbreaking", "miracle",
    "breakthrough", "must-have", "unbeatable",
]
URGENCY = [
    "don't miss", "dont miss", "limited time", "hurry", "last chance", "act now", "only a few left",
    "while stocks last", "today only", "before it's gone", "selling out",
]
SALES_CTA = [
    "link in bio", "shop now", "buy now", "use code", "dm me", "order now", "check out our",
    "available at", "grab yours", "discount",
]
LOADED_WORDS = ["natural", "clean"]  # only allowed when interrogated -- a human has to judge that

EMOJI_RE = re.compile(
    "[\U0001F300-\U0001FAFF\U00002600-\U000027BF\U0001F000-\U0001F2FF\U0001F900-\U0001F9FF⭐✅]"
)
NUMBER_RE = re.compile(r"(?<![\w.])(\d+(?:[.,]\d+)?)(?:\s?%)?")
CHECK_RE = re.compile(r"\[CHECK:[^\]]*\]")


def _has_word(text, word):
    return re.search(rf"\b{re.escape(word)}\b", text, re.I) is not None


def _norm_num(s):
    return s.replace(",", "")


HASHTAG_RE = re.compile(r"(?<!\w)#\w+")
WORD_RE = re.compile(r"\b[\w'’-]+\b")
MIN_WORDS, MAX_WORDS, MAX_HASHTAGS = 100, 200, 2


def lint(body, allowed_source_text="", source_input=""):
    """allowed_source_text: Meera's input + the cited headlines. Any number in the draft that isn't
    in there was invented by the model. source_input: her raw input (emoji are allowed only if she
    used them)."""
    checks = []

    def add(level, rule, detail):
        checks.append({"level": level, "rule": rule, "detail": detail})

    stripped = body.strip()
    lines = [l for l in stripped.splitlines() if l.strip()]
    first_line = lines[0].strip() if lines else ""
    last_line = lines[-1].strip() if lines else ""

    # 1. Opening
    if first_line.endswith("?"):
        add("fail", "Opening", "First line is a question to the reader. Guide: never.")
    opens_meta = re.match(r"^(I want to|I've been avoiding|I have been avoiding|I want to be)", first_line)
    opens_data = re.search(r"\d", first_line)
    if not (opens_meta or opens_data):
        add("warn", "Opening", "First line is neither a data/scene drop (no number or date) nor a meta-announcement.")
    if re.search(r"\bskinstinct\b", first_line, re.I):
        add("fail", "Opening", "Product/company named in the first line.")

    # 2. Banned register
    if "!" in stripped:
        add("fail", "No exclamation points", f"{stripped.count('!')} found.")
    if EMOJI_RE.search(stripped) and not EMOJI_RE.search(source_input or ""):
        add("fail", "No emoji", "Emoji found, and her input had none.")
    tags = HASHTAG_RE.findall(stripped)
    if len(tags) > MAX_HASHTAGS:
        add("fail", "Hashtags", f"{len(tags)} hashtags; max {MAX_HASHTAGS}.")
    for w in SUPERLATIVES:
        if _has_word(stripped, w):
            add("fail", "No superlatives", f'"{w}"')
    for p in URGENCY:
        if p in stripped.lower():
            add("fail", "No urgency/scarcity", f'"{p}"')
    for p in SALES_CTA:
        if p in stripped.lower():
            add("fail", "No sales CTA", f'"{p}"')
    for w in LOADED_WORDS:
        if _has_word(stripped, w):
            add("warn", f'"{w}" used', f'Only OK if the post is interrogating the word, not using it approvingly.')

    # 3. Numbers must come from somewhere real
    source_nums = {_norm_num(n) for n in NUMBER_RE.findall(allowed_source_text)}
    invented = sorted({_norm_num(n) for n in NUMBER_RE.findall(stripped)} - source_nums)
    if invented:
        add("fail", "Unsourced numbers", "Not in the note or the cited source: " + ", ".join(invented))

    # 4. Signature moves (soft -- not every post needs every move)
    if not re.search(r"I'm not saying|I am not saying|I want to be (careful|honest|precise)", stripped):
        add("warn", "Clarifying hedge", "No \"I'm not saying X. I'm saying Y.\" -- her signature move is missing.")
    closing = " ".join(lines[-4:-1]).lower() if len(lines) > 1 else ""
    if "skinstinct" in closing or "our product" in closing:
        add("fail", "Outward close", "Closing points at her own product. It should point the reader at third parties.")

    body_lines = lines[:-1] if last_line == "Meera" else lines
    if not any(l.strip().endswith("?") for l in body_lines[-4:]):
        add("warn", "Closing question", "No open-ended question inviting comments near the end.")

    # 5. Sign-off
    if last_line != "Meera":
        add("fail", "Sign-off", f'Last line must be exactly "Meera" (got "{last_line[:40]}").')

    # 6. Leftover verification markers
    markers = CHECK_RE.findall(stripped)
    if markers:
        add("warn", "Needs your check", f"{len(markers)} claim(s) marked for you to verify before posting.")

    # 7. Length (words exclude the sign-off and hashtag line)
    words = len(WORD_RE.findall("\n".join(l for l in body_lines if not HASHTAG_RE.fullmatch(l.strip()))))
    if not MIN_WORDS <= words <= MAX_WORDS:
        add("fail", "Length", f"{words} words; target is {MIN_WORDS}-{MAX_WORDS}.")
    if len(stripped) > LINKEDIN_LIMIT:
        add("fail", "Length", f"{len(stripped)} chars; LinkedIn cuts off at {LINKEDIN_LIMIT}.")

    return {
        "checks": checks,
        "fails": sum(1 for c in checks if c["level"] == "fail"),
        "warns": sum(1 for c in checks if c["level"] == "warn"),
        "chars": len(stripped),
        "words": words,
    }
