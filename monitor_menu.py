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
    3. Test custom schedule - Run with a custom interval (5-59 minutes)
    4. Exit - Exits the program
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
    print("  3. Test custom schedule")
    print("  4. Exit")
    print()

    while True:
        choice = input("Select option (1-4): ").strip()
        if choice in ("1", "2", "3", "4"):
            return choice
        print("Invalid option. Please enter 1, 2, 3, or 4.")


def get_custom_interval():
    """Prompt user for custom interval in minutes (5-59)."""
    while True:
        try:
            minutes = input("Enter interval in minutes (5-59): ").strip()
            minutes = int(minutes)
            if 5 <= minutes <= 59:
                return minutes
            print("Please enter a number between 5 and 59.")
        except ValueError:
            print("Please enter a valid number.")


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
            # Test custom schedule
            print()
            interval = get_custom_interval()
            print()
            print(f"Starting custom schedule: every {interval} minutes (Ctrl+C to stop)...")
            print("-" * 40)
            try:
                run_monitor_loop(custom_interval_minutes=interval)
            except KeyboardInterrupt:
                print()
                print("Job stopped by user.")
            except Exception as e:
                print(f"Error: {e}")
            print("-" * 40)
            print("Returning to menu...")

        elif choice == "4":
            # Exit
            print()
            print("Goodbye!")
            break


if __name__ == "__main__":
    main()
