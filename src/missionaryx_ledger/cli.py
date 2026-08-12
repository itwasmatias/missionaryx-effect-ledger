"""Command-line interface for the effect ledger."""

import argparse
import sys
from pathlib import Path

from missionaryx_ledger import demo
from missionaryx_ledger.ledger import EffectLedger


def cmd_demo(args: argparse.Namespace) -> int:
    """Run a demo scenario."""
    scenario = args.scenario

    if scenario == "connection-loss":
        demo.demo_connection_loss()
    elif scenario == "invalid-address":
        demo.demo_invalid_address()
    elif scenario == "accepted":
        demo.demo_accepted()
    else:
        print(f"Unknown scenario: {scenario}", file=sys.stderr)
        print("Valid scenarios: connection-loss, invalid-address, accepted", file=sys.stderr)
        return 1

    return 0


def cmd_events(args: argparse.Namespace) -> int:
    """Show events for an effect."""
    effect_id = args.effect_id
    db_path = args.db

    if not Path(db_path).exists():
        print(f"Database not found: {db_path}", file=sys.stderr)
        return 1

    try:
        ledger = EffectLedger(db_path)
        events = ledger.events_for(effect_id)

        print(f"\nEvent Timeline for {effect_id}")
        print("=" * 70)

        for event in events:
            print(f"\n[{event.sequence}] {event.event_type}")
            print(f"    Timestamp: {event.timestamp.isoformat()}")
            if event.evidence_reference:
                print(f"    Evidence: {event.evidence_reference}")
            if event.metadata:
                print(f"    Metadata: {event.metadata}")

        print()
        return 0

    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1


def main() -> int:
    """Main CLI entry point."""
    parser = argparse.ArgumentParser(
        prog="missionaryx-ledger",
        description="MissionaryX Effect Ledger - Honest, governed execution for AI missions",
    )

    subparsers = parser.add_subparsers(dest="command", help="Commands")

    # Demo command
    demo_parser = subparsers.add_parser("demo", help="Run a demonstration scenario")
    demo_parser.add_argument(
        "scenario",
        choices=["connection-loss", "invalid-address", "accepted"],
        help="Scenario to demonstrate",
    )
    demo_parser.set_defaults(func=cmd_demo)

    # Events command
    events_parser = subparsers.add_parser("events", help="Show event timeline for an effect")
    events_parser.add_argument("effect_id", help="Effect ID to query")
    events_parser.add_argument("--db", required=True, help="Path to database file")
    events_parser.set_defaults(func=cmd_events)

    args = parser.parse_args()

    if not hasattr(args, "func"):
        parser.print_help()
        return 1

    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
