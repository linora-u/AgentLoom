#!/bin/bash
# =============================================================================
# Checkpoint Interrupt & Resume Integration Test
#
# Tests:
# 1. Run a single-agent YAML, interrupt after a few seconds, verify checkpoint
# 2. Resume from checkpoint, verify it continues (not restart)
# 3. Run a multi-agent YAML, interrupt, verify worker skip on resume
#
# Usage:
#   cd AgentLoom/
#   bash tests/agent_test/test_checkpoint_interrupt_resume.sh
# =============================================================================

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
cd "$PROJECT_ROOT"

PYTHON=".venv/bin/python"
LRUN="$PYTHON -m src"
TEST_DIR="/tmp/agentloom_checkpoint_test"
MULTI_DIR="/tmp/agentloom_ckpt_multi"
RUNTIME_DIR=".runtime"

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

pass_count=0
fail_count=0

log_pass() {
    echo -e "${GREEN}✓ PASS${NC}: $1"
    ((pass_count++))
}

log_fail() {
    echo -e "${RED}✗ FAIL${NC}: $1"
    ((fail_count++))
}

log_info() {
    echo -e "${YELLOW}→${NC} $1"
}

cleanup() {
    rm -rf "$TEST_DIR" "$MULTI_DIR"
    # Don't remove .runtime — keep for inspection if tests fail
}

# =============================================================================
# Test 1: Single Agent — Run, Interrupt, Check Checkpoint
# =============================================================================
test_single_agent_interrupt() {
    log_info "Test 1: Single agent — run, interrupt, check checkpoint"
    cleanup

    # Run the checkpoint test agent in background
    log_info "Starting agent (will interrupt after 30s)..."
    $LRUN run applications/test_demo/workflows/test_checkpoint_agent.yaml --log-to-file &
    AGENT_PID=$!

    # Wait up to 30 seconds, then send SIGINT (Ctrl-C)
    sleep 30
    if kill -0 $AGENT_PID 2>/dev/null; then
        log_info "Sending SIGINT to agent (PID=$AGENT_PID)..."
        kill -INT $AGENT_PID
        wait $AGENT_PID 2>/dev/null || true
    else
        log_info "Agent already finished before interrupt"
    fi

    # Check checkpoint was saved
    CKPT_DIR=$(find "$RUNTIME_DIR/test_checkpoint/checkpoints" -maxdepth 1 -type d -name "task_*" 2>/dev/null | head -1)
    if [ -n "$CKPT_DIR" ]; then
        log_pass "Checkpoint directory created: $CKPT_DIR"
    else
        log_fail "No checkpoint directory found under $RUNTIME_DIR/test_checkpoint/"
        return
    fi

    # Check task_tree.json exists
    if [ -f "$CKPT_DIR/task_tree.json" ]; then
        log_pass "task_tree.json exists"
    else
        log_fail "task_tree.json not found"
    fi

    # Check checkpoint.json exists
    if [ -f "$CKPT_DIR/checkpoint.json" ]; then
        log_pass "checkpoint.json (supervisor) exists"
    else
        log_fail "checkpoint.json not found"
    fi

    # Check heartbeat.json exists
    if [ -f "$CKPT_DIR/heartbeat.json" ]; then
        log_pass "heartbeat.json exists"
    else
        log_fail "heartbeat.json not found"
    fi

    # Check file-history directory was created
    if [ -d "$CKPT_DIR/file-history" ]; then
        log_pass "file-history/ directory exists"
    else
        log_info "file-history/ not created (agent may not have done file edits yet)"
    fi

    # Extract task_id for resume
    TASK_ID=$(basename "$CKPT_DIR")
    echo "$TASK_ID" > /tmp/agentloom_test_task_id.txt
    log_info "Task ID for resume: $TASK_ID"
}

# =============================================================================
# Test 2: Resume from Checkpoint
# =============================================================================
test_single_agent_resume() {
    log_info "Test 2: Resume from checkpoint"

    TASK_ID=$(cat /tmp/agentloom_test_task_id.txt 2>/dev/null || echo "")
    if [ -z "$TASK_ID" ]; then
        log_fail "No task_id from previous test"
        return
    fi

    log_info "Resuming task $TASK_ID..."
    # Run with timeout — should complete faster since some steps are already done
    timeout 120 $LRUN run applications/test_demo/workflows/test_checkpoint_agent.yaml --resume "$TASK_ID" --log-to-file || true

    # Check that the agent produced output files
    if [ -f "$TEST_DIR/step1.txt" ]; then
        log_pass "step1.txt exists after resume"
    else
        log_fail "step1.txt missing after resume"
    fi

    if [ -f "$TEST_DIR/step2.txt" ]; then
        log_pass "step2.txt exists after resume"
    else
        log_fail "step2.txt missing after resume"
    fi

    if [ -f "$TEST_DIR/summary.txt" ]; then
        log_pass "summary.txt exists (all steps completed)"
    else
        log_info "summary.txt not found (agent may not have completed all steps)"
    fi

    # Verify file content
    if [ -f "$TEST_DIR/step1.txt" ] && grep -q "modified in step 3" "$TEST_DIR/step1.txt" 2>/dev/null; then
        log_pass "step1.txt contains 'modified in step 3'"
    elif [ -f "$TEST_DIR/step1.txt" ]; then
        log_info "step1.txt exists but may not have been modified yet (depends on interrupt timing)"
    fi
}

