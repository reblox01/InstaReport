# Instagram Report Tool
The script is designed to automate the process of reporting a user on Instagram for violating the platform's community guidelines. 
Here's a step-by-step guide on how to use the script:

## Step 1:
**First, make sure you have a valid Instagram account with the necessary permissions to report other users.**

## Step 2:
**Install the requests library for Python if you don't already have it installed. You can install it using pip: pip install requests.**

## Step 3:
**Save the script to a file with a .py extension, for example igban.py.**

## Step 4:
**Modify the `your_username` and `your_password` variables in the script with your own Instagram login credentials.**

## Step 5:
**Modify the  user_id_to_report variable in the script with the Instagram user ID of the user you want to report.** 
  You can find the user ID by looking at the URL of the user's profile page. 
  For example, in the URL `https://www.instagram.com/username/`, 
  the `user ID` is the number after `/p/`.

  ## Step 6:
**Modify the `num_reports` variable in the script with the number of times you want to report the user.**

## Step 7:
**Run the script using one of the methods I described earlier**
  ``python igban.py``
  ``python -m igban``
    or 
  ``python -c "import igban; 
    igban.main()"``

## Step 8:
**The script will log in to your Instagram account and report the user the specified number of times.**
