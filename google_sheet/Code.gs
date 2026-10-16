/**
 * QA bot - shared survey counters, live Log and live Report, inside your Google Sheet.
 *
 * Setup (details in SETUP_GOOGLE_SHEET.md):
 *   1. In the Sheet: Extensions > Apps Script, paste this whole file, Save.
 *   2. Select the function "setup" and press Run (allow access). It creates the tabs and prints your TOKEN
 *      in the Execution log.
 *   3. Deploy > New deployment > type "Web app" > Execute as: Me, Who has access: Anyone > Deploy.
 *      Copy the Web app URL (ends with /exec) - that is the counter_url / QA_COUNTER_URL.
 *
 * The bot calls this web app for every quota check and after every chat. Each call takes a lock, so 10
 * parallel workers on different machines can never give an agent more surveys than its quota.
 */

const TAB = { config: 'Config', state: 'State', res: 'Reservations', log: 'Log', report: 'Report' };
const RESERVATION_TTL_MS = 60 * 60 * 1000;   // a chat that never reported back frees its slot after 1 hour
const LOG_HEADERS = ['time', 'session_date', 'worker', 'agent_id', 'agent_name', 'outcome', 'chat_counted',
  'messages_total', 'messages_bot', 'messages_agent', 'survey_shown', 'survey_submitted', 'test_case', 'note'];
const CONFIG_TEMPLATE = [
  ['type', 'key', 'value'],
  ['setting', 'enabled', 'yes'],
  ['setting', 'start_time', '7:00 PM'],
  ['setting', 'end_time', '11:00 PM'],
  ['setting', 'timezone', 'America/Chicago'],
  ['setting', 'parallel_runs', '2'],
  ['setting', 'reply_delay_seconds', '30'],
  ['setting', 'min_messages_per_chat', '6'],
  ['setting', 'cooldown_seconds', '120'],
  ['setting', 'max_consecutive_skips', '5'],
  ['setting', 'backoff_minutes', '30'],
  ['setting', 'worker_stagger_seconds', '30'],
  ['setting', 'test_case_mode', 'random'],
  ['setting', 'test_case_file', ''],
  ['setting', 'reuse_browser', 'skips'],
  ['agent', 'AT3469', '3'],
];

// ------------------------------------------------------------------ web app entry points
function doGet() {
  return json_({ ok: true, service: 'qa-bot-counter', time: new Date().toISOString() });
}

function doPost(e) {
  let out;
  try {
    const req = JSON.parse((e && e.postData && e.postData.contents) || '{}');
    const token = PropertiesService.getScriptProperties().getProperty('TOKEN');
    if (!token) throw new Error('TOKEN script property is not set - run setup() once');
    if (req.token !== token) throw new Error('bad token');
    const lock = LockService.getScriptLock();
    lock.waitLock(28000);
    try {
      out = handle_(req);
      SpreadsheetApp.flush();
    } finally {
      lock.releaseLock();
    }
  } catch (err) {
    out = { ok: false, error: String((err && err.message) || err) };
  }
  return json_(out);
}

function json_(o) {
  return ContentService.createTextOutput(JSON.stringify(o)).setMimeType(ContentService.MimeType.JSON);
}

function handle_(req) {
  const ss = SpreadsheetApp.getActiveSpreadsheet();
  switch (req.action) {
    case 'ping':
      return { ok: true };

    case 'room': {             // {agents: {ID: quota}} -> {room: {ID: surveys still available}}
      const done = readDone_(ss);
      const res = activeReservations_(ss);
      const room = {};
      Object.keys(req.agents || {}).forEach(function (a) {
        const A = up_(a);
        room[A] = Number(req.agents[a]) - (done[A] || 0) - (res.counts[A] || 0);
      });
      return { ok: true, room: room };
    }

    case 'reserve': {          // {agent, quota, worker} -> {granted, used}
      const A = up_(req.agent);
      const quota = Number(req.quota);
      const used = (readDone_(ss)[A] || 0) + (activeReservations_(ss).counts[A] || 0);
      if (!(used < quota)) return { ok: true, granted: false, used: used };
      sheet_(ss, TAB.res).appendRow([String(req.worker), "'" + A, Date.now()]);
      return { ok: true, granted: true, used: used + 1 };
    }

    case 'commit': {           // {agent, worker, submitted, entry} -> release slot, count survey, log the chat
      let done = null;
      if (req.agent) {
        const A = up_(req.agent);
        removeReservation_(ss, String(req.worker), A);
        done = req.submitted ? addDone_(ss, A) : (readDone_(ss)[A] || 0);
      }
      if (req.entry) appendLog_(ss, req.entry);
      return { ok: true, done: done };
    }

    default:
      throw new Error('unknown action: ' + req.action);
  }
}

