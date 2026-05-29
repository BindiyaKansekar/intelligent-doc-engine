@echo off
setlocal enabledelayedexpansion

REM Create directories
mkdir "c:\Work\intelligent-doc-engine\research" 2>nul
mkdir "c:\Work\intelligent-doc-engine\plans" 2>nul
mkdir "c:\Work\intelligent-doc-engine\scripts" 2>nul
mkdir "c:\Work\intelligent-doc-engine\testscripts" 2>nul
mkdir "c:\Work\intelligent-doc-engine\thoughts" 2>nul
mkdir "c:\Work\intelligent-doc-engine\.claude\agents" 2>nul
mkdir "c:\Work\intelligent-doc-engine\guardrails" 2>nul
mkdir "c:\Work\intelligent-doc-engine\src\agents" 2>nul

REM Create .gitkeep files
type nul > "c:\Work\intelligent-doc-engine\research\.gitkeep"
type nul > "c:\Work\intelligent-doc-engine\plans\.gitkeep"
type nul > "c:\Work\intelligent-doc-engine\scripts\.gitkeep"
type nul > "c:\Work\intelligent-doc-engine\testscripts\.gitkeep"
type nul > "c:\Work\intelligent-doc-engine\thoughts\.gitkeep"

echo Directories and .gitkeep files created successfully!
