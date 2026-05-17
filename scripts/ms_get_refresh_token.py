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

    # offline_access (implicit) is what makes Microsoft return a refresh token.
    # The scopes below are everything the daemon needs:
    #   - Mail.Read       → /me/messages (Outlook poller)
    #   - Calendars.Read  → /me/calendarView (today's meetings in the briefing)
    scopes = ["Mail.Read", "Calendars.Read"]

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
    print(f"   Refresh token length: {len(rt)} chars (typical: 1500-2500)")
    print(f"   Starts with: {rt[:6]}...")
    print()

    # Auto-push to Secret Manager — avoids any copy-paste / whitespace
    # mistakes. Pipes the raw token bytes directly into gcloud.
    print("Pushing to Secret Manager (gcloud secrets versions add ms-refresh-token)…")
    import subprocess
    proc = subprocess.run(
        [
            "gcloud", "secrets", "versions", "add", "ms-refresh-token",
            "--data-file=-",
            "--project=tabp-second-brain",
        ],
        input=rt.encode("utf-8"),
        capture_output=True,
    )
    if proc.returncode == 0:
        print("✓ Pushed. The daemon will pick up the new token on next cold start.")
        print(f"  ({proc.stdout.decode().strip() or 'no stdout'})")
    else:
        sys.stderr.write(
            f"\n❌ gcloud push failed:\n{proc.stderr.decode()}\n\n"
            "Fallback: copy this token manually and push yourself:\n"
            f"\n{rt}\n\n"
            "  printf 'TOKEN_HERE' | gcloud secrets versions add ms-refresh-token "
            "--data-file=- --project=tabp-second-brain\n"
        )
        sys.exit(1)


if __name__ == "__main__":
    main()
