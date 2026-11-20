"""Create the Supabase administrator once using the project's server secret."""

import getpass
import hashlib
import hmac
import secrets

from supabase_accounts import ADMIN_EMAIL, SupabaseError, load_config, request


def main():
    config = load_config()
    if config is None:
        raise SystemExit("Configure .streamlit/secrets.toml before provisioning the administrator.")

    password = getpass.getpass("Temporary administrator password: ")
    if not password or not hmac.compare_digest(password, getpass.getpass("Repeat password: ")):
        raise SystemExit("Passwords must match.")

    setup_code = secrets.token_urlsafe(24)
    try:
        request(config, "/auth/v1/admin/users", method="POST", private=True, data={
            "email": ADMIN_EMAIL,
            "password": password,
            "email_confirm": True,
            "app_metadata": {
                "coin_role": "admin",
                "must_change_password": True,
                "setup_code_hash": hashlib.sha256(setup_code.encode()).hexdigest(),
            },
        })
    except SupabaseError as error:
        detail = f"HTTP {error.status}" if error.status else "connection failed"
        raise SystemExit(f"Could not create administrator ({detail}).") from None
    print("Administrator created. Log in as admin and enter this one-time setup code:")
    print(setup_code)


if __name__ == "__main__":
    main()