// ------------------------------------------------------------------ helpers
function up_(v) { return String(v || '').trim().toUpperCase(); }

function sheet_(ss, name) { return ss.getSheetByName(name) || ss.insertSheet(name); }

function rows_(sh, ncols) {    // data rows (without the header row)
  const n = sh.getLastRow() - 1;
  return n > 0 ? sh.getRange(2, 1, n, ncols).getValues() : [];
}

function readDone_(ss) {
  const done = {};
  rows_(sheet_(ss, TAB.state), 2).forEach(function (r) {
    if (r[0] !== '') done[up_(r[0])] = Number(r[1]) || 0;
  });
  return done;
}

function addDone_(ss, A) {
  const sh = sheet_(ss, TAB.state);
  const rows = rows_(sh, 2);
  for (let i = 0; i < rows.length; i++) {
    if (up_(rows[i][0]) === A) {
      const n = (Number(rows[i][1]) || 0) + 1;
      sh.getRange(i + 2, 2).setValue(n);
      return n;
    }
  }
  sh.appendRow(["'" + A, 1]);
  return 1;
}

function activeReservations_(ss) {   // drops expired reservations, returns {counts: {ID: n}}
  const sh = sheet_(ss, TAB.res);
  const rows = rows_(sh, 3);
  const now = Date.now();
  const keep = rows.filter(function (r) { return r[0] !== '' && now - Number(r[2]) < RESERVATION_TTL_MS; });
  if (keep.length !== rows.length) {
    if (rows.length) sh.getRange(2, 1, rows.length, 3).clearContent();
    if (keep.length) sh.getRange(2, 1, keep.length, 3).setValues(keep.map(function (r) {
      return [r[0], "'" + up_(r[1]), r[2]];
    }));
  }
  const counts = {};
  keep.forEach(function (r) { const A = up_(r[1]); counts[A] = (counts[A] || 0) + 1; });
  return { counts: counts };
}

function removeReservation_(ss, worker, A) {
  const sh = sheet_(ss, TAB.res);
  const rows = rows_(sh, 3);
  const i = rows.findIndex(function (r) { return String(r[0]) === worker && up_(r[1]) === A; });
  if (i < 0) return;
  rows.splice(i, 1);
  sh.getRange(2, 1, rows.length + 1, 3).clearContent();      // rewrite instead of deleteRow (never fails)
  if (rows.length) sh.getRange(2, 1, rows.length, 3).setValues(rows.map(function (r) {
    return [r[0], "'" + up_(r[1]), r[2]];
  }));
}

function appendLog_(ss, e) {
  const t = function (v) { return v === undefined || v === null || v === '' ? '' : "'" + String(v); };  // keep as text
  sheet_(ss, TAB.log).appendRow([
    t(e.time), t(e.session_date), Number(e.worker) || 0, t(up_(e.agent_id)), t(e.agent_name), String(e.outcome || ''),
    e.chat_counted === true, Number(e.messages_total) || 0, Number(e.messages_client) || 0,
    Number(e.messages_agent) || 0, e.survey_shown === true, e.survey_submitted === true,
    String(e.test_case || ''), String(e.reason || ''),
  ]);
}

