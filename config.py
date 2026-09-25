import os

from dotenv import dotenv_values, load_dotenv

HERE = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(HERE, ".env"))
# Fall back to the sibling project's .env for anything left blank here (e.g. GEMINI_API_KEY=).
for _k, _v in dotenv_values(os.path.join(HERE, "..", ".env")).items():
    if _v and not os.environ.get(_k):
        os.environ[_k] = _v

POSTS_PER_WEEK = int(os.environ.get("POSTS_PER_WEEK", 3))
# Never let unreviewed drafts pile up past one week's worth -- a backlog of drafts is just the
# Google Drive folder again.
MAX_READY_DRAFTS = int(os.environ.get("MAX_READY_DRAFTS", POSTS_PER_WEEK))
PORT = int(os.environ.get("CONTENT_DESK_PORT", 5003))
PUBLIC_URL = os.environ.get("CONTENT_DESK_URL", f"http://localhost:{PORT}")


def env(key, default=""):
    """Read a setting, treating a leftover '# comment' as blank (older .env files had inline comments)."""
    value = os.environ.get(key, default).strip()
    return "" if value.startswith("#") else value
