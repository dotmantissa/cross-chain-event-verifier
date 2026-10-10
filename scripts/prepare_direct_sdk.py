#!/usr/bin/env python3
"""Prepare the release bundle containing this project's pinned direct SDK.

genlayer-test 0.29.2 requests genvm-universal.tar.xz from the latest release
on a fresh machine. New releases renamed that asset. Use the known bundle
containing the concrete runner required by the contracts instead.
"""
from pathlib import Path

from gltest.direct.sdk_loader import setup_sdk_paths


if __name__ == "__main__":
    setup_sdk_paths(Path("contracts/cross_chain_event_verifier.py"), version="v0.2.16")
    print("Direct SDK ready: release v0.2.16, runner pinned by contract header")
