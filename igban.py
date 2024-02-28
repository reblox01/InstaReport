import requests

def login(username, password):
    url = "https://i.instagram.com/api/v1/accounts/login/"
    headers = {
        "Content-Type": "application/x-www-form-urlencoded",
        "User-Agent": "Instagram 126.0.0.25.121 Android (24/7.0; 640dpi; 1440x2560; samsung; SM-G930F; herolte; samsungexynos7885; en_US)",
    }
    data = {
        "username": username,
        "password": password,
        "login_attempt_account_cross_auth_enabled": "true",
    }
    response = requests.post(url, headers=headers, data=data)
    if response.status_code == 200:
        print("Login successful.")
        return response.cookies
    else:
        print("Login failed.")
        return None

def report_user(session, user_id):
    url = "https://i.instagram.com/api/v1/users/{}/flag_user/".format(user_id)
    headers = {
        "Content-Type": "application/x-www-form-urlencoded",
        "User-Agent": "Instagram 126.0.0.25.121 Android (24/7.0; 640dpi; 1440x2560; samsung; SM-G930F; herolte; samsungexynos7885; en_US)",
    }
    data = {
        "reason": "1",  # 1: Spam, 2: Inappropriate content, 3: Violence or harm, 4: Impersonation or deceptive content, 5: Bullying or harassment, 6: False information, 7: Promotion of a harmful organization, 8: Illegal activity, 9: Personal and confidential information, 10: Copyrighted material, 11: Trademarked material, 12: Other
        "source_name": "profile",
        "user_id": user_id,
    }
    response = session.post(url, headers=headers, data=data)
    if response.status_code == 200:
        print(f"User {user_id} reported successfully.")
    else:
        print(f"Failed to report user {user_id}.")

if __name__ == "__main__":
    your_username = 'waqi__frooda1'
    your_password = 'Waqi@2420641#'
    
    user_id_to_report = '48538585733'
    num_reports = 100

    session = login(your_username, your_password)

    if session is not None:
        for i in range(num_reports):
            report_user(session, user_id_to_report)
            print(f"Report {i + 1} completed.")

        print("All reports completed.")
    else:
        print("Login failed. Exiting.")
