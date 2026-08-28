import csv
import json
import os
import sys
import urllib.request
import urllib.error

CTFD_URL = os.environ.get("CTFD_URL", "http://ctfd:8000")
ADMIN_TOKEN = os.environ.get("CTFD_ADMIN_TOKEN")
CSV_FILE_PATH = os.environ.get("ROSTER_CSV", "/app/state/roster_cache_phase2.csv")

if not ADMIN_TOKEN:
    sys.exit("Error: CTFD_ADMIN_TOKEN environment variable is not set.")

def create_ctfd_user(name, email, password, type_="user"):
    url = f"{CTFD_URL.rstrip('/')}/api/v1/users"
    payload = json.dumps({
        "name": name,
        "email": email,
        "password": password,
        "type": type_,
        "verified": True,
        "hidden": False,
        "banned": False
    }).encode("utf-8")

    req = urllib.request.Request(
        url,
        data=payload,
        headers={
            "Authorization": f"Bearer {ADMIN_TOKEN}",
            "Content-Type": "application/json",
            "User-Agent": "CTFd-Importer"
        },
        method="POST"
    )

    try:
        with urllib.request.urlopen(req) as response:
            res_data = json.loads(response.read().decode("utf-8"))
            if res_data.get("success"):
                print(f"[+] Successfully created user: {name} ({email})")
                return res_data["data"]
    except urllib.error.HTTPError as e:
        err_body = e.read().decode("utf-8")
        print(f"[!] Failed to create user '{name}': HTTP {e.code} - {err_body}")
    except Exception as e:
        print(f"[X] Exception occurred while creating user '{name}': {e}")
    return None

def main():
    if not os.path.exists(CSV_FILE_PATH):
        sys.exit(f"Error: CSV file not found at '{CSV_FILE_PATH}'")

    print(f"[*] Starting CTFd user import from: {CSV_FILE_PATH}")
    
    with open(CSV_FILE_PATH, mode="r", encoding="utf-8") as csv_file:
        reader = csv.DictReader(csv_file)
        for row in reader:
            username = row.get("username") or row.get("name")
            email = row.get("email")
            password = row.get("password")

            if not username or not email or not password:
                print(f"[!] Skipping invalid row: {row}")
                continue

            create_ctfd_user(name=username, email=email, password=password)

    print("[*] Import process complete.")

if __name__ == "__main__":
    main()