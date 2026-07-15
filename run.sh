#!/usr/bin/env bash
cd "$(dirname "$0")"
exec .venv/bin/python3 vicy.py "$@"
