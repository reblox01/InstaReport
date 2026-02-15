import requests
import re
import sys
import os

# Common browser User-Agent for web requests
WEB_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)

# Instagram Web App ID
IG_APP_ID = "936619743392459"
IG_ASBD_ID = "198387"


def get_web_headers(csrf_token=""):
    """Return standard headers for Instagram Web API requests."""
    return {
        "User-Agent": WEB_USER_AGENT,
        "X-IG-App-ID": IG_APP_ID,
        "X-ASBD-ID": IG_ASBD_ID,
        "X-CSRFToken": csrf_token,
        "X-Requested-With": "XMLHttpRequest",
        "X-Instagram-AJAX": "1",
        "Referer": "https://www.instagram.com/",
        "Origin": "https://www.instagram.com",
        "Accept": "*/*",
        "Accept-Language": "en-US,en;q=0.9",
    }


def get_csrf_token(session):
    """Fetch a CSRF token from Instagram."""
    headers = {
        "User-Agent": WEB_USER_AGENT,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    }
    try:
        response = session.get("https://www.instagram.com/", headers=headers, timeout=10)
        csrf_token = session.cookies.get("csrftoken")
        if csrf_token:
            return csrf_token
    except requests.RequestException as e:
        print(f"[!] Failed to fetch CSRF token: {e}")
    return None


def resolve_with_session(session, username):
    """Resolve username to ID using an authenticated session."""
    csrf_token = session.cookies.get("csrftoken", "")
    headers = get_web_headers(csrf_token)
    headers["Referer"] = f"https://www.instagram.com/{username}/"

    # Method 1: web_profile_info API
    url = f"https://www.instagram.com/api/v1/users/web_profile_info/?username={username}"
    try:
        response = session.get(url, headers=headers, timeout=10)
        if response.status_code == 200:
            data = response.json()
            user_id = data.get("data", {}).get("user", {}).get("id")
            if user_id:
                return user_id, "Web Profile Info API"
    except (requests.RequestException, ValueError, KeyError):
        pass

    # Method 2: ?__a=1&__d=dis
    try:
        url2 = f"https://www.instagram.com/{username}/?__a=1&__d=dis"
        response = session.get(url2, headers=headers, timeout=10)
        if response.status_code == 200:
            data = response.json()
            user = data.get("graphql", {}).get("user", {})
            if not user:
                user = data.get("user", {})
            user_id = user.get("id") or user.get("pk")
            if user_id:
                return str(user_id), "GraphQL Fallback"
    except (requests.RequestException, ValueError, KeyError):
        pass

    return None, None


def resolve_anonymous(username):
    """Resolve username to ID without a session, using HTML scraping."""
    headers = {
        "User-Agent": WEB_USER_AGENT,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    }

    # Method 1: Profile page HTML scraping
    try:
        url = f"https://www.instagram.com/{username}/"
        response = requests.get(url, headers=headers, timeout=10)
        if response.status_code == 200:
            text = response.text

            match = re.search(r'"user_id":"(\d+)"', text)
            if match:
                return match.group(1), "Profile HTML (user_id)"

            match = re.search(r'profilePage_(\d+)', text)
            if match:
                return match.group(1), "Profile HTML (profilePage)"

            ids = re.findall(r'"id":"(\d+)"', text)
            for i in ids:
                if len(i) > 5:
                    return i, "Profile HTML (generic ID)"
    except requests.RequestException:
        pass

    # Method 2: Embed page
    try:
        url2 = f"https://www.instagram.com/{username}/embed/captioned/"
        response = requests.get(url2, headers=headers, timeout=10)
        if response.status_code == 200:
            match = re.search(r'"owner_id":"(\d+)"', response.text)
            if match:
                return match.group(1), "Embed Page"
            match = re.search(r'"id":"(\d+)"', response.text)
            if match:
                return match.group(1), "Embed Page (generic)"
    except requests.RequestException:
        pass

    return None, None


def main():
    print(r"""
  _____           _        _____                       _ 
 |_   _|         | |      |  __ \                     | |
   | |  _ __  ___| |_ __ _| |__) |___ _ __   ___  _ __| |_ 
   | | | '_ \/ __| __/ _` |  _  // _ \ '_ \ / _ \| '__| __|
  _| |_| | | \__ \ || (_| | | \ \  __/ |_) | (_) | |  | |_ 
 |_____|_| |_|___/\__\__,_|_|  \_\___| .__/ \___/|_|   \__|
                                     | |                   
                                     |_|                   
    """)
    print("  Get User ID — Instagram Helper")
    print("  Made by: 0x8D")
    print("  Repo: https://github.com/reblox01/InstaReport")
    print("  Version: 1.7")
    print("=" * 60)
    print()

    # --- Login Mode ---
    print("Select mode:")
    print("1. Session ID (Recommended — most reliable)")
    print("2. Anonymous (may not work — Instagram blocks most requests)")
    mode = input("\nEnter choice (1 or 2): ").strip()

    session = requests.Session()
    use_session = False

    if mode == "1":
        sessionid = os.environ.get("IG_SESSIONID")
        if not sessionid:
            print("\n[?] To get your sessionid: Open Instagram > F12 > Application > Cookies > copy 'sessionid'.")
            sessionid = input("Enter your sessionid cookie: ").strip()

        if not sessionid:
            print("[!] Session ID is required for this mode.")
            sys.exit(1)

        session.cookies.set("sessionid", sessionid)
        print("[*] Fetching CSRF token...")
        csrf = get_csrf_token(session)
        if csrf:
            print("[+] Session ready.")
            use_session = True
        else:
            print("[!] Could not validate session. Continuing anyway...")
            use_session = True
    else:
        print("\n[!] Anonymous mode — results may be unreliable.")
        print("[!] If it fails, re-run with Session ID (option 1).\n")

    # --- Resolve Loop ---
    while True:
        username = input("Enter username to resolve (or 'q' to quit): ").strip()
        if username.lower() == 'q':
            break
        if not username:
            continue

        # Remove @ if user includes it
        username = username.lstrip("@")

        print(f"[*] Resolving '{username}'...")

        user_id = None
        source = None

        if use_session:
            user_id, source = resolve_with_session(session, username)

        if not user_id:
            # Try anonymous scraping as fallback
            user_id, source = resolve_anonymous(username)

        if user_id:
            print(f"\n    ╔══════════════════════════════════════╗")
            print(f"    ║  Username:  {username:<25} ║")
            print(f"    ║  User ID:   {user_id:<25} ║")
            print(f"    ║  Source:    {source:<26} ║")
            print(f"    ╚══════════════════════════════════════╝")
            print(f"    Copy the User ID and use it in igban.py\n")
        else:
            print(f"\n[-] FAILED to resolve '{username}'.")
            if not use_session:
                print("    Instagram blocks most anonymous requests.")
                print("    Re-run with Session ID (option 1) for reliable results.\n")
            else:
                print("    The account may not exist or is private.\n")


if __name__ == "__main__":
    main()
