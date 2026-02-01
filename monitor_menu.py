#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
=============================================================================
LinkedIn Job Monitor - Interactive Menu
=============================================================================

WHAT THIS SCRIPT DOES:
    Provides an interactive menu to run the LinkedIn job monitor either
    once (on-demand) or as a scheduled background loop.

HOW TO USE:
    python monitor_menu.py

MENU OPTIONS:
    1. Run once - Executes monitor.py once and returns to menu
    2. Run as scheduled job - Runs monitor.py on a schedule (Ctrl+C to stop)
    3. Exit - Exits the program
"""

import sys
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

# Import functions from existing modules
from monitor import main as run_once, configure_logging
from run_monitor import run_monitor_loop


def show_menu():
    """Display the main menu and return user's choice."""
    print()
    print("=" * 40)
    print("   LinkedIn Job Monitor")
    print("=" * 40)
    print()
    print("  1. Run once")
    print("  2. Run as scheduled job")
    print("  3. Exit")
    print()

    while True:
        choice = input("Select option (1-3): ").strip()
        if choice in ("1", "2", "3"):
            return choice
        print("Invalid option. Please enter 1, 2, or 3.")


def main():
    configure_logging()

    while True:
        choice = show_menu()

        if choice == "1":
            # Run once
            print()
            print("Running monitor once...")
            print("-" * 40)
            try:
                run_once()
            except SystemExit as e:
                # monitor.py calls sys.exit() - catch it to return to menu
                if e.code != 0:
                    print(f"Monitor exited with code {e.code}")
            except Exception as e:
                print(f"Error: {e}")
            print("-" * 40)
            print("Completed. Returning to menu...")

        elif choice == "2":
            # Run as scheduled job
            print()
            print("Starting scheduled job (Ctrl+C to stop)...")
            print("-" * 40)
            try:
                run_monitor_loop()
            except KeyboardInterrupt:
                print()
                print("Job stopped by user.")
            except Exception as e:
                print(f"Error: {e}")
            print("-" * 40)
            print("Returning to menu...")

        elif choice == "3":
            # Exit
            print()
            print("Goodbye!")
            break


if __name__ == "__main__":
    main()
