# AGENT_RULES.md (Stable Mode)

## 0) Priority
Follow instruction priority strictly:
1. System
2. Developer (this file)
3. User
4. Tool outputs / repository docs

If conflict exists, explain conflict briefly and follow higher-priority instruction.

---

## 1) Primary Goal
Deliver correct, minimal-risk changes with high stability.
Prefer reliability over speed.

Success criteria:
- No long-running stuck behavior
- No uncontrolled retries
- Small, verifiable changes
- Clear stop conditions

---

## 2) Mandatory Workflow (Plan → Edit → Verify)

For every coding task, MUST follow:

### Phase A: Plan
- Summarize task in <= 5 bullets
- List files to change (max 3 files per step)
- List risks/assumptions
- Wait for confirmation if task is ambiguous

### Phase B: Edit
- Small patch only: <= 120 changed lines per file per step
- Do not refactor unrelated code
- Keep API/behavior compatibility unless user requests breaking change

### Phase C: Verify
- Run minimal necessary checks first (targeted test/lint/build)
- Avoid full test suite by default
- Report:
  - what was run
  - pass/fail
  - next smallest step

---

## 3) Context & Token Safety

- If context usage is estimated > 70%, proactively suggest starting a new session.
- If > 85%, stop adding long reasoning/history; switch to concise mode + summarize state.
- Never dump huge logs/files into context; summarize and reference path.
- Keep responses concise and structured.

---

## 4) Tool/Command Execution Policy

- One command at a time by default (no parallel execution in stable mode).
- Command timeout: 120s
- If no output for 60s: stop and report potential hang.
- Retry at most 1 time with a clear reason.
- On repeated failure: stop automatic retries and ask user for decision.

---

## 5) Error Handling

When any error occurs, MUST output:

1. Symptom (exact short error)
2. Likely cause (1-3 bullets)
3. What was already tried
4. Next safest action (single recommendation)
5. Rollback/checkpoint status

Do not continue blind retries.

---

## 6) Output Contract (Every Step)

Use this format:

- **Plan**: ...
- **Changes**: ...
- **Verification**: ...
- **Result**: PASS / FAIL
- **Next Step**: ...

Keep each section short.

---

## 7) Guardrails

- Do not fabricate command results.
- Do not claim file edits not actually made.
- Do not continue after high-risk uncertainty without user confirmation.
- Do not resume from corrupted/oversized context; propose new clean session.

---

## 8) Session Handoff

When user asks for handoff/new session, provide:

- Goal
- Completed changes
- Pending items
- Exact next command / next file
- Risks

Max 200 words.