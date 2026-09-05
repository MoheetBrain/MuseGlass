"""Command router: turns a transcript into an intent.

Deliberately rule-based. It only needs to recognise a small control vocabulary reliably;
everything else is content for the agent (a new task when idle, a steer when busy).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum

from museglass.summariser.summariser import Verbosity


class IntentKind(str, Enum):
    IGNORE = "ignore"  # no wake word while not in a dialogue
    OPEN_PROJECT = "open_project"
    TASK = "task"
    STEER = "steer"
    STOP = "stop"
    PAUSE = "pause"
    CONTINUE = "continue"
    YES = "yes"
    NO = "no"
    STATUS = "status"
    DETAIL = "detail"
    SHORT = "short"
    WHY = "why"
    UNDO = "undo"
    SHOW_DIFF = "show_diff"
    VERBOSITY = "verbosity"
    LIST_PROJECTS = "list_projects"
    END_SESSION = "end_session"


@dataclass
class Intent:
    kind: IntentKind
    text: str = ""  # the content to forward (task text, steer text, feedback)
    raw: str = ""
    project: str | None = None
    verbosity: Verbosity | None = None


_WAKE_RE = re.compile(r"^\s*(?:(?:hey|ok|okay)[\s,]+)?(?:muse|news|mews|moose|muse code)[\s,.:!?-]*", re.IGNORECASE)
_OPEN_RE = re.compile(
    r"^(?:please\s+)?(?:open|switch to|go to|load|work on)\s+(?:up\s+)?(?:the\s+)?(?P<name>[\w\s-]+?)(?:\s+project|\s+repo|\s+repository)?\s*(?P<rest>[.,;:]|\band\b|\bthen\b|$)",
    re.IGNORECASE,
)
_STOP_RE = re.compile(r"^(?:stop|halt|abort|cancel(?: that| the task| it)?|never ?mind)[.!\s]*$", re.IGNORECASE)
_PAUSE_RE = re.compile(r"^(?:pause|hold on|wait|hang on)(?: a (?:second|sec|moment|minute))?[.!\s]*$", re.IGNORECASE)
_CONTINUE_RE = re.compile(r"^(?:continue|resume|carry on|go on|keep going|proceed|go ahead)[.!\s]*$", re.IGNORECASE)
_STATUS_RE = re.compile(r"^(?:status|what'?s the status|where are (?:you|we)|how(?:'s| is) it going|what are you doing|are you (?:still )?(?:there|working|running))[?.!\s]*$", re.IGNORECASE)
_DETAIL_RE = re.compile(r"^(?:(?:give me |tell me )?(?:more )?details?|(?:the )?(?:long|full|detailed) version|more|tell me more|elaborate)[?.!\s]*$", re.IGNORECASE)
_SHORT_RE = re.compile(r"^(?:(?:the )?short version|summary|summarise|summarize|keep it short|brief(?:ly)?)[?.!\s]*$", re.IGNORECASE)
_WHY_RE = re.compile(r"^why\b", re.IGNORECASE)
_UNDO_RE = re.compile(r"^(?:undo|revert)(?: (?:the |that )?last (?:change|edit|step))?[.!\s]*$", re.IGNORECASE)
_DIFF_RE = re.compile(r"^(?:show|tell) me what (?:has )?changed|^what (?:did you|has) change[d]?|^show (?:me )?the diff", re.IGNORECASE)
_VERB_RE = re.compile(r"^(?:be |go |set (?:verbosity )?(?:to )?|switch to |)(?P<mode>quiet|normal|talkative|verbose|silent|chatty)(?: mode)?[.!\s]*$", re.IGNORECASE)
_LIST_RE = re.compile(r"^(?:list|what|which) (?:are the |are my |the )?projects\b", re.IGNORECASE)
_END_RE = re.compile(r"^(?:end|close|finish) (?:the )?session[.!\s]*$|^goodbye[.!\s]*$", re.IGNORECASE)
_YES_RE = re.compile(r"^(?:yes|yeah|yep|yup|sure|ok(?:ay)?|approve[d]?|go ahead|do it|confirm(?:ed)?|correct|affirmative|please do|fine|that'?s fine)\b", re.IGNORECASE)
_NO_RE = re.compile(r"^(?:no|nope|nah|don'?t|do not|deny|denied|reject(?:ed)?|negative|stop|cancel|never ?mind|not now)\b", re.IGNORECASE)

_VERB_MAP = {"quiet": Verbosity.QUIET, "silent": Verbosity.QUIET, "normal": Verbosity.NORMAL, "talkative": Verbosity.TALKATIVE, "verbose": Verbosity.TALKATIVE, "chatty": Verbosity.TALKATIVE}


def strip_wake_word(text: str) -> tuple[str, bool]:
    m = _WAKE_RE.match(text)
    if not m:
        return text.strip(), False
    return text[m.end():].strip(), True


class CommandRouter:
    def __init__(self, *, wake_word_required: bool = True) -> None:
        self.wake_word_required = wake_word_required

    def parse(self, transcript: str, *, busy: bool = False, awaiting_response: bool = False) -> Intent:
        raw = transcript.strip()
        text, had_wake = strip_wake_word(raw)
        if self.wake_word_required and not had_wake and not awaiting_response and not busy:
            return Intent(IntentKind.IGNORE, raw=raw)
        if not text:
            return Intent(IntentKind.IGNORE, raw=raw)

        # Answers to a pending question / approval take priority.
        if awaiting_response:
            if _YES_RE.match(text):
                return Intent(IntentKind.YES, text=_after_first_clause(text), raw=raw)
            if _NO_RE.match(text) or _STOP_RE.match(text):
                return Intent(IntentKind.NO, text=_after_first_clause(text), raw=raw)

        for pattern, kind in (
            (_STOP_RE, IntentKind.STOP),
            (_PAUSE_RE, IntentKind.PAUSE),
            (_CONTINUE_RE, IntentKind.CONTINUE),
            (_STATUS_RE, IntentKind.STATUS),
            (_DETAIL_RE, IntentKind.DETAIL),
            (_SHORT_RE, IntentKind.SHORT),
            (_UNDO_RE, IntentKind.UNDO),
            (_DIFF_RE, IntentKind.SHOW_DIFF),
            (_LIST_RE, IntentKind.LIST_PROJECTS),
            (_END_RE, IntentKind.END_SESSION),
        ):
            if pattern.match(text):
                return Intent(kind, text=text, raw=raw)
        m = _VERB_RE.match(text)
        if m:
            return Intent(IntentKind.VERBOSITY, text=text, raw=raw, verbosity=_VERB_MAP[m.group("mode").lower()])
        if _WHY_RE.match(text):
            return Intent(IntentKind.WHY, text=text, raw=raw)

        m = _OPEN_RE.match(text)
        if m:
            name = m.group("name").strip()
            rest = text[m.end():].strip() if m.group("rest") else ""
            rest = re.sub(r"^(?:and|then)\s+", "", rest, flags=re.IGNORECASE).strip()
            return Intent(IntentKind.OPEN_PROJECT, text=rest, raw=raw, project=name)

        if busy:
            return Intent(IntentKind.STEER, text=text, raw=raw)
        return Intent(IntentKind.TASK, text=text, raw=raw)


def _after_first_clause(text: str) -> str:
    """"Yes. Commit but don't push." → "Commit but don't push." """
    parts = re.split(r"[.,;:!]\s*|\s+(?:but|and|then)\s+", text, maxsplit=1)
    return parts[1].strip() if len(parts) > 1 else ""
