"""Makerator entrypoint — stub.

Phase 0 placeholder so deploy.sh has something to invoke. The real loop
lands in Phase 3 (paper) / Phase 4 (live) once Manifest order primitives
are built in Phase 2.
"""
import sys


def main() -> int:
    print("makerator: phase-0 stub. no order loop yet.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
