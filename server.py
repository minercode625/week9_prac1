from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
import asyncio
import json
import time
import uuid
import random

app = FastAPI()
app.mount("/static", StaticFiles(directory="static"), name="static")


@app.get("/")
async def team_page():
    return FileResponse("static/index.html")


@app.get("/admin")
async def admin_page():
    return FileResponse("static/admin.html")


# --- Game State ---

game = {
    "status": "waiting",  # waiting | running | finished
    "timer_end": None,
    "teams": {},          # team_name -> {connected, ws, score, status, strategy_prompt}
    "active_calls": {},   # call_id -> {caller, receiver, started_at, messages, timer_end, buttons, timer_task}
    "history": {},        # "teamA::teamB" (sorted) -> [{round, messages, buttons, scores}]
}

admin_ws = None
GAME_DURATION = 360       # 5 minutes
CALL_DURATION = 40        # 40 seconds conversation
BUTTON_TIMEOUT = 10       # 10 seconds to choose button


def get_history_key(team_a: str, team_b: str) -> str:
    return "::".join(sorted([team_a, team_b]))


def get_pair_history(team_a: str, team_b: str) -> list:
    key = get_history_key(team_a, team_b)
    return game["history"].get(key, [])


def calculate_scores(button_a: str, button_b: str) -> tuple[int, int]:
    if button_a == "green" and button_b == "green":
        return 3, 3
    elif button_a == "green" and button_b == "red":
        return 0, 5
    elif button_a == "red" and button_b == "green":
        return 5, 0
    else:  # both red
        return 1, 1


def build_leaderboard() -> list:
    return sorted(
        [{"team": name, "score": info["score"], "status": info["status"]}
         for name, info in game["teams"].items()],
        key=lambda x: x["score"],
        reverse=True,
    )


def build_state_update() -> dict:
    teams = {}
    for name, info in game["teams"].items():
        teams[name] = {
            "score": info["score"],
            "status": info["status"],
            "connected": info["connected"],
        }
    active_calls = []
    for call_id, call in game["active_calls"].items():
        elapsed = time.time() - call["started_at"]
        active_calls.append({
            "call_id": call_id,
            "caller": call["caller"],
            "receiver": call["receiver"],
            "elapsed": round(elapsed, 1),
            "timer_end": call["timer_end"],
        })
    return {
        "type": "state_update",
        "game_status": game["status"],
        "timer_end": game["timer_end"],
        "teams": teams,
        "active_calls": active_calls,
        "leaderboard": build_leaderboard(),
    }


async def broadcast_state():
    msg = json.dumps(build_state_update())
    tasks = []
    for name, info in game["teams"].items():
        if info["connected"] and info["ws"]:
            tasks.append(info["ws"].send_text(msg))
    if admin_ws:
        tasks.append(admin_ws.send_text(msg))
    await asyncio.gather(*tasks, return_exceptions=True)


async def send_to_team(team_name: str, data: dict):
    info = game["teams"].get(team_name)
    if info and info["connected"] and info["ws"]:
        await info["ws"].send_text(json.dumps(data))


async def send_to_admin(data: dict):
    global admin_ws
    if admin_ws:
        try:
            await admin_ws.send_text(json.dumps(data))
        except Exception:
            admin_ws = None


game_timer_task = None


async def start_game():
    global game_timer_task
    if game["status"] != "waiting":
        return

    game["status"] = "running"
    game["timer_end"] = time.time() + GAME_DURATION

    # Reset scores
    for team in game["teams"].values():
        team["score"] = 0
        team["status"] = "idle"
    game["active_calls"] = {}
    game["history"] = {}

    # Notify all teams
    start_msg = {"type": "game_start", "timer_end": game["timer_end"]}
    for name in game["teams"]:
        await send_to_team(name, start_msg)
    await send_to_admin(start_msg)
    await send_to_admin({"type": "log", "message": "Game started!"})
    await broadcast_state()

    # Start game timer
    game_timer_task = asyncio.create_task(game_timer())


async def game_timer():
    remaining = game["timer_end"] - time.time()
    if remaining > 0:
        await asyncio.sleep(remaining)

    # Wait for active calls to finish
    while game["active_calls"]:
        await asyncio.sleep(1)

    await stop_game()


async def stop_game():
    global game_timer_task
    if game["status"] == "finished":
        return

    game["status"] = "finished"

    final_scores = build_leaderboard()
    end_msg = {"type": "game_end", "final_scores": final_scores}
    for name in game["teams"]:
        await send_to_team(name, end_msg)
    await send_to_admin(end_msg)
    await send_to_admin({"type": "log", "message": "Game ended!"})
    await broadcast_state()

    if game_timer_task and not game_timer_task.done():
        game_timer_task.cancel()
    game_timer_task = None


