"""
Tracker de apuestas Betway — todo en un archivo.
Lo ejecuta GitHub Actions cada 15 min:
  1) lee mensajes nuevos del canal de Telegram
  2) registra las apuestas en bets.json
  3) determina (ganada/perdida) las que ya pasaron 2 h
"""
import os
import re
import json
import asyncio
import datetime
import requests

# ===========================================================================
# CONFIG (viene de los Secrets de GitHub)
# ===========================================================================
TG_API_ID = int(os.environ.get("TG_API_ID", "0") or "0")
TG_API_HASH = os.environ.get("TG_API_HASH", "")
TG_SESSION = os.environ.get("TG_SESSION", "")
TG_CHANNEL = os.environ.get("TG_CHANNEL", "")
RESOLVE_AFTER_HOURS = float(os.environ.get("RESOLVE_AFTER_HOURS", "2"))
MAX_RESOLVE_WINDOW_HOURS = float(os.environ.get("MAX_RESOLVE_WINDOW_HOURS", "12"))
# Carga inicial (primera vez, sin historial guardado): solo lee los mensajes de
# los ultimos X minutos, porque el historial del canal es enorme.
INITIAL_LOAD_MINUTES = float(os.environ.get("INITIAL_LOAD_MINUTES", "15"))
STAKE_EUR = 100.0
BETS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "bets.json")

# ===========================================================================
# ALMACENAMIENTO
# ===========================================================================
def load():
    if not os.path.exists(BETS_FILE):
        return {"state": {"last_message_id": 0}, "bets": []}
    with open(BETS_FILE, "r", encoding="utf-8") as f:
        data = json.load(f)
    data.setdefault("state", {"last_message_id": 0})
    data.setdefault("bets", [])
    return data


def save(data):
    with open(BETS_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


# ===========================================================================
# PARSER DE MENSAJES
# ===========================================================================
def _field(text, *labels):
    for label in labels:
        m = re.search(label + r"\s*:\s*:?\s*(.+)", text, re.I)
        if m:
            return m.group(1).strip()
    return None


def _parse_odds(s):
    if not s:
        return None
    m = re.search(r"(\d+(?:[.,]\d+)?)", s)
    return float(m.group(1).replace(",", ".")) if m else None


def _parse_event_time(s):
    if not s:
        return None
    m = re.search(r"(\d{1,2})/(\d{1,2})/(\d{4})", s)
    if not m:
        return None
    d, mo, y = (int(x) for x in m.groups())
    if y <= 1970:
        return None
    hh = mm = ss = 0
    t = re.search(r"(\d{1,2}):(\d{2})(?::(\d{2}))?\s*(AM|PM)?", s, re.I)
    if t:
        hh, mm = int(t.group(1)), int(t.group(2))
        ss = int(t.group(3) or 0)
        ampm = (t.group(4) or "").upper()
        if ampm == "PM" and hh < 12:
            hh += 12
        if ampm == "AM" and hh == 12:
            hh = 0
    try:
        return datetime.datetime(y, mo, d, hh, mm, ss).isoformat()
    except ValueError:
        return None


def _parse_pick(pick):
    out = {"raw": pick, "tipo": "otro", "equipo": None, "linea": None, "periodo": "full"}
    if not pick:
        return out
    p = pick.strip()
    period = "full"
    pm = re.search(r"-\s*(1st|2nd|first half|second half|with ot|regular time|.*set.*game.*|.*set.*)$", p, re.I)
    if pm:
        period = pm.group(1).strip().lower()
    out["periodo"] = period
    pl = p.lower()

    m = re.search(r"asian handicap\s*([12])\s*\(\s*([+-]?\d+(?:[.,]\d+)?)\s*\)", pl)
    if m:
        out.update(tipo="ah", equipo=m.group(1), linea=float(m.group(2).replace(",", ".")))
        return out
    m = re.search(r"total\s*(over|under)\s*\(\s*(\d+(?:[.,]\d+)?)\s*\)", pl)
    if m:
        out.update(tipo="total", equipo=("over" if m.group(1) == "over" else "under"),
                   linea=float(m.group(2).replace(",", ".")))
        return out
    m = re.search(r"team\s*([12])\s*win", pl)
    if m:
        out.update(tipo="winner", equipo=m.group(1))
        return out
    base = re.sub(r"\s*-\s*.*$", "", p).strip()
    if base in ("1", "2", "X", "x"):
        out.update(tipo="1x2", equipo=(base.lower() if base.lower() == "x" else base))
        return out
    return out


def parse(text, msg_id, date):
    if not text:
        return None
    pick = _field(text, "Pick")
    cuota = _field(text, "Cuota")
    if not pick or not cuota:
        return None

    odds = _parse_odds(cuota)
    sport = _field(text, "Deporte")
    competition = _field(text, "Competici.n", "Competicion")
    match = _field(text, "Partido")
    fecha = _field(text, "Fecha")
    enlace = _field(text, "Enlace del partido", "Enlace")
    ev = _field(text, "EV")

    home = away = None
    event = match
    if match:
        parts = re.split(r"\s+-\s+", match, maxsplit=1)
        if len(parts) == 2:
            home, away = parts[0].strip(), parts[1].strip()
            event = f"{home} - {away}"

    betway_event_id = None
    if enlace:
        m = re.search(r"/event/(\d+)", enlace)
        if m:
            betway_event_id = m.group(1)

    if isinstance(date, datetime.datetime):
        if date.tzinfo is None:
            date = date.replace(tzinfo=datetime.timezone.utc)
        registered_at = date.astimezone(datetime.timezone.utc).isoformat()
    else:
        registered_at = datetime.datetime.now(datetime.timezone.utc).isoformat()

    return {
        "id": f"b{msg_id}", "msg_id": msg_id, "registered_at": registered_at,
        "event_time": _parse_event_time(fecha), "raw": text.strip(),
        "bookmaker": _field(text, "Casa de Apuestas"),
        "sport": (sport or "").lower() or None, "competition": competition,
        "event": event, "home": home, "away": away,
        "pick": pick, "pick_struct": _parse_pick(pick), "ev": ev,
        "odds": odds, "stake": STAKE_EUR,
        "betway_event_id": betway_event_id, "betway_url": enlace,
        "status": "pendiente", "resolved_at": None, "result_source": None,
        "event_result": None, "needs_review": False, "auto_attempts": 0, "last_error": None,
    }


# ===========================================================================
# RESOLVER (SofaScore, conservador)
# ===========================================================================
SOFA = "https://api.sofascore.com/api/v1"
HEADERS = {"User-Agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
           "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"),
           "Accept": "*/*"}