// ------------------------------------------------------------------ one-time setup (run from the editor)
function setup() {
  const ss = SpreadsheetApp.getActiveSpreadsheet();

  const cfg = sheet_(ss, TAB.config);
  if (cfg.getLastRow() === 0) {
    cfg.getRange(1, 1, CONFIG_TEMPLATE.length, 3).setNumberFormat('@').setValues(CONFIG_TEMPLATE);
  }
  cfg.getRange('D1').setValue('surveys_done (live)');
  cfg.getRange('D2').setFormula(
    '=ARRAYFORMULA(IF(LOWER(A2:A)="agent",IFERROR(VLOOKUP(UPPER(B2:B),State!A2:B,2,FALSE),0),""))');

  header_(sheet_(ss, TAB.state), ['agent_id', 'surveys_done']);
  header_(sheet_(ss, TAB.res), ['worker', 'agent_id', 'epoch_ms']);
  header_(sheet_(ss, TAB.log), LOG_HEADERS);

  const rep = sheet_(ss, TAB.report);
  rep.clear();
  rep.getRange('A1:B1').setValues([['Day to show (blank = latest day)', '']]);
  rep.getRange('A2').setValue('Showing');
  rep.getRange('B2').setFormula('=IF(B1="",IFERROR(INDEX(SORT(FILTER(Log!B2:B,Log!B2:B<>""),1,FALSE),1),""),' +
    'IF(ISNUMBER(B1),TEXT(B1,"yyyy-mm-dd"),B1))');
  rep.getRange('A3:H3').setValues([['Contacts', '', 'Chats', '', 'Surveys given', '', 'Chats, no survey', '']]);
  rep.getRange('B3').setFormula('=COUNTIF(Log!B2:B,$B$2)');
  rep.getRange('D3').setFormula('=COUNTIFS(Log!B2:B,$B$2,Log!G2:G,TRUE)');
  rep.getRange('F3').setFormula('=COUNTIFS(Log!B2:B,$B$2,Log!L2:L,TRUE)');
  rep.getRange('H3').setFormula('=D3-F3');
  rep.getRange('A5:G5').setValues([['Agent ID', 'Name', 'Contacted', 'Chats', 'Surveys given',
    'Chats, no survey', 'Below chat threshold']]);
  rep.getRange('A6').setFormula('=IFERROR(SORT(UNIQUE(FILTER(Log!D2:D,Log!B2:B=$B$2,Log!D2:D<>""))),"")');
  rep.getRange('B6').setFormula('=ARRAYFORMULA(IF(A6:A="","",IFERROR(VLOOKUP(A6:A,Log!D2:E,2,FALSE),"")))');
  rep.getRange('C6').setFormula('=ARRAYFORMULA(IF(A6:A="","",COUNTIFS(Log!D2:D,A6:A,Log!B2:B,$B$2)))');
  rep.getRange('D6').setFormula('=ARRAYFORMULA(IF(A6:A="","",COUNTIFS(Log!D2:D,A6:A,Log!B2:B,$B$2,Log!G2:G,TRUE)))');
  rep.getRange('E6').setFormula('=ARRAYFORMULA(IF(A6:A="","",COUNTIFS(Log!D2:D,A6:A,Log!B2:B,$B$2,Log!L2:L,TRUE)))');
  rep.getRange('F6').setFormula('=ARRAYFORMULA(IF(A6:A="","",D6:D-E6:E))');
  rep.getRange('G6').setFormula('=ARRAYFORMULA(IF(A6:A="","",C6:C-D6:D))');
  rep.getRange('A5:G5').setFontWeight('bold');
  rep.setFrozenRows(5);

  const props = PropertiesService.getScriptProperties();
  let token = props.getProperty('TOKEN');
  if (!token) {
    token = Utilities.getUuid().replace(/-/g, '');
    props.setProperty('TOKEN', token);
  }
  Logger.log('Setup done. Your TOKEN (counter_token / QA_COUNTER_TOKEN) is: ' + token);
}

function header_(sh, headers) {
  if (sh.getLastRow() === 0) sh.getRange(1, 1, 1, headers.length).setValues([headers]).setFontWeight('bold');
  sh.setFrozenRows(1);
}

/** Starts a fresh day of counting: clears surveys_done and in-flight reservations (the Log is kept). */
function resetCounters() {
  const ss = SpreadsheetApp.getActiveSpreadsheet();
  [TAB.state, TAB.res].forEach(function (name) {
    const sh = sheet_(ss, name);
    if (sh.getLastRow() > 1) sh.getRange(2, 1, sh.getLastRow() - 1, sh.getLastColumn()).clearContent();
  });
}
