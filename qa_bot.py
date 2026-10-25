#!/usr/bin/env python3
r"""
qa_bot.py - automated QA tester for the live-agent chat.

Drives a real browser through the chat UI exactly like a person (click, type, read the screen). The site's own
/v1/ws responses are only READ, to know when the agent has replied; the bot never calls the site's APIs.

Modes (all settings live in bot_settings.json and the Google Sheet - see the setup guide):
  python qa_bot.py --once                 one chat with a test case from test_cases/ (no Sheet needed)
  python qa_bot.py                        scheduler: chats repeatedly inside the Sheet's run window
  python qa_bot.py --parallel             launcher: starts parallel_runs workers (local PC version)
  python qa_bot.py --gate-output          GitHub Actions: decide whether to start workers now

Per chat: open site -> chat icon -> initial message -> bot menu -> wait for live agent -> check the agent's ID
against the Sheet -> scripted messages (each one only after the agent replied, plus reply_delay_seconds) ->
close (X) -> End chat -> survey (if it appears) -> log the result.

For AUTHORIZED QA testing only: it occupies real agents and submits real CSAT surveys.
"""
import argparse
import contextlib
import json
import os
import re
import sys
import time
import traceback
from datetime import datetime, timedelta
from pathlib import Path

try:
    from playwright.sync_api import sync_playwright
except ImportError:          # the GitHub "gate" job and config checks run without Playwright
    sync_playwright = None

import builtins as _bi
import random

# --------------------------------------------------------------------------- privacy masking (public GitHub logs)
# In GitHub mode the Actions logs of a public repo are public, so agent names/IDs are replaced with [agent] in
# everything this script prints, and IDs are also registered with GitHub's own log masking.
_real_print = _bi.print
MASK = False
_SENS = set()
_NICK_RE = re.compile(r"[^\s|'\"]+\s*\|\s*[A-Za-z0-9]{6}")


def register_sensitive(*vals):
    for v in vals:
        if not v:
            continue
        v = str(v).strip()
        parts = {v} | ({p.strip() for p in v.split("|") if p.strip()} if "|" in v else set())
        for p in parts:
            if len(p) >= 2 and p not in _SENS:
                _SENS.add(p)
                if MASK and os.environ.get("GITHUB_ACTIONS") == "true" and len(p) >= 4:
                    _real_print(f"::add-mask::{p}")


def scrub(text):
    text = _NICK_RE.sub("[agent]", text)
    for s_ in sorted(_SENS, key=len, reverse=True):
        text = re.sub(r"(?i)(?<![A-Za-z0-9])" + re.escape(s_) + r"(?![A-Za-z0-9])", "[agent]", text)
    return text


def print(*a, **k):  # noqa: A001  (module-level print: masks agent identities when MASK is on)
    if MASK:
        a = tuple(scrub(str(x)) for x in a)
    _real_print(*a, **k)


# --------------------------------------------------------------------------- browser launch (same
# approach as the chat_recon recorder: real window, no automation flag, Brave/Chrome/Chromium fallback)
def find_brave():
    cands = []
    for env in ("PROGRAMFILES", "PROGRAMFILES(X86)", "LOCALAPPDATA"):
        base = os.environ.get(env)
        if base:
            cands.append(Path(base) / "BraveSoftware" / "Brave-Browser" / "Application" / "brave.exe")
    cands += [Path("/Applications/Brave Browser.app/Contents/MacOS/Brave Browser"),
              Path("/usr/bin/brave-browser"), Path("/usr/bin/brave")]
    for c in cands:
        if c.exists():
            return str(c)
    return None


def launch(pw, args, profile_dir=None):
    kw = dict(
        headless=args.headless,
        no_viewport=True,
        ignore_default_args=["--enable-automation"],
        args=["--disable-blink-features=AutomationControlled", "--start-maximized"],
    )
    order = ["brave", "chrome", "chromium"] if args.browser == "auto" else [args.browser]
    last = None
    for kind in order:
        prof = profile_dir or args.profile or f"qa_bot_profile_{kind}"
        try:
            if kind == "brave":
                exe = args.brave_path or find_brave()
                if not exe:
                    print(f"[browser] Brave not found (use --brave-path)")
                    continue
                ctx = pw.chromium.launch_persistent_context(prof, executable_path=exe, **kw)
            elif kind == "chrome":
                ctx = pw.chromium.launch_persistent_context(prof, channel="chrome", **kw)
            else:
                ctx = pw.chromium.launch_persistent_context(prof, **kw)
            print(f"[browser] {kind} started (profile folder: {prof})")
            return ctx
        except Exception as e:  # noqa: BLE001
            last = e
            print(f"[browser] {kind} failed: {str(e)[:200]}")
    raise RuntimeError(f"no usable browser found: {last}")


# --------------------------------------------------------------------------- the QA run
class StepFailed(Exception):
    pass


class AgentEnded(BaseException):
    """The agent closed the chat (or left with nobody taking over) before the script was finished."""


class StopNow(BaseException):
    """Raised at a safe point (never in the middle of a browser command) after Ctrl+C or typing 'stop'."""


# stop requests: "now" = end the current chat cleanly and exit; "after_chat" = finish the current chat, then exit
STOP = {"now": False, "after_chat": False, "force": False, "why": ""}


def check_stop():
    if STOP["now"]:
        raise StopNow()


def install_ctrl_c():
    import signal

    def _h(signum, frame):
        if STOP["now"]:
            STOP["force"] = True
            raise KeyboardInterrupt          # second Ctrl+C: force-quit
        STOP["now"] = True
        STOP["why"] = STOP["why"] or "Ctrl+C"
        _real_print("\n*** Ctrl+C: ending the current chat cleanly, saving the result, then stopping. "
                    "(Ctrl+C again = force-quit) ***")
    try:
        signal.signal(signal.SIGINT, _h)
    except Exception:  # noqa: BLE001
        pass


_ID_RE = re.compile(r"\b(?=[A-Za-z0-9]*\d)(?=[A-Za-z0-9]*[A-Za-z])[A-Za-z0-9]{6}\b")


def parse_agent_id(nickname):
    """Agent nicknames look like 'Ana | AT3469'. The ID is the 6-character letters+digits token;
    prefer the part after the last '|', else any such token in the string. Returned upper-case."""
    if not nickname:
        return None
    nickname = str(nickname)
    tail = nickname.split("|")[-1].strip()
    m = _ID_RE.fullmatch(tail) or _ID_RE.search(nickname)
    return m.group(0).upper() if m else None


