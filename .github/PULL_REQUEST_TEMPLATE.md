<!--
Kept short on purpose. A long template gets skimmed, and the parts of it that
matter are the same three things every time.
-->

## What this changes

<!-- One or two sentences. What is different after this PR that was not true before. -->

## Why

<!-- The reasoning. If this fixes a bug, link the issue. -->

## How it was checked

<!-- Be specific - "ran the tests" is not a check. -->

- [ ] `python -m pytest -q` passes
- [ ] Added or updated a test in `tests/`, if the change is testable
- [ ] `config.json` is still valid JSON, and provider `id`s are still unique

<!-- Drop these if they do not apply. -->

- [ ] **No secrets.** No `.env` values, bot tokens, API keys, or memory file
      contents in the diff. `.env` is gitignored; if something sensitive got
      committed, **Reset Token** and treat it as compromised rather than just
      removing the file.
- [ ] **No personal data.** No contents from `memory/*.md`, `affinity/*.json`,
      `private/`, or `backups/`. Those are gitignored for a reason.
- [ ] **Voice preserved.** If this touches `persona.md`, `skills/`, or reply
      text, she still sounds like a friend and not like a help desk. The rules
      she must never break are listed in `persona.md`.
- [ ] **No assistant boilerplate.** Nothing that produces "As an AI",
      "Certainly!", "Here's a list", or narrating what it is about to do.

## Notes for the reviewer

<!-- Anything you want a second opinion on. What you are unsure about, or what
     you deliberately left alone. -->