import { GameClient, GamePlay } from './game_client.js';

// ** Target endpoint matches your Uvicorn launch configuration port precisely **
const game = new GameClient("ws://127.0.0.1:8000/ws");

// --- DOM OBJECT TARGET CACHING ---
const loginScreen = document.getElementById("login_screen");
const lobbyScreen = document.getElementById("lobby_screen");
const gameScreen = document.getElementById("game_screen");
const postgameScreen = document.getElementById("postgame_screen");

const gameImage = document.getElementById("game_image");
const timerDisplay = document.getElementById("timer_display");
const activeTurnControls = document.getElementById("active_turn_controls");
const spectatorMessage = document.getElementById("spectator_message");
const promptInput = document.getElementById("prompt_input");
const loginButton = document.getElementById("login_button");

let currentTimerInterval = null;

// --- DOM ROUTING CONTROLLERS ---
function showScreen(screenElement) {
    loginScreen.classList.remove("active");
    lobbyScreen.classList.remove("active");
    gameScreen.classList.remove("active");
    postgameScreen.classList.remove("active");
    
    screenElement.classList.add("active");
}

function addLog(message) {
    const logDiv = document.getElementById("lobby_log");
    if (!logDiv) return;
    const p = document.createElement("p");
    p.textContent = `[${new Date().toLocaleTimeString()}] ${message}`;
    logDiv.appendChild(p);
    logDiv.scrollTop = logDiv.scrollHeight;
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
            timerDisplay.textContent = "Time Expired";
            if (activeTurnControls.style.display === "block") {
               submitCurrentPrompt();
            }
        }
    }, 1000);
}

// --- SECURE LIFECYCLE NETWORK GATES ---
game.on_connected = () => {
    loginButton.disabled = false;
    loginButton.textContent = "Authenticate Connection";
    addLog("System Tunnel Link verified online.");
};

game.on_disconnected = () => {
    loginButton.disabled = true;
    loginButton.textContent = "Server Offline";
    showScreen(loginScreen);
    alert("🚨 Connection lost. Ensure Uvicorn FastAPI backend is fully operational on Port 8000.");
};

game.on_connection_error = (err) => {
    console.error("WebSocket Pipeline Exception Encountered:", err);
};

// --- ROUTER SUBSCRIPTION HANDLERS ---
game.on_login_success = (userId) => {
    showScreen(lobbyScreen);
    addLog(`Operator successfully verified. ID: ${userId}`);
};

game.on_logout_success = () => {
    showScreen(loginScreen);
};

game.on_login_failed = (status) => {
    alert("Authentication refused: Token mismatched or invalid username parameters.");
    document.getElementById("password_input").value = "";
};

game.on_game_countdown = (seconds) => {
    addLog(`Inbound structural operational execution countdown initialization: ${seconds}s`);
};

game.on_roster_update = (rosterList) => {
    const rosterUl = document.getElementById("roster_list");
    if (!rosterUl) return;
    rosterUl.innerHTML = "";
    
    // Fallback protection against structural non-array responses
    if (Array.isArray(rosterList)) {
        rosterList.forEach(name => {
            let li = document.createElement("li");
            li.textContent = name;
            rosterUl.appendChild(li);
        });
    }
};

game.on_game_start = () => {
    showScreen(gameScreen);
    gameImage.style.display = "block";
    spectatorMessage.style.display = "block";
    spectatorMessage.textContent = "Channel streaming activated. Awaiting turn delegation assignment...";
    activeTurnControls.style.display = "none";
};

game.run_turn = (imageUrl, timeLimitSeconds, isPlayerTurn) => {
    showScreen(gameScreen);
    
    // 1. Update the image to show either the Wait SVG or the actual game image
    gameImage.style.display = "block";
    gameImage.src = imageUrl;

    // 2. Conditionally show controls based on isPlayerTurn
    if (isPlayerTurn) {
        // Active prompter's view
        activeTurnControls.style.display = "block";
        spectatorMessage.style.display = "none";
        
        promptInput.value = "";
        promptInput.disabled = false;
        document.getElementById("submit_prompt_button").disabled = false;
        promptInput.focus();
    } else {
        // Non-active player's view (Waiting)
        activeTurnControls.style.display = "none";
        
        // Show a helpful waiting message
        spectatorMessage.style.display = "block";
        spectatorMessage.textContent = "Waiting for the active player to prompt...";
    }

    // 3. Keep the timer running for everyone so they know how much time is left
    startVisualTimer(timeLimitSeconds);
};

