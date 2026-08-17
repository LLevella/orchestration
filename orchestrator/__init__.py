"""Local AI orchestration for the Millennium AI travel-agent project.

Deterministic control flow drives role-bound AI agents (Claude, Codex, Gemini,
Mistral) through architecture and development review loops. The orchestrator is
"dumb" on purpose: all reasoning lives in the agents and their role files; this
package only sequences steps, parses verdicts, enforces safety, and preserves
the full run history.
"""
