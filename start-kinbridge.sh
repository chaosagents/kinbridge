#!/usr/bin/env bash
# Launches Kinbridge from wherever this script lives, regardless of the
# current working directory.
set -e
cd "$(dirname "$0")"
python3 kinbridge.py