game.on_prompt_success = (isValid, serverMessage, attemptsLeft) => {
    if (isValid) {
        if (currentTimerInterval) clearInterval(currentTimerInterval);
        activeTurnControls.style.display = "none";
        spectatorMessage.style.display = "block";
        spectatorMessage.textContent = "Structural payload mutation valid! Processing network state adjustments...";
    } 
    else if (serverMessage === GamePlay.OUT_OF_CHANCES) {
        if (currentTimerInterval) clearInterval(currentTimerInterval);
        activeTurnControls.style.display = "none";
        spectatorMessage.style.display = "block";
        spectatorMessage.textContent = "Three structural injection errors flagged. Revoking active turn matrix...";
    } 
    else if (serverMessage === GamePlay.INVALID_PROMPT) {
        activeTurnControls.style.display = "block";
        promptInput.disabled = false;
        document.getElementById("submit_prompt_button").disabled = false;
        promptInput.value = "";
        promptInput.focus();
        
        spectatorMessage.style.display = "block";
        spectatorMessage.textContent = `Parameter check failure! ${attemptsLeft} attempts remaining prior to system lock.`;
    }
};

game.on_prompt_fail = () => {
    if (currentTimerInterval) clearInterval(currentTimerInterval);
    activeTurnControls.style.display = "none";
    spectatorMessage.style.display = "block";
    spectatorMessage.textContent = "Operation allocation exhaustion. Turn timeout penalty incurred.";
};

game.on_team_done = () => {
    showScreen(gameScreen);
    activeTurnControls.style.display = "none";
    spectatorMessage.style.display = "block";
    spectatorMessage.textContent = "Your team has completed all structural operations. Awaiting final matrix evaluation...";
};

game.on_round_over = (round, roundScore, totalScore, countdown) => {
    if (currentTimerInterval) clearInterval(currentTimerInterval);
    
    const overlay = document.getElementById("round_over_overlay");
    overlay.style.display = "flex";
    
    document.getElementById("round_over_title").textContent = `Cycle ${round} Terminated`;
    document.getElementById("round_score_display").textContent = `${roundScore} pts`;
    document.getElementById("total_score_display").textContent = `Total Score Accumulation: ${totalScore} pts`;

    let timeLeft = countdown;
    const cdt = document.getElementById("round_countdown");
    cdt.textContent = `Next extraction sequence in ${timeLeft}s...`;
    
    const countdownInterval = setInterval(() => {
        timeLeft--;
        cdt.textContent = `Next extraction sequence in ${timeLeft}s...`;
        if (timeLeft <= 0) {
            clearInterval(countdownInterval);
            overlay.style.display = "none";
        }
    }, 1000);
};

game.on_game_over = (teamScore, teamRank, top3Scores) => {
    if (currentTimerInterval) clearInterval(currentTimerInterval);
    document.getElementById("round_over_overlay").style.display = "none";
    showScreen(postgameScreen);
    
    const leaderboard = document.getElementById("leaderboard_list");
    let calculatedRank = (typeof teamRank === 'number') ? teamRank + 1 : '--';
    
    let html = `<h3 style='color: var(--accent-color);'>Terminal Diagnostics Complete</h3>`;
    html += `<p>Accumulated Output Score: <strong>${teamScore}</strong> (Rank Reference: <strong>${calculatedRank}</strong>)</p><hr style='border-color: var(--border-color);'>`;
    html += `<h4>Top Analytics Yields:</h4><ol style='padding-left: 20px;'>`;
    
    if (Array.isArray(top3Scores)) {
        top3Scores.forEach(entry => {
            if (Array.isArray(entry) && entry.length >= 2) {
                html += `<li style='margin-bottom: 8px;'>Team Space Vector ${entry[0]}: <span style='color: var(--accent-color);'>${entry[1]} pts</span></li>`;
            }
        });
    }
    html += `</ol>`;
    leaderboard.innerHTML = html;
};

// --- COMPONENT INTERACTION PIPELINE ---
function submitCurrentPrompt() {
    const text = promptInput.value.trim();
    if (text.length > 0) {
        game.send_prompt(GamePlay.PROMPTED, text);
    } else {
        game.send_prompt(GamePlay.NOT_PROMPTED, "");
    }

    promptInput.disabled = true;
    document.getElementById("submit_prompt_button").disabled = true;
    spectatorMessage.style.display = "block";
    spectatorMessage.textContent = "Transmitting parameters across network layers...";
}

// Event Subscriptions
document.getElementById("login_button").addEventListener("click", () => {
    const user = document.getElementById("username_input").value.trim();
    const pass = document.getElementById("password_input").value;
    if (user && pass) {
        game.login(user, pass);
    } else {
        alert("Credentials field inputs cannot reside empty.");
    }
});

document.getElementById("logout_button").addEventListener("click", () => {
    game.logout();
});

document.getElementById("submit_prompt_button").addEventListener("click", () => {
    submitCurrentPrompt();
});

promptInput.addEventListener("keypress", (e) => {
    if (e.key === "Enter" && !promptInput.disabled) {
        submitCurrentPrompt();
    }
});