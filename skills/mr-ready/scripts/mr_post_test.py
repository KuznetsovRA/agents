#!/usr/bin/env python3
"""Самопроверка mr_post.py: GitLab ответил на POST 500, но заметку создал (MR !32772, 2026-11-17).
Запуск: python3 mr_post_test.py"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import mr_post  # noqa: E402

BODY = "находка"
STORED = {"id": "D1", "notes": [{"id": 7, "body": BODY + "\n", "system": False}]}


def post_500(host, method, endpoint, body=None, soft=False):
    if method == "POST":
        return {"error": "glab: 500 Internal Server Error (HTTP 500)"}
    return STORED


mr_post.api = post_500
mr_post.discussions = lambda host, proj, iid: [STORED]

assert mr_post.unpositioned("h", "p", 1, BODY)["note_id"] == 7, "тред создан при 500 — ok с его note_id"
assert mr_post.reply("h", "p", 1, "D1", BODY)["note_id"] == 7, "ответ создан при 500 — ok с его note_id"
try:
    mr_post.unpositioned("h", "p", 1, "другое тело")
    sys.exit("FAIL: 500 без заметки должен падать")
except SystemExit as e:
    assert "no note with this body" in str(e), e
print("ok")