async def reset_game():
    game["status"] = "waiting"
    game["timer_end"] = None
    game["active_calls"] = {}
    game["history"] = {}
    for team in game["teams"].values():
        team["score"] = 0
        team["status"] = "idle"
    await broadcast_state()


async def handle_call(caller: str, data: dict):
    target = data.get("target_team")
    if not target or target not in game["teams"]:
        await send_to_team(caller, {"type": "error", "message": f"Team {target} not found"})
        return
    if game["status"] != "running":
        await send_to_team(caller, {"type": "error", "message": "Game is not running"})
        return
    if game["teams"][caller]["status"] != "idle":
        await send_to_team(caller, {"type": "error", "message": "You are already in a call"})
        return
    if game["teams"][target]["status"] != "idle":
        await send_to_team(caller, {"type": "error", "message": f"{target} is busy"})
        return
    if game["timer_end"] and time.time() > game["timer_end"]:
        await send_to_team(caller, {"type": "error", "message": "Game time is up, no new calls"})
        return

    call_id = str(uuid.uuid4())[:8]
    now = time.time()
    call = {
        "caller": caller,
        "receiver": target,
        "started_at": now,
        "messages": [],
        "timer_end": now + CALL_DURATION,
        "buttons": {caller: None, target: None},
        "timer_task": None,
        "button_timer_task": None,
        "phase": "conversation",  # conversation | choosing
    }
    game["active_calls"][call_id] = call
    game["teams"][caller]["status"] = "in_call"
    game["teams"][target]["status"] = "in_call"

    pair_history = get_pair_history(caller, target)

    # Notify caller
    await send_to_team(caller, {
        "type": "call_started",
        "call_id": call_id,
        "opponent": target,
        "history": pair_history,
        "timer_end": call["timer_end"],
        "you_are": "caller",
    })

    # Notify receiver
    await send_to_team(target, {
        "type": "call_incoming",
        "call_id": call_id,
        "from_team": caller,
        "history": pair_history,
        "timer_end": call["timer_end"],
        "you_are": "receiver",
    })

    await send_to_admin({"type": "log", "message": f"{caller} called {target} (call {call_id})"})
    await broadcast_state()

    # Start 40-second timer
    call["timer_task"] = asyncio.create_task(call_timer(call_id))


async def call_timer(call_id: str):
    call = game["active_calls"].get(call_id)
    if not call:
        return
    remaining = call["timer_end"] - time.time()
    if remaining > 0:
        await asyncio.sleep(remaining)

    call = game["active_calls"].get(call_id)
    if not call or call["phase"] != "conversation":
        return

    call["phase"] = "choosing"
    game["teams"][call["caller"]]["status"] = "choosing"
    game["teams"][call["receiver"]]["status"] = "choosing"

    # Ask both teams to choose buttons
    choose_msg = {"type": "choose_button", "call_id": call_id}
    await send_to_team(call["caller"], choose_msg)
    await send_to_team(call["receiver"], choose_msg)
    await send_to_admin({"type": "log", "message": f"Call {call_id}: conversation ended, choosing buttons"})
    await broadcast_state()

    # Start button timeout
    call["button_timer_task"] = asyncio.create_task(button_timeout(call_id))


async def handle_chat_message(sender: str, data: dict):
    call_id = data.get("call_id")
    message = data.get("message", "")
    call = game["active_calls"].get(call_id)
    if not call or call["phase"] != "conversation":
        return
    if sender not in (call["caller"], call["receiver"]):
        return

    opponent = call["receiver"] if sender == call["caller"] else call["caller"]
    call["messages"].append({"from": sender, "content": message})

    await send_to_team(opponent, {
        "type": "chat_receive",
        "call_id": call_id,
        "message": message,
    })
    await send_to_admin({
        "type": "log",
        "message": f"[{call_id}] {sender}: {message[:80]}",
    })


async def handle_button_choice(team: str, data: dict):
    call_id = data.get("call_id")
    button = data.get("button")
    call = game["active_calls"].get(call_id)
    if not call or call["phase"] != "choosing":
        return
    if team not in call["buttons"]:
        return
    if button not in ("green", "red"):
        return
    if call["buttons"][team] is not None:
        return  # already chosen

    call["buttons"][team] = button
    await send_to_admin({
        "type": "log",
        "message": f"[{call_id}] {team} chose {button}",
    })

    # Check if both have chosen
    caller = call["caller"]
    receiver = call["receiver"]
    if call["buttons"][caller] is not None and call["buttons"][receiver] is not None:
        await resolve_call(call_id)