def _norm(s):
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


def sofascore_find_event(home, away):
    nh, na = _norm(home), _norm(away)
    if not (nh and na):
        return None
    for q in (f"{home} {away}", home, away):
        try:
            results = requests.get(f"{SOFA}/search/all", params={"q": q},
                                   headers=HEADERS, timeout=15).json().get("results", [])
        except Exception:
            continue
        for res in results:
            if res.get("type") != "event":
                continue
            ev = res.get("entity", {})
            h = _norm((ev.get("homeTeam") or {}).get("name"))
            a = _norm((ev.get("awayTeam") or {}).get("name"))
            if h and a and (nh in h or h in nh) and (na in a or a in na):
                return ev.get("id")
    return None


def sofascore_event(event_id):
    return requests.get(f"{SOFA}/event/{event_id}", headers=HEADERS, timeout=15).json().get("event", {})


# Deportes donde "current" = goles/puntos (totales y handicaps sobre home+away).
# Nombres normalizados con _norm (sin acentos ni espacios): futbol -> "ftbol".
GOAL_SPORTS = {"ftbol", "futbol", "baloncesto", "basketball", "hockey",
               "balonmano", "handball", "waterpolo", "rugby",
               "beisbol", "baseball", "futbolamericano", "americanfootball",
               "voleibol", "volleyball"}
# Deportes por sets donde "current" = sets ganados; los totales son de JUEGOS.
SET_SPORTS = {"tenis", "tennis", "padel"}


def _period_scores(ev, period):
    hs = ev.get("homeScore") or {}
    as_ = ev.get("awayScore") or {}
    if period in ("1st", "first half", "1", "1 set", "1set"):
        return hs.get("period1"), as_.get("period1")
    return hs.get("current"), as_.get("current")


def _games_total(ev, period):
    """Tenis: suma de juegos (todos los sets, o solo el set 1)."""
    only_first = period in ("1st", "1 set", "1set", "first half", "1")
    total = 0
    found = False
    for side in ("homeScore", "awayScore"):
        s = ev.get(side) or {}
        keys = ["period1"] if only_first else ["period" + str(i) for i in range(1, 8)]
        for k in keys:
            v = s.get(k)
            if isinstance(v, (int, float)):
                total += v
                found = True
    return total if found else None


def evaluate(bet, ev):
    ps = bet.get("pick_struct") or {}
    tipo = ps.get("tipo")
    period = (ps.get("periodo") or "full").lower()
    nsport = _norm(bet.get("sport"))   # sin acentos: "futbol"->"ftbol", etc.

    # Mercados de un juego/set concreto o 2a parte: no resolubles -> manual.
    if any(x in period for x in ["game", "set,", "2nd", "second"]):
        return None

    hs, as_ = _period_scores(ev, period)

    # 1X2 / ganador (cualquier deporte; en tenis "current" = sets ganados)
    if tipo == "1x2":
        if hs is None or as_ is None:
            return None
        eq = ps.get("equipo")
        if eq == "1":
            return "ganada" if hs > as_ else "perdida"
        if eq == "2":
            return "ganada" if as_ > hs else "perdida"
        if eq in ("x", "X"):
            return "ganada" if hs == as_ else "perdida"
        return None

    # Totales Over/Under
    if tipo == "total":
        line = ps.get("linea")
        if line is None:
            return None
        if nsport in SET_SPORTS:
            total = _games_total(ev, period)          # tenis: total de juegos
        elif nsport in GOAL_SPORTS or nsport == "":
            total = (hs + as_) if (hs is not None and as_ is not None) else None
        else:
            return None
        if total is None:
            return None
        if total == line:
            return "anulada"
        if ps.get("equipo") == "over":
            return "ganada" if total > line else "perdida"
        return "ganada" if total < line else "perdida"

    # Handicap asiatico: deportes de goles/puntos, lineas .5, partido completo.
    if tipo == "ah" and period in ("full", "with ot", "regular time"):
        if nsport not in GOAL_SPORTS and nsport != "":
            return None
        if hs is None or as_ is None:
            return None
        line = ps.get("linea")
        eq = ps.get("equipo")
        if line is None or eq not in ("1", "2"):
            return None
        if (line * 2) % 2 == 0:   # linea entera -> posible push -> manual
            return None
        diff = (hs - as_) if eq == "1" else (as_ - hs)
        return "ganada" if diff + line > 0 else "perdida"

    return None


