import os
import random
import re
import uuid
from string import ascii_uppercase

import requests
from flask import Flask, jsonify, render_template, request, session, redirect, url_for
from flask_socketio import join_room, leave_room, send, SocketIO

app = Flask(__name__)
app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY", "dev-secret-change-me")
socketio = SocketIO(app)

TENOR_API_KEY = os.environ.get("TENOR_API_KEY", "")

rooms = {}

# Lightweight, offline, rule-based vibe detector — not real sentiment AI,
# just keyword matching, but it's fast, free, and needs no API key.
# First matching pattern wins, so order = priority.
EMOTION_RULES = [
    (r"\b(fired|laid off|layoff|quitting|quit|resign(ed)?)\b", "\U0001F480"),       # 💀
    (r"\b(promot(ed|ion)|raise|bonus)\b", "\U0001F389"),                            # 🎉
    (r"\b(crush|dating|secretly|hooking up|seeing each other)\b", "\U0001F440"),    # 👀
    (r"\b(omg|no way|shut up|seriously\?|wait what)\b", "\U0001F631"),              # 😱
    (r"\b(lol|lmao|haha+|hilarious)\b", "\U0001F602"),                              # 😂
    (r"\b(furious|angry|mad|hate|annoyed|pissed)\b", "\U0001F624"),                 # 😤
    (r"\b(exhausted|tired|burnt out|burned out|drained)\b", "\U0001F629"),          # 😩
    (r"\b(coffee|lunch|snack|break)\b", "\u2615"),                                  # ☕
    (r"\b(deadline|meeting|zoom|standup|sprint|overtime)\b", "\U0001F62C"),         # 😬
    (r"\b(boss|manager|hr department)\b", "\U0001F454"),                            # 👔
    (r"\b(love|adorable|sweet|crushing on)\b", "\U0001F970"),                       # 🥰
]


def detect_emotion(text):
    lower = text.lower()
    for pattern, emoji in EMOTION_RULES:
        if re.search(pattern, lower):
            return emoji
    if text.count("!") >= 2:
        return "\U0001F632"  # 😲
    if text.strip().endswith("?"):
        return "\U0001F914"  # 🤔
    return ""


def generate_unique_code(length):
    while True:
        code = ""
        for _ in range(length):
            code += random.choice(ascii_uppercase)

        if code not in rooms:
            break

    return code


@app.route("/", methods=["POST", "GET"])
def home():
    session.clear()
    if request.method == "POST":
        name = request.form.get("name")
        code = request.form.get("code")
        join = request.form.get("join", False)
        create = request.form.get("create", False)

        if not name:
            return render_template("home.html", error="Please enter a name.", code=code, name=name)

        if join != False and not code:
            return render_template("home.html", error="Please enter a room code.", code=code, name=name)

        room = code
        if create != False:
            room = generate_unique_code(4)
            rooms[room] = {"members": 0, "messages": []}
        elif code not in rooms:
            return render_template("home.html", error="Room does not exist.", code=code, name=name)

        session["room"] = room
        session["name"] = name
        return redirect(url_for("room"))

    return render_template("home.html")


@app.route("/room")
def room():
    room = session.get("room")
    if room is None or session.get("name") is None or room not in rooms:
        return redirect(url_for("home"))
    return render_template(
        "room.html",
        code=room,
        messages=[],
        name=session.get("name"),
    )


@app.route("/gif-search")
def gif_search():
    query = request.args.get("q", "").strip()
    if not TENOR_API_KEY:
        return jsonify(results=[], error="GIF search not configured"), 200

    url = "https://tenor.googleapis.com/v2/search" if query else "https://tenor.googleapis.com/v2/featured"
    params = {"key": TENOR_API_KEY, "client_key": "chatroom_1", "limit": 24, "media_filter": "gif"}
    if query:
        params["q"] = query

    try:
        resp = requests.get(url, params=params, timeout=6)
        resp.raise_for_status()
        data = resp.json()
    except requests.RequestException:
        return jsonify(results=[], error="GIF search failed"), 200

    results = [
        item["media_formats"]["gif"]["url"]
        for item in data.get("results", [])
        if item.get("media_formats", {}).get("gif", {}).get("url")
    ]
    return jsonify(results=results)


@socketio.on("message")
def message(data):
    room = session.get("room")
    if room not in rooms:
        return

    text = data["data"]
    msg_type = data.get("type", "text")
    content = {
        "id": str(uuid.uuid4()),
        "name": session.get("name"),
        "message": text,
        "type": msg_type,
        "emoji": detect_emotion(text) if msg_type == "text" else "",
        "reactions": {},
    }
    send(content, to=room)
    rooms[room]["messages"].append(content)
    print(f"{session.get('name')} said: {text}")


@socketio.on("react")
def react(data):
    room = session.get("room")
    if room not in rooms:
        return

    msg_id = data.get("message_id")
    emoji = data.get("emoji")
    if not msg_id or not emoji or len(emoji) > 8:
        return

    for m in rooms[room]["messages"]:
        if m.get("id") == msg_id:
            m.setdefault("reactions", {})
            m["reactions"][emoji] = m["reactions"].get(emoji, 0) + 1
            socketio.emit("reaction_update", {"message_id": msg_id, "reactions": m["reactions"]}, to=room)
            break


@socketio.on("connect")
def connect(auth):
    room = session.get("room")
    name = session.get("name")
    if not room or not name:
        return
    if room not in rooms:
        leave_room(room)
        return

    join_room(room)
    send({"name": name, "message": "has entered the room"}, to=room)
    rooms[room]["members"] += 1
    rooms[room].setdefault("names", []).append(name)
    socketio.emit(
        "presence_update",
        {"count": rooms[room]["members"], "names": rooms[room]["names"]},
        to=room,
    )
    print(f"{name} joined room {room}")


@socketio.on("disconnect")
def disconnect():
    room = session.get("room")
    name = session.get("name")
    leave_room(room)
    if room in rooms:
        rooms[room]["members"] -= 1
        names = rooms[room].get("names", [])
        if name in names:
            names.remove(name)
        if rooms[room]["members"] <= 0:
            del rooms[room]
        else:
            socketio.emit(
                "presence_update",
                {"count": rooms[room]["members"], "names": names},
                to=room,
            )

    send({"name": name, "message": "has left the room"}, to=room)
    print(f"{name} has left the room {room}")


@socketio.on("typing")
def typing():
    room = session.get("room")
    name = session.get("name")
    if room not in rooms:
        return
    socketio.emit("typing", {"name": name}, room=room, skip_sid=request.sid)


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    socketio.run(app, host="0.0.0.0", port=port)