class Runner:
    def __init__(self, page, chat_frame_name, out_dir, step_timeout_s,
                 agent_wait_timeout_s=420, turn_wait_timeout_s=240):
        self.page = page
        self.chat_frame_name = chat_frame_name
        self.out = out_dir
        self.step_timeout = step_timeout_s
        # These two are deliberately generous: a real human agent can take a while after the
        # "connected to an agent" UI moment to actually start typing (queue/handoff lag, or they're
        # mid-task on another chat) - a 180s budget was seen to time out in practice even though the
        # UI showed a successful connection, with nothing in the transcript to say WHY it was slow vs.
        # broken. Raising the budget plus the heartbeat/raw-dump logging above means a future timeout
        # here is self-diagnosing instead of needing another round-trip to ask for logs.
        self.agent_wait_timeout = agent_wait_timeout_s
        self.turn_wait_timeout = turn_wait_timeout_s
        self.t0 = time.time()
        self.report = []
        self.transcript_f = open(out_dir / "transcript.jsonl", "w", encoding="utf-8")
        self.raw_ws_f = open(out_dir / "raw_ws.jsonl", "w", encoding="utf-8")
        self._last_raw_str = None
        self.seen_msg_keys = set()   # (index,) already written to transcript
        self.agent_msg_count = 0     # count of distinct agent "Message" entries seen
        self.last_agent_from = None  # most recent agent sender name/participantId seen (any event type)
        self.snap_n = 0
        self.agent_ready = False        # True once the first live-agent message has been seen
        self.agent_joined = False       # True as soon as ANY agent event arrives (join comes before the greeting)
        # Whose turn is it? The bot may reply only to an agent message that is NEWER than the last client-side
        # message - its own, or one YOU typed (paused or not). So after you take over and answer the agent, the
        # bot waits for the agent's next message instead of answering the one you already answered.
        self.turn_mark = 0.0
        self.bot_texts = []             # texts the bot sent, to tell its own messages from yours
        # timings (seconds since epoch) for the per-chat durations in the Log / Report
        self.t_menu_done = None         # last menu option clicked = asked for an agent
        self.t_joined = None            # agent joined
        self.t_greeting = None          # agent's first message
        self.t_chat_end = None          # chat closed (X)
        self.reply_times = []           # seconds the agent took to answer each bot message
        self._bot_sent_t = None
        self.transcript_rows = []       # every message, for transcript.txt
        self.chat_ended = False         # "End chat" was clicked (normal end, skip, or wrap-up)
        self.agents_seen = []           # agent IDs in the order they appeared (more than one = transfer)
        self.agents_present = set()
        self.agent_gone_t = None        # when the last agent left the chat
        self.remote_ended = False       # the server marked the chat as ended
        self.agent_ended_chat = False   # the script was cut short because the agent ended the chat
        self.interrupted = False        # stopped with Ctrl+C / window closed
        self.committed = False          # result already saved (so a close handler doesn't save it twice)
        # agent targeting (set by the scheduler; None = talk to any agent)
        self.gate = None                # callable(agent_id)->(ok, reason); None = talk to any agent
        self.reserved_agent = None      # agent whose quota slot this chat holds (released when the chat ends)
        self.agent_id = None            # 6-char ID parsed from the first Agent participant's nickname
        self.skip_reason = None         # set if the connected agent isn't a target -> chat is ended early
        self.survey_submitted = False
        self.survey_shown = False
        self.reply_delay = 30            # seconds to wait after the agent's latest message before replying
        self.last_agent_msg_t = None
        self.msg_client = 0             # messages the bot (client side) actually sent, per the server
        Runner.current = self
        self.page.on("response", self._on_response)

    # ---- manual takeover (press Enter in the terminal to toggle; see start_pause_listener below).
    # While paused the bot sends nothing and its timeouts stop counting, so you can type in the chat
    # window yourself. Agent messages that arrive during a pause are treated as replies to YOUR
    # messages, so the bot doesn't mistake them for replies to its own scripted lines.
    current = None      # the active Runner (the listener thread toggles pause on this)
    PAUSED = False      # class-level so a pause also holds the scheduler between chats

    @property
    def paused(self):
        return Runner.PAUSED

    def timings(self):
        end = self.t_chat_end or time.time()
        r = lambda v: None if v is None else int(round(v))  # noqa: E731
        return {
            "duration_total_s": r(end - self.t0),
            "queue_wait_s": r(self.t_joined - self.t_menu_done) if self.t_joined and self.t_menu_done else None,
            "chat_duration_s": r(end - self.t_joined) if self.t_joined else None,
            "first_reply_s": r(self.t_greeting - self.t_joined) if self.t_greeting and self.t_joined else None,
            "avg_agent_reply_s": r(sum(self.reply_times) / len(self.reply_times)) if self.reply_times else None,
        }

    @property
    def transferred_to(self):
        """The agent the chat ended up with, if it isn't the one who joined first (a transfer)."""
        firsts = [a for a in self.agents_seen if a]
        if len(set(firsts)) > 1 and firsts[-1] != firsts[0]:
            return firsts[-1]
        if len(set(firsts)) > 1:          # moved away and back: still a transfer, report the other agent
            return next(a for a in firsts if a != firsts[0])
        return ""

    def agent_has_ended(self):
        """True when the server says the chat ended, or the last agent left and nobody joined within 30 s."""
        return self.remote_ended or (self.agent_gone_t is not None and not self.agents_present
                                     and time.time() - self.agent_gone_t > 30)

    def my_turn(self):
        # never talk into an empty chat: during a transfer (agent left, next one not joined yet) wait for the new agent
        if self.agent_gone_t is not None and not self.agents_present:
            return False
        return self.last_agent_msg_t is not None and self.last_agent_msg_t > self.turn_mark

    def respect_reply_delay(self):
        """Wait until `reply_delay` seconds have passed since the agent's LATEST message (so a human-like
        pause before every reply; if the agent sends another message meanwhile, the clock restarts)."""
        while True:
            if self.last_agent_msg_t is None or self.reply_delay <= 0:
                return
            left = self.reply_delay - (time.time() - self.last_agent_msg_t)
            if left <= 0:
                return
            if self.paused:
                self.wait_if_paused()
                continue
            check_stop()
            self.page.wait_for_timeout(int(min(left, 1.0) * 1000) + 20)

    def wait_if_paused(self):
        t = time.time()
        nag = t + 60
        while self.paused:
            self.page.wait_for_timeout(250)
            check_stop()
            if time.time() > nag:      # a forgotten pause holds the agent's slot - remind every minute
                print(f"   *** still PAUSED ({int((time.time() - t) // 60)} min) - press Enter in this window "
                      f"to resume ***")
                nag = time.time() + 60
        return time.time() - t

    # ---- network listener: READ-ONLY. Parses /v1/ws long-poll responses to track the conversation.
    def _on_response(self, resp):
        try:
            if "/v1/ws" not in resp.url or resp.request.method != "GET":
                return
            if "json" not in (resp.headers.get("content-type") or ""):
                return
            data = resp.json()

            # Dump every *distinct* raw body seen (deduped against the previous one) to raw_ws.jsonl.
            # This is the full, unfiltered response - so if a "connected"/"agent joined" signal ever
            # arrives through a field we don't parse below, or the real schema differs from what we
            # expect, it's captured here for after-the-fact debugging instead of being silently dropped.
            # (Keeps the file small: a long-poll that keeps returning the same "nothing new" body only
            # gets written once.)
            raw_str = json.dumps(data, sort_keys=True, ensure_ascii=False)
            if raw_str != self._last_raw_str:
                self._last_raw_str = raw_str
                self.raw_ws_f.write(json.dumps(
                    {"t_ms": int((time.time() - self.t0) * 1000), "url": resp.url, "body": data},
                    ensure_ascii=False) + "\n")
                self.raw_ws_f.flush()

            orig = data.get("original") or {}
            if self.agent_joined and (orig.get("chatEnded") is True or data.get("chatEnded") is True):
                self.remote_ended = True
            msgs = orig.get("messages") or []
            for m in msgs:
                idx = m.get("index")
                key = (idx, m.get("type"))
                if key in self.seen_msg_keys:
                    continue
                self.seen_msg_keys.add(key)
                frm = m.get("from") or {}
                rec = {"t_ms": int((time.time() - self.t0) * 1000), "index": idx,
                       "type": m.get("type"), "from_type": frm.get("type"),
                       "from_name": frm.get("nickname"), "text": m.get("text")}
                rec["clock"] = time.strftime("%H:%M:%S")
                self.transcript_f.write(json.dumps(rec, ensure_ascii=False) + "\n")
                self.transcript_f.flush()
                self.transcript_rows.append(rec)
                if frm.get("type") == "Agent":
                    if not self.agent_joined:
                        self.t_joined = time.time()
                    self.agent_joined = True
                    self.last_agent_from = frm.get("nickname") or frm.get("participantId")
                    if self.agent_id is None:
                        self.agent_id = parse_agent_id(frm.get("nickname"))
                    register_sensitive(frm.get("nickname"), self.agent_id)
                    who = parse_agent_id(frm.get("nickname")) or str(frm.get("nickname") or frm.get("participantId") or "")
                    if who and (not self.agents_seen or self.agents_seen[-1] != who):
                        self.agents_seen.append(who)
                    mtype = str(m.get("type") or "")
                    if re.search(r"left|disconnect|ended|removed", mtype, re.I):
                        self.agents_present.discard(who)
                        if not self.agents_present:
                            self.agent_gone_t = time.time()
                    else:
                        self.agents_present.add(who)
                        self.agent_gone_t = None
                if m.get("type") == "Message" and frm.get("type") == "Agent":
                    self.agent_msg_count += 1
                    self.last_agent_msg_t = time.time()
                    self.t_greeting = self.t_greeting or self.last_agent_msg_t
                    if self._bot_sent_t:
                        self.reply_times.append(self.last_agent_msg_t - self._bot_sent_t)
                        self._bot_sent_t = None
                if m.get("type") == "Message" and frm.get("type") == "Client":
                    self.msg_client += 1
                    norm = " ".join(str(m.get("text") or "").split())
                    rec["by"] = "bot"
                    if norm in self.bot_texts:
                        self.bot_texts.remove(norm)          # the bot's own message coming back
                    elif self.agent_joined:
                        rec["by"] = "you (typed by hand)"
                        self.turn_mark = max(self.turn_mark, time.time())   # someone typed by hand
                        print("         (manual message seen - the bot will wait for the agent's next reply)")
        except Exception:  # noqa: BLE001
            pass  # a non-JSON or malformed poll response is not fatal

    def chat_frame(self):
        f = self.page.frame(name=self.chat_frame_name)
        if f is None:
            raise StepFailed(f"chat iframe (name={self.chat_frame_name!r}) not found on the page")
        return f

    def snapshot(self, label):
        self.snap_n += 1
        name = f"{self.snap_n:02d}_{re.sub(r'[^a-zA-Z0-9]+', '_', label)[:40]}.png"
        try:
            self.page.screenshot(path=str(self.out / name))
        except Exception as e:  # noqa: BLE001
            print(f"  (screenshot failed: {str(e)[:120]})")

    def step(self, name, fn, *a, **kw):
        check_stop()
        self.wait_if_paused()
        t_start = time.time()
        print(f"[{int(t_start - self.t0):4d}s] {name} ...")
        try:
            fn(*a, **kw)
            dt = time.time() - t_start
            print(f"         done ({dt:.1f}s)")
            self.report.append({"step": name, "status": "ok", "seconds": round(dt, 1)})
            self.snapshot(name)
        except Exception as e:  # noqa: BLE001
            dt = time.time() - t_start
            tb = traceback.format_exc()
            print(f"         FAILED after {dt:.1f}s: {str(e)[:300]}")
            self.report.append({"step": name, "status": "failed", "seconds": round(dt, 1),
                                "error": str(e)[:500]})
            self.snapshot(name + "_FAILED")
            (self.out / "error.txt").write_text(tb, encoding="utf-8")
            raise

    def wait_until(self, desc, predicate, timeout=None, heartbeat_every=15):
        """Poll `predicate` until true or `timeout` seconds pass. For waits longer than
        `heartbeat_every` seconds, prints a progress line periodically (elapsed/timeout plus
        agent_msg_count and the last-seen agent sender) so a long real-agent wait doesn't read as
        a hang - and so the console log itself is a timeline if it does eventually time out."""
        timeout = timeout or self.step_timeout
        t_start = time.time()
        deadline = t_start + timeout
        next_beat = t_start + heartbeat_every
        while time.time() < deadline:
            self.page.wait_for_timeout(250)
            check_stop()
            if self.paused:
                dt = self.wait_if_paused()   # paused time doesn't count against the timeout
                deadline += dt
                next_beat += dt
                continue
            try:
                if predicate():
                    return
            except Exception:  # noqa: BLE001
                pass
            now = time.time()
            if now >= next_beat:
                print(f"         ... still waiting ({int(now - t_start)}s/{timeout}s) "
                      f"[agent_msg_count={self.agent_msg_count}, last_agent={self.last_agent_from!r}]")
                next_beat = now + heartbeat_every
        raise StepFailed(
            f"timed out waiting for: {desc} "
            f"(agent_msg_count={self.agent_msg_count}, last_agent={self.last_agent_from!r}; "
            f"see raw_ws.jsonl for everything the server actually sent during this wait)")

    def finish(self):
        try:
            self.page.remove_listener("response", self._on_response)   # the page is reused across chats
        except Exception:  # noqa: BLE001
            pass
        self.transcript_f.close()
        self.raw_ws_f.close()
        (self.out / "report.json").write_text(
            json.dumps({"seconds": round(time.time() - self.t0, 1),
                        "agent_id": self.agent_id, "agent_name": self.last_agent_from,
                        "skipped": self.skip_reason, "survey_shown": self.survey_shown,
                        "survey_submitted": self.survey_submitted,
                        "messages_client": self.msg_client, "messages_agent": self.agent_msg_count,
                        "timings": self.timings(),
                        "steps": self.report}, indent=2), encoding="utf-8")


