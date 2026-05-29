"""
Intelligent Documentation Engine
==================================
AI-powered documentation utility that detects source-file changes via SHA-256
hashing and generates structured AUD (.docx) documentation via the Claude API.

Modules:
    sha_scanner     -- SHA-256 change detection against a persistent hash store
    claude_engine   -- Anthropic API calls and structured section generation
    template_writer -- python-docx placeholder injection ({{PLACEHOLDER}} style)
    versioner       -- Semantic versioning and archive management
    runner          -- Async multi-project orchestration entry-point
"""

__version__ = "0.1.0"
