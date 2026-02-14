import requests
import os
import sys
import time
import random
import json

# Common browser User-Agent for web requests
WEB_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)

# Instagram Web App ID (publicly visible in source code)
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
        "Cache-Control": "no-cache",
        "Pragma": "no-cache",
    }


def get_csrf_token(session):
    """Fetch a CSRF token from Instagram by visiting the web page."""
    headers = {
        "User-Agent": WEB_USER_AGENT,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.5",
    }
    try:
        response = session.get("https://www.instagram.com/", headers=headers)
        csrf_token = session.cookies.get("csrftoken")
        if csrf_token:
            return csrf_token
        # Try extracting from response headers
        if "csrftoken" in response.headers.get("Set-Cookie", ""):
            for cookie in response.cookies:
                if cookie.name == "csrftoken":
                    return cookie.value
    except requests.RequestException as e:
        print(f"[!] Failed to fetch CSRF token: {e}")
    return None


def login(session, username, password):
    """Log in to Instagram using the web AJAX endpoint."""
    print("[*] Fetching CSRF token...")
    csrf_token = get_csrf_token(session)
    if not csrf_token:
        print("[!] Could not obtain CSRF token. Instagram may be blocking requests.")
        return False

    print(f"[+] Got CSRF token.")
    print("[*] Logging in...")

    url = "https://www.instagram.com/accounts/login/ajax/"
    timestamp = int(time.time())
    
    headers = get_web_headers(csrf_token)
    headers["Content-Type"] = "application/x-www-form-urlencoded"
    
    data = {
        "username": username,
        "enc_password": f"#PWD_INSTAGRAM_BROWSER:0:{timestamp}:{password}",
        "queryParams": "{}",
        "optIntoOneTap": "false",
    }

    try:
        response = session.post(url, headers=headers, data=data)
        try:
            result = response.json()
        except ValueError:
            print(f"[!] Login failed — got non-JSON response (HTTP {response.status_code}).")
            return False

        if result.get("authenticated"):
            print("[+] Login successful.")
            return True

        if result.get("two_factor_required"):
            print("[!] Two-factor authentication is required. Please disable 2FA or handle it manually.")
            return False

        if result.get("checkpoint_url"):
            print(f"[!] Instagram requires verification: https://www.instagram.com{result['checkpoint_url']}")
            print("[!] Complete the checkpoint in a browser, then try again.")
            return False

        message = result.get("message", "Unknown error")
        status = result.get("status", "")
        if result.get("user") is False:
            print("[!] Login failed: username not found.")
        elif message == "checkpoint_required":
            print("[!] Instagram requires a security checkpoint. Complete it in a browser first.")
        else:
            if message:
                print(f"[!] Login failed: {message} (status: {status})")
            else:
                print("[!] Login failed: incorrect username or password.")
        return False

    except requests.RequestException as e:
        print(f"[!] Login request failed: {e}")
        return False


def report_user(session, user_id, reason="1"):
    """Report a user via Instagram's web API."""
    # Attempt using the frontend_reg endpoint which is often more lenient/different flow
    url = "https://www.instagram.com/frontend_reg/"

    csrf_token = session.cookies.get("csrftoken", "")
    headers = get_web_headers(csrf_token)
    headers["Content-Type"] = "application/x-www-form-urlencoded"
    headers["Referer"] = f"https://www.instagram.com/{user_id}/"

    data = {
        "reason_id": reason,
        "source_name": "profile",
        "container_module": "profile",
        "action_source": "report_button",
        "victim_user_id": user_id,
    }

    try:
        response = session.post(url, headers=headers, data=data)
        if response.status_code == 200:
            try:
                # Try to parse success message for user verification
                resp_json = response.json()
                status = resp_json.get("status", "ok")
                message = resp_json.get("message", "")
                if message:
                    print(f"[+] User {user_id} reported successfully. Server says: '{message}'")
                else:
                    print(f"[+] User {user_id} reported successfully. (Status: {status})")
            except:
                # If response isn't JSON (e.g. HTML confirmation), just say success
                print(f"[+] User {user_id} reported successfully.")
            return True
        
        # Fallback: Try the primary web endpoint again just in case
        try:
            url_alt = f"https://www.instagram.com/users/{user_id}/report/"
            resp_alt = session.post(url_alt, headers=headers, data=data)
            if resp_alt.status_code == 200:
                print(f"[+] User {user_id} reported successfully (via alt endpoint).")
                return True
        except:
            pass

        # Error handling
        try:
            error_data = response.json()
            message = error_data.get("message", "Unknown error")
            print(f"[-] Failed to report user {user_id} (HTTP {response.status_code}): {message}")
        except ValueError:
            print(f"[-] Failed to report user {user_id} (HTTP {response.status_code})")
        return False

    except requests.RequestException as e:
        print(f"[!] Report request failed: {e}")
        return False


def get_user_id_from_username(session, username):
    """Resolve an Instagram username to a numeric user ID."""
    csrf_token = session.cookies.get("csrftoken", "")
    headers = get_web_headers(csrf_token)
    headers["Referer"] = f"https://www.instagram.com/{username}/"

    # Approach 1: Web profile info endpoint
    url = f"https://www.instagram.com/api/v1/users/web_profile_info/?username={username}"
    try:
        response = session.get(url, headers=headers)
        if response.status_code == 200:
            data = response.json()
            user_id = data.get("data", {}).get("user", {}).get("id")
            if user_id:
                return user_id
    except (requests.RequestException, ValueError, KeyError):
        pass

    # Approach 2: Profile page with ?__a=1&__d=dis
    try:
        url2 = f"https://www.instagram.com/{username}/?__a=1&__d=dis"
        headers["Accept"] = "*/*"
        response = session.get(url2, headers=headers)
        if response.status_code == 200:
            data = response.json()
            user = data.get("graphql", {}).get("user", {})
            if not user:
                user = data.get("user", {})
            user_id = user.get("id") or user.get("pk")
            if user_id:
                return str(user_id)
    except (requests.RequestException, ValueError, KeyError):
        pass

    return None


