#!/bin/sh
set -eu
printf "local-probe artifact\n" > "$AGENTLOOM_SKILL_WORKSPACE/local_probe_artifact.txt"
printf "wrote %s\n" "$AGENTLOOM_SKILL_WORKSPACE/local_probe_artifact.txt"
