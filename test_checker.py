import datetime as dt
from zoneinfo import ZoneInfo

import checker

TZ = ZoneInfo("America/Chicago")
# Tests use their own fixed settings so they pass no matter how config.yaml is set up for a facility.
CFG = {
    "channel_id": "CTEST",
    "timezone": "America/Chicago",
    "shifts": [{"name": "Day", "start": "06:00", "end": "16:30"},
               {"name": "Night", "start": "18:00", "end": "04:30"}],
    "work_days": [0, 1, 2, 3, 4, 5, 6],
    "alert_user_ids": ["U1"],
    "accepted_reaction": "white_check_mark",
    "timing": {"start_checklist_due_after_start": 30, "handoff_reminder_before_end": 30,
               "handoff_due_after_end": 15, "catch_up_minutes": 60},
    "messages": checker.load_config()["messages"],
}


class FakeSlack:
    def __init__(self, messages=None):
        self.messages = list(messages or [])
        self.posted = []
        self.reactions = []

    def conversations_history(self, channel, oldest, latest, **kw):
        msgs = [m for m in self.messages if float(oldest) <= float(m["ts"]) <= float(latest)]
        return {"messages": msgs, "response_metadata": {}}

    def auth_test(self):
        return {"user_id": "UBOT"}

    def reactions_add(self, channel, timestamp, name):
        for m in self.messages:
            if m["ts"] == timestamp:
                m.setdefault("reactions", []).append({"name": name, "users": ["UBOT"]})
                self.reactions.append(timestamp)

    def chat_postMessage(self, channel, text, metadata):
        self.posted.append(text)
        self.messages.append({"ts": str(self._now.timestamp()), "bot_id": "B1", "text": text, "metadata": metadata})


def at(y, mo, d, h, mi):
    return dt.datetime(y, mo, d, h, mi, tzinfo=TZ)


def photo(when):
    return {"ts": str(when.timestamp()), "user": "U1", "files": [{"mimetype": "image/jpeg"}]}


def run(client, now):
    client._now = now
    return checker.run(client, CFG, now=now)


def test_start_reminder_posts_once():
    c = FakeSlack()
    run(c, at(2026, 10, 27, 6, 2))
    run(c, at(2026, 10, 27, 6, 12))
    assert len([p for p in c.posted if "Day shift has started" in p]) == 1


def test_alert_when_start_photo_missing():
    c = FakeSlack()
    run(c, at(2026, 10, 27, 6, 2))
    acts = run(c, at(2026, 10, 27, 6, 35))
    assert ("alert", "start_check", "2026-10-27-Day") in acts
    assert any("No start-of-shift checklist photo" in p for p in c.posted)


def test_no_alert_when_photo_posted():
    c = FakeSlack([photo(at(2026, 10, 27, 6, 20))])
    acts = run(c, at(2026, 10, 27, 6, 35))
    assert not any(a[0] == "alert" for a in acts)


def test_alert_not_repeated():
    c = FakeSlack()
    run(c, at(2026, 10, 27, 6, 35))
    run(c, at(2026, 10, 27, 6, 45))
    assert len([p for p in c.posted if "No start-of-shift" in p]) == 1


def test_overnight_handoff_checked_next_morning():
    # Night shift 18:00 -> 04:30; handoff due 04:45 the next day
    c = FakeSlack([photo(at(2026, 10, 28, 4, 20))])
    acts = run(c, at(2026, 10, 28, 4, 50))
    assert not any(a == ("alert", "handoff_check", "2026-10-27-Night") for a in acts)
    c2 = FakeSlack()
    acts2 = run(c2, at(2026, 10, 28, 4, 50))
    assert ("alert", "handoff_check", "2026-10-27-Night") in acts2


def test_bot_messages_do_not_count_as_photos():
    c = FakeSlack([{"ts": str(at(2026, 10, 27, 6, 10).timestamp()), "bot_id": "B9", "files": [{}]}])
    acts = run(c, at(2026, 10, 27, 6, 35))
    assert ("alert", "start_check", "2026-10-27-Day") in acts


def test_photo_in_window_gets_one_check():
    p = photo(at(2026, 10, 27, 6, 10))
    c = FakeSlack([p])
    run(c, at(2026, 10, 27, 6, 12))   # window still open: confirm right away
    run(c, at(2026, 10, 27, 6, 22))
    run(c, at(2026, 10, 27, 6, 35))
    assert c.reactions == [p["ts"]]


def test_late_photo_gets_no_check():
    p = photo(at(2026, 10, 27, 7, 30))   # Day start window closed at 6:30
    c = FakeSlack([p])
    run(c, at(2026, 10, 27, 7, 35))
    assert c.reactions == []
