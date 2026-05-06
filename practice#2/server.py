from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
import asyncio
import json
import random
import os

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

app = FastAPI()
app.mount("/static", StaticFiles(directory=os.path.join(BASE_DIR, "static")), name="static")

with open(os.path.join(BASE_DIR, "keywords.json"), "r", encoding="utf-8") as f:
    KEYWORD_POOL = json.load(f)

GAME_RULES = (
    "[게임 룰]\n"
    "- 이 게임은 한국 파티게임 '스파이 게임'의 LLM 버전이다.\n"
    "- 시민(citizen)들은 카테고리와 키워드를 모두 알고 있다.\n"
    "- 스파이(spy)는 카테고리만 알고 키워드는 모른다.\n"
    "- 총 2라운드 동안 각 팀이 한 번씩 키워드에 대해 묘사한다.\n"
    "- 시민은 다른 시민이 알아챌 수 있도록 묘사하되, 스파이가 추측하지 못할 만큼 모호해야 한다.\n"
    "- 스파이는 카테고리와 다른 팀들의 발언을 듣고 시민인 척 자연스럽게 묘사해야 한다.\n"
    "- 모든 발언은 즉시 모두에게 공개되며 history로 누적된다.\n"
    "- 발언은 80자 이내로 짧게 한다.\n"
    "- 2라운드가 끝나면 모두가 동시에 누가 스파이인지 투표한다.\n"
    "- 시민이 스파이를 정확히 지목하면 스파이는 마지막 1회 기회로 키워드를 추측한다.\n"
    "- 키워드 추측 성공 시 스파이 승리, 실패 시 시민 승리.\n"
    "- 시민이 스파이를 못 맞추거나 동률이면 즉시 스파이 승리."
)

MIN_TEAMS = 4

# === Game State ===
game = {
    "status": "waiting",   # waiting | round1 | round2 | voting | spy_final | finished
    "teams": {},           # name -> {connected, ws, prompts, role}
    "category": None,
    "keyword": None,
    "spy": None,
    "history": [],         # list of {team, message, round}
    "speaking_order": [],
    "current_speaker_idx": 0,
    "round_num": 0,
    "votes": {},           # team -> voted_team
    "vote_counts": {},
    "suspected": None,
    "spy_final_guess": None,
    "winner": None,
    "reason": None,
}

admin_ws = None


@app.get("/")
async def team_page():
    return FileResponse(os.path.join(BASE_DIR, "static", "index.html"))


@app.get("/admin")
async def admin_page():
    return FileResponse(os.path.join(BASE_DIR, "static", "admin.html"))


def reset_game_state():
    game["status"] = "waiting"
    game["category"] = None
    game["keyword"] = None
    game["spy"] = None
    game["history"] = []
    game["speaking_order"] = []
    game["current_speaker_idx"] = 0
    game["round_num"] = 0
    game["votes"] = {}
    game["vote_counts"] = {}
    game["suspected"] = None
    game["spy_final_guess"] = None
    game["winner"] = None
    game["reason"] = None
    for team in game["teams"].values():
        team["role"] = None


def all_prompts_set(team_info) -> bool:
    p = team_info.get("prompts", {})
    keys = ("citizen_describe", "citizen_vote", "spy_deceive", "spy_final")
    return all(p.get(k, "").strip() for k in keys)


def build_state_update() -> dict:
    teams = {}
    for name, info in game["teams"].items():
        teams[name] = {
            "connected": info["connected"],
            "has_prompts": all_prompts_set(info),
        }
    current_speaker = None
    if game["speaking_order"] and game["current_speaker_idx"] < len(game["speaking_order"]):
        current_speaker = game["speaking_order"][game["current_speaker_idx"]]
    return {
        "type": "state_update",
        "game_status": game["status"],
        "teams": teams,
        "round_num": game["round_num"],
        "current_speaker": current_speaker,
        "category": game["category"],
        "history": game["history"],
    }


