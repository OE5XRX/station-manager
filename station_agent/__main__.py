"""Entry point for the Station Agent.

Usage:
  python -m station_agent                     run the agent (default)
  python -m station_agent selftest serial [--slot N] [--base PATH]
"""
import argparse
import sys

from .agent import StationAgent


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="station_agent")
    sub = parser.add_subparsers(dest="cmd")
    st = sub.add_parser("selftest", help="run a self-test")
    st_sub = st.add_subparsers(dest="what")
    serial_p = st_sub.add_parser("serial", help="serial contract test against a slot")
    serial_p.add_argument("--slot", type=int, default=0)
    serial_p.add_argument("--base", default="/dev/oe5xrx")
    serial_p.add_argument("--timeout", type=float, default=3.0)

    args = parser.parse_args(argv)

    if args.cmd == "selftest":
        if args.what == "serial":
            from station_agent import selftest

            path = f"{args.base}/slot{args.slot}/control"
            return selftest.run_serial(path, timeout=args.timeout)
        # `selftest` with no/unknown sub-command must NOT silently start the
        # long-running agent (a typo would otherwise boot production behaviour).
        st.print_help(sys.stderr)
        return 2

    # Default (no sub-command): run the agent.
    StationAgent().run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
