# QA bot (GitHub Actions version)

Automated QA tester for the a website live-agent chat. It runs on GitHub's machines on a schedule and is controlled
from a Google Sheet (run window, parallel runs, agent IDs and survey quotas). Live counters and the daily report
are in the same Sheet.

Setup: **GUIDE_GITHUB_SETUP.md** (start with `google_sheet/SETUP_GOOGLE_SHEET.md`).
Test cases: `test_cases/` (copy `_template.json`). For authorized QA testing only.