def build_admin_state() -> dict:
    base = build_state_update()
    base["admin_view"] = True
    base["spy"] = game["spy"]
    base["keyword"] = game["keyword"]
    base["votes"] = game["votes"]
    base["vote_counts"] = game["vote_counts"]
    base["suspected"] = game["suspected"]
    base["winner"] = game["winner"]
    base["reason"] = game["reason"]
    return base


async def broadcast_state():
    msg_team = json.dumps(build_state_update(), ensure_ascii=False)
    msg_admin = json.dumps(build_admin_state(), ensure_ascii=False)
    tasks = []
    for info in game["teams"].values():
        if info["connected"] and info["ws"]:
            tasks.append(info["ws"].send_text(msg_team))
    if admin_ws:
        tasks.append(admin_ws.send_text(msg_admin))
    await asyncio.gather(*tasks, return_exceptions=True)


async def send_to_team(team_name: str, data: dict):
    info = game["teams"].get(team_name)
    if info and info["connected"] and info["ws"]:
        try:
            await info["ws"].send_text(json.dumps(data, ensure_ascii=False))
        except Exception:
            pass


async def send_to_admin(data: dict):
    global admin_ws
    if admin_ws:
        try:
            await admin_ws.send_text(json.dumps(data, ensure_ascii=False))
        except Exception:
            admin_ws = None


async def admin_log(message: str):
    await send_to_admin({"type": "log", "message": message})


async def broadcast_speech(speech: dict):
    msg = {"type": "speech", "speech": speech}
    for name in game["teams"]:
        await send_to_team(name, msg)
    await send_to_admin(msg)


async def start_game():
    if game["status"] != "waiting":
        await admin_log("Game is not in waiting state.")
        return
    if len(game["teams"]) < MIN_TEAMS:
        await admin_log(f"Need at least {MIN_TEAMS} teams to start (current: {len(game['teams'])}).")
        return
    for name, info in game["teams"].items():
        if not info["connected"]:
            await admin_log(f"{name} is not connected.")
            return
        if not all_prompts_set(info):
            await admin_log(f"{name} has not submitted all 4 prompts.")
            return

    # Pick category & keyword
    category = random.choice(list(KEYWORD_POOL.keys()))
    keyword = random.choice(KEYWORD_POOL[category])
    game["category"] = category
    game["keyword"] = keyword

    # Pick spy
    team_names = list(game["teams"].keys())
    spy = random.choice(team_names)
    game["spy"] = spy
    for name in team_names:
        game["teams"][name]["role"] = "spy" if name == spy else "citizen"

    # Round 1 speaking order — hidden rule: spy not first
    order = team_names.copy()
    random.shuffle(order)
    if order[0] == spy and len(order) > 1:
        swap_idx = random.randint(1, len(order) - 1)
        order[0], order[swap_idx] = order[swap_idx], order[0]
    game["speaking_order"] = order
    game["current_speaker_idx"] = 0
    game["round_num"] = 1
    game["status"] = "round1"
    game["history"] = []

    # Notify each team
    for name, info in game["teams"].items():
        role = info["role"]
        await send_to_team(name, {
            "type": "game_start",
            "role": role,
            "category": category,
            "keyword": keyword if role == "citizen" else None,
            "rules": GAME_RULES,
            "team_names": team_names,
            "speaking_order": order,
        })

    await admin_log(f"Game started. Category={category}, Keyword={keyword}, Spy={spy}")
    await admin_log(f"Round 1 order: {order}")
    await broadcast_state()
    await advance_turn()


