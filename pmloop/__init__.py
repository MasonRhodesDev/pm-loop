"""pm-loop: a procedural, event-driven PM loop for GitHub repos worked by Claude Code lanes.

Everything an LLM does not need to decide is a script here; the LLM is invoked only with a
small brief when the event queue is non-empty. See docs/DESIGN.md.
"""
__version__ = "0.1.0"
