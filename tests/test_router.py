import pytest

from museglass.host.router import CommandRouter, IntentKind
from museglass.summariser.summariser import Verbosity


@pytest.fixture
def router() -> CommandRouter:
    return CommandRouter()


def test_open_project_with_task(router):
    intent = router.parse("Muse, open the demo project. Add a /health endpoint, include the current Git SHA, write tests, and keep me updated.")
    assert intent.kind is IntentKind.OPEN_PROJECT
    assert intent.project == "demo"
    assert intent.text.startswith("Add a /health endpoint")


def test_open_project_variants(router):
    assert router.parse("Muse open project A").project == "project A"
    assert router.parse("Hey Muse, switch to project-b and run the tests").project == "project-b"
    assert router.parse("Muse, open the demo project").text == ""


def test_wake_word_required_when_idle_but_not_when_busy_or_awaiting(router):
    assert router.parse("add a health endpoint").kind is IntentKind.IGNORE
    assert router.parse("Muse, add a health endpoint").kind is IntentKind.TASK
    assert router.parse("also include uptime", busy=True).kind is IntentKind.STEER
    assert router.parse("yes", awaiting_response=True).kind is IntentKind.YES


def test_control_words(router):
    assert router.parse("Muse, stop.").kind is IntentKind.STOP
    assert router.parse("stop", busy=True).kind is IntentKind.STOP
    assert router.parse("pause", busy=True).kind is IntentKind.PAUSE
    assert router.parse("Muse, continue.").kind is IntentKind.CONTINUE
    assert router.parse("Muse, what are you doing?").kind is IntentKind.STATUS
    assert router.parse("more detail", busy=True).kind is IntentKind.DETAIL
    assert router.parse("Muse, the short version").kind is IntentKind.SHORT
    assert router.parse("Muse, undo the last change").kind is IntentKind.UNDO
    assert router.parse("Muse, show me what changed").kind is IntentKind.SHOW_DIFF
    assert router.parse("why are you changing that?", busy=True).kind is IntentKind.WHY
    assert router.parse("Muse, list projects").kind is IntentKind.LIST_PROJECTS


def test_verbosity(router):
    assert router.parse("Muse, be quiet").verbosity is Verbosity.QUIET
    assert router.parse("Muse, talkative mode").verbosity is Verbosity.TALKATIVE
    assert router.parse("Muse, normal").verbosity is Verbosity.NORMAL


def test_yes_no_with_trailing_instruction(router):
    yes = router.parse("Yes. Commit but don't push.", awaiting_response=True)
    assert yes.kind is IntentKind.YES and yes.text == "Commit but don't push."
    no = router.parse("No. Commit locally only.", awaiting_response=True)
    assert no.kind is IntentKind.NO and no.text == "Commit locally only."
    assert router.parse("go ahead", awaiting_response=True).kind is IntentKind.YES
    assert router.parse("don't", awaiting_response=True).kind is IntentKind.NO


def test_steering_phrases_go_to_agent(router):
    for phrase in ("Don't refactor that.", "Use an adapter instead.", "Also run the benchmark.", "Keep the patch minimal."):
        intent = router.parse(phrase, busy=True)
        assert intent.kind is IntentKind.STEER and intent.text == phrase


def test_recognised_wake_word_variants(router):
    assert router.parse("Hey Muse, run the tests").kind is IntentKind.TASK
    assert router.parse("Moose, run the tests").kind is IntentKind.TASK  # common STT mishearing
