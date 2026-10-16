# QA bot on GitHub Actions - setup guide

Everything runs on GitHub's machines on a schedule. You control it from the Google Sheet on your phone and read
the results there. Nothing has to stay on at your end.

## How it works
1. Every 15 minutes GitHub starts a small **gate** job. It reads your Config tab and asks the Sheet's counter
   how many surveys are still open. If `enabled` is yes, the time is inside your window, and some agent still
   has surveys left, it starts **parallel_runs** worker jobs (1-10). Otherwise it stops (about 20 seconds).
2. Each **worker** is a separate GitHub machine with its own browser. It loops: pick a test case (random by
   default) > fresh browser profile > chat > check the agent's ID against the Sheet > full script + survey only
   for target agents > write the result to the Sheet > `cooldown_seconds` > next chat.
3. Before the full chat, the worker asks the Sheet to **reserve** a slot for that agent. The Sheet's script
   takes a lock, so two workers can never both take an agent's last survey. A chat in progress counts against
   the quota, and the slot is released or counted when the chat ends.
4. Workers stop by themselves when the window ends, when every quota is reached, or when you set
   `enabled` to `no` (each one finishes the chat it's in). A new scheduled run then sees the window closed and
   does nothing until tomorrow.
5. Results appear live in the Sheet: **Log** (every contact), **State** (surveys per agent), **Report**
   (per-agent day summary).

## What you need
- A GitHub account (free).
- The Google Sheet set up with `google_sheet/SETUP_GOOGLE_SHEET.md`. From it you have three values:
  the **published Config link**, the **web app URL** and the **TOKEN**.

## Step 1 - Create the repository
1. github.com > **+** (top right) > **New repository**.
2. Name it, e.g. `qa-bot`. Choose **Public** (Actions minutes are free for public repos). Create.
3. On the empty repo page click **uploading an existing file**. Drag in the *contents* of this folder (not
   the folder itself): `qa_bot.py`, `bot_settings.json`, `requirements.txt`, `test_cases/`, `google_sheet/`,
   the guides, and `.gitignore`.
4. **The `.github` folder:** the browser uploader often skips folders that start with a dot. If after
   committing you don't see `.github/workflows/qa-bot.yml` in the repo, create it by hand: **Add file > Create
   new file**, type the name `.github/workflows/qa-bot.yml` (the slashes create the folders), paste the
   content of that file, **Commit**.

## Step 2 - Add the three secrets
Repo > **Settings > Secrets and variables > Actions > New repository secret**. Add:

| Name | Value |
|---|---|
| `QA_SHEET_CSV_URL` | The published Config link (Publish to web > Config tab > CSV) |
| `QA_COUNTER_URL` | The Apps Script web app URL (ends with `/exec`) |
| `QA_COUNTER_TOKEN` | The TOKEN from the setup() log |

They go here and not in `bot_settings.json` because the repo is public: anyone can read its files, but nobody can
read secrets. Your agent list stays private this way.

## Step 3 - Turn it on and test
1. Repo > **Actions** tab. If asked, click **I understand my workflows, go ahead and enable them**.
2. Click **QA bot** (left) > **Run workflow** > Run. This is a manual run - it still respects the Sheet.
3. Open the run > **gate** job > the step `Run python qa_bot.py --gate-output` shows the decision, e.g.
   `[gate] outside the run window: Mon 14:05 America/Chicago (window 7:00 PM-11:00 PM)`.
4. For a first real test inside the window: set `parallel_runs` to `1` and one agent ID in Config, then run the
   workflow manually during the window. Watch **Worker 1**'s log and the Sheet's Log tab.

From then on it runs by itself every day in the window. Scheduled runs on GitHub can start 5-20 minutes late
when GitHub is busy, so the first chats may begin a little after `start_time`.

## Day-to-day control (phone)
In the Google Sheets app, Config tab:
- **Stop now:** `enabled` = `no` (workers finish their current chat, then exit).
- **More or fewer parallel runs:** change `parallel_runs`. It applies to the next gate run. Lowering it also
  stops extra running workers after their current chat; raising it mid-window adds workers only when the next
  scheduled run starts (after the current one ends), so set it before the window when you can.
- **Agents and quotas:** add or edit `agent` rows. Raising a number gives that agent more surveys.
- **Window:** change `start_time` / `end_time`. The workflow never needs editing.
- **Test cases:** `test_case_mode` = `random` / `sequential` / `fixed` (+ `test_case_file`).

Edits reach the bot within about 5 minutes (Google's publish delay).

## Adding test cases
Repo > `test_cases` folder > **Add file > Create new file** > name it e.g. `test_case_2.json` > paste the
content of `_template.json` and fill it in > Commit. Every `.json` file in the folder not starting with `_`
is used. A file with a mistake is skipped and the reason is shown in the logs.

## Privacy in a public repo
- The Actions logs of a public repo are public. In GitHub mode the bot replaces every agent name and ID with
  `[agent]` in what it prints, and also registers the IDs with GitHub's own log masking.
- Screenshots and chat transcripts stay on the GitHub machine and are deleted with it. Nothing is uploaded.
- Results with real IDs go only to your private Sheet.
- Test case files are public. Keep real customer details out of them.

## Costs and limits
- Public repo: GitHub-hosted runner minutes are free.
- Each job can run up to 6 hours. The workflow stops workers at 350 minutes, so a window longer than about
  5h 50m is continued by the next scheduled run.
- The free plan allows 20 jobs at once (the bot uses at most 11: 1 gate + 10 workers).
- GitHub turns off scheduled workflows in a public repo after **60 days without any commit**. If that
  happens, you get an email and the Actions tab shows a button to re-enable it. Any small commit also resets the
  timer.

## Things to know
- GitHub's machines are in Microsoft datacenters. AT&T's bot protection may treat datacenter traffic
  differently from a normal home or office connection. If chats fail at "click chat icon" or the widget doesn't
  load, that's the likely reason, and the local PC version is the fallback.
- Each worker reads the Sheet before every chat. If the Sheet or the counter is unreachable, the worker waits
  and retries. It never runs a full chat without a confirmed reservation.
- A worker stops after 3 failed chats in a row so a broken page doesn't keep occupying agents. Check its log.
- This is for authorized QA testing: every chat occupies a real agent, and every survey is a real CSAT record.

## Files
| File | Purpose |
|---|---|
| `.github/workflows/qa-bot.yml` | The schedule and jobs (gate + parallel workers) |
| `qa_bot.py` | The bot |
| `bot_settings.json` | Start page and test-case defaults (no secrets) |
| `test_cases/` | Test cases; `_template.json` shows every field |
| `google_sheet/` | `Code.gs` (Sheet script), setup guide, Config template |
| `requirements.txt` | Python packages |

## Behaviour notes (v2)
- **Taking over:** (local version only - GitHub runs have no keyboard) press Enter in a worker window to pause, type to the agent yourself, press Enter again to resume.
  The bot answers only an agent message that is newer than the last message sent by you or the bot, so it never
  answers a message you already answered; it continues with its next scripted line after the agent's next reply.
  (It also notices if you type without pausing.)
- **Non-target agents:** the agent's ID arrives with the "agent joined" event, so the bot checks it right away and
  leaves a non-target agent before they even send a greeting.
- **Same browser:** with `reuse_browser` = `skips` (default), after leaving a non-target agent the bot starts the
  next chat in the same browser window (reloading the page if the chat button isn't showing). A back-off, a failed
  chat or the end of the window closes it; the next browser gets a fresh profile.
- **"Do you need anything else?" questions:** if the site shows a Yes/No question before the menu or before an agent
  connects, the bot clicks **No**. If no question appears, nothing changes. Set `"auto_answer_buttons"` in a test
  case to change this (`[]` turns it off).
