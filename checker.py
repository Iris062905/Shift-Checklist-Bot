"""
Aspire X Dock - Shift Checklist Bot

Run every 5-10 minutes. Each run it:
  1. Posts the start-of-shift and end-of-shift reminders when they are due.
  2. Counts a photo only when its message includes the right word:
     "start" for the Start-of-Shift Checklist, "handoff" for the End-of-Shift Handoff.
     On-time photos get a check mark, late photos get an alarm clock, and photos with
     no keyword get a note asking for one.
  3. Alerts when a checklist is missing at its due time.
  4. Escalates to the people in `escalation.user_ids` if it is still missing later.
  5. Posts a weekly completion report.

It keeps no database: it marks its own messages with invisible Slack metadata and
reads them back, so it never posts the same message twice.
"""

import os
import re
import sys
import datetime as dt
from zoneinfo import ZoneInfo

import yaml
from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError

EVENT_TYPE = "shift_checklist_bot"
CONFIG_PATH = os.environ.get("CHECKLIST_CONFIG", os.path.join(os.path.dirname(__file__), "config.yaml"))

KINDS = ("start", "handoff")
LABELS = {"start": "Start-of-Shift Checklist", "handoff": "End-of-Shift Handoff"}
SHORT = {"start": "start", "handoff": "handoff"}

DEFAULT_KEYWORDS = {"start": ["start"], "handoff": ["handoff", "hand off"]}
DEFAULT_TIMING = {
    "start_checklist_due_after_start": 30,
    "handoff_reminder_before_end": 30,
    "handoff_due_after_end": 15,
    "late_grace_minutes": 120,
    "catch_up_minutes": 60,
}
DEFAULT_MESSAGES = {
    "start_reminder": ":clipboard: *{shift} shift has started.* Post a photo of the completed "
                      "Start-of-Shift Checklist by *{due}* and type *{keyword}* in the message.",
    "handoff_reminder": ":arrows_counterclockwise: *{shift} shift ends soon.* Post a photo of the signed "
                        "End-of-Shift Handoff by *{due}* and type *{keyword}* in the message.",
    "missing_alert": ":warning: No {checklist} photo was posted for the *{shift}* shift by {due}. "
                     "Please post it now with *{keyword}* in the message.",
    "escalation": ":rotating_light: *Escalation:* the {checklist} for the *{shift}* shift on {date} "
                  "is still missing, {minutes} minutes past due.",
    "caption_hint": ":grey_question: I can't tell which checklist this photo is for. Edit the message "
                    "and add *start* or *handoff* so it counts.",
}


# ---------------------------------------------------------------- config helpers

def load_config(path=CONFIG_PATH):
    with open(path) as f:
        return yaml.safe_load(f)


def timing(cfg):
    t = dict(DEFAULT_TIMING)
    t.update(cfg.get("timing") or {})
    return t


def messages(cfg):
    m = dict(DEFAULT_MESSAGES)
    m.update(cfg.get("messages") or {})
    return m


def keywords(cfg):
    k = dict(DEFAULT_KEYWORDS)
    k.update(cfg.get("keywords") or {})
    return k


def reactions_cfg(cfg):
    r = {"on_time": cfg.get("accepted_reaction", "white_check_mark"), "late": "alarm_clock"}
    r.update(cfg.get("reactions") or {})
    return r


def escalation_cfg(cfg):
    e = {"after_minutes": 30, "user_ids": []}
    e.update(cfg.get("escalation") or {})
    return e


def report_cfg(cfg):
    r = {"enabled": True, "weekday": 0, "time": "07:00", "user_ids": None}
    r.update(cfg.get("weekly_report") or {})
    if not r["user_ids"]:
        r["user_ids"] = cfg.get("alert_user_ids", [])
    return r


def parse_hhmm(value):
    h, m = str(value).split(":")
    return dt.time(int(h), int(m))


def mentions(ids):
    return " ".join(f"<@{u}>" for u in ids or [])


def fmt_time(d):
    return d.strftime("%I:%M %p").lstrip("0")


def fmt_day(d):
    return f"{d:%a} {d:%b} {d.day}"


# ---------------------------------------------------------------- shifts & events