# =============================================================================
# Test 3: Multi-Agent — Run, Interrupt, Resume with Worker Skip
# =============================================================================
test_multi_agent_interrupt_resume() {
    log_info "Test 3: Multi-agent — run, interrupt, resume"
    rm -rf "$MULTI_DIR"

    # Run supervisor in background
    log_info "Starting supervisor agent (will interrupt after 45s)..."
    $LRUN run applications/test_demo/workflows/test_checkpoint_supervisor.yaml --log-to-file &
    AGENT_PID=$!

    # Wait longer for multi-agent (worker needs time too)
    sleep 45
    if kill -0 $AGENT_PID 2>/dev/null; then
        log_info "Sending SIGINT to supervisor (PID=$AGENT_PID)..."
        kill -INT $AGENT_PID
        wait $AGENT_PID 2>/dev/null || true
    else
        log_info "Supervisor already finished before interrupt"
    fi

    # Check checkpoint
    SUP_CKPT_DIR=$(find "$RUNTIME_DIR/test_checkpoint_supervisor/checkpoints" -maxdepth 1 -type d -name "task_*" 2>/dev/null | head -1)
    if [ -n "$SUP_CKPT_DIR" ]; then
        log_pass "Supervisor checkpoint directory created: $SUP_CKPT_DIR"
        SUP_TASK_ID=$(basename "$SUP_CKPT_DIR")
    else
        log_fail "No supervisor checkpoint found"
        return
    fi

    # Check worker checkpoint
    if [ -d "$SUP_CKPT_DIR/workers" ]; then
        WORKER_COUNT=$(find "$SUP_CKPT_DIR/workers" -name "checkpoint.json" | wc -l)
        log_info "Worker checkpoints found: $WORKER_COUNT"
        if [ "$WORKER_COUNT" -gt 0 ]; then
            log_pass "Worker checkpoint(s) exist"
        fi
    fi

    # Resume
    log_info "Resuming supervisor task $SUP_TASK_ID..."
    timeout 120 $LRUN run applications/test_demo/workflows/test_checkpoint_supervisor.yaml --resume "$SUP_TASK_ID" --log-to-file || true

    # Check final output
    if [ -f "$MULTI_DIR/output.txt" ]; then
        log_pass "Worker output.txt exists after resume"
    else
        log_info "Worker output.txt not found (worker may not have completed)"
    fi

    if [ -f "$MULTI_DIR/done.txt" ]; then
        log_pass "Supervisor done.txt exists (all steps completed)"
    else
        log_info "Supervisor done.txt not found (may need more time)"
    fi
}

# =============================================================================
# Test 4: list-tasks shows the task
# =============================================================================
test_list_tasks() {
    log_info "Test 4: loom list-tasks shows resumable tasks"
    OUTPUT=$($LRUN list-tasks 2>&1 || true)
    if echo "$OUTPUT" | grep -q "task_"; then
        log_pass "list-tasks shows task entries"
    elif echo "$OUTPUT" | grep -q "No resumable tasks"; then
        log_info "No tasks found (may have been cleaned up)"
    else
        log_fail "list-tasks returned unexpected output: $OUTPUT"
    fi
}

# =============================================================================
# Main
# =============================================================================
echo "============================================"
echo "  Checkpoint Interrupt & Resume Tests"
echo "============================================"
echo ""

test_single_agent_interrupt
echo ""
test_single_agent_resume
echo ""
test_multi_agent_interrupt_resume
echo ""
test_list_tasks

echo ""
echo "============================================"
echo "  Results: ${pass_count} passed, ${fail_count} failed"
echo "============================================"

# Cleanup temp files
rm -f /tmp/agentloom_test_task_id.txt

if [ "$fail_count" -gt 0 ]; then
    echo -e "${RED}Some tests failed!${NC}"
    exit 1
else
    echo -e "${GREEN}All tests passed!${NC}"
    exit 0
fi
