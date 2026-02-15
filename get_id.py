import requests
import time
import sys
import os

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

def get_user_id_from_username(session, username):
    """Resolve an Instagram username to a numeric user ID."""
    print(f"[*] Resolving '{username}'...")
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
                return user_id, "Web Profile Info"
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
                return str(user_id), "GraphQL Fallback"
    except (requests.RequestException, ValueError, KeyError):
        pass

    # Approach 3: Embed Page (Low reliability, but sometimes works)
    try:
        url3 = f"https://www.instagram.com/{username}/embed/captioned/"
        headers["Accept"] = "text/html,application/xhtml+xml,application/xml;q=0.9"
        response = session.get(url3, headers=headers)
        if response.status_code == 200:
            import re
            match = re.search(r'"id":"(\d+)"', response.text)
            if match:
                return match.group(1), "Embed Page Scrape"
            match_owner = re.search(r'"owner_id":"(\d+)"', response.text)
            if match_owner:
                return match_owner.group(1), "Embed Page Owner ID"
    except (requests.RequestException, ValueError, KeyError):
        pass

    # Approach 4: Main Profile Page HTML Scrape (Fallback)
    try:
        url4 = f"https://www.instagram.com/{username}/"
        # Use a short timeout to fail fast if it hangs
        response = session.get(url4, headers=headers, timeout=10)
        if response.status_code == 200:
            import re
            text = response.text
            
            # Pattern A: "user_id":"12345" (often in sharedData)
            match_uid = re.search(r'"user_id":"(\d+)"', text)
            if match_uid:
                return match_uid.group(1), "Profile HTML (user_id)"

            # Pattern B: profilePage_12345 (rare but possible in old scripts)
            match_pp = re.search(r'profilePage_(\d+)', text)
            if match_pp:
                return match_pp.group(1), "Profile HTML (profilePage)"
            
            # Pattern C: "id":"12345" inside a user-like object
            # e.g. {"username":"aroscki",...,"id":"12345"}
            # We look for the username followed by "id"
            try:
                # Find all "id":"digits"
                ids = re.findall(r'"id":"(\d+)"', text)
                # If we found any, the first one is often the profile owner in new layouts
                # But to be safe, we can check if it's near "username"
                # For now, return the first one if it looks plausible (length > 5)
                for i in ids:
                    if len(i) > 5:
                        return i, "Profile HTML (Generic ID match)"
            except:
                pass
                
    except (requests.RequestException, ValueError, KeyError):
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

    session = requests.Session()

    # --- Session ID ---
    print("[?] Ideally, provide a session ID to ensure reliable access.")
    sessionid = os.environ.get("IG_SESSIONID")
    if not sessionid:
        print("[?] To get your sessionid: Open Instagram in a browser, press F12, go to Application > Cookies.")
        sessionid = input("Enter your sessionid cookie (press Enter to try anonymously): ").strip()
    
    if sessionid:
        session.cookies.set("sessionid", sessionid)
        print("[*] Using session ID.")

    # Get CSRF
    print("[*] Fetching CSRF token...")
    get_csrf_token(session)

    while True:
        username = input("\nEnter username to resolve (or 'q' to quit): ").strip()
        if username.lower() == 'q':
            break
        
        if not username:
            continue

        user_id, source = get_user_id_from_username(session, username)
        
        if user_id:
            print(f"\n[+] SUCCESS! User ID: {user_id}")
            print(f"    (Source: {source})")
            print(f"    Copy this ID and use it in igban.py")
        else:
            print(f"\n[-] FAILED. Could not resolve '{username}'.")
            if not sessionid:
                print("    Try running the script again and provide a valid session ID.")

if __name__ == "__main__":
    main()
