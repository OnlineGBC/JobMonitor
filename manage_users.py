#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
=============================================================================
JobMonitor — User Account Management
=============================================================================

Create and manage web UI accounts. There is deliberately no sign-up page: the
only way to get an account is for someone with shell access to create it.

Usage:
    python manage_users.py list
    python manage_users.py add raja@onlinegbc.com --role admin
    python manage_users.py add roopa@example.com
    python manage_users.py role roopa@example.com --role admin
    python manage_users.py delete roopa@example.com

There are no passwords to set. Signing in means requesting a one-time code,
which is emailed to the address using the SMTP settings in .env - so the address
you enter here must be one the person can actually read.

The first account you create should be an admin - only admins can reach
settings, logs and the scheduler controls.
"""

import argparse
import sys

import auth


def cmd_list(args):
    users = auth.load_users()
    if not users:
        print("No users yet. Create an admin first:")
        print("  python manage_users.py add you@example.com --role admin")
        return
    width = max(len(u.get("email", "")) for u in users)
    print(f"{'EMAIL'.ljust(width)}  ROLE")
    for u in users:
        print(f"{u.get('email', '').ljust(width)}  {u.get('role', 'user')}")


def cmd_add(args):
    try:
        user = auth.add_user(args.email, args.role)
    except ValueError as e:
        print(f"Error: {e}")
        sys.exit(1)
    print(f"Created {user['email']} ({user['role']}).")
    print("They sign in at the web UI by requesting a code sent to that address.")
    if user["role"] != auth.ROLE_ADMIN:
        print("Assign monitors to them by setting Owner on the monitor's edit page.")


def cmd_role(args):
    try:
        auth.set_role(args.email, args.role)
    except ValueError as e:
        print(f"Error: {e}")
        sys.exit(1)
    print(f"{auth.normalize_email(args.email)} is now {args.role}.")


def cmd_delete(args):
    try:
        auth.delete_user(args.email)
    except ValueError as e:
        print(f"Error: {e}")
        sys.exit(1)
    print(f"Deleted {auth.normalize_email(args.email)}.")
    print("Any monitors they owned are now admin-only until reassigned.")


def main():
    parser = argparse.ArgumentParser(
        description="Manage JobMonitor web UI accounts",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("list", help="List all accounts").set_defaults(func=cmd_list)

    p_add = sub.add_parser("add", help="Create an account")
    p_add.add_argument("email")
    p_add.add_argument("--role", choices=auth.VALID_ROLES, default=auth.ROLE_USER)
    p_add.set_defaults(func=cmd_add)

    p_role = sub.add_parser("role", help="Change an account role")
    p_role.add_argument("email")
    p_role.add_argument("--role", choices=auth.VALID_ROLES, required=True)
    p_role.set_defaults(func=cmd_role)

    p_del = sub.add_parser("delete", help="Delete an account")
    p_del.add_argument("email")
    p_del.set_defaults(func=cmd_delete)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