def resolve_due(data):
    now = datetime.datetime.now(datetime.timezone.utc)
    changed = 0
    for bet in data.get("bets", []):
        if bet.get("status") != "pendiente":
            continue
        anchor = datetime.datetime.fromisoformat(bet.get("event_time") or bet["registered_at"])
        if anchor.tzinfo is None:
            anchor = anchor.replace(tzinfo=datetime.timezone.utc)
        age_h = (now - anchor).total_seconds() / 3600.0
        if age_h < RESOLVE_AFTER_HOURS:
            continue

        bet["auto_attempts"] = bet.get("auto_attempts", 0) + 1
        try:
            eid = sofascore_find_event(bet.get("home"), bet.get("away"))
            if eid:
                ev = sofascore_event(eid)
                status = (ev.get("status") or {}).get("type")
                ch = (ev.get("homeScore") or {}).get("current")
                ca = (ev.get("awayScore") or {}).get("current")
                if ch is not None and ca is not None:
                    bet["event_result"] = f"{ch}-{ca}"
                if status == "finished":
                    bet["result_source"] = "sofascore"
                    verdict = evaluate(bet, ev)
                    if verdict:
                        bet["status"] = verdict
                        bet["resolved_at"] = now.isoformat()
                    else:
                        bet["status"] = "sin_resolver"
                        bet["needs_review"] = True
                    changed += 1
                    continue
        except Exception as e:
            bet["last_error"] = str(e)[:200]

        if age_h > MAX_RESOLVE_WINDOW_HOURS:
            bet["status"] = "sin_resolver"
            bet["needs_review"] = True
            changed += 1
    return changed


# ===========================================================================
# LECTURA DE TELEGRAM
# ===========================================================================
async def fetch_new_messages(last_id):
    from telethon import TelegramClient
    from telethon.sessions import StringSession
    client = TelegramClient(StringSession(TG_SESSION), TG_API_ID, TG_API_HASH)
    await client.connect()
    if not await client.is_user_authorized():
        await client.disconnect()
        raise RuntimeError("Sesión de Telegram no autorizada (revisa TG_SESSION).")
    channel = TG_CHANNEL
    try:
        channel = int(channel)
    except (TypeError, ValueError):
        pass
    entity = await client.get_entity(channel)
    messages = []
    if last_id == 0:
        # Primera vez: solo los ultimos INITIAL_LOAD_MINUTES minutos.
        cutoff = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(minutes=INITIAL_LOAD_MINUTES)
        async for msg in client.iter_messages(entity):  # del mas nuevo al mas viejo
            if msg.date < cutoff:
                break
            if msg.message:
                messages.append(msg)
        messages.reverse()  # orden cronologico
    else:
        async for msg in client.iter_messages(entity, min_id=last_id, reverse=True):
            if msg.message:
                messages.append(msg)
    await client.disconnect()
    return messages


# ===========================================================================
# MAIN
# ===========================================================================
def main():
    data = load()
    seen = {b.get("id") for b in data["bets"]}
    last_id = data["state"].get("last_message_id", 0)

    if TG_SESSION and TG_API_ID and TG_CHANNEL:
        try:
            messages = asyncio.run(fetch_new_messages(last_id))
            added = 0
            for msg in messages:
                last_id = max(last_id, msg.id)
                bet = parse(msg.message, msg.id, msg.date)
                if bet and bet["id"] not in seen:
                    data["bets"].append(bet)
                    seen.add(bet["id"])
                    added += 1
            data["state"]["last_message_id"] = last_id
            print(f"Telegram: {len(messages)} mensajes nuevos, {added} apuestas registradas.")
        except Exception as e:
            print(f"[AVISO] No se pudo leer Telegram: {e}")
    else:
        print("[AVISO] Faltan credenciales de Telegram.")

    print(f"Resolver: {resolve_due(data)} apuestas actualizadas.")
    save(data)
    print(f"Guardado bets.json con {len(data['bets'])} apuestas.")


if __name__ == "__main__":
    main()
