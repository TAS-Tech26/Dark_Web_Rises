import { GameClient } from './login.js';

const game = new GameClient("ws://127.0.0.1:65432/ws");

// DOM Elements
const loginScreen = document.getElementById("login_screen");
const lobbyScreen = document.getElementById("lobby_screen");
const usernameInput = document.getElementById("username_input");
const passwordInput = document.getElementById("password_input");
const lobbyLog = document.getElementById("lobby_log");

// Helper to write to the lobby screen
function addLog(text, className = "log-msg") {
    const msg = document.createElement("div");
    msg.className = className;
    msg.textContent = text;
    lobbyLog.appendChild(msg);
    lobbyLog.scrollTop = lobbyLog.scrollHeight;
}

// --- NETWORK HOOKS ---

game.on_login_success = (userId) => {
    loginScreen.style.display = "none";
    lobbyScreen.style.display = "block";
    addLog(`System: You have joined the lobby.`);
};

game.on_login_failed = (status) => {
    alert("Login failed! Code: " + status);
};

game.on_logout_success = () => {
    lobbyScreen.style.display = "none";
    loginScreen.style.display = "block";
    lobbyLog.innerHTML = ""; // clear log
};

// --- TEAM STATE HOOKS ---

game.on_player_joined = (username) => {
    addLog(`[+] ${username} joined the team.`);
};

game.on_player_left = (username) => {
    addLog(`[-] ${username} left the team.`);
};

game.on_team_ready = () => {
    addLog(`⭐ TEAM IS FULL AND READY! ⭐`, "log-ready");
};

game.on_team_unready = () => {
    addLog(`⚠️ Team lost a member. Not ready.`, "log-unready");
};


// --- BUTTON ACTIONS ---

document.getElementById("login_button").addEventListener("click", () => {
    game.login(usernameInput.value.trim(), passwordInput.value.trim());
});

document.getElementById("logout_button").addEventListener("click", () => {
    game.logout();
});