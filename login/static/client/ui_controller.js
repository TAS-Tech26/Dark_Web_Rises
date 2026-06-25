import { GameClient, GamePlay } from './login.js';

// ** Ensure the port matches your Python server exactly! **
const game = new GameClient("ws://127.0.0.1:8000/ws");

// --- DOM ELEMENTS ---
// Screens
const loginScreen = document.getElementById("login_screen");
const lobbyScreen = document.getElementById("lobby_screen");
const gameScreen = document.getElementById("game_screen");
const postgameScreen = document.getElementById("postgame_screen");

// Game Controls
const gameImage = document.getElementById("game_image");
const timerDisplay = document.getElementById("timer_display");
const activeTurnControls = document.getElementById("active_turn_controls");
const spectatorMessage = document.getElementById("spectator_message");
const promptInput = document.getElementById("prompt_input");

// Global Timer Variable (so we can clear it when a turn ends early)
let currentTimerInterval = null;


// --- HELPER FUNCTIONS ---

function addLog(message, cssClass = "log-msg") {
    const logDiv = document.getElementById("lobby_log");
    const p = document.createElement("p");
    p.className = cssClass;
    p.textContent = message;
    logDiv.appendChild(p);
    logDiv.scrollTop = logDiv.scrollHeight; // Auto-scroll to bottom
}

function showScreen(screenElement) {
    // Hide all screens
    loginScreen.style.display = "none";
    lobbyScreen.style.display = "none";
    gameScreen.style.display = "none";
    postgameScreen.style.display = "none";
    // Show the requested one
    screenElement.style.display = "block";
}

function startVisualTimer(seconds) {
    if (currentTimerInterval) clearInterval(currentTimerInterval);
    
    let timeLeft = Math.ceil(seconds);
    timerDisplay.textContent = `Time: ${timeLeft}s`;

    currentTimerInterval = setInterval(() => {
        timeLeft--;
        if (timeLeft >= 0) {
            timerDisplay.textContent = `Time: ${timeLeft}s`;
        } else {
            clearInterval(currentTimerInterval);
            timerDisplay.textContent = "Time's up!";
            // Force a submit if they ran out of time!
            if (activeTurnControls.style.display === "block") {
               submitCurrentPrompt();
            }
        }
    }, 1000);
}


// --- NETWORK HOOKS ---

// 1. Lobby/Login Routing
game.on_login_success = (userId) => {
    showScreen(lobbyScreen);
};
game.on_logout_success = () => {
    showScreen(loginScreen);
};

game.on_login_failed = (status) => {
    // A simple browser pop-up to warn the user
    alert("Login failed! Please check your username and password.");
    
    // (Optional) Clear the password box
    document.getElementById("password_input").value = "";
};

// 2. Game Start Transition
game.on_game_countdown = (seconds) => {
    // (You can add the visual countdown to the chat log here if you want)
};

game.on_roster_update = (rosterList) => {
    const rosterUl = document.getElementById("roster_list");
    rosterUl.innerHTML = ""; // Clear the old list
    
    // Add every teammate to the list!
    rosterList.forEach(name => {
        let li = document.createElement("li");
        li.textContent = name;
        rosterUl.appendChild(li);
    });
};

game.on_game_start = () => {
    showScreen(gameScreen);
    gameImage.style.display = "block";
    spectatorMessage.textContent = "Match started! Waiting for your turn...";
    activeTurnControls.style.display = "none";
};

// 3. The Core Gameplay Loop
game.run_turn = (imageUrl, timeLimitSeconds) => {
    // Ensure we are on the game screen (critical for reconnects!)
    showScreen(gameScreen);
    
    // Update the image
    gameImage.style.display = "block";
    gameImage.src = imageUrl;

    // Show the input controls
    activeTurnControls.style.display = "block";
    spectatorMessage.style.display = "none";
    
    // Clear the input box from last time
    promptInput.value = "";
    promptInput.focus();

    // Start the clock
    startVisualTimer(timeLimitSeconds);
};