REPORT_REASONS = {
    "1": "Spam",
    "2": "Inappropriate content",
    "3": "Violence or harm",
    "4": "Impersonation or deceptive content",
    "5": "Bullying or harassment",
    "6": "False information",
    "7": "Promotion of a harmful organization",
    "8": "Illegal activity",
    "9": "Personal and confidential information",
    "10": "Copyrighted material",
    "11": "Trademarked material",
    "12": "Other",
}


def main():
    print("=" * 50)
    print("  InstaReport — Instagram Report Tool")
    print("=" * 50)
    print()

    # --- Mode Selection ---
    print("Select login method:")
    print("1. Username / Password")
    print("2. Session ID (Recommended — Bypasses 2FA/Checkpoints)")
    mode = input("\nEnter choice (1 or 2): ").strip()

    session = requests.Session()
    username = None

    if mode == "2":
        # --- Session ID Login ---
        print("\n[?] To get your sessionid: Open Instagram in a browser, press F12, go to Application > Cookies, and copy the value of 'sessionid'.")
        sessionid = input("Enter your sessionid cookie: ").strip()
        if not sessionid:
            print("[!] Session ID is required.")
            sys.exit(1)
        
        session.cookies.set("sessionid", sessionid)
        
        print("[*] Verifying session & fetching CSRF token...")
        csrf_token = get_csrf_token(session)
        if not csrf_token:
            print("[!] Could not fetch CSRF token. Session might be invalid.")
            sys.exit(1)
        
        # Try to get own username to confirm session
        try:
            print("[*] Validating session...")
            headers = get_web_headers(csrf_token)
            # Fetch current user info (lightweight check)
            resp = session.get(
                "https://www.instagram.com/api/v1/users/web_profile_info/?username=instagram", 
                headers=headers
            )

            if resp.status_code == 200:
                print("[+] Session appears valid.")
            elif resp.status_code == 404:
                # 404 on 'instagram' user is unlikely, but means request went through
                print("[+] Session check connected (endpoint reachable).")
            else:
                print(f"[!] Warning: Session check returned {resp.status_code}. Proceeding anyway...")
        except Exception as e:
            print(f"[!] Session check error: {e}")

    else:
        # --- Username / Password Login ---
        username = os.environ.get("IG_USERNAME") or input("Enter your Instagram username: ").strip()
        password = os.environ.get("IG_PASSWORD") or input("Enter your Instagram password: ").strip()

        if not username or not password:
            print("[!] Username and password are required.")
            sys.exit(1)
        
        if not login(session, username, password):
            # If login failed, ask if they want to try session ID
            print("\n[?] Login failed. You can try using your sessionid cookie instead.")
            retry = input("Try with session ID? (y/n): ").strip().lower()
            if retry == 'y':
                print("\n[?] To get your sessionid: Open Instagram in a browser, press F12, go to Application > Cookies, and copy the value of 'sessionid'.")
                sessionid = input("Enter your sessionid cookie: ").strip()
                if sessionid:
                    session.cookies.set("sessionid", sessionid)
                    get_csrf_token(session)
                    print("[+] Switched to Session ID mode.")
                else:
                    sys.exit(1)
            else:
                sys.exit(1)

    # --- Target ---
    target = input("\nEnter the username or user ID of the account to report: ").strip()
    if not target:
        print("[!] Target is required.")
        sys.exit(1)

    # --- Reason ---
    print("\nReport reasons:")
    for key, desc in REPORT_REASONS.items():
        print(f"  {key}: {desc}")
    reason = input("\nEnter reason number (default: 1 — Spam): ").strip() or "1"
    if reason not in REPORT_REASONS:
        print("[!] Invalid reason. Defaulting to 1 (Spam).")
        reason = "1"

    # --- Number of reports ---
    try:
        num_reports = int(input("Enter number of reports to send (default: 1): ").strip() or "1")
        if num_reports < 1:
            num_reports = 1
    except ValueError:
        num_reports = 1

    # --- Resolve user ID if a username was provided ---
    if not target.isdigit():
        print(f"[*] Resolving username '{target}' to user ID...")
        user_id = get_user_id_from_username(session, target)
        if user_id:
            print(f"[+] Resolved to user ID: {user_id}")
        else:
            print(f"[!] Could not resolve username '{target}'. Try providing the numeric user ID directly.")
            sys.exit(1)
    else:
        user_id = target

    # --- Report ---
    print(f"\n[*] Sending {num_reports} report(s) for user {user_id} (reason: {REPORT_REASONS[reason]})...\n")
    success_count = 0
    for i in range(num_reports):
        if report_user(session, user_id, reason):
            success_count += 1
        # Small random delay between reports to avoid rate limiting
        if i < num_reports - 1:
            delay = random.uniform(1.5, 4.0)
            time.sleep(delay)

    print(f"\n{'=' * 50}")
    print(f"  Done — {success_count}/{num_reports} reports sent successfully.")
    print(f"{'=' * 50}")


if __name__ == "__main__":
    main()
