"""Entry point for the Station Agent.

Usage:
  python -m station_agent                     run the agent (default)
  python -m station_agent selftest serial [--slot N] [--base PATH]
  python -m station_agent selftest audio  [--slot N] [--tx-freq HZ] [--rate HZ]
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

    audio_p = st_sub.add_parser("audio", help="audio-path test against a slot (needs PipeWire)")
    audio_p.add_argument("--slot", type=int, default=1)  # sim=slot1; bench=slot3
    audio_p.add_argument("--tx-freq", type=int, default=1500)
    audio_p.add_argument("--rate", type=int, default=8000)
    audio_p.add_argument("--base", default="/dev/oe5xrx")
    audio_p.add_argument("--duration", type=float, default=1.0)

    args = parser.parse_args(argv)

    if args.cmd == "selftest":
        if args.what == "serial":
            from station_agent import selftest

            path = f"{args.base}/slot{args.slot}/control"
            return selftest.run_serial(path, timeout=args.timeout)
        if args.what == "audio":
            from station_agent.audio import selftest as audio_selftest

            return audio_selftest.run_audio(
                slot=args.slot,
                tx_freq=args.tx_freq,
                rate=args.rate,
                base=args.base,
                duration=args.duration,
            )
        # `selftest` with no/unknown sub-command must NOT silently start the
        # long-running agent (a typo would otherwise boot production behaviour).
        st.print_help(sys.stderr)
        return 2

    # Default (no sub-command): run the agent.
    StationAgent().run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
