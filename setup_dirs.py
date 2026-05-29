import os
import sys

dirs = [
    r'c:\Work\intelligent-doc-engine\research',
    r'c:\Work\intelligent-doc-engine\plans',
    r'c:\Work\intelligent-doc-engine\scripts',
    r'c:\Work\intelligent-doc-engine\testscripts',
    r'c:\Work\intelligent-doc-engine\thoughts',
    r'c:\Work\intelligent-doc-engine\.claude\agents',
    r'c:\Work\intelligent-doc-engine\guardrails',
    r'c:\Work\intelligent-doc-engine\src\agents',
]

gitkeep_dirs = [
    r'c:\Work\intelligent-doc-engine\research',
    r'c:\Work\intelligent-doc-engine\plans',
    r'c:\Work\intelligent-doc-engine\scripts',
    r'c:\Work\intelligent-doc-engine\testscripts',
    r'c:\Work\intelligent-doc-engine\thoughts',
]

# Create all directories
for dir_path in dirs:
    try:
        os.makedirs(dir_path, exist_ok=True)
        print(f'Created: {dir_path}')
    except Exception as e:
        print(f'Error creating {dir_path}: {e}', file=sys.stderr)

# Create .gitkeep files
for dir_path in gitkeep_dirs:
    gitkeep_path = os.path.join(dir_path, '.gitkeep')
    try:
        with open(gitkeep_path, 'w') as f:
            pass
        print(f'Created .gitkeep: {gitkeep_path}')
    except Exception as e:
        print(f'Error creating .gitkeep in {dir_path}: {e}', file=sys.stderr)

print('Done!')
