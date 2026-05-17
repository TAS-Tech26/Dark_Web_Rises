// 1. Import your beautifully structured network class
import { GameClient } from './login.js';

// Initialize the client (Make sure the URL matches your FastAPI uvicorn settings!)
const game = new GameClient("ws://127.0.0.1:65432/ws");

// ==========================================
// 2. DOM Element Selection
// ==========================================

// Login Screen
const loginScreen = document.getElementById("login_screen");
const usernameInput = document.getElementById("username_input");
const passwordInput = document.getElementById("password_input");
const loginButton = document.getElementById("login_button");

// Team Init Screen
const logoutButton = document.getElementById("logout_button");
const teamInitScreen = document.getElementById("team_init_screen");
const teamCreateBtn = document.getElementById("team_create");
const teamSelectBtn = document.getElementById("team_select");

// Create Team Screen
const createTeamScreen = document.getElementById("create_team_screen");
const createTeamNameInput = document.getElementById("create_team_name_input");
const createTeamBtn = document.getElementById("create_team_button");

// Select Team Screen (Note: Matching the typo 'screem' from your HTML)
const selectTeamScreen = document.getElementById("select_team_screem");
const selectTeamNameInput = document.getElementById("select_team_name_input");
const selectTeamBtn = document.getElementById("select_team_button");

// Game Screen
const gameScreen = document.getElementById("game_screen");
const chatBox = document.getElementById("chat_box");
const messageInput = document.getElementById("message_input");
const sendButton = document.getElementById("send_button");

// ==========================================
// 3. Network Callbacks (The "Hooks")
// ==========================================

// When the server says YES to a login
game.on_login_success = function(userId) {
    console.log("Login successful! Assigned User ID:", userId);
    
    // Hide login, show team selection
    loginScreen.style.display = "none";
    teamInitScreen.style.display = "block";
};

// When the server says NO to a login
game.on_login_failed = function(status) {
    alert("Login failed. Status Code: " + status);
    
    // Highlight the box red to show an error
    usernameInput.style.borderColor = "#ef4444";
    passwordInput.value = ""; // Clear the password for safety
};

game.on_logout_success = function() {
    console.log("Successfully logged out.");
    
    // 1. Hide the game screen, show the login screen
    gameScreen.style.display = "none";
    loginScreen.style.display = "block";
    
    // 2. Clean up the UI so it's fresh for the next login
    chatBox.innerHTML = ""; 
    passwordInput.value = ""; // Clear the password field for security
    usernameInput.style.borderColor = "#3d3d3d"; // Reset border color
};

game.on_logout_failed = function(status) {
    alert("Failed to log out cleanly. Status: " + status);
};

// ==========================================
// 4. UI Event Listeners
// ==========================================

// --- LOGIN FLOW ---
loginButton.addEventListener("click", function() {
    const username = usernameInput.value.trim();
    const password = passwordInput.value.trim();

    if (username === "" || password === "") {
        alert("Please enter both a username and password.");
        return;
    }

    // Reset border color in case it was red from a previous failure
    usernameInput.style.borderColor = "#3d3d3d";

    // Call your clean network API
    game.login(username, password);
});

logoutButton.addEventListener("click", function() {
    // Call your clean network API
    game.logout();
});

// --- TEAM ROUTING ---
teamCreateBtn.addEventListener("click", function() {
    teamInitScreen.style.display = "none";
    createTeamScreen.style.display = "block";
});

teamSelectBtn.addEventListener("click", function() {
    teamInitScreen.style.display = "none";
    selectTeamScreen.style.display = "block";
});

// --- TEAM CREATION / JOINING (UI testing placeholders) ---
// Note: You will wire these up to the server later, but for now, 
// they just advance the UI to the game screen so you can test the flow!

createTeamBtn.addEventListener("click", function() {
    const teamName = createTeamNameInput.value.trim();
    if (teamName === "") return;

    // TODO: game.createTeam(teamName);
    
    createTeamScreen.style.display = "none";
    gameScreen.style.display = "block";
    addSystemMessage(`You created and joined team: ${teamName}`);
});

selectTeamBtn.addEventListener("click", function() {
    const teamName = selectTeamNameInput.value.trim();
    if (teamName === "") return;

    // TODO: game.joinTeam(teamName);

    selectTeamScreen.style.display = "none";
    gameScreen.style.display = "block";
    addSystemMessage(`You joined team: ${teamName}`);
});

// --- GAME CHAT CONTROLS ---
sendButton.addEventListener("click", function() {
    const text = messageInput.value.trim();
    if (text === "") return;

    // TODO: game.sendChatMessage(text);
    
    // For now, just print it locally to test the UI
    addChatMessage("You", text);
    messageInput.value = "";
});

// ==========================================
// 5. UI Helper Functions
// ==========================================

function addSystemMessage(text) {
    const msg = document.createElement("div");
    msg.className = "chat-message";
    msg.style.color = "#6366f1"; // Indigo accent
    msg.style.fontWeight = "bold";
    msg.textContent = `System: ${text}`;
    
    chatBox.appendChild(msg);
    chatBox.scrollTop = chatBox.scrollHeight; // Auto-scroll to bottom
}

function addChatMessage(sender, text) {
    const msg = document.createElement("div");
    msg.className = "chat-message";
    msg.textContent = `${sender}: ${text}`;
    
    chatBox.appendChild(msg);
    chatBox.scrollTop = chatBox.scrollHeight; 
}