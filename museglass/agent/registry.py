"""Backend selection. `auto` picks the first backend that is actually usable."""

from __future__ import annotations

from museglass.agent.interface import AgentHealth, CodingAgent

BACKENDS = ("muse", "claude", "scripted")


def make_agent(name: str) -> CodingAgent:
    name = name.lower()
    if name == "muse":
        from museglass.agent.muse_msp.adapter import MuseCodeAgent

        return MuseCodeAgent()
    if name == "claude":
        from museglass.agent.claude.adapter import ClaudeCodeAgent

        return ClaudeCodeAgent()
    if name in ("scripted", "demo", "scripted-demo"):
        from museglass.agent.scripted import ScriptedDemoAgent

        return ScriptedDemoAgent(step_delay=0.4)
    raise ValueError(f"unknown backend {name!r}; choose one of {BACKENDS} or 'auto'")


async def select_agent(preference: str = "auto") -> tuple[CodingAgent, dict[str, AgentHealth]]:
    """Return the chosen agent and the health report of every backend considered."""
    report: dict[str, AgentHealth] = {}
    if preference != "auto":
        agent = make_agent(preference)
        report[agent.name] = await agent.health()
        return agent, report
    for name in ("muse", "claude"):
        try:
            agent = make_agent(name)
            health = await agent.health()
        except Exception as exc:  # noqa: BLE001
            report[name] = AgentHealth(False, f"failed to load: {exc}")
            continue
        report[name] = health
        if health.available:
            return agent, report
    agent = make_agent("scripted")
    report[agent.name] = await agent.health()
    return agent, report
