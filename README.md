# Instagram Report Tool

## Overview

This script automates the process of reporting a user on Instagram for violating the platform's community guidelines.

## Features

- **Automation:** Automatically reports a specified user multiple times.
- **Login Methods:**
  - **Username/Password:** Standard login (may trigger checkpoints/2FA).
  - **Session ID:** Bypass login checkpoints by using your browser's session cookie (Recommended).
- **Username Resolution:** Accepts either a username or numeric user ID — usernames are resolved automatically.
- **Secure:** Supports environment variables or interactive prompts — no hardcoded credentials.
- **Smart:** Uses random delays to avoid rate limiting.

## Prerequisites

1. A valid Instagram account.
2. Python 3.7+ installed.
3. Dependencies installed (`pip install -r requirements.txt`).

## Setup

1. **Clone the repository:**
   ```bash
   git clone https://github.com/reblox01/InstaReport.git
   cd InstaReport
   ```

2. **Install dependencies:**
   ```bash
   pip install -r requirements.txt
   ```

## Usage

Run the script:

```bash
python igban.py
```

### Login Options

The script will ask you to choose a login method:

1. **Username / Password:** Enter your credentials. If you have 2FA enabled or get a "checkpoint required" error, use option 2.
2. **Session ID (Recommended):** Use this if standard login fails.
   - Open Instagram.com in your browser and log in.
   - Press **F12** to open Developer Tools.
   - Go to **Application** (Chrome) or **Storage** (Firefox) > **Cookies**.
   - Find the cookie named `sessionid` and copy its value.
   - Paste it into the script when prompted.

### Environment Variables (Optional)

You can skip prompts by setting variables:

```bash
# Linux / macOS
export IG_USERNAME="your_user"
export IG_PASSWORD="your_pass"
# OR for session ID:
export IG_SESSIONID="your_session_id_cookie"

python igban.py
```

## Report Reasons

| # | Reason |
|---|--------|
| 1 | Spam |
| 2 | Inappropriate content |
| 3 | Violence or harm |
| 4 | Impersonation |
| 5 | Bullying |
| 6 | False info |
| 7 | Harmful orgs |
| 8 | Illegal activity |
| 9 | Private info |
| 10 | Copyright |
| 11 | Trademark |
| 12 | Other |

## Disclaimer

This tool is for educational purposes only. Misuse to harass or harm others is strictly prohibited. Use responsibly.

## License

MIT License.