// 4. Submission Callbacks
game.on_prompt_success = (isValid, serverMessage, attemptsLeft) => {
    console.log("on_prompt_success called. Valid:", isValid, "Message ID:", serverMessage);
    
    // Scenario 1: The prompt was perfectly valid!
    if (isValid) {
        if (currentTimerInterval) clearInterval(currentTimerInterval);
        activeTurnControls.style.display = "none";
        spectatorMessage.style.display = "block";
        spectatorMessage.textContent = "Prompt accepted! Generating next image...";
    } 
    // Scenario 2: Prompt was invalid, and they are completely out of chances
    // CHANGED: Match against the Enum property rather than a hardcoded string
    else if (serverMessage === GamePlay.OUT_OF_CHANCES) {
        if (currentTimerInterval) clearInterval(currentTimerInterval);
        activeTurnControls.style.display = "none";
        spectatorMessage.style.display = "block";
        spectatorMessage.textContent = "Three strikes! No valid prompt submitted. Moving to the next player...";
    } 
    // Scenario 3: Prompt was invalid, but they still have remaining attempts
    else if (serverMessage === GamePlay.INVALID_PROMPT) {
        // Re-enable the input controls so they can type another try
        activeTurnControls.style.display = "block";
        promptInput.disabled = false;
        document.getElementById("submit_prompt_button").disabled = false;
        promptInput.value = "";
        promptInput.focus();
        
        spectatorMessage.style.display = "block";
        spectatorMessage.textContent = `Invalid prompt! ${attemptsLeft} tries remaining.`;
    }
};
game.on_prompt_fail = () => {
    if (currentTimerInterval) clearInterval(currentTimerInterval);
    activeTurnControls.style.display = "none";
    spectatorMessage.style.display = "block";
    spectatorMessage.textContent = "You ran out of time! Penalty applied.";
};

// 5. Post-Game
game.on_game_over = (teamScore, teamRank, top3Scores) => {
    showScreen(postgameScreen);
    
    const leaderboard = document.getElementById("leaderboard_list");
    let html = `<h3>Your Score: ${teamScore} (Rank ${teamRank + 1})</h3><hr>`;
    html += `<h4>Top 3 Teams:</h4><ol>`;
    
    // Remember top3Scores is a list of tuples: [[team_id, score], [team_id, score]]
    top3Scores.forEach(entry => {
        html += `<li>Team ${entry[0]}: ${entry[1]} pts</li>`;
    });
    html += `</ol>`;
    
    leaderboard.innerHTML = html;
};


// --- BUTTON ACTIONS ---

// 1. Login Button
document.getElementById("login_button").addEventListener("click", () => {
    const user = document.getElementById("username_input").value;
    const pass = document.getElementById("password_input").value;
    game.login(user, pass);
});

// 2. Logout Button
document.getElementById("logout_button").addEventListener("click", () => {
    game.logout();
});

// Submit Prompt Logic
function submitCurrentPrompt() {
    const text = promptInput.value.trim();
    if (text.length > 0) {
        game.send_prompt(GamePlay.PROMPTED, text);
    } else {
        game.send_prompt(GamePlay.NOT_PROMPTED, "");
    }

    // Disable but keep visible — wait for server response
    promptInput.disabled = true;
    document.getElementById("submit_prompt_button").disabled = true;
    spectatorMessage.style.display = "block";
    spectatorMessage.textContent = "Submitting...";
    // ← NO activeTurnControls.style.display = "none" here
}

document.getElementById("submit_prompt_button").addEventListener("click", () => {
    submitCurrentPrompt();
});

// Allow hitting "Enter" in the textbox to submit
document.getElementById("prompt_input").addEventListener("keypress", (e) => {
    if (e.key === "Enter") {
        submitCurrentPrompt();
    }
});