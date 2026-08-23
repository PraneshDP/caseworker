# AI-USAGE.md

This document records how AI tools were used during development of this project.

---

## Tools Used

### Antigravity IDE (Google Gemini-powered coding assistant)
- **What it did**: Pair-programmed the entire codebase — architecture planning, code generation, debugging, documentation.
- **How it was used**: Conversational coding — I described intent, reviewed generated code, requested changes, and made judgment calls on what to keep/cut.
- **What I reviewed manually**: Every file was reviewed before committing. Architectural decisions (DECISIONS.md) reflect my judgment, not the model's defaults.

---

## AI-Generated vs. Human-Decided

| Aspect | AI-Generated | Human-Decided |
|---|---|---|
| Code implementation | ✓ Most code was AI-assisted | |
| Architecture (risk classifier separation) | | ✓ Deliberate design choice |
| Scope cuts (no Postgres, no LangGraph) | | ✓ Based on hackathon constraints |
| Risk weights and threshold | | ✓ Calibrated to caseworker intuition |
| Seed data scenarios | ✓ Generated to spec | ✓ Edge cases specified by human |
| Policy manual content | ✓ Synthetic content | ✓ Structure/coverage specified by human |
| Test cases | ✓ Implementation | ✓ What to test decided by human |

---

## Honest Disclosure

This is a hackathon. AI tooling was used extensively and intentionally. The value I'm demonstrating is **judgment** — what to build, what to cut, and why the Risk Classifier architecture matters — not manual keystroke count.
