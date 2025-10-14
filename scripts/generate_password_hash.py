"""
generate_password_hash.py
----------------------------------------
Interactive bcrypt-based password hasher for the personal_finance project.

Usage:
  python scripts/generate_password_hash.py --env sandbox --write
  python scripts/generate_password_hash.py --env production --write
  python scripts/generate_password_hash.py --verify <password> <hash>

Notes:
  - Updates the correct config/env/.env.<env> file automatically.
  - Uses bcrypt for salted, adaptive hashing.
"""

import bcrypt
import getpass
import argparse
from pathlib import Path


def generate_hash(password: str) -> str:
    """Generate a bcrypt hash for a plaintext password."""
    return bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()


def verify_hash(password: str, hashed: str) -> bool:
    """Verify a password against a given bcrypt hash."""
    try:
        return bcrypt.checkpw(password.encode(), hashed.encode())
    except Exception:
        return False


def main():
    parser = argparse.ArgumentParser(description="Generate or verify bcrypt password hashes for .env.<env>")
    parser.add_argument("--env", choices=["sandbox", "production"], default="sandbox",
                        help="Target environment (default: sandbox)")
    parser.add_argument("--write", action="store_true",
                        help="Write the generated hash line directly into config/env/.env.<env>")
    parser.add_argument("--verify", nargs=2, metavar=("PASSWORD", "HASH"),
                        help="Verify a plaintext password against a bcrypt hash")
    args = parser.parse_args()

    if args.verify:
        password, hashed = args.verify
        ok = verify_hash(password, hashed)
        print("✅ Match" if ok else "❌ No match")
        return

    password = getpass.getpass("Enter password to hash: ")
    confirm = getpass.getpass("Confirm password: ")
    if password != confirm:
        print("Passwords do not match.")
        return

    hashed = generate_hash(password)
    env_key = f"REPORTS_PASS_HASH_{args.env.upper()}"
    line = f"{env_key}={hashed}"
    print(f"\nGenerated bcrypt hash line:\n{line}\n")

    if args.write:
        env_path = Path(__file__).resolve().parents[1] / f"config/env/.env.{args.env}"
        if not env_path.exists():
            print(f"Creating new {env_path}")
        # Backup existing
        backup = env_path.with_suffix(env_path.suffix + ".bak")
        if env_path.exists():
            env_path.replace(backup)
            print(f"Backup saved as {backup.name}")
        # Write new hash
        lines = []
        if backup.exists():
            lines = backup.read_text(encoding="utf-8").splitlines()
        found = False
        for i, line_ in enumerate(lines):
            if line_.startswith(env_key + "="):
                lines[i] = line
                found = True
        if not found:
            lines.append(line)
        env_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        print(f"✅ Updated {env_path}")
    else:
        print("Use --write to append automatically.")


if __name__ == "__main__":
    main()
