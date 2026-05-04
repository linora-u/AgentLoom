#!/bin/bash
# Switch to the directory where the script is located (project root)
cd "$(dirname "$0")" || exit 1

# Activate virtual environment
source .venv/bin/activate
export PYTHONPATH=.

echo "============================================"
echo "🚀 Starting automated tests..."
echo "============================================"

# Use the passed argument as the target file/directory, otherwise default to the tests/ directory
TARGET=${1:-tests/}

# Run pytest
# Parameter explanations:
# -v: Verbose mode, clearly lists each test file and test case
# --tb=short: Shorter traceback formatting, only shows the line that caused the error
# --color=yes: Colored output for better readability
.venv/bin/pytest "$TARGET" -v --tb=short --color=yes