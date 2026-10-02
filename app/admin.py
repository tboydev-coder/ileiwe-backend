"""Local CLI for the explicitly privileged platform administrator bootstrap."""

import argparse
from sqlalchemy import select
from .core.database import SessionLocal, migrate
from .core.security import assign_role, audit
from .models import User


def main():
    parser = argparse.ArgumentParser(description="Promote an existing school owner to platform administrator.")
    parser.add_argument("--email", required=True)
    args = parser.parse_args()
    migrate()
    with SessionLocal.begin() as db:
        user = db.scalar(select(User).where(User.email == args.email.lower()))
        if not user:
            raise SystemExit("Account not found. Onboard a platform workspace first.")
        if user.role != "SCHOOL_OWNER":
            raise SystemExit("Only an existing school owner can be bootstrapped as platform administrator.")
        user.role = "PLATFORM_SUPER_ADMIN"
        user.token_version += 1
        assign_role(db, user)
        audit(db, user, "platform.admin_bootstrapped", "users", user.id)
    print("Platform administrator enabled. Sign in again to use platform controls.")


if __name__ == "__main__":
    main()
