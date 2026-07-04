#!/bin/bash
set -e

# Composer entrypoint: forward all arguments to the Python orchestrator.
exec python -m composer "$@"
