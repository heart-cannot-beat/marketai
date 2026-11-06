# Separate the SaaS control plane from the MarketAI domain service

MarketAI will adopt an open-source SaaS control plane for identity, Workspace membership, subscriptions, and administration, while the existing FastAPI and Worker system exclusively owns creative-experiment domain state, rules, and external side effects. We rejected rewriting the validated domain engine in a Node/Prisma starter or allowing two ORMs to write the same domain tables because either choice would duplicate invariants and create divergent sources of truth.