def shift_times(shift, day, tz):
    """(start, end) for a shift starting on `day`. Handles shifts that end the next morning."""
    start = dt.datetime.combine(day, parse_hhmm(shift["start"]), tzinfo=tz)
    end = dt.datetime.combine(day, parse_hhmm(shift["end"]), tzinfo=tz)
    if end <= start:
        end += dt.timedelta(days=1)
    return start, end


def shift_instances(cfg, first_day, last_day):
    tz = ZoneInfo(cfg["timezone"])
    t = timing(cfg)
    work_days = cfg.get("work_days", [0, 1, 2, 3, 4, 5, 6])
    out, day = [], first_day
    while day <= last_day:
        if day.weekday() in work_days:
            for s in cfg["shifts"]:
                start, end = shift_times(s, day, tz)
                out.append(dict(
                    key=f"{day.isoformat()}-{s['name']}", day=day, shift=s["name"], start=start, end=end,
                    windows={
                        "start": (start, start + dt.timedelta(minutes=t["start_checklist_due_after_start"])),
                        "handoff": (end - dt.timedelta(minutes=t["handoff_reminder_before_end"]),
                                    end + dt.timedelta(minutes=t["handoff_due_after_end"])),
                    }))
        day += dt.timedelta(days=1)
    return out


def events_for(inst, cfg):
    esc = escalation_cfg(cfg)
    evs = []
    for k in KINDS:
        ws, we = inst["windows"][k]
        evs.append(dict(kind=f"{k}_remind", check=k, at=ws, inst=inst))
        evs.append(dict(kind=f"{k}_check", check=k, at=we, inst=inst))
        if esc["user_ids"]:
            evs.append(dict(kind=f"{k}_escalate", check=k, at=we + dt.timedelta(minutes=esc["after_minutes"]),
                            inst=inst))
    return evs


# ---------------------------------------------------------------- Slack history helpers

def fetch_history(client, channel, oldest, latest):
    msgs, cursor = [], None
    while True:
        resp = client.conversations_history(
            channel=channel, oldest=f"{oldest.timestamp():.6f}", latest=f"{latest.timestamp():.6f}",
            inclusive=True, limit=200, include_all_metadata=True, cursor=cursor)
        msgs += resp.get("messages", [])
        cursor = (resp.get("response_metadata") or {}).get("next_cursor")
        if not cursor:
            return msgs


def is_person_file(msg):
    """A file (photo or PDF scan) posted by a person, not a bot."""
    if msg.get("bot_id") or msg.get("subtype") == "bot_message":
        return False
    return bool(msg.get("files"))


def caption_kinds(msg, cfg):
    """Which checklist(s) the message text names: {'start'}, {'handoff'}, or empty."""
    text = (msg.get("text") or "").lower()
    found = set()
    for kind, words in keywords(cfg).items():
        if any(re.search(r"\b" + re.escape(w.lower()) + r"\b", text) for w in words):
            found.add(kind)
    return found


def already_posted(msgs, kind, key):
    for m in msgs:
        meta = m.get("metadata") or {}
        if meta.get("event_type") == EVENT_TYPE:
            p = meta.get("event_payload") or {}
            if p.get("kind") == kind and p.get("key") == key:
                return True
    return False


def has_bot_reaction(msg, emoji, bot_user_id):
    return any(r.get("name") == emoji and bot_user_id in (r.get("users") or [])
               for r in msg.get("reactions", []) or [])


def status(history, inst, kind, cfg, until):
    """'on_time', 'late' or 'missing' for one checklist of one shift, as of `until`."""
    ws, we = inst["windows"][kind]
    grace_end = min(until, we + dt.timedelta(minutes=timing(cfg)["late_grace_minutes"]))
    photos = [m for m in history if is_person_file(m) and kind in caption_kinds(m, cfg)]
    if any(ws.timestamp() <= float(m["ts"]) <= we.timestamp() for m in photos):
        return "on_time"
    if any(we.timestamp() < float(m["ts"]) <= grace_end.timestamp() for m in photos):
        return "late"
    return "missing"


def post(client, channel, text, kind, key, dry_run):
    if dry_run:
        print(f"[dry-run] would post: {text}")
        return
    client.chat_postMessage(channel=channel, text=text,
                            metadata={"event_type": EVENT_TYPE, "event_payload": {"kind": kind, "key": key}})


