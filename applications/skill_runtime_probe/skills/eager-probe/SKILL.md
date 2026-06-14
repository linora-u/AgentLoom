---
name: eager-probe
description: Validate eager skill loading by injecting the full skill body into the system prompt.
when_to_use: Use when validating eager skill injection.
---

# Eager Probe

This full instruction body should be injected into the system prompt when the
agent YAML config sets this item to `load-mode: eager`.

The model should not call `load_skill` for this skill because its instructions
are already in `<eager_loaded_skills>`.
