#!/usr/bin/env python3
"""
One-off: get a refresh token for md@tabp.co.in via the Microsoft device-code flow.

Usage:
  python3 scripts/ms_get_refresh_token.py <client_id> <tenant_id>

  e.g.
  python3 scripts/ms_get_refresh_token.py \\
    12345678-1234-1234-1234-123456789abc \\
    87654321-4321-4321-4321-cba987654321

What it does:
  1. Starts a device-code OAuth flow (msal library)
  2. Prints a code + URL — you open the URL in your browser, paste the code,
     sign in as md@tabp.co.in, approve the Mail.Read + offline_access scopes
  3. Once Microsoft confirms, msal returns an access token + refresh token
  4. Prints the refresh token to your terminal — copy it and push to Secret Manager

The refresh token is long-lived (~90 days idle, longer with regular use).
Stays valid as long as the daemon polls regularly. The daemon will auto-refresh
the access token from this refresh token on every poll.

Prereq: pip3 install msal --break-system-packages
"""

import sys

try:
    from msal import PublicClientApplication
except ImportError:
    sys.stderr.write(
        "msal not installed. Run: pip3 install msal --break-system-packages\n"
    )
    sys.exit(1)


def main():
    if len(sys.argv) != 3:
        sys.stderr.write(
            "Usage: python3 ms_get_refresh_token.py <client_id> <tenant_id>\n"
        )
        sys.exit(1)

    client_id, tenant_id = sys.argv[1], sys.argv[2]
    authority = f"https://login.microsoftonline.com/{tenant_id}"

    app = PublicClientApplication(client_id, authority=authority)

    # offline_access is what makes Microsoft return a refresh token.
    # Mail.Read scopes the access to reading mail only.
    scopes = ["Mail.Read"]

    flow = app.initiate_device_flow(scopes=scopes)
    if "user_code" not in flow:
        sys.stderr.write(
            f"Failed to start device flow: {flow}\n\n"
            "Common causes:\n"
            "  - 'Allow public client flows' is not enabled on the Azure app\n"
            "  - Wrong tenant ID\n"
            "  - Wrong client ID\n"
        )
        sys.exit(1)

    print()
    print("=" * 70)
    print(flow["message"])
    print("=" * 70)
    print()
    print("Signing in as md@tabp.co.in. Waiting for you to complete the browser flow...")
    print()

    result = app.acquire_token_by_device_flow(flow)

    if "access_token" not in result:
        sys.stderr.write(
            f"\nFailed: {result.get('error_description', result)}\n"
        )
        sys.exit(1)

    rt = result.get("refresh_token")
    if not rt:
        sys.stderr.write(
            "\nNo refresh token returned. Make sure 'offline_access' is in the\n"
            "delegated permissions on the Azure app, and admin consent is granted.\n"
        )
        sys.exit(1)

    print()
    print("✅ Sign-in successful.")
    print()
    print("Refresh token (KEEP SECRET):")
    print(rt)
    print()
    print("Next step — push it to Secret Manager:")
    print()
    print("  printf '%s' '<paste-refresh-token-here>' | \\")
    print("    gcloud secrets versions add ms-refresh-token --data-file=- \\")
    print("    --project=tabp-second-brain")
    print()
    print("(If the secret doesn't exist yet, replace 'versions add' with 'create'")
    print(" and add --replication-policy=automatic)")


if __name__ == "__main__":
    main()
