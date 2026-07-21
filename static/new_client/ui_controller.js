import { GameClient, GamePlay } from './game_client.js';

// ** Target endpoint matches your Uvicorn launch configuration port precisely **
const game = new GameClient("ws://127.0.0.1:8000/ws");

// --- DOM OBJECT TARGET CACHING ---
let activeAdminId = null;
let adminMonitorInterval = null;

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
const adminScreen = document.getElementById("admin_screen");
const runGameButton = document.getElementById("run_game_button");
const adminStatusMsg = document.getElementById("admin_status_msg");

let currentTimerInterval = null;

// --- DOM ROUTING CONTROLLERS ---
function showScreen(screenElement) {
    loginScreen.classList.remove("active");
    lobbyScreen.classList.remove("active");
    gameScreen.classList.remove("active");
    postgameScreen.classList.remove("active");
    if (adminScreen) adminScreen.classList.remove("active");
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

game.on_admin_login_success = (adminId) => {
    console.log("=== ADMIN HOOK ACTIVATED ===");
    console.log("Raw adminId parameter received:", adminId);
    
    // 1. If the value is undefined, it means game_client.js read the wrong JSON key.
    // Let's log 'game' object status to see if it got stored somewhere else:
    console.log("Current game client state:", game);

    // 2. Assign the true value (or check game.user_id if adminId was misplaced)
    //    Use ?? so an admin id of 0 isn't treated as missing
    activeAdminId = adminId ?? game.user_id; 
    
    if (activeAdminId === null || activeAdminId === undefined) {
        console.error("CRITICAL: No valid admin session key found in login packet!");
        adminStatusMsg.textContent = "Auth Error: Missing Session Key.";
        adminStatusMsg.style.color = "var(--danger-color)";
        return; // Don't start polling with bad data
    }

    showScreen(adminScreen);
    adminStatusMsg.textContent = "Authorized System Command Access Verified.";
    adminStatusMsg.style.color = "var(--accent-color)";
    runGameButton.disabled = false;

    if (adminMonitorInterval) clearInterval(adminMonitorInterval);
    adminMonitorInterval = setInterval(fetchAdminDashboardTelemetry, 2000);
    fetchAdminDashboardTelemetry(); 
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

async function fetchAdminDashboardTelemetry() {
    // If this prints, it means the polling loop is active but you aren't authenticated as an admin yet
    if (activeAdminId === null || activeAdminId === undefined) {
        console.warn("Telemetry Polling: Skipped (activeAdminId is null). Ensure admin login succeeded.");
        return; 
    }
    
    console.log(`[Telemetry Request] Fetching data with X-Admin-Id: ${activeAdminId}`);
    
    try {
        const response = await fetch("/admin/dashboard", {
            method: "GET",
            headers: { 
                "X-Admin-Id": String(activeAdminId),
                "Accept": "application/json"
            }
        });
        
        if (response.ok) {
            const data = await response.json();
            console.log("[Telemetry Response] Received payload:", data);
            renderAdminMonitorGrid(data);
        } else {
            console.error(`[Telemetry Error] Server responded with status: ${response.status}`);
            adminStatusMsg.textContent = `Sync Error: HTTP ${response.status}`;
            adminStatusMsg.style.color = "var(--danger-color)";
        }
    } catch (err) {
        console.error("[Telemetry Transport Fault] Failed to reach endpoint:", err);
    }
}
function renderAdminMonitorGrid(data) {
    const noTeamsMsg = document.getElementById("admin_no_teams_msg");
    const statCards = document.querySelectorAll("#admin_grid_display .panel");
    if (!data || data.total_teams === 0) {
        if (noTeamsMsg) noTeamsMsg.style.display = "block";
        statCards.forEach(card => card.style.display = "none");
        return;
    }
    if (noTeamsMsg) noTeamsMsg.style.display = "none";
    statCards.forEach(card => card.style.display = "");
    document.getElementById("stat_connected_teams").textContent = data.connected_teams;
    document.getElementById("stat_total_teams").textContent = data.total_teams;
    document.getElementById("stat_players_online").textContent = data.total_connected_players;
    document.getElementById("stat_current_round").textContent = data.current_round;
    document.getElementById("stat_total_rounds").textContent = data.total_rounds;
    document.getElementById("stat_game_state").textContent = data.game_state;

}

async function adminTriggerRunGame() {
    adminStatusMsg.textContent = "Transmitting initialization vector...";
    adminStatusMsg.style.color = "var(--text-muted)";
    
    try {
        const response = await fetch("/admin/rungame", {
            method: "POST",
            headers: {
                "Content-Type": "application/json",
                "X-Admin-Id": activeAdminId // Authorization context header criteria
            }
        });
        
        const data = await response.json();
        
        if (response.ok) {
            adminStatusMsg.textContent = "Match ignition successful! Global countdown active.";
            adminStatusMsg.style.color = "var(--accent-color)";
            runGameButton.disabled = true;
        } else {
            adminStatusMsg.textContent = `Execution Denied: ${data.detail || "Server logic halt."}`;
            adminStatusMsg.style.color = "var(--danger-color)";
        }
    } catch (err) {
        adminStatusMsg.textContent = "Network Transport Fault: Check FastAPI console.";
        adminStatusMsg.style.color = "var(--danger-color)";
        console.error(err);
    }
}
runGameButton.addEventListener("click", () => {
    adminTriggerRunGame();
});


// Event Subscriptions
document.getElementById("login_button").addEventListener("click", () => {
    const user = document.getElementById("username_input").value.trim();
    const pass = document.getElementById("password_input").value;
    
    if (user && pass) {
        // Admin accounts authenticate through the same login flow as players;
        // the backend identifies them via server-side admin credentials and
        // responds with an admin_response, which is already handled below.
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