async def advance_turn():
    """Advance to next speaker, next round, or voting phase."""
    if game["current_speaker_idx"] >= len(game["speaking_order"]):
        if game["round_num"] == 1:
            order = list(game["teams"].keys())
            random.shuffle(order)
            game["speaking_order"] = order
            game["current_speaker_idx"] = 0
            game["round_num"] = 2
            game["status"] = "round2"
            await admin_log(f"Round 2 order: {order}")
            await broadcast_state()
        elif game["round_num"] == 2:
            await start_voting()
            return

    if game["current_speaker_idx"] >= len(game["speaking_order"]):
        return

    speaker = game["speaking_order"][game["current_speaker_idx"]]
    role = game["teams"][speaker]["role"]
    msg = {
        "type": "your_turn",
        "round": game["round_num"],
        "history": game["history"],
        "speaker_position": game["current_speaker_idx"] + 1,
        "total_speakers": len(game["speaking_order"]),
    }
    await send_to_team(speaker, msg)
    # Notify others to wait
    wait_msg = {
        "type": "waiting_for_speaker",
        "speaker": speaker,
        "round": game["round_num"],
    }
    for name in game["teams"]:
        if name != speaker:
            await send_to_team(name, wait_msg)
    await admin_log(f"Round {game['round_num']}: {speaker} ({role}) is speaking ({game['current_speaker_idx']+1}/{len(game['speaking_order'])})")
    await broadcast_state()


async def handle_speech(team_name: str, message: str):
    if game["status"] not in ("round1", "round2"):
        return
    if game["current_speaker_idx"] >= len(game["speaking_order"]):
        return
    speaker = game["speaking_order"][game["current_speaker_idx"]]
    if team_name != speaker:
        return  # Not their turn

    # Truncate to 80 chars defensively (students should also instruct their LLM)
    clean_message = (message or "").strip()
    if len(clean_message) > 80:
        clean_message = clean_message[:80]
    if not clean_message:
        clean_message = "..."

    speech = {
        "team": team_name,
        "message": clean_message,
        "round": game["round_num"],
    }
    game["history"].append(speech)
    await broadcast_speech(speech)
    await admin_log(f"[R{game['round_num']}] {team_name}: {clean_message}")

    game["current_speaker_idx"] += 1
    await advance_turn()


async def start_voting():
    game["status"] = "voting"
    game["votes"] = {}
    msg = {
        "type": "voting_phase",
        "history": game["history"],
        "team_names": list(game["teams"].keys()),
    }
    for name in game["teams"]:
        await send_to_team(name, msg)
    await admin_log("Voting phase started — all teams voting simultaneously.")
    await broadcast_state()


async def handle_vote(team_name: str, voted_team: str):
    if game["status"] != "voting":
        return
    if voted_team not in game["teams"] or voted_team == team_name:
        return
    if team_name in game["votes"]:
        return  # already voted

    game["votes"][team_name] = voted_team
    await admin_log(f"{team_name} voted (vote hidden until reveal)")
    await broadcast_state()

    if len(game["votes"]) >= len(game["teams"]):
        await reveal_votes()


async def reveal_votes():
    counts = {}
    for voted in game["votes"].values():
        counts[voted] = counts.get(voted, 0) + 1
    game["vote_counts"] = counts

    if not counts:
        await end_game(winner="spy", reason="no_votes")
        return

    max_count = max(counts.values())
    suspected_list = [t for t, c in counts.items() if c == max_count]

    # Tie -> spy wins
    if len(suspected_list) > 1:
        game["suspected"] = None
        msg = {
            "type": "vote_results",
            "votes": game["votes"],
            "counts": counts,
            "suspected": None,
            "tie": True,
            "actual_spy": game["spy"],
        }
        for name in game["teams"]:
            await send_to_team(name, msg)
        await send_to_admin(msg)
        await admin_log(f"Vote tie. Spy wins. Counts={counts}")
        await end_game(winner="spy", reason="vote_tie")
        return

    suspected = suspected_list[0]
    game["suspected"] = suspected
    msg = {
        "type": "vote_results",
        "votes": game["votes"],
        "counts": counts,
        "suspected": suspected,
        "tie": False,
        "actual_spy": game["spy"],
    }
    for name in game["teams"]:
        await send_to_team(name, msg)
    await send_to_admin(msg)
    await admin_log(f"Suspected: {suspected}, Actual spy: {game['spy']}, Counts={counts}")

    if suspected == game["spy"]:
        await start_spy_final()
    else:
        await end_game(winner="spy", reason="wrong_suspect")