async def button_timeout(call_id: str):
    await asyncio.sleep(BUTTON_TIMEOUT)
    call = game["active_calls"].get(call_id)
    if not call or call["phase"] != "choosing":
        return

    for team in [call["caller"], call["receiver"]]:
        if call["buttons"][team] is None:
            call["buttons"][team] = random.choice(["green", "red"])
            await send_to_admin({
                "type": "log",
                "message": f"[{call_id}] {team} timed out, random: {call['buttons'][team]}",
            })

    await resolve_call(call_id)


async def resolve_call(call_id: str):
    call = game["active_calls"].get(call_id)
    if not call:
        return

    caller = call["caller"]
    receiver = call["receiver"]
    btn_caller = call["buttons"][caller]
    btn_receiver = call["buttons"][receiver]
    score_caller, score_receiver = calculate_scores(btn_caller, btn_receiver)

    # Update scores
    game["teams"][caller]["score"] += score_caller
    game["teams"][receiver]["score"] += score_receiver
    game["teams"][caller]["status"] = "idle"
    game["teams"][receiver]["status"] = "idle"

    # Save history
    key = get_history_key(caller, receiver)
    if key not in game["history"]:
        game["history"][key] = []
    game["history"][key].append({
        "round": len(game["history"][key]) + 1,
        "caller": caller,
        "receiver": receiver,
        "messages": call["messages"],
        "buttons": {caller: btn_caller, receiver: btn_receiver},
        "scores": {caller: score_caller, receiver: score_receiver},
    })

    # Notify caller
    await send_to_team(caller, {
        "type": "call_result",
        "call_id": call_id,
        "my_button": btn_caller,
        "their_button": btn_receiver,
        "my_score": score_caller,
        "their_score": score_receiver,
    })

    # Notify receiver
    await send_to_team(receiver, {
        "type": "call_result",
        "call_id": call_id,
        "my_button": btn_receiver,
        "their_button": btn_caller,
        "my_score": score_receiver,
        "their_score": score_caller,
    })

    await send_to_admin({
        "type": "log",
        "message": f"[{call_id}] Result: {caller}={btn_caller}(+{score_caller}), {receiver}={btn_receiver}(+{score_receiver})",
    })

    # Cancel button timer if still running
    if call.get("button_timer_task") and not call["button_timer_task"].done():
        call["button_timer_task"].cancel()

    # Remove call
    del game["active_calls"][call_id]
    await broadcast_state()


@app.websocket("/ws/admin")
async def admin_websocket(websocket: WebSocket):
    global admin_ws
    await websocket.accept()
    admin_ws = websocket
    await broadcast_state()

    try:
        while True:
            raw = await websocket.receive_text()
            data = json.loads(raw)
            msg_type = data.get("type")

            if msg_type == "admin_start":
                await start_game()
            elif msg_type == "admin_stop":
                await stop_game()
            elif msg_type == "admin_reset":
                await reset_game()

    except WebSocketDisconnect:
        admin_ws = None


@app.websocket("/ws/{team_name}")
async def team_websocket(websocket: WebSocket, team_name: str):
    await websocket.accept()

    # Register or reconnect team
    if team_name not in game["teams"]:
        game["teams"][team_name] = {
            "connected": True,
            "ws": websocket,
            "score": 0,
            "status": "idle",
            "strategy_prompt": "",
        }
    else:
        game["teams"][team_name]["connected"] = True
        game["teams"][team_name]["ws"] = websocket

    await broadcast_state()
    await send_to_admin({"type": "log", "message": f"{team_name} connected"})

    try:
        while True:
            raw = await websocket.receive_text()
            data = json.loads(raw)
            msg_type = data.get("type")

            if msg_type == "set_strategy":
                if game["status"] != "waiting":
                    await send_to_team(team_name, {"type": "error", "message": "Cannot change strategy during game"})
                else:
                    game["teams"][team_name]["strategy_prompt"] = data.get("strategy_prompt", "")
                    await send_to_admin({"type": "log", "message": f"{team_name} set strategy"})

            elif msg_type == "call":
                await handle_call(team_name, data)

            elif msg_type == "chat_message":
                await handle_chat_message(team_name, data)

            elif msg_type == "button_choice":
                await handle_button_choice(team_name, data)

    except WebSocketDisconnect:
        game["teams"][team_name]["connected"] = False
        game["teams"][team_name]["ws"] = None

        # If in a call, auto-forfeit with random button
        for call_id, call in list(game["active_calls"].items()):
            if team_name in (call["caller"], call["receiver"]):
                if call["buttons"][team_name] is None:
                    call["buttons"][team_name] = random.choice(["green", "red"])
                    other = call["receiver"] if team_name == call["caller"] else call["caller"]
                    if call["buttons"][other] is None:
                        call["buttons"][other] = random.choice(["green", "red"])
                    await resolve_call(call_id)

        await broadcast_state()
        await send_to_admin({"type": "log", "message": f"{team_name} disconnected"})


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
