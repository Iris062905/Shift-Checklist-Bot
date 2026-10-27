"""
Aspire X Dock - Shift Checklist Bot

Run this script every 10 minutes (GitHub Actions, cron, or Task Scheduler).
Each run it:
  1. Posts the start-of-shift and end-of-shift reminders when they are due.
  2. Checks #shift-checklists for a photo posted inside each checklist window.
  3. Posts an alert (tagging the people in config.yaml) if no photo was posted.

It keeps no database: it marks its own messages with invisible Slack
metadata and reads them back, so it never posts the same reminder or
alert twice.
"""

import os
import sys
import datetime as dt
from zoneinfo import ZoneInfo

import yaml
from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError

EVENT_TYPE = "shift_checklist_bot"
CONFIG_PATH = os.environ.get("CHECKLIST_CONFIG", os.path.join(os.path.dirname(__file__), "config.yaml"))


def load_config(path=CONFIG_PATH):
    with open(path) as f:
        return yaml.safe_load(f)


def parse_hhmm(value):
    h, m = str(value).split(":")
    return dt.time(int(h), int(m))


def shift_times(shift, day, tz):
    """Return (start, end) datetimes for a shift that starts on `day`. Handles overnight shifts."""
    start = dt.datetime.combine(day, parse_hhmm(shift["start"]), tzinfo=tz)
    end = dt.datetime.combine(day, parse_hhmm(shift["end"]), tzinfo=tz)
    if end <= start:
        end += dt.timedelta(days=1)
    return start, end


def build_events(cfg, now):
    """All reminder and check events for shifts that started yesterday or today."""
    tz = ZoneInfo(cfg["timezone"])
    t = cfg["timing"]
    events = []
    today = now.astimezone(tz).date()
    for day in (today - dt.timedelta(days=1), today):
        if day.weekday() not in cfg.get("work_days", [0, 1, 2, 3, 4, 5, 6]):
            continue
        for shift in cfg["shifts"]:
            start, end = shift_times(shift, day, tz)
            name = shift["name"]
            key = f"{day.isoformat()}-{name}"
            start_due = start + dt.timedelta(minutes=t["start_checklist_due_after_start"])
            handoff_open = end - dt.timedelta(minutes=t["handoff_reminder_before_end"])
            handoff_due = end + dt.timedelta(minutes=t["handoff_due_after_end"])
            events += [
                dict(kind="start_remind", key=key, at=start, shift=name, due=start_due),
                dict(kind="start_check", key=key, at=start_due, shift=name,
                     window=(start, start_due), label="start-of-shift checklist"),
                dict(kind="handoff_remind", key=key, at=handoff_open, shift=name, due=handoff_due),
                dict(kind="handoff_check", key=key, at=handoff_due, shift=name,
                     window=(handoff_open, handoff_due), label="End-of-Shift Handoff"),
            ]
    return events


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


def is_checklist_photo(msg):
    """A file (photo or PDF scan) posted by a person, not a bot."""
    if msg.get("bot_id") or msg.get("subtype") == "bot_message":
        return False
    return bool(msg.get("files"))


def already_posted(msgs, kind, key):
    for m in msgs:
        meta = m.get("metadata") or {}
        if meta.get("event_type") == EVENT_TYPE:
            p = meta.get("event_payload") or {}
            if p.get("kind") == kind and p.get("key") == key:
                return True
    return False


def fmt_time(d):
    return d.strftime("%I:%M %p").lstrip("0")


def mentions(cfg):
    return " ".join(f"<@{u}>" for u in cfg.get("alert_user_ids", []))


def message_for(ev, cfg):
    m = cfg["messages"]
    if ev["kind"] == "start_remind":
        return m["start_reminder"].format(shift=ev["shift"], due=fmt_time(ev["due"]))
    if ev["kind"] == "handoff_remind":
        return m["handoff_reminder"].format(shift=ev["shift"], due=fmt_time(ev["due"]))
    text = m["missing_alert"].format(shift=ev["shift"], checklist=ev["label"],
                                     due=fmt_time(ev["at"]))
    tag = mentions(cfg)
    return f"{tag} {text}".strip()


def post(client, channel, text, ev, dry_run):
    if dry_run:
        print(f"[dry-run] would post: {text}")
        return
    client.chat_postMessage(
        channel=channel, text=text,
        metadata={"event_type": EVENT_TYPE, "event_payload": {"kind": ev["kind"], "key": ev["key"]}})


def in_window(msg, window):
    return window[0].timestamp() <= float(msg["ts"]) <= window[1].timestamp()


def has_bot_reaction(msg, emoji, bot_user_id):
    for r in msg.get("reactions", []) or []:
        if r.get("name") == emoji and bot_user_id in (r.get("users") or []):
            return True
    return False


def run(client, cfg, now=None, dry_run=False):
    tz = ZoneInfo(cfg["timezone"])
    now = now or dt.datetime.now(tz)
    catch_up = dt.timedelta(minutes=cfg["timing"].get("catch_up_minutes", 60))
    channel = cfg["channel_id"]
    emoji = cfg.get("accepted_reaction", "white_check_mark")
    actions = []

    events = build_events(cfg, now)
    due = [e for e in events if e["at"] <= now < e["at"] + catch_up]
    # checklist windows that are open now, or closed recently enough to still confirm photos
    windows = [e["window"] for e in events
               if e["kind"].endswith("_check") and e["window"][0] <= now < e["window"][1] + catch_up]
    if not due and not windows:
        return actions

    starts = [e["at"] for e in due] + [w[0] for e in due if e.get("window") for w in [e["window"]]] + [w[0] for w in windows]
    history = fetch_history(client, channel, min(starts) - dt.timedelta(minutes=5), now)

    # 1. Confirm accepted photos with a reaction
    if emoji and windows:
        bot_user_id = None
        for m in history:
            if not is_checklist_photo(m) or not any(in_window(m, w) for w in windows):
                continue
            if bot_user_id is None:
                bot_user_id = "dry-run" if dry_run else client.auth_test()["user_id"]
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

    # 2. Reminders and missing-photo alerts
    for ev in due:
        if already_posted(history, ev["kind"], ev["key"]):
            continue
        if ev["kind"].endswith("_remind"):
            post(client, channel, message_for(ev, cfg), ev, dry_run)
            actions.append(("remind", ev["kind"], ev["key"]))
            continue
        found = any(is_checklist_photo(m) and in_window(m, ev["window"]) for m in history)
        if not found:
            post(client, channel, message_for(ev, cfg), ev, dry_run)
            actions.append(("alert", ev["kind"], ev["key"]))
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
        upcoming = sorted((e for e in build_events(cfg, now) if e["at"] > now), key=lambda e: e["at"])
        if upcoming:
            e = upcoming[0]
            print(f"Next: {e['kind']} for {e['shift']} shift at {e['at']:%Y-%m-%d %I:%M %p}")


if __name__ == "__main__":
    main()
