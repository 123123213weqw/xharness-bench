"""Controlled harness comparison harness on top of Terminal-Bench / Harbor.

Two experiment tiers:

Tier A -- replica fidelity.  XHarness is a Rust re-implementation of
    ``deepseek-ai/deepseek-harness``.  Both speak the same JSON-RPC contract, so
    the same task, prompt, tool surface and model can be driven through both.
    The treatment is the implementation; almost everything else is pinned.

Tier B -- cross-harness capability.  The same tasks across independent agents
    (Claude Code, Codex, OpenHands, mini-swe-agent, ...).  Here the system
    prompt and tool surface *are* the treatment and cannot be equalised.  Only
    externally verified pass/fail is comparable; self-reported token counts are
    recorded but never used for the primary claim.

This package deliberately does not re-implement orchestration.  Harbor already
provides the task sandbox, the oracle / nop / mini-swe-agent baselines and the
commercial agent adapters.  What it does not provide is an adapter for
XHarness or for the upstream DeepSeek Harness, plus the controls and the
analysis discipline.  That is what lives here.
"""

__all__ = ["__version__"]
__version__ = "0.1.0"