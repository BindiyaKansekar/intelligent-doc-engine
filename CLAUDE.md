# Intelligent Documentation Engine

## Project Context
AI-powered documentation utility using a **multi-agent pipeline architecture**.
Tracks project source files (Python, JSON, YAML, Markdown) via SHA-256 hashing.
On each run, detects changed files, orchestrates a 4-phase agent pipeline, and writes
structured AUD documentation into a versioned Word (.docx) template.

## Architecture: 4-Phase Multi-Agent Pipeline

```
Phase 1: RESEARCH   — Scanner Agent   → research/{project}_scan_report.yaml
Gate 1:               Skip if no changes (no API cost, no version bump)
Phase 2: PLANNING   — Analyzer Agent  → plans/{project}_doc_plan.yaml
Gate 2:               Devil's Advocate validates the doc plan
Phase 3: BUILD      — Builder Agent   → output/{project}/AUD_v{version}.docx
Gate 3:               Peer Reviewer validates document completeness
Phase 4: REVIEW     — Reviewer Agent  → testscripts/{project}_validation_report.yaml
                       Persists hashes to hash_store/ ONLY on PASS (idempotency)
```

## Core Philosophy

### From intelligent-doc-engine (preserved)
- **Efficiency**: Gate 1 stops the pipeline if no files changed — no wasted API calls
- **Idempotency**: `hash_store/` updated ONLY after Phase 4 PASS — failed runs retry from scratch
- **Concurrency**: `asyncio.gather` processes multiple projects in parallel
- **No silent errors**: All exceptions logged and propagated

### From devops toolkit (adopted)
- **Multi-agent roles**: Each agent owns its folder boundary (see `GUARDRAILS.yaml`)
- **Phase gates**: Non-negotiable quality checks between phases
- **Shared task list**: Lead Orchestrator coordinates via Claude Code agent teams
- **Guardrails enforcement**: `guardrails/run_guardrails.py` validates all conventions

## Stack
Python 3.11+, anthropic SDK, python-docx, PyYAML, hashlib, pathlib, asyncio, requests

## Azure DevOps Integration

### How it works
1. Source files change → commit pushed to ADO Git repo
2. `azure-pipelines.yml` triggers (path filter: `src/**`, `config.json`, `templates/**`)
3. Pipeline installs deps, runs `python -m src.runner` (4-phase pipeline)
4. `scripts/publish_to_wiki.py` uploads the generated `.docx` to the ADO Wiki as an attachment and updates the wiki page with a link

### Files
| File | Purpose |
|------|---------|
| `azure-pipelines.yml` | CI/CD pipeline definition |
| `scripts/publish_to_wiki.py` | Uploads `.docx` to ADO Wiki via REST API |

### Pipeline Variables (set in Azure DevOps → Pipelines → Variables)
| Variable | Description |
|---|---|
| `ANTHROPIC_API_KEY` | Claude API key (mark as **secret**) |
| `ADO_ORG_URL` | e.g. `https://dev.azure.com/myorg` |
| `ADO_PROJECT` | e.g. `MyProject` |
| `ADO_WIKI_ID` | Wiki identifier from the wiki URL |
| `ADO_WIKI_PAGE_PATH` | Page to create/update, e.g. `/Documentation/AUD` |

### Required Pipeline Permission
In your pipeline settings → enable **"Allow scripts to access the OAuth token"**
so that `$(System.AccessToken)` is available to `publish_to_wiki.py` for wiki API calls.

### No-op behaviour
If no source files changed (Gate 1 skip), no `.docx` is generated — `publish_to_wiki.py` detects this and exits cleanly without touching the wiki.

## Module Structure

### Core modules (unchanged — do not modify)
| Module | Purpose |
|--------|---------|
| `src/sha_scanner.py` | SHA-256 change detection, diff against `hash_store/{project}.json` |
| `src/claude_engine.py` | Anthropic API calls, structured doc section generation |
| `src/template_writer.py` | python-docx placeholder injection (`{{PLACEHOLDER}}` pattern) |
| `src/versioner.py` | Semantic versioning (major/minor/patch), archive management |

### Agent modules (new)
| Module | Phase | Writes To |
|--------|-------|-----------|
| `src/agents/scanner_agent.py` | Phase 1 | `research/` |
| `src/agents/analyzer_agent.py` | Phase 2 | `plans/` |
| `src/agents/builder_agent.py` | Phase 3 | `output/`, `scripts/` |
| `src/agents/reviewer_agent.py` | Phase 4 | `testscripts/`, `hash_store/` |

### Orchestration
| Module | Purpose |
|--------|---------|
| `src/runner.py` | Lead Orchestrator — runs 4-phase pipeline with gates, `asyncio.gather` concurrency |

### Guardrails
| Module | Purpose |
|--------|---------|
| `guardrails/run_guardrails.py` | Master validator — runs all 4 guards |
| `guardrails/input_guard.py` | Validates `config.json` structure |
| `guardrails/folder_guard.py` | Checks agent folder boundaries exist |
| `guardrails/phase_guard.py` | Validates phase prerequisites |
| `guardrails/content_guard.py` | Validates YAML files and validation reports |

## Agent Definitions (`.claude/agents/`)
| Agent | Role |
|-------|------|
| `lead-orchestrator.md` | Coordinates 4-phase pipeline via task list |
| `scanner-agent.md` | Phase 1: scan + diff |
| `analyzer-agent.md` | Phase 2: plan sections + version bump |
| `builder-agent.md` | Phase 3: generate + version + write |
| `reviewer-agent.md` | Phase 4: validate + persist hashes |
| `devils-advocate-agent.md` | Gate 2: challenge doc plans |
| `peer-reviewer-agent.md` | Gate 3: review generated docs |

## Folder Conventions
| Folder | Owner | Purpose |
|--------|-------|---------|
| `research/` | Scanner Agent | Scan reports with changed files and diff stats |
| `plans/` | Analyzer Agent | Doc plans with sections and version bump type |
| `scripts/` | Builder Agent | Reproducible doc builder scripts |
| `output/` | Builder Agent | Versioned Word documents (AUD_v{version}.docx) |
| `testscripts/` | Reviewer + Peer Reviewer + Devil's Advocate | Validation reports |
| `hash_store/` | **Reviewer Agent only** | SHA-256 snapshots — written ONLY on Phase 4 PASS |
| `templates/` | Read-only | Word document templates — never modified |

## Template Placeholders
`{{PROJECT_NAME}}`, `{{OVERVIEW}}`, `{{FILE_INVENTORY}}`, `{{DATA_FLOWS}}`,
`{{DEPENDENCIES}}`, `{{CONFIGURATION}}`, `{{KNOWN_ISSUES}}`, `{{VERSION}}`, `{{GENERATED_DATE}}`

## Output Structure
`output/{project_name}/AUD_v{version}.docx` + `output/{project_name}/version_history.json`

## Code Conventions
- All functions typed with type hints and docstrings
- Errors logged, never silently swallowed
- Config loaded from `config.json` at project root
- All file operations use `pathlib`
- Be idempotent — safe to re-run