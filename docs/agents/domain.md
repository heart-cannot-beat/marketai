# Domain Docs

This repository uses a single-context domain-documentation layout.

## Before exploring

Read these files when they exist:

- `CONTEXT.md`
- Relevant ADRs under `docs/adr/`

If they do not exist, proceed silently. Domain-modeling skills create them lazily when terminology or architectural decisions are resolved.

## Layout

```text
/
├── CONTEXT.md
├── docs/
│   └── adr/
└── app/
```

## Vocabulary

Use terminology defined in `CONTEXT.md` in issue titles, specifications, tests and implementation code.

Do not silently introduce synonyms for established concepts. If a required concept is missing, reconsider the terminology or record the gap for domain modeling.

## Architectural decisions

Before changing an area, read the relevant ADRs.

If proposed work contradicts an ADR, surface the conflict explicitly instead of silently overriding the decision.
