const login_screen = document.getElementById("login_screen");
const username_input = document.getElementById("username_input");
const login_button = document.getElementById("login_button");

const team_init_screen = document.getElementById("team_init_screen");
const team_create = document.getElementById("team_create");
const team_select = document.getElementById("team_select");

const create_team_screen = document.getElementById("create_team_screen");
const create_team_name_input = document.getElementById("create_team_name_input");
const create_team_button = document.getElementById("create_team_button");

const select_team_screen = document.getElementById("select_team_screem");
const select_team_name_input = document.getElementById("select_team_name_input");
const select_team_button = document.getElementById("select_team_button");

const game_screen = document.getElementById("game_screen");
const chat_box = document.getElementById("chat_box");
const message_input = document.getElementById("message_input");
const send_button = document.getElementById("send_button");

let socket;

let user_id = null;

let username = "";
let team_name = "";

login_button.addEventListener('click', function(){
    username = username_input.value;

    if (username.trim() == "")
    {
        alert("Enter a username.");
        return;
    }

    socket = new WebSocket("ws://127.0.0.1:65432");

    socket.onmessage = function(event){
        const incoming_data = JSON.parse(event.data);

        if (incoming_data.type === "login_response")
        {
            if (incoming_data.data & 0x0001 === 1) 
            {
                alert("Username taken");
                socket.close();
            }
            else 
            {
                user_id = incoming_data.data & 0x1110;
                login_screen.style.display = "none";
                team_init_screen.style.display = "block";
            }
        }
        
        else if (incoming_data.type === "create_team_response")
        {
            if (incoming_data.data & 0x0001 === 1)
            {
                alert("Team name taken");
            }
            else 
            {
                create_team_screen.style.display = "none";
                game_screen.style.display = "block";
            }
        }
    };

    socket.onopen = function(event)
    {
        const login_data = {
            type: "login",
            text: username
        };

        socket.send(JSON.stringify(login_data));
    };

});

team_create.addEventListener('click', function(){
    team_init_screen.style.display = "none";
    create_team_screen.style.display = "block";
});

create_team_button.addEventListener('click', function(){
    team_name = create_team_name_input.value;

    if (team_name.trim() == "")
    {
        alert("Enter a team name");
        return;
    }

    const team_create_data = {
        type: "create_team",
        text: team_name
    };

    socket.send(JSON.stringify(team_create_data));
}); 

team_select.addEventListener('click', function(){
    team_init_screen.style.display = "none";
    select_team_screen.style.display = "block";
});

select_team_button.addEventListener('click', function(){
    team_name = select_team_name_input.value;

    if (team_name.trim() == "")
    {
        alert("Enter a team name");
        return;
    }

    const team_select_data = {
        type: "select_team",
        text: team_name
    };

    socket.send(JSON.stringify(team_select_data));
});