# --------------------------------------------------------------------------- the fixed script
def run_test_case(runner: Runner, tc: dict, start_url: str, reuse=False):
    page = runner.page

    # ---- optional questions: the site sometimes asks a yes/no question (e.g. "do you still need help?") before
    # the menu or before connecting to an agent. If one of these buttons appears, the bot clicks it; if it
    # doesn't appear, nothing happens. Each button is clicked once (it's marked), so old buttons are ignored.
    answers = tc.get("auto_answer_buttons", ["No"])
    answered = {"n": 0}

    def answer_prompts():
        if not answers or answered["n"] >= 5:
            return
        f = page.frame(name=runner.chat_frame_name)
        if f is None:
            return
        for label in answers:
            loc = f.locator("button").filter(has_text=re.compile(r"^\s*" + re.escape(label) + r"\s*$", re.I))
            try:
                for i in range(loc.count() - 1, -1, -1):
                    b = loc.nth(i)
                    if b.get_attribute("data-qa-clicked") or not b.is_visible() or not b.is_enabled():
                        continue
                    b.evaluate("e => e.setAttribute('data-qa-clicked', '1')")
                    b.click()
                    answered["n"] += 1
                    print(f"         the site asked a question - answered {label!r}")
                    return
            except Exception:  # noqa: BLE001 - the chat re-rendered mid-check; try again next poll
                return

    def goto():
        if reuse:      # same browser as the last chat: use its chat button directly if it's showing
            f = page.frame(name=runner.chat_frame_name)
            if f is not None:
                btn = f.locator('[data-testid="fusionChatButton"]')
                try:
                    runner.wait_until("chat button on the current page",
                                      lambda: btn.count() > 0 and btn.first.is_visible(), timeout=4)
                    print("         reusing the current page")
                    return
                except StepFailed:
                    pass
        page.goto(start_url, wait_until="commit", timeout=60_000)
    runner.step("open site (or reuse the open page)" if reuse else "open site", goto)

    def open_chat():
        runner.wait_until("chat frame to exist", lambda: page.frame(name=runner.chat_frame_name) is not None)
        f = runner.chat_frame()
        btn = f.locator('[data-testid="fusionChatButton"]')
        runner.wait_until("chat button visible", lambda: btn.count() > 0 and btn.first.is_visible())
        btn.first.click()
    runner.step("click chat icon", open_chat)

    def type_initial():
        f = runner.chat_frame()
        box = f.locator('[data-testid="doorway-search-input"]')
        runner.wait_until("search box visible", lambda: box.count() > 0 and box.first.is_visible())
        box.first.click()
        box.first.fill(tc["initial_message"])
        box.first.press("Enter")
    runner.step("type initial message", type_initial)

    def pick_menu(label):
        f = runner.chat_frame()
        btn = f.locator(f'button[data-testid="buttonElement"][value="{label}"]')

        def menu_ready():
            answer_prompts()
            return btn.count() > 0 and btn.first.is_visible()
        runner.wait_until(f'menu option "{label}" visible', menu_ready, timeout=45)
        btn.first.click()
        runner.t_menu_done = time.time()
    for opt in tc["menu_path"]:
        runner.step(f'choose menu option "{opt}"', pick_menu, opt)

    def wait_for_agent():
        # the agent's join event (with name + ID) arrives before their first message
        def joined():
            answer_prompts()
            return runner.agent_joined
        runner.wait_until("a live agent to join", joined, timeout=runner.agent_wait_timeout)
        runner.agent_ready = True
    runner.step("wait for live agent to join", wait_for_agent)

    # ---- agent gate: only run the full conversation + survey with a TARGET agent. Anyone else gets a
    # polite early exit (no scripted chat, no survey) so their time isn't used.
    if runner.gate is not None:
        def check_agent():
            try:
                runner.wait_until("agent ID to be visible", lambda: runner.agent_id is not None, timeout=10)
            except StepFailed:
                pass
            if runner.agent_id is None:
                runner.skip_reason = "agent ID not visible"
            else:
                ok, why = runner.gate(runner.agent_id)     # atomic: checks quota AND reserves a slot
                if ok:
                    runner.reserved_agent = runner.agent_id
                else:
                    runner.skip_reason = why
            print(f"         connected agent: {runner.last_agent_from!r} -> id={runner.agent_id} "
                  f"({'SKIP: ' + runner.skip_reason if runner.skip_reason else 'target, continuing'})")
        runner.step("check agent ID", check_agent)

        if runner.skip_reason:
            def skip_close():
                f = runner.chat_frame()
                b = f.locator('[data-testid="fusion-header-close-btn"]')
                runner.wait_until("close (X) button visible", lambda: b.count() > 0 and b.first.is_visible())
                b.first.click()
                runner.t_chat_end = time.time()
            runner.step("skip: click close (X)", skip_close)

            def skip_end():
                f = runner.chat_frame()
                b = f.get_by_text(tc["end_chat_button_text"], exact=True)
                runner.wait_until('"End chat" option visible', lambda: b.count() > 0 and b.first.is_visible())
                b.first.click()
                runner.chat_ended = True
            runner.step("skip: choose End chat", skip_end)
            page.wait_for_timeout(1500)
            return

    def reply(text, first):
        """Wait until the agent has written something newer than the last client message (the bot's or
        yours), wait reply_delay_seconds, re-check (a pause or your own typing may have taken the turn), send."""
        while True:
            runner.wait_until("the agent's greeting" if first else "the agent's reply",
                              lambda: runner.my_turn() or runner.agent_has_ended(),
                              timeout=runner.agent_wait_timeout if first else runner.turn_wait_timeout)
            if runner.agent_has_ended():
                raise AgentEnded()
            runner.respect_reply_delay()
            runner.wait_if_paused()
            if runner.agent_has_ended():
                raise AgentEnded()
            if runner.my_turn():
                break
        f = runner.chat_frame()
        box = f.locator('input[aria-label="Reply or ask me a question"]')
        runner.wait_until("reply box visible", lambda: box.count() > 0 and box.first.is_visible())
        box.first.click()
        box.first.fill(text)
        runner.bot_texts.append(" ".join(text.split()))
        runner.turn_mark = max(runner.turn_mark, time.time())
        box.first.press("Enter")
        runner._bot_sent_t = time.time()

    n = len(tc["scripted_messages"])
    try:
        for i, msg in enumerate(tc["scripted_messages"], start=1):
            runner.step(f"message {i}/{n} (after the agent's reply): {msg[:40]!r}", reply, msg, i == 1)
    except AgentEnded:
        runner.agent_ended_chat = True
        runner.report.append({"step": f"the agent ended the chat after {runner.msg_client} bot message(s)",
                              "status": "ok", "seconds": 0})
        print(f"         the agent ended the chat before the script finished ({runner.msg_client}/{n} messages sent)")
        runner.snapshot("agent ended the chat")

    if runner.transferred_to:
        print(f"         note: this chat was transferred from {runner.agent_id} to {runner.transferred_to}")

    # brief grace period for the agent's closing reply, not required to proceed
    page.wait_for_timeout(1500 if runner.agent_ended_chat else 4000)

    if runner.agent_ended_chat:
        # the chat is already over on the agent's side: close the window if the X/End chat are still offered
        # (short waits, no failure if they aren't), then look for the survey as usual
        try:
            f = runner.chat_frame()
            f.locator('[data-testid="fusion-header-close-btn"]').first.click(timeout=5000)
            runner.t_chat_end = time.time()
            f.get_by_text(tc["end_chat_button_text"], exact=True).first.click(timeout=5000)
        except Exception:  # noqa: BLE001
            pass
        runner.t_chat_end = runner.t_chat_end or time.time()
        runner.chat_ended = True

    def close_chat():
        if runner.agent_ended_chat:
            return
        f = runner.chat_frame()
        close_btn = f.locator('[data-testid="fusion-header-close-btn"]')
        runner.wait_until("close (X) button visible", lambda: close_btn.count() > 0 and close_btn.first.is_visible())
        close_btn.first.click()
        runner.t_chat_end = time.time()
    if not runner.agent_ended_chat:
        runner.step("click close (X)", close_chat)

    def end_chat():
        if runner.agent_ended_chat:
            return
        f = runner.chat_frame()
        end_btn = f.get_by_text(tc["end_chat_button_text"], exact=True)
        runner.wait_until('"End chat" option visible', lambda: end_btn.count() > 0 and end_btn.first.is_visible())
        end_btn.first.click()
        runner.chat_ended = True
    if not runner.agent_ended_chat:
        runner.step("choose End chat", end_chat)

    sv = tc["survey"]
    if runner.transferred_to:
        # by design: a transferred chat gets no survey (it would rate a different agent than the target)
        runner.report.append({"step": "no survey: the chat was transferred", "status": "ok", "seconds": 0})
        print("         transferred chat - not filling in a survey (by design)")
        return

    def wait_survey():
        f = runner.chat_frame()
        star = f.locator(f'[data-testid="feedback-survey-assistanceRating-star-{sv["stars"]}"]')
        try:
            runner.wait_until("survey to appear", lambda: star.count() > 0 and star.first.is_visible(),
                              timeout=60)
            runner.survey_shown = True
        except StepFailed:
            runner.survey_shown = False
            print("         no survey appeared (the conversation still counts) - finishing this chat")
    runner.step("wait for survey (optional)", wait_survey)
    if not runner.survey_shown:
        return

    def rate_stars():
        f = runner.chat_frame()
        star = f.locator(f'[data-testid="feedback-survey-assistanceRating-star-{sv["stars"]}"]')
        runner.wait_until("survey star rating visible", lambda: star.count() > 0 and star.first.is_visible(),
                          timeout=60)
        star.first.click()
    runner.step(f'rate {sv["stars"]} stars', rate_stars)

    def pick_resolved():
        f = runner.chat_frame()
        opt = f.locator(f'[data-testid="feedback-survey-requestResolved-{sv["resolved"]}"]')
        runner.wait_until("resolved yes/no option visible", lambda: opt.count() > 0 and opt.first.is_visible())
        opt.first.click()
    runner.step(f'choose resolved = {sv["resolved"]}', pick_resolved)

    def write_comment():
        f = runner.chat_frame()
        area = f.locator('[data-testid="feedback-survey-additionalFeedback-textarea"]')
        runner.wait_until("comment box visible", lambda: area.count() > 0 and area.first.is_visible())
        area.first.click()
        area.first.fill(sv["comment"])
    runner.step("write feedback comment", write_comment)

    def submit():
        f = runner.chat_frame()
        btn = f.locator('[data-testid="feedback-survey-submit"]')
        runner.wait_until("submit button visible", lambda: btn.count() > 0 and btn.first.is_visible())
        btn.first.click()
        runner.survey_submitted = True
    runner.step("submit survey", submit)

    def confirm_returned():
        f = runner.chat_frame()
        btn = f.locator('[data-testid="fusionChatButton"]')
        runner.wait_until("back at the starting chat-icon screen",
                          lambda: btn.count() > 0 and btn.first.is_visible(), timeout=30)
    runner.step("confirm returned to start screen", confirm_returned)


# --------------------------------------------------------------------------- remote config + scheduler
# Config is a tiny CSV you can edit from a phone (Google Sheets "published to the web" as CSV, or a local
# file). One row per line, first column says what the row is:
#     setting,enabled,yes
#     setting,start_time,7:00 PM
#     setting,end_time,11:00 PM
#     setting,timezone,America/Chicago
#     agent,AT3469,3          <- agent ID, number of surveys to give this agent before it is retired
#     agent,BX1207,2
# Optional settings: cooldown_seconds (default 120), max_consecutive_skips (5), backoff_minutes (30),
# min_messages_per_chat (6), reply_delay_seconds (30), parallel_runs (1, max 10), worker_stagger_seconds (30),
# test_case_mode (random | sequential | fixed), test_case_file (for fixed),
# reuse_browser (skips = keep the browser after a non-target agent | always | never). Other rows are ignored.
UNLIMITED = 1_000_000      # "agent, ID, unlimited" in the Sheet


def show_quota(n):
    return "unlimited" if n >= UNLIMITED // 2 else n


def _span(sec):
    return f"{int(sec) // 60} min" if sec >= 60 else f"{int(sec)} s"


def end_reason(runner, entry, tc):
    """One plain sentence for the Log: what ended this chat."""
    o = entry.get("outcome")
    n = len(tc.get("scripted_messages") or [])
    sent = runner.msg_client
    if o == "skipped":
        return f"skipped - {runner.skip_reason}"
    if o == "interrupted":
        return f"stopped mid-chat ({STOP.get('why') or 'Ctrl+C'}) after {sent}/{n} bot messages - chat closed by the bot"
    if runner.transferred_to and o != "failed":
        return (f"transferred from {runner.agent_id} to {runner.transferred_to} after {sent}/{n} bot messages - "
                f"no survey (by design)")
    if o == "agent_ended":
        surv = "survey submitted" if runner.survey_submitted else ("survey shown" if runner.survey_shown else "no survey appeared")
        return f"the agent ended the chat after {sent}/{n} bot messages - {surv}"
    if o == "chat_no_survey":
        return f"script completed ({sent}/{n} bot messages) - the survey did not appear"
    if o == "survey_submitted":
        return "completed - survey submitted" + ("" if entry.get("chat_counted") else " (below the message threshold)")
    if o == "incomplete":
        return "survey appeared but was not submitted"
    if o == "failed":
        bad = next((st for st in reversed(runner.report) if st.get("status") == "failed"), None)
        step, err = (bad or {}).get("step", ""), (bad or {}).get("error", "")
        m = re.match(r"message (\d+)/", step)
        tail = " - the bot closed the chat" if runner.chat_ended else ""
        if runner.transferred_to:
            tail += f" (after a transfer to {runner.transferred_to})"
        if m and "greeting" in err:
            return f"the agent joined but never sent a greeting ({_span(runner.agent_wait_timeout)}){tail}"
        if m and "agent's reply" in err:
            return (f"no reply from the agent for {_span(runner.turn_wait_timeout)} after bot message "
                    f"{int(m.group(1)) - 1}/{n}{tail}")
        if m and "reply box" in err:
            return f"the reply box disappeared before bot message {m.group(1)}/{n} (chat closed on the site?){tail}"
        if step.startswith("wait for live agent"):
            return f"no agent joined within {_span(runner.agent_wait_timeout)}"
        if step in ("click close (X)", "choose End chat"):
            return f"could not close the chat at the end ({step})"
        if any(k in step for k in ("survey", "stars", "resolved", "comment", "start screen")):
            return f"problem in the survey step '{step}'"
        if not runner.agent_joined:
            return f"site problem before reaching an agent, at '{step}'"
        return f"bot error at '{step}': {err[:90]}"
    return o or ""


def is_unplanned(e):
    """A real conversation that didn't go as planned: transferred, the agent ended it, no survey, the bot failed or
    was stopped mid-chat, or too few messages to count. Not: skipped agents (not a target / quota reached), contacts
    with no messages at all, and normal completed chats with a survey."""
    if e.get("outcome") == "skipped" or not (e.get("messages_total") or 0):
        return False
    if e.get("outcome") == "survey_submitted" and e.get("chat_counted") and not e.get("transferred_to"):
        return False
    return True


