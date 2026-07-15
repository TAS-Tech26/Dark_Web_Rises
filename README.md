# Dark_Web_Rises



# Documentation

## WebSocket Endpoint

`WS /ws`

Scope: Main game communication endpoint. Handles login, logout, gameplay messages, and server events.

### Logout

```JSON
{
  "type": "logout"
}
```


### Submit Prompt

```JSON
{
  "type": 5,
  "status": 1,
  "prompt": "a futuristic city at night"
}
```

## Server → Client Messages

### Login Response

```JSON
{
  "type": "login_response",
  "authorised": "accepted",
  "user_id": 0,
  "team_state": 4,
  "game_state": 2
}
```

Gameplay Response
```JSON
{
  "type": "gameplay_response",
  "message": 4,
  "image": "...",
  "time": 30,
  "is_player_turn": true
}
```


(Note: Additional server responses include Team State, Game State, and Logout Response, following a similar JSON structure based on event type.)

