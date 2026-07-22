import os
import json
from supabase import create_client, Client

url: str = os.environ.get("SUPABASE_URL")
key: str = os.environ.get("SUPABASE_KEY")
supabase: Client = create_client(url, key)

#returns dict of the form : 
"""
{
    player_id : [name, password, team_id]
}
"""
#and team count

def generate_team_dictionary():
    response = supabase.table('logged_in_users') \
        .select('team_members') \
        .eq('team_leader', True) \
        .eq('event_name', 'Dark Web Rises') \
        .eq('event_slug', 'dark-web-rises') \
        .execute()
    
    records = response.data
    
    final_dict = {}
    team_counter = 0

    for row in records:
        raw_team_members = row.get('team_members', [])
        
        #in case json is received as string or list
        if isinstance(raw_team_members, str):
            team_members = json.loads(raw_team_members)
        else:
            team_members = raw_team_members
            
        for member in team_members:
            member_id = member.get('id')
            full_name = member.get('name', '')
            phone = member.get('phone', '')

            first_word_name = full_name.split()[0] if full_name else ""
            
            phone_prefix = str(phone)[:5] if phone else ""
            
            generated_string = f"{phone_prefix}{first_word_name}"
            
            final_dict[member_id] = [full_name, generated_string, team_counter]
            
        team_counter += 1
        
    return final_dict, team_counter
