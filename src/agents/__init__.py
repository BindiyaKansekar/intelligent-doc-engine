"""
agents sub-package for the Intelligent Documentation Engine.

Contains the phase-specific agent modules that implement the multi-agent pipeline:

  scanner_agent     -- Phase 1: scan source files, detect changes
  analyzer_agent    -- Phase 2: plan doc sections and version bump type
  builder_agent     -- Phase 3: call Claude API, write versioned Word doc
  reviewer_agent    -- Phase 4: validate document, persist hashes (idempotency)
  integration_agent -- Integration pipeline: generate wiki from PR + ADO context
"""
