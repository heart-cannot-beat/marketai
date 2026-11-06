# Issue tracker: GitHub

Issues and specs for this repository live in GitHub Issues. Use the `gh` CLI for all operations.

## Repository

`Raybbish/marketai`

Infer the repository from `git remote -v`; `gh` does this automatically inside the clone.

## Conventions

- Create: `gh issue create --title "..." --body "..."`
- Read: `gh issue view <number> --comments`
- List: `gh issue list --state open --json number,title,body,labels,comments`
- Comment: `gh issue comment <number> --body "..."`
- Add a label: `gh issue edit <number> --add-label "..."`
- Remove a label: `gh issue edit <number> --remove-label "..."`
- Close: `gh issue close <number> --comment "..."`

## Pull requests as a triage surface

**PRs as a request surface: no.**

GitHub Issues are the source of truth for feature requests, specifications and implementation tasks. Pull requests are not automatically added to the triage queue.

## Skill operations

When a skill says “publish to the issue tracker”, create a GitHub issue.

When a skill says “fetch the relevant ticket”, run:

`gh issue view <number> --comments`

A bare `#42` may refer to an issue or pull request. Try `gh pr view 42` and fall back to `gh issue view 42`.

## Wayfinding

- A map is one issue labelled `wayfinder:map`.
- Child tickets use `wayfinder:research`, `wayfinder:prototype`, `wayfinder:grilling`, or `wayfinder:task`.
- Use native GitHub sub-issues and issue dependencies when available.
- If unavailable, record `Part of #<map>` and `Blocked by: #<number>` in the issue body.
- Claim work using `gh issue edit <number> --add-assignee @me`.
- Resolve work by adding the result as a comment and closing the issue.