def should_save(cfg, entry):
    """save_chats in the Sheet: none | all | unplanned | chat | outcome names | agent IDs (mix with ; or ,).
    e.g. "chat_no_survey; AT3469" saves every chat without a survey plus every chat with agent AT3469."""
    toks = {t.strip().lower() for t in re.split(r"[;,\s]+", str(cfg.get("save_chats") or "")) if t.strip()}
    if not toks or toks == {"none"} or "no" in toks:
        return False
    return ("all" in toks or str(entry.get("outcome", "")).lower() in toks
            or ("unplanned" in toks and is_unplanned(entry))
            or ("transferred" in toks and entry.get("transferred_to"))
            or ("chat" in toks and entry.get("chat_counted"))
            or str(entry.get("agent_id") or "").lower() in toks)


def parse_clock(text):
    m = re.fullmatch(r"\s*(\d{1,2})(?::(\d{2}))?\s*([AaPp][Mm])?\s*", str(text))
    if not m:
        raise ValueError(f"can't read time {text!r} (use e.g. 19:00 or 7:00 PM)")
    h, mi, ap_ = int(m.group(1)), int(m.group(2) or 0), (m.group(3) or "").lower()
    if ap_:
        h = h % 12 + (12 if ap_ == "pm" else 0)
    if not (0 <= h <= 23 and 0 <= mi <= 59):
        raise ValueError(f"time out of range: {text!r}")
    return h * 60 + mi


def fetch_config_text(src):
    if re.match(r"^https?://", src):
        import urllib.request
        req = urllib.request.Request(src, headers={"User-Agent": "qa-bot-config"})
        with urllib.request.urlopen(req, timeout=20) as r:
            return r.read().decode("utf-8-sig")
    return Path(src).read_text(encoding="utf-8-sig")


def load_config(src):
    import csv
    import io
    cfg = {"enabled": True, "start": "19:00", "end": "23:00", "tz": "America/Chicago",
           "cooldown_seconds": 120, "max_consecutive_skips": 5, "backoff_minutes": 30,
           "min_messages_per_chat": 6, "parallel_runs": 1, "worker_stagger_seconds": 30,
           "reply_delay_seconds": 30, "test_case_mode": "", "test_case_file": "",
           "reuse_browser": "skips", "save_chats": "unplanned; survey_submitted", "save_screenshots": False,
           "no_new_chats_last_minutes": 10, "agents": {}}
    for row in csv.reader(io.StringIO(fetch_config_text(src))):
        row = [c.strip() for c in row]
        if len(row) < 3 or not row[0]:
            continue
        kind, a, b = row[0].lower(), row[1], row[2]
        if kind == "setting":
            k = a.lower().replace(" ", "_")
            if k == "enabled":
                cfg["enabled"] = b.lower() in ("yes", "y", "true", "1", "on")
            elif k in ("start_time", "start"):
                cfg["start"] = b
            elif k in ("end_time", "end"):
                cfg["end"] = b
            elif k in ("timezone", "tz"):
                cfg["tz"] = b
            elif k in ("test_case_mode", "test_case_file"):
                cfg[k] = b
            elif k == "reuse_browser":
                cfg[k] = b.strip().lower() or "skips"
            elif k == "save_chats":
                cfg[k] = b.strip() or "none"
            elif k == "save_screenshots":
                cfg[k] = b.strip().lower() in ("yes", "y", "true", "1", "on")
            elif k in ("cooldown_seconds", "max_consecutive_skips", "backoff_minutes", "min_messages_per_chat",
                       "parallel_runs", "worker_stagger_seconds", "reply_delay_seconds",
                       "no_new_chats_last_minutes"):
                try:
                    cfg[k] = int(b)
                except ValueError:
                    print(f"[config] ignoring bad number for {k}: {b!r}")
        elif kind == "agent":
            if b.strip().lower() in ("unlimited", "no limit", "nolimit", "inf", "infinite", "*", "-1", "\u221e"):
                cfg["agents"][a.upper()] = UNLIMITED
                continue
            try:
                cfg["agents"][a.upper()] = int(b)
            except ValueError:
                print(f"[config] ignoring agent row {a!r}: surveys must be a number, got {b!r}")
    parse_clock(cfg["start"])
    parse_clock(cfg["end"])
    register_sensitive(*cfg["agents"])
    return cfg


def window_minutes_left(cfg):
    """Minutes until end_time (meaningful while the window is open)."""
    from zoneinfo import ZoneInfo
    now = datetime.now(ZoneInfo(cfg["tz"]))
    return (parse_clock(cfg["end"]) - (now.hour * 60 + now.minute)) % 1440


def window_status(cfg):
    """(is_open, text). Window may wrap past midnight (e.g. 22:00-02:00). Uses the config's time zone
    (America/Chicago handles CST/CDT automatically)."""
    from zoneinfo import ZoneInfo
    now = datetime.now(ZoneInfo(cfg["tz"]))
    cur = now.hour * 60 + now.minute
    s, e = parse_clock(cfg["start"]), parse_clock(cfg["end"])
    is_open = (s <= cur < e) if s <= e else (cur >= s or cur < e)
    return is_open, f"{now:%a %H:%M} {cfg['tz']} (window {cfg['start']}-{cfg['end']})"


MAX_PARALLEL = 10


@contextlib.contextmanager
def file_lock(path, timeout=30):
    """Tiny cross-process lock (works on Windows and Linux): the lock is a file that exists while held.
    A lock file older than `timeout` seconds is treated as stale (crashed worker) and removed."""
    path = str(path)
    while True:
        try:
            os.close(os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY))
            break
        except FileExistsError:
            try:
                if time.time() - os.path.getmtime(path) > timeout:
                    os.remove(path)
            except OSError:
                pass
            time.sleep(0.05)
    try:
        yield
    finally:
        try:
            os.remove(path)
        except OSError:
            pass


def load_state(path):
    """State = {"done": {agent: surveys}, "reserved": {agent: {worker_key: epoch}}}. 'reserved' are chats in
    flight right now, so parallel workers never over-assign an agent's last survey slots."""
    try:
        d = json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        d = {}
    if "done" not in d:               # older flat format {agent: n}
        d = {"done": d, "reserved": {}}
    d.setdefault("reserved", {})
    now = time.time()
    for ag in list(d["reserved"]):    # drop reservations from workers that crashed (>60 min old)
        d["reserved"][ag] = {k: t for k, t in d["reserved"][ag].items() if now - t < 3600}
    return d


def save_state(path, state):
    Path(path).write_text(json.dumps(state, indent=2, sort_keys=True), encoding="utf-8")


def disable_quickedit():
    """Windows: clicking inside a console window starts 'QuickEdit' text selection, which FREEZES the program
    until you press Enter/Esc (and that Enter would also toggle the bot's pause). Turn it off for this window."""
    if os.name != "nt":
        return
    try:
        import ctypes
        from ctypes import wintypes
        k32 = ctypes.windll.kernel32
        h = k32.GetStdHandle(-10)
        mode = wintypes.DWORD()
        if k32.GetConsoleMode(h, ctypes.byref(mode)):
            k32.SetConsoleMode(h, (mode.value & ~0x0040) | 0x0080)
    except Exception:  # noqa: BLE001
        pass


_WIN_HANDLERS = []


def on_window_close(callback):
    """Windows: run `callback` (quickly - Windows allows about 5 s) when this console window is closed, the user
    logs off or the PC shuts down. Used to release the agent's slot and log the chat as interrupted."""
    if os.name != "nt":
        return
    try:
        import ctypes
        from ctypes import wintypes
        HANDLER = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.DWORD)

        def _h(event):
            if event in (2, 5, 6):        # CTRL_CLOSE_EVENT, CTRL_LOGOFF_EVENT, CTRL_SHUTDOWN_EVENT
                try:
                    callback()
                except Exception:  # noqa: BLE001
                    pass
            return False
        _WIN_HANDLERS.append(HANDLER(_h))
        ctypes.windll.kernel32.SetConsoleCtrlHandler(_WIN_HANDLERS[-1], True)
    except Exception:  # noqa: BLE001
        pass


def idle(seconds):
    end = time.time() + seconds
    while time.time() < end and not (STOP["now"] or STOP["after_chat"]):
        time.sleep(1)


def start_pause_listener():
    """Press Enter in this window to pause/resume. While paused the bot types/clicks nothing and (between
    chats) won't start a new one, so you can take over in the browser."""
    import threading

    def loop():
        while True:
            try:
                line = input().strip().lower()
            except EOFError:
                return
            if line in ("stop", "s", "q", "quit"):
                STOP["now"] = True
                STOP["why"] = STOP["why"] or "typed stop"
                Runner.PAUSED = False
                print("   *** STOP: ending the current chat cleanly (no survey), saving the result, then exiting ***")
                continue
            if line in ("finish", "f"):
                STOP["after_chat"] = True
                print("   *** FINISH: the current chat will be completed normally (incl. survey), then the bot exits ***")
                continue
            if not Runner.PAUSED:
                Runner.PAUSED = True
                print("\n   *** PAUSED - the bot will not type or click. Chat manually in the browser. "
                      "Press Enter again to resume. ***")
            else:
                Runner.PAUSED = False
                print("   *** RESUMED ***")
    if not sys.stdin or not sys.stdin.isatty():
        return
    threading.Thread(target=loop, daemon=True).start()
    print("[keys] Enter = pause/resume | type finish + Enter = finish this chat, then exit | "
          "type stop + Enter (or Ctrl+C) = end this chat now and exit\n")


def run_once(page, args, tc, gate, reply_delay=30, reuse=False):
    suffix = f"_w{args.worker}" if args.worker else ""
    run_dir = Path(args.out) / (datetime.now().strftime("%Y%m%d_%H%M%S") + suffix)
    run_dir.mkdir(parents=True, exist_ok=True)
    print(f"Output: {run_dir}")
    runner = Runner(page, args.chat_frame_name, run_dir, args.step_timeout,
                    agent_wait_timeout_s=args.agent_wait_timeout,
                    turn_wait_timeout_s=args.turn_wait_timeout)
    runner.gate = gate
    runner.reply_delay = reply_delay
    runner.failed = False
    runner.run_dir = run_dir
    # unique per chat: lets the Sheet recognise a retried call (slow network) instead of acting on it twice
    runner.chat_id = f"{run_dir.name}-{os.getpid()}-{random.randint(1000, 9999)}"
    try:
        run_test_case(runner, tc, tc.get("start_url") or args.url, reuse=reuse)
    except StopNow:
        runner.failed = runner.interrupted = True
        wrap_up(runner, tc, STOP["why"] or "stopped by you")
    except KeyboardInterrupt:              # forced (second Ctrl+C): don't touch the browser again
        runner.failed = runner.interrupted = True
    except Exception:  # noqa: BLE001
        runner.failed = True
        print("\n*** TEST CASE FAILED - see error.txt and the _FAILED screenshot ***")
        wrap_up(runner, tc, "the bot failed mid-chat")
    finally:
        runner.finish()
    return runner


def wrap_up(runner, tc, why):
    """Best effort after a failure or Ctrl+C: close the chat properly (X > End chat) so the agent isn't left
    holding an open chat. No survey. Never raises."""
    if runner.chat_ended or not runner.agent_joined:
        return
    try:
        f = runner.page.frame(name=runner.chat_frame_name)
        if f is None:
            return
        f.locator('[data-testid="fusion-header-close-btn"]').first.click(timeout=6000)
        runner.t_chat_end = runner.t_chat_end or time.time()
        f.get_by_text(tc["end_chat_button_text"], exact=True).first.click(timeout=6000)
        runner.chat_ended = True
        print(f"         ended the chat on the agent's side ({why})")
    except Exception as e:  # noqa: BLE001
        print(f"         could not end the chat cleanly ({str(e)[:100]})")


