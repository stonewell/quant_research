# Model Routing & Task Delegation Policy

You operate with access to two models: **DeepSeek Flash** (`deepseek-api:deepseek-v4-flash`) and **DeepSeek Pro** (`deepseek-api:deepseek-v4-pro`).

Follow these workload routing criteria:

## Route to DeepSeek Flash:
- **Fast Edits & Formatting:** Single-function updates, linting fixes, comment additions, and minor syntax corrections.
- **Scaffolding & Boilerplate:** Generating repetitive tests, schemas, typing definitions, or standard CRUD endpoints.
- **Exploratory Queries & Search:** Explaining localized code snippets, regex generation, and parsing error logs.
- **Draft Reviews:** First-pass diff inspection and basic unit test generation.

## Route to DeepSeek Pro:
- **Architectural & Multi-File Refactoring:** Cross-module updates, system restructuring, and breaking API migrations.
- **Deep Debugging:** Heisenbugs, race conditions, memory leaks, and complex dependency inversion problems.
- **Algorithm Design & Optimization:** Complex data structures, high-performance routines, and concurrency logic.
- **Security & Threat Modeling:** Auth flows, cryptographic routines, and vulnerability audits.

## Dynamic Fallback Rule:
- When operating in an autonomous agentic loop, execute file indexing, initial diff planning, and validation sweeps with **Flash**.
- Escalate to **Pro** if a test suite fails more than twice on the same fix attempt or if cross-package semantic reasoning is needed.
