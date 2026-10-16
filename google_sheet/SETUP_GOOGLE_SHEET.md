# Google Sheet setup (control panel + live counters + live report)

You do this once. Afterwards you control everything from the Google Sheets app on your phone.

The Sheet ends up with five tabs:

| Tab | What it is | Who writes it |
|---|---|---|
| **Config** | Run window, number of parallel runs, agent IDs and survey quotas, test-case mode | **You** (phone or PC) |
| **State** | Surveys done per agent (the live counters) | The bot |
| **Reservations** | Chats in progress right now (so parallel runs never over-assign an agent) | The bot |
| **Log** | One row per chat: time, agent, outcome, messages, survey, test case | The bot |
| **Report** | Per-agent summary for a day: contacted, chats, surveys given, chats with no survey | Formulas (updates live) |

The bot connects to the Sheet in two ways:
- **Published Config link** (`sheet_csv_url`): the bot reads the Config tab as CSV before every chat.
- **Apps Script web app** (`counter_url` + `counter_token`): the bot reserves quota slots and writes results.
  Every call takes a lock, so 10 parallel runs on different machines can't double-count.

## Step 1 - Create the Sheet
1. Go to sheets.google.com and create a blank spreadsheet, e.g. "QA bot control".
2. File > Settings > Locale: **United States** (the formulas use commas). Time zone doesn't matter
   (the bot uses the `timezone` row in Config).

## Step 2 - Add the script
1. In the Sheet: **Extensions > Apps Script**.
2. Delete what's in the editor, paste the whole content of `google_sheet/Code.gs`, click **Save** (disk icon).
3. In the function dropdown at the top select **setup**, click **Run**.
4. Google asks for permission: Review permissions > pick your account > Advanced > "Go to (project) (unsafe)" >
   Allow. (It's your own script; "unsafe" only means Google hasn't reviewed it.)
5. Open **Execution log** (bottom). It shows: `Your TOKEN ... is: xxxxxxxx`. Copy it - this is
   `counter_token` / `QA_COUNTER_TOKEN`. (You can see or change it later under Project Settings (gear) >
   Script properties > TOKEN.)

Back in the Sheet you now have the tabs Config (pre-filled with an example), State, Reservations, Log, Report.
You can delete the empty "Sheet1" tab.

## Step 3 - Deploy the web app (the counter)
1. In Apps Script: **Deploy > New deployment**.
2. Gear next to "Select type" > **Web app**.
3. Execute as: **Me**. Who has access: **Anyone**. Click **Deploy**.
4. Copy the **Web app URL** (ends with `/exec`). This is `counter_url` / `QA_COUNTER_URL`.
5. Test it: open that URL in a browser. You should see `{"ok":true,"service":"qa-bot-counter",...}`.

"Anyone" is needed because the bot has no Google login. Writes are protected by the TOKEN: without it every
request is refused. Keep the URL and token private.

If you ever edit Code.gs: Deploy > Manage deployments > pencil > Version: **New version** > Deploy (same URL).

## Step 4 - Publish the Config tab (the link the bot reads)
1. In the Sheet: **File > Share > Publish to web**.
2. Under "Link": choose the **Config** tab (not "Entire document") and **Comma-separated values (.csv)**.
3. Click **Publish**, confirm, copy the link. This is `sheet_csv_url` / `QA_SHEET_CSV_URL`.
4. Leave "Automatically republish when changes are made" ticked (under "Published content & settings").

Only the Config tab is published - the Log and Report stay private. Google refreshes the published copy within
a few minutes of an edit, so a change from your phone reaches the bot within about 5 minutes.

## Step 5 - Fill in Config
One row per line, three columns: `type`, `key`, `value`. Column D ("surveys_done (live)") is a formula that
shows each agent's live count - don't type in column D.

| Row | Meaning |
|---|---|
| `setting, enabled, yes` | Master switch. Set `no` to stop new chats (running chats finish). |
| `setting, start_time, 7:00 PM` / `end_time, 11:00 PM` | Daily run window. 24h (`19:00`) also works; can cross midnight. |
| `setting, timezone, America/Chicago` | US Central (CST/CDT handled automatically). |
| `setting, parallel_runs, 2` | How many chats at the same time (max 10). |
| `setting, reply_delay_seconds, 30` | Wait after the agent's latest message before the bot replies. |
| `setting, min_messages_per_chat, 6` | Messages (bot + agent) needed to count a contact as a chat. |
| `setting, cooldown_seconds, 120` | Pause between chats, per run. |
| `setting, max_consecutive_skips, 5` / `backoff_minutes, 30` | After 5 non-target agents in a row, wait 30 min. |
| `setting, worker_stagger_seconds, 30` | Gap between starting parallel runs. |
| `setting, test_case_mode, random` | `random`, `sequential`, or `fixed`. |
| `setting, test_case_file, test_case_1.json` | Which file to use when mode is `fixed`. |
| `setting, reuse_browser, skips` | `skips` (default): after a non-target agent, the next chat starts in the same browser. `always`: keep the browser after every chat. `never`: a fresh browser every chat. A back-off, a failure or the end of the window always closes it. |
| `agent, AT3469, 3` | Agent ID and how many surveys to give it. Add one row per agent. |

Agent IDs: when typing an ID like `12E345`, put an apostrophe first (`'12E345`) so Sheets doesn't turn it into
a number.

## Step 6 - Reading the results
- **State** tab and Config column D: surveys done per agent, live.
- **Log** tab: every contact as it finishes.
- **Report** tab: per-agent table for the latest day. To see another day, type it in B1 with an apostrophe,
  e.g. `'2026-10-05`. Clear B1 to go back to the latest day. Days follow the Config time zone; a window past
  midnight stays with the day it started.
- To start counting from zero again (e.g. a new test round): Apps Script > select **resetCounters** > Run.
  It clears State and Reservations; the Log is kept. Raising a quota in Config also works without a reset.
