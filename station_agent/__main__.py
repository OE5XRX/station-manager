"""Entry point for the Station Agent.

Usage:
  python -m station_agent                     run the agent (default)
  python -m station_agent selftest serial [--slot N] [--base PATH]
  python -m station_agent selftest audio  [--slot N] [--tx-freq HZ] [--rate HZ]
  python -m station_agent selftest audio-diag [--slot N] [--anchor C|U] [--freq HZ]
                                              [--level-dbfs DBFS] [--duration-ms MS]
  python -m station_agent selftest control-stability [--base PATH] [--duration S]
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
    audio_p.add_argument("--duration", type=float, default=1.0)

    diag_p = st_sub.add_parser(
        "audio-diag", help="audio-path diagnostics measure/inject (needs PipeWire)"
    )
    diag_p.add_argument("--slot", type=int, default=1)
    diag_p.add_argument("--anchor", choices=["C", "U"], default="C")
    diag_p.add_argument("--freq", type=int, default=1000)
    diag_p.add_argument("--level-dbfs", type=float, default=-20.0)
    diag_p.add_argument("--duration-ms", type=int, default=500)

    stab_p = st_sub.add_parser(
        "control-stability",
        help="re-discovery under concurrent telemetry-poll load (PR #135 flap gate)",
    )
    stab_p.add_argument("--base", default="/dev/oe5xrx")
    stab_p.add_argument("--duration", type=float, default=20.0)
    stab_p.add_argument("--poll-hz", type=float, default=10.0)
    stab_p.add_argument("--rescan-s", type=float, default=2.0)

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
                duration=args.duration,
            )
        if args.what == "audio-diag":
            import json

            from station_agent.audio import diagnostics
            from station_agent.audio.router_backend import PipeWireRouterBackend

            report = diagnostics.run_diagnostic(
                anchor=args.anchor,
                slot=args.slot,
                signal={
                    "kind": "sine",
                    "freq_hz": args.freq,
                    "level_dbfs": args.level_dbfs,
                    "duration_ms": args.duration_ms,
                },
                backend=PipeWireRouterBackend(),
            )
            print(json.dumps(report, indent=2))
            return 0
        if args.what == "control-stability":
            from station_agent import selftest

            return selftest.run_control_stability(
                args.base,
                duration=args.duration,
                poll_hz=args.poll_hz,
                rescan_s=args.rescan_s,
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