# ---------------------------------------------------------------- weekly report

def report_due(cfg, now):
    r = report_cfg(cfg)
    if not r["enabled"]:
        return None
    tz = ZoneInfo(cfg["timezone"])
    local = now.astimezone(tz)
    send_at = dt.datetime.combine(local.date(), parse_hhmm(r["time"]), tzinfo=tz)
    catch_up = dt.timedelta(minutes=timing(cfg)["catch_up_minutes"])
    if local.weekday() != r["weekday"] or not (send_at <= now < send_at + catch_up):
        return None
    first = local.date() - dt.timedelta(days=7)
    last = local.date() - dt.timedelta(days=1)
    return dict(key=f"week-{first.isoformat()}", first=first, last=last,
                oldest=dt.datetime.combine(first, dt.time(0, 0), tzinfo=tz))


def build_report(cfg, history, rep, now):
    counts = {k: {"on_time": 0, "late": 0, "missing": 0} for k in KINDS}
    by_shift, missed = {}, []
    for inst in shift_instances(cfg, rep["first"], rep["last"]):
        for k in KINDS:
            st = status(history, inst, k, cfg, now)
            counts[k][st] += 1
            row = by_shift.setdefault(inst["shift"], {kk: [0, 0] for kk in KINDS})
            row[k][1] += 1
            if st == "on_time":
                row[k][0] += 1
            if st == "missing":
                missed.append(f"{fmt_day(inst['day'])} {inst['shift']} {SHORT[k]}")
    facility = cfg.get("facility_name", "")
    title = f":bar_chart: *Weekly checklist report{' - ' + facility if facility else ''}: " \
            f"{rep['first']:%b} {rep['first'].day} to {rep['last']:%b} {rep['last'].day}*"
    lines = [f"{mentions(report_cfg(cfg)['user_ids'])} {title}".strip()]
    for k in KINDS:
        c = counts[k]
        total = sum(c.values())
        pct = round(100 * c["on_time"] / total) if total else 0
        lines.append(f"*{LABELS[k]}:* {c['on_time']}/{total} on time ({pct}%) · {c['late']} late · {c['missing']} missed")
    if by_shift:
        lines.append("By shift: " + " · ".join(
            f"{s} start {v['start'][0]}/{v['start'][1]}, handoff {v['handoff'][0]}/{v['handoff'][1]}"
            for s, v in by_shift.items()))
    if missed:
        more = f" (+{len(missed) - 15} more)" if len(missed) > 15 else ""
        lines.append("Missed: " + "; ".join(missed[:15]) + more)
    return "\n".join(lines)


# ---------------------------------------------------------------- main loop