def readable_transcript(runner, tc, entry=None):
    """transcript.txt: the whole interaction as plain text, with a summary header, for analysis."""
    def mmss(v):
        return "-" if v is None else f"{int(v) // 60}m {int(v) % 60:02d}s"
    t = runner.timings()
    e = entry or {}
    lines = [
        f"QA bot chat  {e['time'] + ' (Sheet time zone)' if e.get('time') else time.strftime('%Y-%m-%d %H:%M:%S (PC time)')}",
        f"Test case:   {tc.get('name')} ({tc.get('_file', '')})",
        f"Agent:       {runner.last_agent_from or '-'}   ID: {runner.agent_id or '-'}"
        + (f"   TRANSFERRED: {' -> '.join(runner.agents_seen)}" if runner.transferred_to else "")
        + ("   AGENT ENDED THE CHAT before the script finished" if runner.agent_ended_chat else ""),
        f"Outcome:     {e.get('outcome', 'failed' if runner.failed else 'finished')}"
        + (f"  ({e['reason']})" if e.get("reason") else ""),
        f"Ended by:    {e.get('end_reason', '-')}",
        f"Messages:    {runner.msg_client + runner.agent_msg_count} (bot/customer {runner.msg_client}, "
        f"agent {runner.agent_msg_count})" + (f"   counted as a chat: {'yes' if e.get('chat_counted') else 'no'}"
                                              if entry else ""),
        f"Survey:      {'submitted' if runner.survey_submitted else ('shown, not submitted' if runner.survey_shown else 'not shown')}",
        f"Times:       whole run {mmss(t['duration_total_s'])} | waiting for an agent {mmss(t['queue_wait_s'])} | "
        f"conversation {mmss(t['chat_duration_s'])} | agent's first message after joining "
        f"{mmss(t['first_reply_s'])} | agent's average reply {mmss(t['avg_agent_reply_s'])}",
        "", "Conversation (PC clock):", "",
    ]
    for r in runner.transcript_rows:
        ft = r.get("from_type")
        who = {"Agent": f"AGENT ({r.get('from_name') or '?'})",
               "Client": "BOT" if r.get("by", "bot") == "bot" else "YOU (typed by hand)"}.get(ft, ft or "SYSTEM")
        if r.get("type") == "Message":
            lines.append(f"[{r.get('clock', '')}] {who}: {r.get('text') or ''}")
        else:
            lines.append(f"[{r.get('clock', '')}] -- {who}: {r.get('type')}")
    steps = getattr(runner, "report", [])
    if steps:
        lines += ["", "Bot steps:"]
        lines += [f"  {s_['status']:6} {s_['seconds']:6.1f}s  {s_['step']}" + (f"  ERROR: {s_['error']}" if s_.get("error") else "")
                  for s_ in steps]
    return "\n".join(lines) + "\n"


def fmt_dur(v):
    return "" if not v else f"{int(v) // 60}m {int(v) % 60:02d}s"


def session_date(dt, cfg):
    """The 'day' a chat belongs to. For a window that wraps past midnight (22:00-02:00) the small hours
    still belong to the previous day's session."""
    s_, e_ = parse_clock(cfg["start"]), parse_clock(cfg["end"])
    if s_ > e_ and (dt.hour * 60 + dt.minute) < e_:
        dt = dt - timedelta(days=1)
    return dt.date().isoformat()


