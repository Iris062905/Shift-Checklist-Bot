# Aspire X Dock - Shift Checklist Bot settings
# Each facility gets its own copy of this file. Only edit the values on the right.
 
facility_name: "XIN4"
 
# Channel ID of the facility's shift-checklists channel
channel_id: "C0CC4ST183V"
 
# Building time zone (examples: America/New_York, America/Chicago, America/Denver, America/Los_Angeles)
timezone: "America/New_York"
 
# Shift times in 24-hour format. A night shift that ends the next morning is handled automatically.
shifts:
  - name: "Day"
    start: "06:00"
    end: "15:00"
  - name: "Night"
    start: "15:00"
    end: "00:00"
 
# Days the building runs: 0=Mon 1=Tue 2=Wed 3=Thu 4=Fri 5=Sat 6=Sun
work_days: [0, 1, 2, 3, 4]
 
# Tagged on missing-checklist alerts (Process Supervisors)
alert_user_ids:
  - "U09HJNDCX36"
 
# The word a supervisor types with the photo so the bot knows which checklist it is
keywords:
  start: ["start"]
  handoff: ["handoff", "hand off"]
 
# Reactions the bot adds to counted photos. Set either to "" to turn it off.
reactions:
  on_time: "white_check_mark"
  late: "alarm_clock"
 
# If a checklist is still missing this many minutes after its alert, tag these people too.
# Leave user_ids empty to turn escalation off.
escalation:
  after_minutes: 30
  user_ids: [U09HJNDCX36]          # e.g. the Program Manager's member ID
 
# Weekly completion report. weekday: 0=Mon ... 6=Sun. Leave user_ids empty to tag alert_user_ids.
weekly_report:
  enabled: true
  weekday: 0
  time: "07:00"
  user_ids: [U09HJNDCX36]
 
timing:
  start_checklist_due_after_start: 30   # start-of-shift photo due this many minutes after shift start
  handoff_reminder_before_end: 30       # handoff reminder posts this many minutes before shift end
  handoff_due_after_end: 15             # handoff photo due this many minutes after shift end
  late_grace_minutes: 120               # photos up to this long after the due time count as "late"
  catch_up_minutes: 60                  # if a run is late, still act on events up to this old
 
