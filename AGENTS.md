# Repository maintenance

- Read README.md and docs/architecture.md before changing behavior. Load only the relevant business Skill and review standard.
- Preserve complete-source reading, independent business/audit requests, provenance, failed attempts, and partition isolation.
- Keep the configured model IDs and reasoning effort; never silently downgrade or replace models.
- Use only local environment variables or ignored local files for credentials and provider addresses. Never print credentials or commit runtime data, routes, logs, local configuration, or private endpoints.
- Changes to research methods must be candidates with comparison and regression evidence. Stable promotion stays disabled until a policy is agreed and frozen.
- Run the relevant tests, and the complete suite before publication. Offline tests do not prove live model quality.
- The supported deployment is a full source checkout with resources, web assets and config.json alongside finresearch. Use Python 3.12 for reproducible setup.
