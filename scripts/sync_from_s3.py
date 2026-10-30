#!/usr/bin/env python3
"""Compatibility entrypoint for the guarded canonical restore operation.

Uses the application's existing settings; never searches alternate public keys
or replaces divergent local state. No credentials are read by this wrapper.
"""
from sg_law_cookies.cli import main

if __name__ == "__main__":
    raise SystemExit(main(["restore"]))
