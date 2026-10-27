import datetime as dt
from zoneinfo import ZoneInfo

import checker

TZ = ZoneInfo("America/New_York")

# Fixed test settings (back-to-back shifts, like a real facility), independent of config.yaml.
CFG = {
    "channel_id": "CTEST",
    "facility_name": "TEST",
    "timezone": "America/New_York",
    "shifts": [{"name": "Day", "start": "06:00", "end": "15:00"},
               {"name": "Night", "start": "15:00", "end": "23:59"}],
    "work_days": [0, 1, 2, 3, 4, 5, 6],
    "alert_user_ids": ["USUP"],
    "escalation": {"after_minutes": 30, "user_ids": ["UPM"]},
    "weekly_report": {"enabled": True, "weekday": 0, "time": "07:00"},
}


class FakeSlack:
    def __init__(self, messages=None):
        self.messages = list(messages or [])
        self.posted, self.reactions, self._n = [], [], 0

    def conversations_history(self, channel, oldest, latest, **kw):
        msgs = [m for m in self.messages if float(oldest) <= float(m["ts"]) <= float(latest)]
        return {"messages": msgs, "response_metadata": {}}

    def auth_test(self):
        return {"user_id": "UBOT"}

    def reactions_add(self, channel, timestamp, name):
        for m in self.messages:
            if m["ts"] == timestamp:
                m.setdefault("reactions", []).append({"name": name, "users": ["UBOT"]})
        self.reactions.append((timestamp, name))

    def chat_postMessage(self, channel, text, metadata):
        self._n += 1
        self.posted.append(text)
        self.messages.append({"ts": f"{self._now.timestamp() + self._n / 1000:.6f}", "bot_id": "B1",
                              "text": text, "metadata": metadata})


def at(y, mo, d, h, mi):
    return dt.datetime(y, mo, d, h, mi, tzinfo=TZ)


def photo(when, text="", user="USUP"):
    return {"ts": f"{when.timestamp():.6f}", "user": user, "text": text, "files": [{"mimetype": "image/jpeg"}]}


def run(c, now):
    c._now = now
    return checker.run(c, CFG, now=now)


def posted(c, fragment):
    return [p for p in c.posted if fragment in p]


# ---- reminders and alerts

def test_reminder_posts_once_and_names_keyword():
    c = FakeSlack()
    run(c, at(2026, 10, 27, 6, 2))
    run(c, at(2026, 10, 27, 6, 12))
    r = posted(c, "Day shift has started")
    assert len(r) == 1 and "*start*" in r[0]


def test_photo_with_start_caption_counts_and_gets_check():
    p = photo(at(2026, 10, 27, 6, 10), "Start of shift checklist")
    c = FakeSlack([p])
    run(c, at(2026, 10, 27, 6, 12))
    acts = run(c, at(2026, 10, 27, 6, 35))
    assert (p["ts"], "white_check_mark") in c.reactions
    assert not any(a[0] == "alert" for a in acts)


def test_photo_without_caption_does_not_count_and_gets_one_hint():
    p = photo(at(2026, 10, 27, 6, 10), "")
    c = FakeSlack([p])
    run(c, at(2026, 10, 27, 6, 12))
    run(c, at(2026, 10, 27, 6, 20))
    acts = run(c, at(2026, 10, 27, 6, 35))
    assert len(posted(c, "can't tell which checklist")) == 1
    assert ("alert", "start_check", "2026-10-27-Day") in acts
    assert c.reactions == []


def test_shift_change_overlap_counts_photo_for_one_checklist_only():
    # 3:05 PM sits inside both the Day handoff window and the Night start window.
    p = photo(at(2026, 10, 27, 15, 5), "start")
    c = FakeSlack([p])
    acts = run(c, at(2026, 10, 27, 15, 35))
    assert ("alert", "handoff_check", "2026-10-27-Day") in acts
    assert ("alert", "start_check", "2026-10-27-Night") not in acts


def test_both_photos_at_shift_change_clear_both_checks():
    c = FakeSlack([photo(at(2026, 10, 27, 15, 2), "handoff"),
                   photo(at(2026, 10, 27, 15, 6), "start", user="UNIGHT")])
    acts = run(c, at(2026, 10, 27, 15, 35))
    assert not any(a[0] == "alert" for a in acts)


def test_bot_files_never_count():
    c = FakeSlack([{"ts": f"{at(2026, 10, 27, 6, 10).timestamp():.6f}", "bot_id": "B9",
                    "text": "start", "files": [{}]}])
    acts = run(c, at(2026, 10, 27, 6, 35))
    assert ("alert", "start_check", "2026-10-27-Day") in acts


def test_overnight_handoff():
    c = FakeSlack([photo(at(2026, 10, 27, 23, 50), "handoff")])
    acts = run(c, at(2026, 10, 28, 0, 20))
    assert ("alert", "handoff_check", "2026-10-27-Night") not in acts
    acts2 = run(FakeSlack(), at(2026, 10, 28, 0, 20))
    assert ("alert", "handoff_check", "2026-10-27-Night") in acts2


# ---- late photos and escalation

def test_escalates_once_when_still_missing():
    c = FakeSlack()
    run(c, at(2026, 10, 27, 6, 35))
    acts = run(c, at(2026, 10, 27, 7, 2))
    run(c, at(2026, 10, 27, 7, 12))
    assert ("escalate", "start_escalate", "2026-10-27-Day") in acts
    esc = posted(c, "Escalation")
    assert len(esc) == 1 and "<@UPM>" in esc[0]


def test_late_photo_gets_alarm_clock_and_stops_escalation():
    p = photo(at(2026, 10, 27, 6, 45), "start")
    c = FakeSlack([p])
    acts = run(c, at(2026, 10, 27, 7, 2))
    assert (p["ts"], "alarm_clock") in c.reactions
    assert not any(a[0] == "escalate" for a in acts)


# ---- weekly report

def test_weekly_report_counts_on_time_late_missed():
    msgs = []
    # Week of Mon Oct 19 - Sun Oct 25, 2026
    for d in range(19, 26):
        msgs.append(photo(at(2026, 10, d, 6, 10), "start"))
        msgs.append(photo(at(2026, 10, d, 14, 50), "handoff"))
        msgs.append(photo(at(2026, 10, d, 15, 10), "start", user="UNIGHT"))
        if d != 21:  # Wed Night handoff missed
            msgs.append(photo(at(2026, 10, d, 23, 45), "handoff", user="UNIGHT"))
    msgs[0]["ts"] = f"{at(2026, 10, 19, 6, 50).timestamp():.6f}"  # Mon Day start posted late
    c = FakeSlack(msgs)
    acts = run(c, at(2026, 10, 26, 7, 3))
    assert ("report", "weekly_report", "week-2026-10-19") in acts
    rep = posted(c, "Weekly checklist report")[0]
    assert "13/14 on time (93%) · 1 late · 0 missed" in rep
    assert "13/14 on time (93%) · 0 late · 1 missed" in rep
    assert "Wed Oct 21 Night handoff" in rep
    run(c, at(2026, 10, 26, 7, 13))
    assert len(posted(c, "Weekly checklist report")) == 1
