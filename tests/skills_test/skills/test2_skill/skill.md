---
name: hooks-skill-2
description: hooks test (relative path)
hooks:
  PreToolUse:
    - matcher: "Write"
      hooks:
        - type: command
          command: python ./scripts/pre_tool_hook.py
  PostToolUse:
    - matcher: "*"
      hooks:
        - type: command
          command: python ./scripts/post_tool_hook.py
  Stop:
    - matcher: "*"
      hooks:
        - type: command
          command: python ./scripts/stop_hook.py
---
# Body
