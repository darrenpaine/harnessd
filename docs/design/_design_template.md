# Design: <title>

Status: draft | accepted | superseded
Specs: <docs/specs/ files this implements>

## Problem and constraints
What we're solving and the limits we're working within.

## Proposed approach
Short overview, then the pieces.

### Components
Modules and their responsibilities (map to src/...).

### Data flow
Step-by-step path of a request, including where state is read and written.

### Data models and storage
Pydantic models involved, Redis keys/TTLs, state shape.

### Failure modes
What can go wrong (tool error, model error, Redis down, timeout) and what happens.

## Alternatives considered
| Option | Pros | Cons | Why not |
|--------|------|------|---------|

## Decision
What we chose and the main trade-off accepted.

## Risks and open questions