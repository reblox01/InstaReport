# Instagram Report Tool

## Overview

This script automates the process of reporting a user on Instagram for violating the platform's community guidelines.

## Features

- **Automation:** Automatically logs in to your Instagram account and reports a specified user multiple times.
- **Customization:** Allows you to specify your Instagram credentials, the user ID of the account to report, and the number of reports to file.
- **Ease of Use:** Simple setup with clear step-by-step instructions.
- **Flexibility:** Supports running the script via different methods (`python igban.py`, `python -m igban`, or inline script execution).
- **Educational Purpose:** Designed for learning about web scraping and API interaction with Instagram.
- **Contributions:** Open to contributions and improvements from the community.
- **License:** Released under the MIT License for open usage and modification.


## Prerequisites

1. A valid Instagram account with the necessary permissions to report other users.
2. Python installed on your system.
3. `requests` library for Python installed (`pip install requests`).

## Setup Instructions

### Step 1: Clone the Repository

Clone this repository to your local machine:

```bash
git clone https://github.com/reblox01/instagram-report-tool.git
cd instagram-report-tool
```

### Step 2: Install Dependencies

Install the required Python libraries:

```bash
pip install requests
```

### Step 3: Configure the Script

Open the `igban.py` script in a text editor and modify the following variables with your information:

- `your_username` and `your_password`: Your Instagram login credentials.
- `user_id_to_report`: The Instagram user ID of the user you want to report.
  - You can find the user ID by examining the URL of the user's profile page. For example, in the URL `https://www.instagram.com/username/`, the `user ID` is the number after `/p/`.
- `num_reports`: The number of times you want to report the user.

### Step 4: Run the Script

Execute the script using one of the following methods:

```bash
python igban.py
```
```bash
python -m igban
```
```bash
python -c "import igban; igban.main()"
```

## Usage

Upon execution, the script will log in to your Instagram account and report the specified user the number of times indicated.

## Disclaimer

This tool is intended for educational purposes only. Misuse of this tool to harass or harm others is strictly prohibited. Always ensure your actions comply with Instagram's community guidelines and terms of service.

## Contributing

Contributions are welcome! Feel free to open an issue or submit a pull request.

## License

This project is licensed under the [MIT License](LICENSE).

---

## Support

If you find this project helpful and would like to support its development, you can buy me a coffee:

[![Buy Me a Coffee](https://www.buymeacoffee.com/assets/img/custom_images/orange_img.png)](https://www.buymeacoffee.com/arosck1)

---

**Note:** Ensure you are fully aware of Instagram's community guidelines and terms of service before using this tool. Misuse may result in your account being suspended or banned.