async def start_spy_final():
    game["status"] = "spy_final"
    spy = game["spy"]
    msg = {
        "type": "spy_final_phase",
        "history": game["history"],
        "category": game["category"],
    }
    await send_to_team(spy, msg)
    wait_msg = {"type": "spy_caught", "spy": spy}
    for name in game["teams"]:
        if name != spy:
            await send_to_team(name, wait_msg)
    await admin_log(f"Spy ({spy}) gets one last chance to guess the keyword.")
    await broadcast_state()


async def handle_final_guess(team_name: str, guess: str):
    if game["status"] != "spy_final":
        return
    if team_name != game["spy"]:
        return
    guess_clean = (guess or "").strip()
    game["spy_final_guess"] = guess_clean
    await admin_log(f"Spy guessed: '{guess_clean}' (actual: '{game['keyword']}')")

    if guess_clean == game["keyword"]:
        await end_game(winner="spy", reason="keyword_guessed")
    else:
        await end_game(winner="citizens", reason="wrong_guess")


async def end_game(winner: str, reason: str):
    game["status"] = "finished"
    game["winner"] = winner
    game["reason"] = reason
    msg = {
        "type": "game_end",
        "winner": winner,
        "reason": reason,
        "category": game["category"],
        "keyword": game["keyword"],
        "spy": game["spy"],
        "suspected": game["suspected"],
        "spy_guess": game["spy_final_guess"],
        "history": game["history"],
        "votes": game["votes"],
        "vote_counts": game["vote_counts"],
    }
    for name in game["teams"]:
        await send_to_team(name, msg)
    await send_to_admin(msg)
    await admin_log(f"Game ended. Winner: {winner}, reason: {reason}")
    await broadcast_state()


# === WebSocket: Admin ===
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
            elif msg_type == "admin_reset":
                reset_game_state()
                await broadcast_state()
                await admin_log("Game reset. Teams must re-submit prompts.")
                # Notify teams to reset their UI
                for name in list(game["teams"].keys()):
                    await send_to_team(name, {"type": "game_reset"})
                # Clear submitted prompts
                for info in game["teams"].values():
                    info["prompts"] = {}
                await broadcast_state()
    except WebSocketDisconnect:
        admin_ws = None


# === WebSocket: Team ===
@app.websocket("/ws/{team_name}")
async def team_websocket(websocket: WebSocket, team_name: str):
    await websocket.accept()
    if team_name not in game["teams"]:
        game["teams"][team_name] = {
            "connected": True,
            "ws": websocket,
            "prompts": {},
            "role": None,
        }
    else:
        game["teams"][team_name]["connected"] = True
        game["teams"][team_name]["ws"] = websocket

    await broadcast_state()
    await admin_log(f"{team_name} connected")

    try:
        while True:
            raw = await websocket.receive_text()
            data = json.loads(raw)
            msg_type = data.get("type")

            if msg_type == "set_prompts":
                if game["status"] != "waiting":
                    await send_to_team(team_name, {
                        "type": "error",
                        "message": "Cannot change prompts during game.",
                    })
                else:
                    game["teams"][team_name]["prompts"] = {
                        "citizen_describe": data.get("citizen_describe", ""),
                        "citizen_vote": data.get("citizen_vote", ""),
                        "spy_deceive": data.get("spy_deceive", ""),
                        "spy_final": data.get("spy_final", ""),
                    }
                    await admin_log(f"{team_name} submitted all 4 prompts.")
                    await send_to_team(team_name, {"type": "prompts_accepted"})
                    await broadcast_state()

            elif msg_type == "submit_speech":
                await handle_speech(team_name, data.get("message", ""))

            elif msg_type == "submit_vote":
                await handle_vote(team_name, data.get("voted_team", ""))

            elif msg_type == "submit_final_guess":
                await handle_final_guess(team_name, data.get("guess", ""))

    except WebSocketDisconnect:
        if team_name in game["teams"]:
            game["teams"][team_name]["connected"] = False
            game["teams"][team_name]["ws"] = None
        await broadcast_state()
        await admin_log(f"{team_name} disconnected")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
