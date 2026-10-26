# Shift Checklist Bot

Posts the shift checklist reminders in Slack and alerts you automatically when a checklist photo is missing.

## What it does each shift

| When | What the bot does |
|---|---|
| Shift start | Posts: "Day shift has started. Post a photo of the completed Start-of-Shift Checklist by 6:30 AM." |
| 30 min after start | Checks for a photo. If none was posted, it tags the people in `config.yaml`. |
| 30 min before end | Posts: "Day shift ends soon. Post a photo of the signed End-of-Shift Handoff by 4:45 PM." |
| 15 min after end | Checks for a photo. If none was posted, it tags the people in `config.yaml`. |

Any file a person posts in the channel during the window counts (photo or PDF scan). Bot messages never count. Reminders and alerts are posted once only, even if the script runs many times. Night shifts that end the next morning are handled automatically.

## Setup (about 20 minutes, needs a Slack admin once)

### 1. Create the Slack app
1. Go to https://api.slack.com/apps and click **Create New App > From an app manifest**.
2. Pick your workspace and paste in the contents of `slack-app-manifest.yaml`.
3. Click **Install to Workspace** and approve.
4. Under **OAuth & Permissions**, copy the **Bot User OAuth Token** (starts with `xoxb-`). Keep it private.

### 2. Prepare the channel
1. Create `#shift-checklists` if it doesn't exist.
2. In the channel, type `/invite @Shift Checklist Bot`.
3. Copy the channel ID: open the channel name > scroll to the bottom of the details panel.

### 3. Fill in `config.yaml`
Set `channel_id`, `timezone`, the shift times, and `alert_user_ids` (the Process Supervisor and whoever they report to). Each facility gets its own copy of this file and its own channel.

### 4. Run it every 10 minutes
Pick one:

**Option A: GitHub Actions (no computer to keep on)**
1. Create a GitHub repository and upload all of these files, including the `.github` folder.
2. Go to **Settings > Secrets and variables > Actions > New repository secret**. Name: `SLACK_BOT_TOKEN`, value: the `xoxb-` token.
3. Go to the **Actions** tab, open "Shift checklist check", and click **Run workflow** to test it.

Cost note: every-10-minute runs use about 4,300 GitHub Actions minutes a month. That is free on a public repository, but a private repository on GitHub's free plan only includes 2,000 minutes. The token stays secret either way, because it is stored as a secret, not in the files. GitHub can also start scheduled runs several minutes late at busy times; the bot catches up on anything up to 60 minutes old.

**Option B: any always-on computer (free)**
1. Install Python 3.10+, then run `pip install -r requirements.txt` in this folder.
2. Set the `SLACK_BOT_TOKEN` environment variable to the `xoxb-` token.
3. Schedule `python checker.py` every 10 minutes:
   - Windows: Task Scheduler > Create Task > Trigger "Daily", repeat every 10 minutes for 1 day.
   - Mac/Linux: `crontab -e` and add `*/10 * * * * cd /path/to/shift-checklist-bot && SLACK_BOT_TOKEN=xoxb-... python3 checker.py`

### 5. Test it
- `python checker.py --dry-run` prints what it would post without posting anything.
- `python -m pytest` runs the built-in tests (needs `pip install pytest`).

## Changing things later
- Shift times, due times, or who gets tagged: edit `config.yaml`.
- Message wording: edit the `messages` section of `config.yaml`. Keep the `{shift}`, `{due}` and `{checklist}` placeholders.
- Days off: remove numbers from `work_days` (0 = Monday, 6 = Sunday).