def write_report(log_path, out_root, date_key, cfg):
    """Rebuild the day's report from scheduler_log.jsonl (so it is always consistent with the log).
    Writes report_<date>.html (readable on a phone) plus two CSVs. Returns the html path."""
    import csv
    import html as H
    entries = []
    try:
        for line in Path(log_path).read_text(encoding="utf-8").splitlines():
            e = json.loads(line)
            if e.get("session_date") == date_key:
                entries.append(e)
    except FileNotFoundError:
        pass
    agents = {}
    for e in entries:
        a = agents.setdefault(e.get("agent_id") or "(unknown)",
                              {"name": e.get("agent_name") or "", "contacted": 0, "chats": 0,
                               "surveys": 0, "chats_no_survey": 0, "not_a_chat": 0, "messages": 0,
                               "chat_secs": 0, "timed": 0, "reply_secs": 0, "replied": 0,
                               "transfers": 0, "unplanned": 0})
        a["contacted"] += 1
        a["messages"] += e.get("messages_total", 0)
        a["transfers"] += 1 if e.get("transferred_to") else 0
        a["unplanned"] += 1 if e.get("unplanned") else 0
        if e.get("chat_counted"):
            a["chats"] += 1
            if e.get("chat_duration_s"):
                a["chat_secs"] += e["chat_duration_s"]
                a["timed"] += 1
            if e.get("avg_agent_reply_s"):
                a["reply_secs"] += e["avg_agent_reply_s"]
                a["replied"] += 1
            if e.get("survey_submitted"):
                a["surveys"] += 1
            else:
                a["chats_no_survey"] += 1
        else:
            a["not_a_chat"] += 1
    rep_dir = Path(out_root) / "reports"
    rep_dir.mkdir(parents=True, exist_ok=True)
    base = rep_dir / f"report_{date_key}"
    avg_min = lambda a: round(a["chat_secs"] / a["timed"] / 60, 1) if a["timed"] else ""  # noqa: E731
    avg_rep = lambda a: round(a["reply_secs"] / a["replied"]) if a["replied"] else ""  # noqa: E731
    cols = ["agent_id", "agent_name", "times_contacted", "chats", "surveys_given", "chats_without_survey",
            "contacts_below_chat_threshold", "messages_total", "avg_chat_minutes", "avg_agent_reply_seconds",
            "transferred_away", "unplanned_chats"]
    with open(f"{base}_agents.csv", "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(cols)
        for aid, a in sorted(agents.items()):
            w.writerow([aid, a["name"], a["contacted"], a["chats"], a["surveys"], a["chats_no_survey"],
                        a["not_a_chat"], a["messages"], avg_min(a), avg_rep(a), a["transfers"], a["unplanned"]])
    dcols = ["time", "agent_id", "agent_name", "outcome", "chat_counted", "messages_total",
             "messages_bot", "messages_agent", "worker", "test_case", "survey_shown", "survey_submitted", "note", "run_dir",
             "duration_total_s", "queue_wait_s", "chat_duration_s", "first_reply_s", "avg_agent_reply_s", "saved_to",
             "transferred_to", "unplanned"]
    with open(f"{base}_chats.csv", "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(dcols)
        for e in entries:
            w.writerow([e.get("time"), e.get("agent_id"), e.get("agent_name"), e.get("outcome"),
                        e.get("chat_counted"), e.get("messages_total"), e.get("messages_client"),
                        e.get("messages_agent"), e.get("worker"), e.get("test_case"), e.get("survey_shown"), e.get("survey_submitted"),
                        e.get("reason") or "", e.get("run_dir"), e.get("duration_total_s"), e.get("queue_wait_s"),
                        e.get("chat_duration_s"), e.get("first_reply_s"), e.get("avg_agent_reply_s"),
                        e.get("saved_to") or "", e.get("transferred_to") or "", bool(e.get("unplanned"))])
    tot = lambda k: sum(a[k] for a in agents.values())  # noqa: E731
    win = f"{cfg['start']} - {cfg['end']} ({cfg['tz']})"
    thr = cfg["min_messages_per_chat"]
    rows = "".join(
        f"<tr><td>{H.escape(aid)}</td><td>{H.escape(a['name'])}</td><td>{a['contacted']}</td><td>{a['chats']}</td>"
        f"<td>{a['surveys']}</td><td>{a['chats_no_survey']}</td><td>{a['not_a_chat']}</td>"
        f"<td>{avg_min(a)}</td><td>{avg_rep(a)}</td><td>{a['transfers']}</td><td>{a['unplanned']}</td></tr>"
        for aid, a in sorted(agents.items()))
    drows = "".join(
        f"<tr><td>{H.escape(str(e.get('time', ''))[11:19])}</td><td>{H.escape(str(e.get('agent_id') or ''))}</td>"
        f"<td>{H.escape(str(e.get('outcome')))}{' &rarr; ' + H.escape(str(e['transferred_to'])) if e.get('transferred_to') else ''}</td><td>{e.get('messages_total', 0)}</td>"
        f"<td>{fmt_dur(e.get('chat_duration_s'))}</td>"
        f"<td>{'yes' if e.get('chat_counted') else 'no'}</td>"
        f"<td>{'submitted' if e.get('survey_submitted') else ('shown, not submitted' if e.get('survey_shown') else 'not shown')}</td>"
        f"<td>{H.escape(str(e.get('reason') or ''))}</td></tr>" for e in entries)
    doc = f"""<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>QA bot report {date_key}</title><style>
:root{{color-scheme:light dark;font-family:system-ui,sans-serif}}body{{margin:16px;max-width:900px}}
table{{border-collapse:collapse;width:100%;margin:8px 0 24px}}td,th{{border-bottom:1px solid #8884;padding:6px 8px;text-align:left}}
.k{{display:flex;gap:12px;flex-wrap:wrap}}.k div{{border:1px solid #8886;border-radius:8px;padding:8px 14px}}.k b{{display:block;font-size:22px}}
.w{{overflow-x:auto}}</style></head><body>
<h1>QA bot report - {date_key}</h1><p>Run window: {H.escape(win)}. A contact counts as a <b>chat</b> when at least {thr} messages were exchanged in total (bot + agent).</p>
<div class="k"><div><b>{len(agents)}</b>agents contacted</div><div><b>{len(entries)}</b>contacts</div>
<div><b>{tot('chats')}</b>chats</div><div><b>{tot('surveys')}</b>surveys given</div><div><b>{tot('chats_no_survey')}</b>chats with no survey</div><div><b>{tot('transfers')}</b>transfers</div><div><b>{tot('unplanned')}</b>unplanned chats</div></div>
<h2>By agent</h2><div class="w"><table><tr><th>Agent ID</th><th>Name</th><th>Contacted</th><th>Chats</th><th>Surveys given</th><th>Chats, no survey</th><th>Below chat threshold</th><th>Avg chat (min)</th><th>Avg agent reply (s)</th><th>Transferred away</th><th>Unplanned</th></tr>{rows}</table></div>
<h2>Every contact</h2><div class="w"><table><tr><th>Time</th><th>Agent</th><th>Outcome</th><th>Messages</th><th>Chat time</th><th>Counted as chat</th><th>Survey</th><th>Note</th></tr>{drows}</table></div>
</body></html>"""
    Path(f"{base}.html").write_text(doc, encoding="utf-8")
    return f"{base}.html"


# --------------------------------------------------------------------------- settings file (bot_settings.json)
DEFAULT_SETTINGS = {
    "start_url": "https://www.att.com/support",
    "sheet_csv_url": "",        # the Google Sheet "Config" tab, published to the web as CSV (or a local .csv path)
    "counter_url": "",          # the Apps Script web-app URL (shared live counters, Log and Report tabs)
    "counter_token": "",        # the secret word you set as TOKEN in the Apps Script project
    "test_cases_dir": "test_cases",
    "test_case_mode": "random",  # random | sequential | fixed  (the Sheet can override this)
    "test_case_file": "",        # used when test_case_mode is fixed, e.g. "test_case_1.json"
    "mask_agent_ids": False,
    "exit_after_window": False,
    "local_report": True,
    "state_file": "agent_state.json",
    "output_dir": "qa_bot_runs",
}


def _truthy(v):
    return str(v).strip().lower() in ("1", "true", "yes", "y", "on")


def _blank(v):
    return not str(v or "").strip() or str(v).strip().upper().startswith("PASTE")


def load_settings(path):
    s = dict(DEFAULT_SETTINGS)
    p = Path(path)
    if p.exists():
        try:
            s.update({k: v for k, v in json.loads(p.read_text(encoding="utf-8-sig")).items()
                      if not k.startswith("_")})
        except Exception as e:  # noqa: BLE001
            sys.exit(f"{p} is not valid JSON: {e}")
    if os.environ.get("QA_MODE", "").strip().lower() == "github":
        s.update(mask_agent_ids=True, exit_after_window=True, local_report=False)
    for env, key in {"QA_START_URL": "start_url", "QA_SHEET_CSV_URL": "sheet_csv_url",
                     "QA_COUNTER_URL": "counter_url", "QA_COUNTER_TOKEN": "counter_token",
                     "QA_TEST_CASE_MODE": "test_case_mode"}.items():
        if os.environ.get(env, "").strip():
            s[key] = os.environ[env].strip()
    for env, key in {"QA_MASK_AGENT_IDS": "mask_agent_ids", "QA_EXIT_AFTER_WINDOW": "exit_after_window",
                     "QA_LOCAL_REPORT": "local_report"}.items():
        if os.environ.get(env, "").strip():
            s[key] = _truthy(os.environ[env])
    for key in ("sheet_csv_url", "counter_url", "counter_token", "test_case_file"):
        if _blank(s.get(key)):
            s[key] = ""
    base = p.resolve().parent
    if not Path(s["test_cases_dir"]).is_absolute():
        s["test_cases_dir"] = str(base / s["test_cases_dir"])
    if s["sheet_csv_url"] and not re.match(r"^https?://", s["sheet_csv_url"]) \
            and not Path(s["sheet_csv_url"]).is_absolute():
        s["sheet_csv_url"] = str(base / s["sheet_csv_url"])
    return s


# --------------------------------------------------------------------------- test cases (test_cases/*.json)
REQUIRED_TC_KEYS = ("initial_message", "menu_path", "scripted_messages", "end_chat_button_text", "survey")


def load_test_case(path):
    tc = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    missing = [k for k in REQUIRED_TC_KEYS if k not in tc]
    if missing:
        raise ValueError(f"missing {', '.join(missing)}")
    missing = [k for k in ("stars", "resolved", "comment") if k not in tc["survey"]]
    if missing:
        raise ValueError(f"survey is missing {', '.join(missing)}")
    if not tc["scripted_messages"]:
        raise ValueError("scripted_messages is empty")
    tc.setdefault("name", Path(path).stem)
    tc["_file"] = Path(path).name
    return tc


def list_test_cases(folder):
    out = []
    for p in sorted(Path(folder).glob("*.json")):
        if p.name.startswith("_"):        # _template.json and other notes are ignored
            continue
        try:
            out.append(load_test_case(p))
        except Exception as e:  # noqa: BLE001
            print(f"[test cases] skipping {p.name}: {e}")
    return out


class TestCasePicker:
    """random (default, never the same one twice in a row when there are 2+), sequential, or fixed."""

    def __init__(self, args, S):
        self.args, self.S = args, S
        self.last = None
        self.i = max(0, args.worker - 1)       # parallel workers start at different places in sequential mode

    def pick(self, cfg=None):
        if self.args.test_case:
            return load_test_case(self.args.test_case)
        cfg = cfg or {}
        mode = (cfg.get("test_case_mode") or self.S["test_case_mode"] or "random").strip().lower()
        fixed = (cfg.get("test_case_file") or self.S["test_case_file"] or "").strip()
        cases = list_test_cases(self.S["test_cases_dir"])
        if not cases:
            raise SystemExit(f"no valid test cases found in {self.S['test_cases_dir']}")
        if mode == "fixed":
            for c in cases:
                if fixed in (c["_file"], c["_file"][:-5], c["name"]):
                    return c
            print(f"[test cases] fixed test case {fixed!r} not found - picking at random instead")
        if mode == "sequential":
            c = cases[self.i % len(cases)]
            self.i += 1
            return c
        c = random.choice([c for c in cases if c["_file"] != self.last] or cases)
        self.last = c["_file"]
        return c


# --------------------------------------------------------------------------- quota counters
class LocalBackend:
    """Counters in a JSON file on this computer, protected by a lock file (parallel workers on ONE machine)."""
    name = "local file"

    def __init__(self, state_path):
        self.state_path, self.lock = state_path, state_path + ".lock"

    def room(self, agents):
        st = load_state(self.state_path)
        return {a: q - st["done"].get(a, 0) - len(st["reserved"].get(a, {})) for a, q in agents.items()}

    def reserve(self, agent, quota, wkey, chat_id=None):
        key = chat_id or wkey
        with file_lock(self.lock):
            st = load_state(self.state_path)
            mine = st["reserved"].get(agent, {})
            if key in mine:
                return True, st["done"].get(agent, 0) + len(mine)
            used = st["done"].get(agent, 0) + len(mine)
            if used >= quota:
                return False, used
            st["reserved"].setdefault(agent, {})[key] = time.time()
            save_state(self.state_path, st)
            return True, used + 1

    def commit(self, agent, wkey, submitted, entry, fast=False):
        if not agent:
            return None
        with file_lock(self.lock):
            st = load_state(self.state_path)
            st["reserved"].get(agent, {}).pop(entry.get("chat_id") or wkey, None)
            if submitted:
                st["done"][agent] = st["done"].get(agent, 0) + 1
            save_state(self.state_path, st)
            return st["done"].get(agent, 0)


class RemoteBackend:
    """Counters, Log and Report in the Google Sheet, through the Apps Script web app (google_sheet/Code.gs).
    Works across separate machines (GitHub Actions jobs, several PCs). The script takes a lock for every call,
    so checking an agent's quota and reserving a slot is one atomic step."""
    name = "Google Sheet (Apps Script)"

    def __init__(self, url, token, pending_path):
        self.url, self.token, self.pending = url, token, str(pending_path)

    def _call(self, action, tries=4, timeout=100, **payload):
        import urllib.request
        body = json.dumps({"token": self.token, "action": action, **payload}).encode("utf-8")
        last = None
        for i in range(tries):
            try:
                req = urllib.request.Request(self.url, data=body, method="POST",
                                             headers={"Content-Type": "text/plain;charset=utf-8"})
                with urllib.request.urlopen(req, timeout=timeout) as r:
                    text = r.read().decode("utf-8")
                try:
                    data = json.loads(text)
                except ValueError:
                    raise RuntimeError("the counter URL did not return JSON - is it the web app /exec URL, "
                                       "deployed with access 'Anyone'?") from None
                if not data.get("ok"):
                    raise RuntimeError(data.get("error") or "counter service returned an error")
                return data
            except Exception as e:  # noqa: BLE001
                last = e
                if "bad token" in str(e) or i == tries - 1:
                    break
                time.sleep(3 * (i + 1) + random.uniform(0, 5))   # spread retries out so workers don't pile up
        raise RuntimeError(f"counter service: {str(last)[:200]}")

    def room(self, agents):
        r = self._call("room", agents=agents)["room"]
        return {str(k).upper(): int(v) for k, v in r.items()}

    def reserve(self, agent, quota, wkey, chat_id=None):
        d = self._call("reserve", agent=agent, quota=quota, worker=wkey, chat_id=chat_id)
        return bool(d.get("granted")), d.get("used")

    def commit(self, agent, wkey, submitted, entry, fast=False):
        payload = {"agent": agent, "worker": wkey, "submitted": bool(submitted), "entry": entry,
                   "chat_id": entry.get("chat_id")}
        try:
            d = self._call("commit", tries=1, timeout=4, **payload) if fast else self._call("commit", **payload)
            if not fast:
                self.flush()
            return d.get("done")
        except Exception as e:  # noqa: BLE001
            print(f"[counter] could not save this chat to the Sheet ({e}); kept in {self.pending} and "
                  f"will retry after the next chat")
            with file_lock(self.pending + ".lock"):
                with open(self.pending, "a", encoding="utf-8") as f:
                    f.write(json.dumps(payload) + "\n")
            return None

    def save_chat(self, date, name, files, images):
        """Upload one chat's files to the 'QA bot chats' folder in your Google Drive; returns the folder link."""
        return self._call("save", tries=2, timeout=180, date=date, name=name, files=files, images=images).get("url")

    def flush(self):
        p = Path(self.pending)
        if not p.exists():
            return
        with file_lock(self.pending + ".lock"):
            keep = []
            for ln in p.read_text(encoding="utf-8").splitlines():
                if not ln.strip():
                    continue
                try:
                    self._call("commit", tries=2, **json.loads(ln))
                except Exception:  # noqa: BLE001
                    keep.append(ln)
            if keep:
                p.write_text("\n".join(keep) + "\n", encoding="utf-8")
            else:
                p.unlink()


def make_backend(args, S):
    if S["counter_url"]:
        if not S["counter_token"]:
            sys.exit("counter_url is set but counter_token is empty (it must match TOKEN in the Apps Script)")
        Path(args.out).mkdir(parents=True, exist_ok=True)
        return RemoteBackend(S["counter_url"], S["counter_token"], Path(args.out) / "unsent_to_sheet.jsonl")
    if os.environ.get("QA_MODE", "").strip().lower() == "github":
        sys.exit("GitHub mode needs the QA_COUNTER_URL and QA_COUNTER_TOKEN secrets: parallel jobs run on "
                 "separate machines, so the survey counters must live in the Google Sheet.")
    return LocalBackend(args.state)


def save_chat_copy(runner, entry, cfg, backend, args, S, tag=""):
    """Keep this chat for analysis: a copy in qa_bot_runs/saved_chats/ (PC) and/or a folder in Google Drive
    (when the Apps Script counter is set up). Returns where it went (Drive link preferred)."""
    import shutil
    name = f"{runner.run_dir.name}_{entry.get('agent_id') or 'unknown'}_{entry.get('outcome')}"
    where = ""
    if S["local_report"]:
        try:
            dest = Path(args.out) / "saved_chats" / str(entry.get("session_date")) / name
            shutil.copytree(runner.run_dir, dest, dirs_exist_ok=True)
            where = str(dest)
        except Exception as e:  # noqa: BLE001
            print(f"{tag}could not copy the chat to saved_chats ({e})")
    if isinstance(backend, RemoteBackend):
        import base64
        files = {}
        for fn in ("transcript.txt", "transcript.jsonl", "report.json", "raw_ws.jsonl", "error.txt"):
            fp = runner.run_dir / fn
            if fp.exists():
                files[fn] = fp.read_text(encoding="utf-8", errors="replace")[:5_000_000]
        images = {}
        if cfg.get("save_screenshots"):
            for fp in sorted(runner.run_dir.glob("*.png")):
                images[fp.name] = base64.b64encode(fp.read_bytes()).decode("ascii")
        try:
            where = backend.save_chat(str(entry.get("session_date")), name, files, images) or where
        except Exception as e:  # noqa: BLE001
            print(f"{tag}could not save the chat to Google Drive ({str(e)[:120]})")
    if where:
        print(f"{tag}chat saved for analysis: {where}")
    return where


# --------------------------------------------------------------------------- scheduler (one worker)
def scheduler_loop(pw, args, S):
    ctl = {"unreported": False, "cfg": None, "date": None, "br": {"ctx": None, "page": None, "prof": None}}
    log_path = Path(args.out) / "scheduler_log.jsonl"
    backend = make_backend(args, S)
    picker = TestCasePicker(args, S)
    print(f"[w{args.worker}] " if args.worker else "[scheduler] ", f"survey counters: {backend.name}", sep="")
    try:
        return _scheduler_body(pw, args, S, ctl, log_path, backend, picker)
    finally:
        if isinstance(backend, RemoteBackend) and Path(backend.pending).exists():
            try:
                backend.flush()
                print("[scheduler] sent the results that were waiting to reach the Sheet")
            except Exception:  # noqa: BLE001
                print(f"[scheduler] some results could not reach the Sheet; kept in {backend.pending} "
                      f"(they are sent automatically next time the bot runs)")
        br = ctl["br"]
        if br["ctx"] is not None:
            try:
                br["ctx"].close()
            except Exception:  # noqa: BLE001
                pass
        if br["prof"]:
            import shutil
            shutil.rmtree(br["prof"], ignore_errors=True)
        if S["local_report"] and ctl["unreported"] and ctl["cfg"] and ctl["date"]:
            try:
                with file_lock(args.state + ".lock"):
                    path = write_report(log_path, args.out, ctl["date"], ctl["cfg"])
                print(f"[scheduler] local report for {ctl['date']}: {path}")
            except Exception as e:  # noqa: BLE001
                print(f"[scheduler] could not write the local report: {e}")


def _scheduler_body(pw, args, S, ctl, log_path, backend, picker):
    import shutil
    import tempfile
    lock_path = args.state + ".lock"
    wkey = f"w{args.worker}-{os.environ.get('GITHUB_RUN_ID', 'local')}-{os.getpid()}"
    stop_file = Path(args.out) / "workers" / f"stop_{args.worker}"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    last_cfg, last_msg = None, None
    skips_in_row, fails_in_row, runs = 0, 0, 0
    started, seen_open, staggered = time.time(), False, False
    room_cache = {"t": 0, "room": {}, "agents": None}
    # GitHub kills a job at its time limit with no warning. QA_JOB_MINUTES (set by the workflow) lets the worker
    # stop starting chats well before that and end a chat in progress cleanly a few minutes before the cut-off.
    budget_min = float(os.environ.get("QA_JOB_MINUTES") or 0)
    hard_stop = started + budget_min * 60 if budget_min > 0 else None
    wrap_s = float(os.environ.get("QA_JOB_WRAPUP_S") or 240)            # end the chat this long before the cut-off
    newchat_s = float(os.environ.get("QA_JOB_NEWCHAT_CUTOFF_MIN") or 25) * 60   # no new chat with less time left
    if hard_stop:
        import threading

        def _job_clock():
            while hard_stop - time.time() > wrap_s:
                time.sleep(min(10, max(0.5, hard_stop - time.time() - wrap_s)))
            if not STOP["now"]:
                STOP["why"] = "GitHub job time limit"
                STOP["now"] = True
                print(f"{tag}GitHub job time almost up - ending the current chat cleanly; the next scheduled run "
                      f"continues.")
        threading.Thread(target=_job_clock, daemon=True).start()
    tag = f"[w{args.worker}] " if args.worker else "[scheduler] "
    br = ctl["br"]

    def release_on_close():
        r = Runner.current
        if r is not None and r.reserved_agent and not r.committed:
            r.committed = True
            e = {"time": time.strftime("%Y-%m-%d %H:%M:%S"), "session_date": ctl.get("date") or time.strftime("%Y-%m-%d"),
                 "worker": args.worker, "agent_id": r.agent_id or "", "agent_name": r.last_agent_from or "",
                 "outcome": "interrupted", "reason": "bot window closed mid-chat", "chat_counted": False,
                 "survey_shown": r.survey_shown, "survey_submitted": False, "chat_id": getattr(r, "chat_id", "")}
            e.update(r.timings())
            backend.commit(r.reserved_agent, wkey, False, e, fast=True)
    on_window_close(release_on_close)

    def close_browser():
        if br["ctx"] is not None:
            try:
                br["ctx"].close()
            except Exception:  # noqa: BLE001
                pass
        if br["prof"]:
            shutil.rmtree(br["prof"], ignore_errors=True)
        br.update(ctx=None, page=None, prof=None)

    def say(msg):
        nonlocal last_msg
        if msg != last_msg:        # only print status when it changes, so the console isn't spammed
            print(f"{tag}{msg}")
            last_msg = msg

    def gate(agent_id):
        """Atomically check the agent's quota (done + chats in progress) and reserve a slot if there's room."""
        quota = ctl["cfg"]["agents"].get(agent_id)
        if quota is None:
            return False, f"agent {agent_id} is not a target"
        try:
            r = Runner.current
            ok, used = backend.reserve(agent_id, quota, wkey, getattr(r, "chat_id", None))
        except Exception as e:  # noqa: BLE001
            return False, f"counter service unreachable ({str(e)[:80]})"
        if not ok:
            return False, f"agent {agent_id} already has {used}/{show_quota(quota)} surveys (incl. chats in progress)"
        return True, None

    def closed_should_exit():
        if not S["exit_after_window"]:
            return False
        if seen_open:
            print(f"{tag}run window is over - exiting.")
            return True
        if time.time() - started > args.window_grace_minutes * 60:
            print(f"{tag}run window did not open within {args.window_grace_minutes} min - exiting.")
            return True
        return False

    while True:
        if args.max_runs and runs >= args.max_runs:
            print(f"{tag}reached --max-runs {args.max_runs}, stopping.")
            return 0
        if args.worker and stop_file.exists():
            print(f"{tag}stop requested (parallel_runs lowered); exiting after the last chat.")
            return 0
        if STOP["now"] or STOP["after_chat"]:
            print(f"{tag}stopped as requested.")
            return 0
        if Runner.PAUSED:
            time.sleep(1)
            continue
        if isinstance(backend, RemoteBackend) and Path(backend.pending).exists():
            try:
                backend.flush()          # results that couldn't reach the Sheet earlier
            except Exception:  # noqa: BLE001
                pass
        try:
            cfg = load_config(args.config)
            last_cfg = cfg
            ctl["cfg"] = cfg
        except Exception as e:  # noqa: BLE001
            if last_cfg is None:
                say(f"can't read the config sheet yet ({str(e)[:120]}); retrying in 60s")
                idle(60)
                continue
            cfg = last_cfg
            say(f"config fetch failed ({str(e)[:80]}); using the last good copy")
        if args.worker and args.worker > max(1, min(cfg["parallel_runs"], MAX_PARALLEL)):
            print(f"{tag}not needed (parallel_runs = {cfg['parallel_runs']}) - exiting.")
            return 0
        if not staggered:              # GitHub starts all jobs at once: spread them out like the local launcher
            staggered = True
            if S["exit_after_window"] and args.worker > 1 and cfg["worker_stagger_seconds"] > 0:
                idle((args.worker - 1) * cfg["worker_stagger_seconds"])
                continue
        is_open, wtxt = window_status(cfg)
        closing = is_open and window_minutes_left(cfg) <= cfg["no_new_chats_last_minutes"]
        if closing:
            is_open = False
            wtxt += f" - last {cfg['no_new_chats_last_minutes']} min of the window, no new chats"
        if not cfg["enabled"] or not is_open:
            if ctl["unreported"] and S["local_report"]:      # the day's window just ended -> final local report
                with file_lock(lock_path):
                    rp = write_report(log_path, args.out, ctl["date"], cfg)
                print(f"{tag}window closed. Local report for {ctl['date']}: {rp}")
                ctl["unreported"] = False
            close_browser()
            if closed_should_exit():
                return 0
            say("disabled in the sheet (enabled = no); checking again in 60s" if not cfg["enabled"]
                else f"outside the run window - now {wtxt}; waiting")
            idle(60)
            continue
        seen_open = True
        try:
            if room_cache["t"] and time.time() - room_cache["t"] < 90 and room_cache["agents"] == cfg["agents"]:
                room = room_cache["room"]
            else:
                room = {a: n for a, n in backend.room(cfg["agents"]).items() if n > 0}
                room_cache.update(t=time.time(), room=room, agents=dict(cfg["agents"]))
        except Exception as e:  # noqa: BLE001
            say(f"counter service unreachable ({str(e)[:100]}); retrying in 60s")
            idle(60)
            continue
        if not room:
            close_browser()
            say("every agent in the list has reached its survey quota (or the list is empty); waiting for "
                "sheet changes")
            if S["exit_after_window"] and time.time() - started > 60:
                print(f"{tag}nothing left to do this run - exiting.")
                return 0
            idle(60)
            continue
        if skips_in_row >= cfg["max_consecutive_skips"]:
            close_browser()          # after the back-off the next chat starts in a fresh browser
            say(f"{skips_in_row} chats in a row reached non-target agents; backing off "
                f"{cfg['backoff_minutes']} min so other agents aren't tied up")
            idle(cfg["backoff_minutes"] * 60)
            skips_in_row = 0
            continue

        if hard_stop and hard_stop - time.time() < newchat_s:
            print(f"{tag}less than {newchat_s / 60:.0f} min left in this GitHub job - not starting another chat; the next scheduled "
                  f"run takes over.")
            return 0
        tc = picker.pick(cfg)
        print(f"\n{tag}{wtxt}; surveys still open: { {a: show_quota(n) for a, n in sorted(room.items())} }; "
              f"test case: {tc['name']}")
        last_msg = None
        # Browser: kept open between chats per reuse_browser (default "skips": after a non-target agent the bot
        # just clicks the chat button again on the same page). A new browser always gets a brand-new profile.
        reused = br["ctx"] is not None
        if not reused:
            if not args.keep_profile and not args.profile:
                br["prof"] = tempfile.mkdtemp(prefix=f"qa_w{args.worker}_")
            br["ctx"] = launch(pw, args, br["prof"])
            br["page"] = br["ctx"].pages[0] if br["ctx"].pages else br["ctx"].new_page()
        try:
            runner = run_once(br["page"], args, tc, gate, cfg["reply_delay_seconds"], reuse=reused)
        except Exception:  # noqa: BLE001 - the browser itself died
            close_browser()
            raise
        if runner.failed and reused and not runner.agent_joined and not runner.interrupted:
            # nothing reached an agent - the old page was just in a bad state. Not a real failure: fresh browser.
            print(f"{tag}the reused page didn't work; starting a fresh browser for the next chat")
            close_browser()
            continue
        runs += 1
        outcome = ("interrupted" if runner.interrupted else
                   "failed" if runner.failed else
                   "skipped" if runner.skip_reason else
                   "transferred" if runner.transferred_to else
                   "agent_ended" if runner.agent_ended_chat else
                   "survey_submitted" if runner.survey_submitted else
                   "chat_no_survey" if not runner.survey_shown else "incomplete")
        total_msgs = runner.msg_client + runner.agent_msg_count
        now_local = datetime.now(__import__("zoneinfo").ZoneInfo(cfg["tz"]))
        ctl["date"] = session_date(now_local, cfg)
        entry = {"time": now_local.strftime("%Y-%m-%d %H:%M:%S"), "session_date": ctl["date"],
                 "worker": args.worker, "agent_id": runner.agent_id or "", "agent_name": runner.last_agent_from or "",
                 "outcome": outcome, "reason": runner.skip_reason or "", "messages_total": total_msgs,
                 "messages_client": runner.msg_client, "messages_agent": runner.agent_msg_count,
                 "chat_counted": bool(total_msgs >= cfg["min_messages_per_chat"] and not runner.skip_reason),
                 "survey_shown": runner.survey_shown, "survey_submitted": runner.survey_submitted,
                 "test_case": tc["name"], "run_dir": str(runner.run_dir), "chat_id": runner.chat_id,
                 "transferred_to": "" if runner.skip_reason else runner.transferred_to}
        entry.update(runner.timings())
        entry["unplanned"] = is_unplanned(entry)
        entry["end_reason"] = end_reason(runner, entry, tc)
        if runner.interrupted:
            entry["reason"] = f"stopped mid-chat ({STOP['why'] or 'Ctrl+C'})"
        try:
            (runner.run_dir / "transcript.txt").write_text(readable_transcript(runner, tc, entry), encoding="utf-8")
        except Exception:  # noqa: BLE001
            pass
        if should_save(cfg, entry):
            entry["saved_to"] = save_chat_copy(runner, entry, cfg, backend, args, S, tag)
        runner.committed = True
        done = backend.commit(runner.reserved_agent, wkey, runner.survey_submitted, entry)
        if S["local_report"]:
            with file_lock(lock_path):
                with open(log_path, "a", encoding="utf-8") as lf:
                    lf.write(json.dumps(entry) + "\n")
                write_report(log_path, args.out, ctl["date"], cfg)   # keep today's local report current
            ctl["unreported"] = True
        mins = f" in {entry['chat_duration_s'] // 60}m {entry['chat_duration_s'] % 60:02d}s" if entry.get("chat_duration_s") else ""
        print(f"{tag}what ended the chat: {entry['end_reason']}")
        print(f"{tag}chat result: {outcome}{mins}" + (f" - survey #{done} recorded for {runner.reserved_agent} "
              f"(quota {show_quota(cfg['agents'].get(runner.reserved_agent, 0))})" if runner.survey_submitted and done else ""))
        if runner.interrupted:
            if STOP["force"]:
                print(f"{tag}result saved; force-quitting.")
                os._exit(130)
            print(f"{tag}stopped as requested.")
            return 0
        mode = cfg.get("reuse_browser", "skips")
        if runner.failed or not (mode == "always" or (mode == "skips" and outcome == "skipped")):
            close_browser()
        skips_in_row = skips_in_row + 1 if outcome == "skipped" else 0
        fails_in_row = fails_in_row + 1 if outcome in ("failed", "incomplete") else 0
        if fails_in_row >= 3:
            print(f"{tag}3 failed chats in a row - stopping so a broken run doesn't keep occupying agents. "
                  "Check the latest run folder's error.txt.")
            return 2
        idle(cfg["cooldown_seconds"] * (3 if outcome == "failed" else 1))


# --------------------------------------------------------------------------- GitHub Actions gate
def gate_main(args, S):
    """Decides whether this scheduled GitHub run should start workers, and how many. Writes `open` and
    `workers` (a JSON list like [1,2,3]) to $GITHUB_OUTPUT for the workflow's matrix."""
    out = {"open": "false", "workers": "[]"}
    try:
        if not args.config:
            raise RuntimeError("no Config sheet link (QA_SHEET_CSV_URL secret / sheet_csv_url setting)")
        cfg = load_config(args.config)
        is_open, wtxt = window_status(cfg)
        if is_open and window_minutes_left(cfg) <= cfg["no_new_chats_last_minutes"]:
            is_open = False
            wtxt += f" - last {cfg['no_new_chats_last_minutes']} min of the window"
        n = max(0, min(int(cfg["parallel_runs"]), MAX_PARALLEL))
        if not cfg["enabled"]:
            reason = "disabled in the sheet (enabled = no)"
        elif not is_open:
            reason = f"outside the run window: {wtxt}"
        elif n == 0:
            reason = "parallel_runs is 0"
        elif not list_test_cases(S["test_cases_dir"]) and not args.test_case:
            reason = f"no valid test cases in {S['test_cases_dir']}"
        else:
            room = make_backend(args, S).room(cfg["agents"])
            if not any(v > 0 for v in room.values()):
                reason = "every agent in the list has reached its survey quota (or the list is empty)"
            else:
                out = {"open": "true", "workers": json.dumps(list(range(1, n + 1)))}
                reason = f"window open ({wtxt}) - starting {n} worker(s)"
    except SystemExit as e:
        reason = f"setup problem: {e}"
    except Exception as e:  # noqa: BLE001
        reason = f"error: {str(e)[:200]}"
    print(f"[gate] {reason}")
    gh = os.environ.get("GITHUB_OUTPUT")
    if gh:
        with open(gh, "a", encoding="utf-8") as f:
            f.write(f"open={out['open']}\nworkers={out['workers']}\n")
    else:
        print(json.dumps(out))


# --------------------------------------------------------------------------- local launcher (parallel workers)
def launcher_main(args):
    """Starts `parallel_runs` worker processes (each its own console window on Windows, its own log file
    elsewhere), keeps that number in step with the Sheet, and exits on Ctrl+C."""
    import subprocess
    out = Path(args.out)
    (out / "workers").mkdir(parents=True, exist_ok=True)
    base = [sys.executable, os.path.abspath(__file__), args.url, "--settings", args.settings,
            "--config", args.config, "--state", args.state, "--out", args.out, "--browser", args.browser,
            "--chat-frame-name", args.chat_frame_name, "--step-timeout", str(args.step_timeout),
            "--agent-wait-timeout", str(args.agent_wait_timeout), "--turn-wait-timeout", str(args.turn_wait_timeout)]
    for flag, val in (("--test-case", args.test_case), ("--brave-path", args.brave_path)):
        if val:
            base += [flag, val]
    for flag, on in (("--keep-profile", args.keep_profile), ("--headless", args.headless)):
        if on:
            base.append(flag)
    procs, dead, last_n = {}, set(), None
    print("[launcher] managing parallel workers from the sheet's parallel_runs. Close this window (or Ctrl+C) "
          "to stop starting new workers; close a worker's window to stop that worker.")
    try:
        while True:
            if STOP["now"] or STOP["after_chat"]:
                print("\n[launcher] stopped (workers keep running until you stop them in their own windows).")
                return
            try:
                cfg = load_config(args.config)
                want = max(0, min(int(cfg["parallel_runs"]), MAX_PARALLEL))
                stagger = max(0, cfg["worker_stagger_seconds"])
            except Exception as e:  # noqa: BLE001
                print(f"[launcher] can't read the config sheet ({str(e)[:100]}); keeping current workers")
                idle(30)
                continue
            if want != last_n:
                print(f"[launcher] parallel_runs = {want}")
                last_n = want
            for i in range(1, MAX_PARALLEL + 1):
                pr = procs.get(i)
                alive = pr is not None and pr.poll() is None
                if pr is not None and not alive:
                    if pr.returncode not in (0, None):
                        dead.add(i)
                        print(f"[launcher] worker {i} stopped with an error (exit {pr.returncode}); "
                              f"not restarting it - see its window / {out / 'workers'}")
                    procs.pop(i)
                    pr = None
                sf = out / "workers" / f"stop_{i}"
                if i <= want and i not in dead:
                    if sf.exists():
                        sf.unlink()
                    if pr is None:
                        kw = {}
                        if os.name == "nt":
                            kw["creationflags"] = subprocess.CREATE_NEW_CONSOLE
                        else:
                            kw["stdout"] = open(out / "workers" / f"worker_{i}.log", "a")
                            kw["stderr"] = subprocess.STDOUT
                        procs[i] = subprocess.Popen(base + ["--worker", str(i)], **kw)
                        print(f"[launcher] started worker {i} (pid {procs[i].pid})")
                        time.sleep(stagger)       # stagger starts so they don't all hit the queue at once
                elif alive:
                    sf.touch()                    # ask it to stop after its current chat
            idle(30)
    except KeyboardInterrupt:
        print("\n[launcher] stopped.")


# --------------------------------------------------------------------------- main
def main():
    global MASK
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("url", nargs="?", help="Start page (default: start_url in bot_settings.json)")
    ap.add_argument("--settings", default="bot_settings.json", help="Settings file (default bot_settings.json)")
    ap.add_argument("--once", action="store_true", help="Run ONE chat with a test case from the folder, no Sheet")
    ap.add_argument("--test-case", help="Always use this test-case file instead of picking from test_cases/")
    ap.add_argument("--config", metavar="PATH_OR_URL", help="Override sheet_csv_url (Config tab CSV link or file)")
    ap.add_argument("--parallel", action="store_true", help="Launcher: start parallel_runs workers, each in its "
                    "own window (local PC version)")
    ap.add_argument("--gate-output", action="store_true", help="GitHub Actions: decide if workers should start")
    ap.add_argument("--window-grace-minutes", type=int, default=30, help="With exit_after_window: how long to wait "
                    "for the window to open before giving up (default 30)")
    ap.add_argument("--state", default=None, help="Local counters file (default from settings: agent_state.json)")
    ap.add_argument("--max-runs", type=int, default=0, help="Scheduler: stop after N chats (0 = no limit)")
    ap.add_argument("--worker", type=int, default=0, help=argparse.SUPPRESS)
    ap.add_argument("--keep-profile", action="store_true", help="Reuse one browser profile instead of a fresh "
                    "throwaway profile for every chat")
    ap.add_argument("--reply-delay", type=int, default=30, help="--once mode: seconds to wait after the agent's "
                    "message before replying (scheduler mode uses reply_delay_seconds from the Sheet)")
    ap.add_argument("--out", default=None, help="Output folder (default from settings: qa_bot_runs)")
    ap.add_argument("--browser", choices=["auto", "brave", "chrome", "chromium"], default="auto")
    ap.add_argument("--brave-path", default=None)
    ap.add_argument("--profile", default=None)
    ap.add_argument("--attach", metavar="CDP_URL", help="--once mode: drive a browser you started yourself "
                    "(e.g. http://127.0.0.1:9222)")
    ap.add_argument("--chat-frame-name", default="123456789", help="window.name of the chat iframe "
                    "(stable on att.com's chat widget; override if the site changes it)")
    ap.add_argument("--step-timeout", type=int, default=30, help="Default per-step wait timeout, seconds")
    ap.add_argument("--agent-wait-timeout", type=int, default=420,
                    help="Wait for the live agent's FIRST message, seconds (default 420)")
    ap.add_argument("--turn-wait-timeout", type=int, default=240,
                    help="Wait for the agent's reply before each scripted message, seconds (default 240)")
    ap.add_argument("--headless", action="store_true", help=argparse.SUPPRESS)
    args = ap.parse_args()

    if not Path(args.settings).exists() and (Path(__file__).resolve().parent / args.settings).exists():
        args.settings = str(Path(__file__).resolve().parent / args.settings)
    S = load_settings(args.settings)
    MASK = bool(S["mask_agent_ids"])
    disable_quickedit()
    install_ctrl_c()
    args.url = args.url or S["start_url"]
    args.config = args.config or S["sheet_csv_url"] or None
    args.state = args.state or S["state_file"]
    args.out = args.out or S["output_dir"]

    if args.gate_output:
        gate_main(args, S)
        return
    if args.parallel and not args.once:
        if not args.config:
            sys.exit("--parallel needs the Config sheet link (sheet_csv_url in bot_settings.json)")
        launcher_main(args)
        return
    if sync_playwright is None:
        sys.exit("Playwright is not installed. Run: pip install -r requirements.txt")

    failed = False
    runner = None
    with sync_playwright() as pw:
        start_pause_listener()
        if args.config and not args.once:
            if args.attach:
                sys.exit("--attach only works with --once")
            try:
                code = scheduler_loop(pw, args, S)
                failed = bool(code)
            except KeyboardInterrupt:
                print("\n[stopped by user]")
            sys.exit(2 if failed else 0)
        if not args.config and not args.once:
            print("No Config sheet link set (sheet_csv_url) - running ONE chat. Use --once to skip this note.\n")
        tc = TestCasePicker(args, S).pick()
        print(f"Test case: {tc['name']} ({tc['_file']})\n")
        if args.attach:
            browser = pw.chromium.connect_over_cdp(args.attach)
            ctx = browser.contexts[0]
            page = ctx.new_page()
            print(f"[browser] attached to {args.attach}")
        else:
            browser = None
            ctx = launch(pw, args)
            page = ctx.pages[0] if ctx.pages else ctx.new_page()
        try:
            runner = run_once(page, args, tc, None, args.reply_delay)
            failed = runner.failed
            (runner.run_dir / "transcript.txt").write_text(readable_transcript(runner, tc), encoding="utf-8")
        except KeyboardInterrupt:
            print("\n[stopped by user]")
        finally:
            try:
                if browser is not None:
                    browser.close()
                else:
                    ctx.close()
            except Exception:  # noqa: BLE001
                pass

    if runner is not None:
        ok = sum(1 for s in runner.report if s["status"] == "ok")
        print(f"\n{ok}/{len(runner.report)} steps completed. Report: {runner.run_dir / 'report.json'}")
        print(f"Conversation transcript: {runner.run_dir / 'transcript.jsonl'}")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
