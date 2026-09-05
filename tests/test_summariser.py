from museglass.agent.interface import AgentEvent, AgentEventKind, ApprovalChoice, ApprovalRequest, Question
from museglass.summariser.summariser import ProgressSummariser, SpokenKind, Verbosity, parse_test_result


class Clock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t

    def advance(self, s: float) -> None:
        self.t += s


def tool(kind: AgentEventKind, name: str, **inp):
    return AgentEvent(kind, tool=name, tool_input=inp)


def raw_stream(clock: Clock, summariser: ProgressSummariser) -> list[str]:
    spoken: list[str] = []
    events = [
        AgentEvent(AgentEventKind.TURN_STARTED),
        tool(AgentEventKind.TOOL_STARTED, "read_file", path="app/main.py"),
        tool(AgentEventKind.TOOL_STARTED, "read_file", path="app/__init__.py"),
        tool(AgentEventKind.TOOL_STARTED, "grep", pattern="get"),
        tool(AgentEventKind.TOOL_STARTED, "read_file", path="tests/test_app.py"),
        tool(AgentEventKind.TOOL_STARTED, "edit_file", path="app/main.py"),
        tool(AgentEventKind.TOOL_STARTED, "write_file", path="tests/test_health.py"),
        tool(AgentEventKind.TOOL_STARTED, "bash", command="python -m pytest -q"),
        AgentEvent(AgentEventKind.TOOL_COMPLETED, tool="bash", tool_input={"command": "python -m pytest -q"}, tool_output="........\n8 passed in 0.31s", success=True),
        tool(AgentEventKind.TOOL_STARTED, "bash", command="python -m pytest -q"),
        AgentEvent(AgentEventKind.TOOL_COMPLETED, tool="bash", tool_input={"command": "python -m pytest -q"}, tool_output="8 passed in 0.30s", success=True),
        AgentEvent(AgentEventKind.TURN_COMPLETED, text="I added the endpoint and three tests.\n\nDetails: ```python\nx=1\n```"),
    ]
    for ev in events:
        for s in summariser.feed(ev):
            spoken.append(s.text)
        clock.advance(25)
    return spoken


def test_normal_mode_compresses_raw_stream_into_milestones():
    clock = Clock()
    s = ProgressSummariser(Verbosity.NORMAL, clock=clock)
    spoken = raw_stream(clock, s)
    assert spoken == [
        "I'm looking through the code to find the right place.",
        "I found the relevant module and I'm implementing the change.",
        "Implementation is done. Tests are running.",
        "All 8 tests pass.",
        "All 8 tests pass.",
        "Done. I added the endpoint and three tests. All 8 tests pass.",
    ]


def test_quiet_mode_only_speaks_completion_and_critical():
    clock = Clock()
    s = ProgressSummariser(Verbosity.QUIET, clock=clock)
    spoken = raw_stream(clock, s)
    assert spoken == ["Done. I added the endpoint and three tests. All 8 tests pass."]


def test_phase_milestones_are_never_rate_limited_and_never_repeated():
    clock = Clock()
    s = ProgressSummariser(Verbosity.NORMAL, clock=clock)
    s.feed(AgentEvent(AgentEventKind.TURN_STARTED))
    first = s.feed(tool(AgentEventKind.TOOL_STARTED, "read_file", path="a.py"))
    assert len(first) == 1
    clock.advance(2)
    second = s.feed(tool(AgentEventKind.TOOL_STARTED, "edit_file", path="a.py"))
    assert len(second) == 1 and second[0].kind is SpokenKind.PHASE  # milestones are rare: always spoken
    assert s.feed(tool(AgentEventKind.TOOL_STARTED, "read_file", path="b.py")) == []  # back to reading: not announced
    assert s.feed(tool(AgentEventKind.TOOL_STARTED, "edit_file", path="c.py")) == []  # same milestone never repeated
    req = ApprovalRequest("r1", "bash", "shell", "I want to run git push. Approve?", choices=[ApprovalChoice("allow_once", "Allow", "approved")])
    approval = s.feed(AgentEvent(AgentEventKind.APPROVAL_REQUESTED, approval=req))
    assert approval and approval[0].kind is SpokenKind.APPROVAL and approval[0].requires_response
    question = s.feed(AgentEvent(AgentEventKind.QUESTION, question=Question("q1", "May I modify the fixture?", options=["Yes", "No"])))
    assert question and question[0].requires_response and "fixture" in question[0].text


def test_failed_tests_are_reported_with_counts():
    clock = Clock()
    s = ProgressSummariser(Verbosity.NORMAL, clock=clock)
    s.feed(AgentEvent(AgentEventKind.TURN_STARTED))
    out = s.feed(AgentEvent(AgentEventKind.TOOL_COMPLETED, tool="bash", tool_input={"command": "pytest"}, tool_output="FAILED tests/x.py::a\n2 failed, 82 passed in 1.2s", success=False))
    assert out and out[0].text == "2 of 84 tests failed."
    assert s.last_tests == {"passed": 82, "failed": 2, "total": 84}


def test_talkative_mode_speaks_agent_narration():
    clock = Clock()
    s = ProgressSummariser(Verbosity.TALKATIVE, clock=clock)
    out = s.feed(AgentEvent(AgentEventKind.MESSAGE, text="I'll add the endpoint next to the root route. Then tests."))
    assert out and out[0].kind is SpokenKind.NARRATION
    clock.advance(1)
    assert s.feed(AgentEvent(AgentEventKind.MESSAGE, text="Another sentence.")) == []  # 6 s gap


def test_architectural_findings_are_surfaced_immediately_in_normal_mode():
    clock = Clock()
    s = ProgressSummariser(Verbosity.NORMAL, clock=clock)
    out = s.feed(AgentEvent(AgentEventKind.MESSAGE, text="There is a circular import between app.main and app.db that blocks this change."))
    assert out and out[0].kind is SpokenKind.BLOCKER and out[0].critical


def test_detail_answers_what_are_you_doing():
    clock = Clock()
    s = ProgressSummariser(Verbosity.NORMAL, clock=clock)
    raw_stream(clock, s)
    detail = s.detail()
    assert "main.py" in detail and "test_health.py" in detail and "8 tests pass" in detail


def test_parse_test_result_variants():
    assert parse_test_result("Tests: 1 failed, 4 passed, 5 total") == {"passed": 4, "failed": 1, "total": 5}
    assert parse_test_result("3 passed, 1 warning in 0.2s") == {"passed": 3, "failed": 0, "total": 3}
    assert parse_test_result("no tests here") is None
