"""
One-off tool to reset a JARVIS account's password directly in the database.
Run this from apps\\backend with the venv activated and Docker Postgres running.

Usage:
    python reset_password.py

You'll be prompted for the account email and a new password (hidden as you type).
Nothing is printed or sent anywhere except your own local database.
"""

import getpass

from app.database import SessionLocal
from app.models import User
from app.auth import hash_password


def main():
    email = input("Email of the account to reset: ").strip()

    db = SessionLocal()
    try:
        user = db.query(User).filter(User.email == email).first()
        if not user:
            print(f"No account found with email: {email}")
            return

        new_password = getpass.getpass("New password (hidden while typing): ").strip()
        confirm = getpass.getpass("Confirm new password: ").strip()

        if not new_password:
            print("Password cannot be empty. Aborting.")
            return
        if new_password != confirm:
            print("Passwords didn't match. Aborting — nothing was changed.")
            return

        user.hashed_password = hash_password(new_password)
        db.commit()
        print(f"Done — password updated for {email}. You can log in with it now.")
    finally:
        db.close()


if __name__ == "__main__":
    main()
