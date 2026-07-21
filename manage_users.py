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
    python manage_users.py passwd roopa@example.com
    python manage_users.py role roopa@example.com --role admin
    python manage_users.py delete roopa@example.com

Passwords are prompted for (hidden) unless --password is given. Prefer the
prompt: an argument lands in your shell history.

The first account you create should be an admin - only admins can reach
settings, logs and the scheduler controls.
"""

import argparse
import getpass
import sys

import auth

MIN_PASSWORD_LENGTH = 8


def _prompt_password(email: str) -> str:
    """Ask for a password twice, without echoing it."""
    for _ in range(3):
        first = getpass.getpass(f"Password for {email}: ")
        if len(first) < MIN_PASSWORD_LENGTH:
            print(f"  Too short - use at least {MIN_PASSWORD_LENGTH} characters.")
            continue
        second = getpass.getpass("Repeat password: ")
        if first != second:
            print("  Passwords did not match.")
            continue
        return first
    print("Giving up after 3 attempts.")
    sys.exit(1)


def _resolve_password(args, email: str) -> str:
    if args.password:
        if len(args.password) < MIN_PASSWORD_LENGTH:
            print(f"Password must be at least {MIN_PASSWORD_LENGTH} characters.")
            sys.exit(1)
        return args.password
    return _prompt_password(email)


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
    password = _resolve_password(args, args.email)
    try:
        user = auth.add_user(args.email, password, args.role)
    except ValueError as e:
        print(f"Error: {e}")
        sys.exit(1)
    print(f"Created {user['email']} ({user['role']}).")
    if user["role"] != auth.ROLE_ADMIN:
        print("Assign monitors to them by setting Owner on the monitor's edit page.")


def cmd_passwd(args):
    password = _resolve_password(args, args.email)
    try:
        auth.set_password(args.email, password)
    except ValueError as e:
        print(f"Error: {e}")
        sys.exit(1)
    print(f"Password updated for {auth.normalize_email(args.email)}.")


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
    p_add.add_argument("--password", help="Skip the prompt (avoid: lands in shell history)")
    p_add.set_defaults(func=cmd_add)

    p_pw = sub.add_parser("passwd", help="Change an account password")
    p_pw.add_argument("email")
    p_pw.add_argument("--password", help="Skip the prompt (avoid: lands in shell history)")
    p_pw.set_defaults(func=cmd_passwd)

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