def run(client, cfg, now=None, dry_run=False):
    tz = ZoneInfo(cfg["timezone"])
    now = now or dt.datetime.now(tz)
    t = timing(cfg)
    catch_up = dt.timedelta(minutes=t["catch_up_minutes"])
    grace = dt.timedelta(minutes=t["late_grace_minutes"])
    channel = cfg["channel_id"]
    msg = messages(cfg)
    kw = keywords(cfg)
    react = reactions_cfg(cfg)
    actions = []

    today = now.astimezone(tz).date()
    insts = shift_instances(cfg, today - dt.timedelta(days=2), today)
    due = [e for i in insts for e in events_for(i, cfg) if e["at"] <= now < e["at"] + catch_up]
    active = [(i, k) for i in insts for k in KINDS
              if i["windows"][k][0] <= now < i["windows"][k][1] + grace + catch_up]
    rep = report_due(cfg, now)
    if not due and not active and not rep:
        return actions

    starts = [e["at"] for e in due] + [e["inst"]["windows"][e["check"]][0] for e in due]
    starts += [i["windows"][k][0] for i, k in active]
    if rep:
        starts.append(rep["oldest"])
    history = fetch_history(client, channel, min(starts) - dt.timedelta(minutes=5), now)

    # 1. React to photos in open windows, and ask for a keyword when one is missing
    bot_user_id, reacted = None, set()
    for m in history:
        if not is_person_file(m):
            continue
        ts = float(m["ts"])
        named = caption_kinds(m, cfg)
        in_window = False
        for inst, k in active:
            ws, we = inst["windows"][k]
            if not (ws.timestamp() <= ts <= (we + grace).timestamp()):
                continue
            in_window = True
            if k not in named:
                continue
            emoji = react["on_time"] if ts <= we.timestamp() else react["late"]
            if not emoji or (m["ts"], emoji) in reacted:
                continue
            if bot_user_id is None:
                bot_user_id = "dry-run" if dry_run else client.auth_test()["user_id"]
            reacted.add((m["ts"], emoji))
            if has_bot_reaction(m, emoji, bot_user_id):
                continue
            if dry_run:
                print(f"[dry-run] would react :{emoji}: to photo at {m['ts']}")
            else:
                try:
                    client.reactions_add(channel=channel, timestamp=m["ts"], name=emoji)
                except SlackApiError as e:
                    if e.response.get("error") != "already_reacted":
                        raise
            actions.append(("react", emoji, m["ts"]))
        if in_window and not named and not already_posted(history, "caption_hint", m["ts"]):
            text = f"{mentions([m.get('user')] if m.get('user') else [])} {msg['caption_hint']}".strip()
            post(client, channel, text, "caption_hint", m["ts"], dry_run)
            actions.append(("hint", "caption_hint", m["ts"]))

    # 2. Reminders, alerts and escalations
    for e in due:
        inst, k, key = e["inst"], e["check"], e["inst"]["key"]
        if already_posted(history, e["kind"], key):
            continue
        ws, we = inst["windows"][k]
        fields = dict(shift=inst["shift"], checklist=LABELS[k], keyword=kw[k][0],
                      due=fmt_time(we), date=fmt_day(inst["day"]))
        if e["kind"].endswith("_remind"):
            post(client, channel, msg[f"{k}_reminder"].format(**fields), e["kind"], key, dry_run)
            actions.append(("remind", e["kind"], key))
        elif e["kind"].endswith("_check"):
            if status(history, inst, k, cfg, now) == "missing":
                text = f"{mentions(cfg.get('alert_user_ids'))} {msg['missing_alert'].format(**fields)}".strip()
                post(client, channel, text, e["kind"], key, dry_run)
                actions.append(("alert", e["kind"], key))
        elif e["kind"].endswith("_escalate"):
            if status(history, inst, k, cfg, now) == "missing":
                esc = escalation_cfg(cfg)
                fields["minutes"] = esc["after_minutes"]
                tags = mentions(list(dict.fromkeys(esc["user_ids"] + (cfg.get("alert_user_ids") or []))))
                post(client, channel, f"{tags} {msg['escalation'].format(**fields)}".strip(),
                     e["kind"], key, dry_run)
                actions.append(("escalate", e["kind"], key))

    # 3. Weekly report
    if rep and not already_posted(history, "weekly_report", rep["key"]):
        post(client, channel, build_report(cfg, history, rep, now), "weekly_report", rep["key"], dry_run)
        actions.append(("report", "weekly_report", rep["key"]))
    return actions


def main():
    token = os.environ.get("SLACK_BOT_TOKEN")
    if not token:
        sys.exit("SLACK_BOT_TOKEN is not set.")
    dry_run = "--dry-run" in sys.argv
    cfg = load_config()
    try:
        actions = run(WebClient(token=token), cfg, dry_run=dry_run)
    except SlackApiError as e:
        r = e.response
        msg = f"Slack error: {r.get('error')}"
        if r.get("needed"):
            msg += f" (needed scope: {r.get('needed')}; token has: {r.get('provided')})"
        sys.exit(msg)
    for a in actions:
        print("done:", *a)
    if not actions:
        tz = ZoneInfo(cfg["timezone"])
        now = dt.datetime.now(tz)
        print(f"Nothing due. Building time now: {now:%Y-%m-%d %I:%M %p %Z}")
        upcoming = sorted((e for i in shift_instances(cfg, now.date(), now.date() + dt.timedelta(days=1))
                           for e in events_for(i, cfg) if e["at"] > now), key=lambda e: e["at"])
        if upcoming:
            e = upcoming[0]
            print(f"Next: {e['kind']} for {e['inst']['shift']} shift at {e['at']:%Y-%m-%d %I:%M %p}")


if __name__ == "__main__":
    main()
