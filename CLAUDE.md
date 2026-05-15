# Boomer — Project Rules for Claude

This file is read automatically by Claude Code at the start of every session.
All rules below apply at all times with no exceptions unless the operator explicitly overrides one for a specific task.

---

## 1. Git — Branch Protection

**Never push directly to `main`.** `main` is always the stable, deployable branch.

Every piece of work — no matter how small — lives on its own branch:

| Prefix | When to use |
|--------|-------------|
| `feat/` | New capability that did not exist before |
| `refactor/` | Restructuring existing code without changing behaviour |
| `enhancement/` | Improvement to an existing feature |
| `bug/` | Fix for a confirmed defect |
| `hotfix/` | Urgent fix that needs fast-track review |

Branch always taken from latest `main`:
```bash
git checkout main && git pull && git checkout -b feat/your-feature-name
```

If `main` has moved while working on a branch, rebase — do not merge `main` into the feature branch.

---

## 2. Pre-Push Gate — No Exceptions

Before any `git push`, all three gates must pass. If any fails, fix it first — do not push with failures and "fix later."

```bash
# 1. Lint
ruff check .

# 2. Unit tests
pytest tests/ -x --tb=short

# 3. Import / syntax check (catches build errors before a CI runner does)
python -m py_compile $(git diff --name-only HEAD | grep '\.py$')
```

If a test is legitimately broken by the current change (expected failure during a refactor), mark it `@pytest.mark.xfail` with a comment explaining why — never delete tests to make the suite pass.

---

## 3. Commits — Logical Grouping

Each commit must represent exactly one logical change. Never bundle unrelated changes in a single commit.

Good: `feat(collector): add NSE FO bhavcopy fetcher`
Good: `fix(executor): handle partial fill on stop-loss order`
Bad: `various fixes and cleanup`
Bad: `collector + executor + dashboard changes`

Commit message format:
```
<type>(<scope>): <short imperative description>

<optional body — WHY, not WHAT>
```

Types: `feat`, `fix`, `refactor`, `test`, `docs`, `chore`
Scopes match system modules: `collector`, `brain`, `executor`, `orchestrator`, `dashboard`, `capital`, `migrations`

---

## 4. File Size — Hard Limit

**No file may exceed 600 lines.** When a file approaches 500 lines, split it.

Permitted exceptions (must be explicitly noted with a comment at the top of the file):
- Auto-generated files (migrations, fixtures)
- Files that are a single large data structure (e.g., a comprehensive test fixture)
- Files that genuinely cannot be split without breaking Python import semantics

When splitting: prefer splitting by responsibility, not by line count. A `collector/fetchers/nse_filings.py` and `collector/fetchers/bse_bulk_deals.py` is better than `collector_part1.py` and `collector_part2.py`.

---

## 5. Unit Tests — Mandatory for Every Feature and Enhancement

**No feature or enhancement is complete without unit tests.**

Test coverage requirements:
- Every public function/method has at least one test
- Happy path tested
- All documented edge cases tested (empty input, boundary values, failure modes)
- Any bug fix must include a regression test that would have caught the original bug

Test file mirrors source structure: `src/collector/fetchers/nse_filings.py` → `tests/collector/fetchers/test_nse_filings.py`

Use `pytest`. Use `unittest.mock` for external dependencies (broker API, HTTP calls). Never make real HTTP calls or real broker API calls in tests.

For financial calculation tests (position sizing, EV gate, circuit breakers): always include a numerical worked example that can be verified by hand, matching the worked example in the design docs where one exists.

---

## 6. Pull Requests — Always with Full Description

Every branch merged to `main` goes through a PR. No direct merges.

PR description must include:

```markdown
## What this does
<!-- One paragraph. What capability does this add or what problem does it fix? -->

## Design reference
<!-- Link to the relevant phase doc section this implements. e.g. "Phase 2 — Collector, NSE filings fetcher" -->

## How to test manually
<!-- Step-by-step instructions for the reviewer to verify the change works -->

## Checklist
- [ ] Lint passes
- [ ] All tests pass
- [ ] No file exceeds 600 lines
- [ ] open-questions.md checked for relevant open questions
- [ ] project-status.md updated
```

No PR merges with open checklist items.

---

## 7. Project Status — Always Update

**Every merged PR must update `project-status.md`** in the project root before the PR is raised.

`project-status.md` is the living record of what has been built. It allows any future session (this one or a new chat) to resume with full context without re-reading all code. Update it as part of the same commit as the feature work — not as a separate "docs" commit afterward.

Format rules:
- Add completed items to the relevant phase section
- For bugs: note the symptom, root cause, and the fix in one sentence
- Keep entries short — one or two lines per item
- Never delete old entries; mark superseded items as ~~struck through~~

---

## 8. Before Implementing Any Phase or Feature

Before writing a single line of code for a new phase or feature:

**Step 1 — Read `designs/open-questions.md`**
Filter for questions relevant to the phase being implemented. If any unanswered question directly affects what you are about to build, surface it to the operator before starting. Do not assume an answer and proceed.

**Step 2 — Read the relevant loopholes section in the phase design doc**
Every phase document has a "Loopholes and decisions" section. Read it. These are the edge cases that will bite during implementation if ignored.

**Step 3 — Confirm the design doc is the source of truth**
If the code you are about to write contradicts the design document, the design doc wins — unless there is a deliberate, documented reason to deviate. Undocumented deviations from the design are bugs, not improvements.

---

## 9. General Coding Standards (Boomer-specific)

**No hardcoded rupee amounts.** Every threshold is a percentage or a function of capital. If you find yourself writing `if loss > 1000`, that's a bug.

**All timestamps use IST (Asia/Kolkata).** India has no DST — IST is always UTC+5:30 with no ambiguity. Store IST in the database; display IST on the dashboard. Never store UTC, never convert to UTC internally.

**Every trade decision is logged with attribution.** Any code path that approves, rejects, or modifies a trade must write a structured log entry including: which rule fired, what the input values were, and what the outcome was.

**Broker calls only through the abstraction.** No code outside `executor/brokers/` ever imports or calls a broker SDK directly. Not even for "just reading a position."

**Feature store queries always use point-in-time filters.** Any query against the `features` table must include `WHERE valid_from <= :date AND source_max_observed_at <= :date`. A feature query without these filters is a lookahead bug.

**Schema changes only via migrations.** Never run `ALTER TABLE` manually on the database. Every schema change is a new file in `migrations/` following the forward-only pattern.

---

## 10. What Claude Should Never Do in This Project

- Never push to `main` — not even "just to check something"
- Never skip tests because "it's a simple change"
- Never widen a stop-loss in trade calculation code (stops only trail in the favourable direction)
- Never write code that converts an intraday position to swing or long-term (the design explicitly forbids this)
- Never hardcode broker-specific logic outside the broker abstraction layer
- Never assume an open question in `open-questions.md` has been resolved without checking
- Never merge a PR without updating `project-status.md`
