---
name: agent-visualization
description: "Passive observer. Auto-collects agent lifecycle events into visualization.json. Invisible to AI."
version: "1.0.0"
hooks:
  TaskCreated:
    - hooks:
        - type: command
          command: python ./scripts/on_task_start.py
  TaskCompleted:
    - hooks:
        - type: command
          command: python ./scripts/on_task_complete.py
  StopFailure:
    - hooks:
        - type: command
          command: python ./scripts/on_task_fail.py
  SubagentStart:
    - matcher: "*"
      hooks:
        - type: command
          command: python ./scripts/on_subtask_start.py
  SubagentStop:
    - matcher: "*"
      hooks:
        - type: command
          command: python ./scripts/on_subtask_finish.py
  PreToolUse:
    - matcher: "*"
      hooks:
        - type: command
          command: python ./scripts/on_pre_tool_use.py
  PostToolUse:
    - matcher: "*"
      hooks:
        - type: command
          command: python ./scripts/on_post_tool_use.py
  PostToolUseFailure:
    - matcher: "*"
      hooks:
        - type: command
          command: python ./scripts/on_post_tool_error.py
---
