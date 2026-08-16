import os
import json
import uuid
from faker import Faker
from supabase import create_client, Client

url: str = "https://ziihtffnqfqticrgrduv.supabase.co"
key: str = "sb_publishable_YSy0lzIBmtN4ZKc1lv0fDg_GDN8K5Ok"
supabase: Client = create_client(url, key)

fake = Faker()

def generate_mock_data():
    events = [
        {"name": "Dark Web Rises", "slug": "dark-web-rises", "total_users": 600, "team_size": 4},
        {"name": "Food Wars", "slug": "food-wars", "total_users": 200, "team_size": 2},
        {"name": "Capture The Flag", "slug": "capture-the-flag", "total_users": 200, "team_size": 2},
    ]

    global_user_id = 1
    global_team_id = 1
    all_rows = []

    for event in events:
        num_teams = event["total_users"] // event["team_size"]
        
        for _ in range(num_teams):
            team_members_meta = []
            team_rows = []
            
            for i in range(event["team_size"]):
                user_id = global_user_id
                sid = f"PES-{user_id:05d}"
                name = fake.name()
                
                email = f"{user_id}_{fake.email()}" 
                
                phone = fake.numerify("##########") 
                qr_token = str(uuid.uuid4())
                
                member_meta = {
                    "id": user_id,
                    "sid": sid,
                    "name": name,
                    "email": email,
                    "phone": phone,
                    "qr_token": qr_token
                }
                team_members_meta.append(member_meta)
                
                row_data = {
                    "sid": sid,
                    "tams_registration_id": global_team_id,
                    "tams_member_id": user_id,
                    "qr_token": qr_token,
                    "event_name": event["name"],
                    "event_slug": event["slug"],
                    "name": name,
                    "email": email,
                    "phone": phone,
                    "school": "People's Education Society",
                    "city": fake.city(),
                    "state": fake.state(),
                    "team_leader": (i == 0), 
                    "team_size": event["team_size"],
                    "counter": global_team_id
                }
                team_rows.append(row_data)
                
                global_user_id += 1
            
            team_members_json = json.dumps(team_members_meta)
            for row in team_rows:
                row["team_members"] = team_members_json
                all_rows.append(row)
                
            global_team_id += 1

    return all_rows

def insert_in_batches(rows, batch_size=100):
    total_rows = len(rows)
    print(f"writing {total_rows} to supabase with batch_size {batch_size}")
    
    for i in range(0, total_rows, batch_size):
        batch = rows[i:i + batch_size]
        try:
            supabase.table('logged_in_users').insert(batch).execute()
            print(f"Successfully inserted rows {i + 1} to {i + len(batch)}")
        except Exception as e:
            print(f"error inserting batch starting at index {i}: {e}")

if __name__ == "__main__":
    mock_data = generate_mock_data()
    

    print("first generated row:")
    print(json.dumps(mock_data[0], indent=4))
    print("-" * 40)
    
    insert_in_batches(mock_data, batch_size=100)
    print("done")