from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlparse
import base64
import html
import json
import os
import time
import uuid
import urllib.error
import urllib.request

ROOT = Path(__file__).parent
DB_PATH = Path(os.environ.get("RUNTOU_DB_PATH") or ("/tmp/runtou-store.json" if os.environ.get("VERCEL") else ROOT / "runtou-store.json"))
ENV = {}
TOKEN_SET = None
ACTIVITIES_CACHE = {}


def load_env():
    env_path = ROOT / ".env"
    if not env_path.exists():
        return
    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        ENV[key.strip()] = value.strip().strip("\"'")


load_env()

PORT = int(ENV.get("PORT") or os.environ.get("PORT") or "3000")
STRAVA_CLIENT_ID = ENV.get("STRAVA_CLIENT_ID") or "249518"
STRAVA_CLIENT_SECRET = ENV.get("STRAVA_CLIENT_SECRET")
STRAVA_REDIRECT_URI = ENV.get("STRAVA_REDIRECT_URI") or f"http://localhost:{PORT}/api/strava/callback"


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        parsed = urlparse(self.path)
        self.session_id = get_cookie(self, "runtou_session")
        try:
            if parsed.path == "/":
                return self.send_html(render_page(connected=bool(get_request_token(self)), language=detect_language(self.headers.get("Accept-Language"))))
            if parsed.path == "/api/strava/login":
                self.session_id = self.session_id or create_session_id()
                return self.redirect(build_authorize_url(self.session_id, request_redirect_uri(self)), session_id=self.session_id)
            if parsed.path == "/api/strava/callback":
                return self.handle_callback(parsed)
            if parsed.path == "/api/strava/activities":
                return self.handle_activities(parsed)
            if parsed.path == "/assets/icones.png":
                return self.send_file(ROOT / "assets" / "icones.png", "image/png")
            if parsed.path == "/assets/recarregar.png":
                return self.send_file(ROOT / "assets" / "recarregar.png", "image/png")
            return self.send_json(404, {"error": "Not found"})
        except Exception as error:
            return self.send_json(500, {"error": str(error)})

    def handle_callback(self, parsed):
        global TOKEN_SET
        query = parse_qs(parsed.query)
        error = first(query.get("error"))
        code = first(query.get("code"))
        session_id = first(query.get("state")) or self.session_id or create_session_id()

        if error:
            return self.send_html(render_page(f"Strava retornou erro: {error}"))
        if not code:
            return self.send_html(render_page("Callback sem codigo OAuth."))
        if not STRAVA_CLIENT_SECRET or "coloque_" in STRAVA_CLIENT_SECRET:
            return self.send_html(render_page("Configure STRAVA_CLIENT_SECRET no arquivo .env antes de conectar."))

        global ACTIVITIES_CACHE
        ACTIVITIES_CACHE = {}
        TOKEN_SET = post_json(
            "https://www.strava.com/oauth/token",
            {
                "client_id": STRAVA_CLIENT_ID,
                "client_secret": STRAVA_CLIENT_SECRET,
                "code": code,
                "grant_type": "authorization_code",
            },
        )
        save_session_token(session_id, TOKEN_SET)
        clear_activity_cache(session_id)
        return self.redirect("/", session_id=session_id, token=TOKEN_SET)

    def handle_activities(self, parsed):
        global TOKEN_SET
        session_id = self.session_id
        TOKEN_SET = get_request_token(self)
        if not TOKEN_SET or not TOKEN_SET.get("access_token"):
            return self.send_json(401, {"error": "Conecte sua conta Strava primeiro."})

        query = parse_qs(parsed.query)
        year = first(query.get("year"))
        month = first(query.get("month"))
        force_refresh = first(query.get("refresh")) in {"1", "true", "yes"}

        token_refreshed = refresh_token_if_needed(session_id)
        data = fetch_activities_for_period(session_id, year, month, force_refresh=force_refresh)
        return self.send_json(200, [format_activity(activity) for activity in data], token=TOKEN_SET if token_refreshed else None)

    def send_html(self, body):
        encoded = body.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def send_json(self, status, data, token=None):
        encoded = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        if token:
            self.send_header("Set-Cookie", token_cookie(token))
        self.end_headers()
        self.wfile.write(encoded)

    def send_file(self, file_path, content_type):
        if not file_path.exists():
            return self.send_json(404, {"error": "Not found"})
        data = file_path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def redirect(self, location, session_id=None, token=None):
        self.send_response(302)
        self.send_header("Location", location)
        if session_id:
            self.send_header("Set-Cookie", session_cookie(session_id))
        if token:
            self.send_header("Set-Cookie", token_cookie(token))
        self.end_headers()

    def log_message(self, format, *args):
        return




def detect_language(value):
    supported = {"pt", "en", "es", "fr", "de"}
    if not value:
        return "pt"
    for item in value.split(","):
        code = item.strip().split(";")[0].split("-")[0].lower()
        if code in supported:
            return code
    return "pt"

def load_store():
    if not DB_PATH.exists():
        return {"sessions": {}, "activity_cache": {}}

    try:
        data = json.loads(DB_PATH.read_text(encoding="utf-8"))
        data.setdefault("sessions", {})
        data.setdefault("activity_cache", {})
        return data
    except json.JSONDecodeError:
        return {"sessions": {}, "activity_cache": {}}


def save_store(data):
    tmp_path = DB_PATH.with_suffix(".tmp")
    tmp_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp_path.replace(DB_PATH)


def create_session_id():
    return uuid.uuid4().hex


def get_cookie(handler, name):
    raw_cookie = handler.headers.get("Cookie", "")
    for part in raw_cookie.split(";"):
        if "=" not in part:
            continue
        key, value = part.strip().split("=", 1)
        if key == name:
            return value
    return None


def session_cookie(session_id):
    max_age = 60 * 60 * 24 * 180
    return f"runtou_session={session_id}; Max-Age={max_age}; Path=/; HttpOnly; SameSite=Lax"


def token_cookie(token):
    max_age = 60 * 60 * 24 * 180
    payload = {
        "access_token": token.get("access_token"),
        "refresh_token": token.get("refresh_token"),
        "expires_at": token.get("expires_at"),
        "athlete": {"id": (token.get("athlete") or {}).get("id")},
    }
    raw = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    value = base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")
    return f"runtou_token={value}; Max-Age={max_age}; Path=/; HttpOnly; SameSite=Lax"


def decode_token_cookie(value):
    if not value:
        return None
    try:
        padded = value + "=" * (-len(value) % 4)
        return json.loads(base64.urlsafe_b64decode(padded.encode("ascii")).decode("utf-8"))
    except (ValueError, json.JSONDecodeError):
        return None


def get_request_token(handler):
    session_token = get_session_token(getattr(handler, "session_id", None))
    if session_token:
        return session_token
    return decode_token_cookie(get_cookie(handler, "runtou_token"))


def get_session_token(session_id):
    if not session_id:
        return None
    store = load_store()
    session = store.get("sessions", {}).get(session_id)
    return session.get("token") if session else None


def save_session_token(session_id, token):
    store = load_store()
    sessions = store.setdefault("sessions", {})
    athlete = token.get("athlete") or {}
    sessions[session_id] = {
        "token": token,
        "athlete": {
            "id": athlete.get("id"),
            "username": athlete.get("username"),
            "firstname": athlete.get("firstname"),
            "lastname": athlete.get("lastname"),
        },
        "updated_at": int(time.time()),
    }
    save_store(store)


def get_cached_activities(session_id, cache_key):
    if not session_id:
        return None
    store = load_store()
    session_cache = store.get("activity_cache", {}).get(session_id, {})
    cached = session_cache.get(cache_key)
    if not cached:
        return None
    return cached.get("activities")


def save_cached_activities(session_id, cache_key, activities):
    store = load_store()
    activity_cache = store.setdefault("activity_cache", {})
    session_cache = activity_cache.setdefault(session_id, {})
    session_cache[cache_key] = {
        "activities": activities,
        "updated_at": int(time.time()),
    }
    save_store(store)


def clear_activity_cache(session_id):
    store = load_store()
    store.setdefault("activity_cache", {}).pop(session_id, None)
    save_store(store)

def request_redirect_uri(handler):
    configured = STRAVA_REDIRECT_URI
    if configured and "localhost" not in configured:
        return configured

    host = handler.headers.get("X-Forwarded-Host") or handler.headers.get("Host")
    if host:
        proto = handler.headers.get("X-Forwarded-Proto") or ("http" if host.startswith("localhost") else "https")
        return f"{proto}://{host}/api/strava/callback"

    return configured or f"http://localhost:{PORT}/api/strava/callback"


def build_authorize_url(session_id, redirect_uri=None):
    params = urlencode(
        {
            "client_id": STRAVA_CLIENT_ID,
            "response_type": "code",
            "redirect_uri": redirect_uri or STRAVA_REDIRECT_URI,
            "approval_prompt": "auto",
            "scope": "read,activity:read_all",
            "state": session_id,
        }
    )
    return f"https://www.strava.com/oauth/authorize?{params}"


def refresh_token_if_needed(session_id):
    global TOKEN_SET
    expires_at = int(TOKEN_SET.get("expires_at") or 0)
    if time.time() < expires_at - 60:
        return False
    TOKEN_SET.update(
        post_json(
            "https://www.strava.com/oauth/token",
            {
                "client_id": STRAVA_CLIENT_ID,
                "client_secret": STRAVA_CLIENT_SECRET,
                "grant_type": "refresh_token",
                "refresh_token": TOKEN_SET["refresh_token"],
            },
        )
    )
    save_session_token(session_id, TOKEN_SET)
    return True


def fetch_activities_for_period(session_id, year=None, month=None, force_refresh=False):
    cache_key = f"{year or 'recent'}:{month or 'all'}"
    cached = None if force_refresh else get_cached_activities(session_id, cache_key)
    if cached is not None:
        return cached

    after, before = period_bounds(year, month)
    params = ["per_page=200", "page={page}"]
    if after:
        params.append(f"after={after}")
    if before:
        params.append(f"before={before}")

    activities = []
    page = 1
    while True:
        query = "&".join(params).format(page=page)
        batch = get_json(
            f"https://www.strava.com/api/v3/athlete/activities?{query}",
            {"Authorization": f"Bearer {TOKEN_SET['access_token']}"},
        )
        if not batch:
            break

        activities.extend(batch)
        if len(batch) < 200:
            break

        page += 1

    save_cached_activities(session_id, cache_key, activities)
    return activities


def period_bounds(year=None, month=None):
    if not year:
        return None, None

    year = int(year)
    if month:
        month = int(month)
        start = time.mktime((year, month, 1, 0, 0, 0, 0, 0, -1))
        if month == 12:
            end = time.mktime((year + 1, 1, 1, 0, 0, 0, 0, 0, -1))
        else:
            end = time.mktime((year, month + 1, 1, 0, 0, 0, 0, 0, -1))
        return int(start), int(end)

    start = time.mktime((year, 1, 1, 0, 0, 0, 0, 0, -1))
    end = time.mktime((year + 1, 1, 1, 0, 0, 0, 0, 0, -1))
    return int(start), int(end)


def post_json(url, payload):
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    return read_json(request)


def get_json(url, headers):
    request = urllib.request.Request(url, headers=headers, method="GET")
    return read_json(request)


def read_json(request):
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            body = response.read().decode("utf-8")
            return parse_json_response(body)
    except urllib.error.HTTPError as error:
        body = error.read().decode("utf-8", errors="replace")
        raise RuntimeError(normalize_strava_error(body))
    except urllib.error.URLError as error:
        raise RuntimeError(f"Não foi possível conectar ao Strava: {error.reason}")


def parse_json_response(body):
    try:
        return json.loads(body)
    except json.JSONDecodeError:
        raise RuntimeError(normalize_strava_error(body))


def normalize_strava_error(body):
    if "Strava is temporarily unavailable" in body or "<!DOCTYPE html" in body:
        return t("stravaUnavailable")

    try:
        data = json.loads(body)
        if isinstance(data, dict):
            return data.get("message") or data.get("error") or json.dumps(data, ensure_ascii=False)
    except json.JSONDecodeError:
        pass

    cleaned = body.strip().replace("\n", " ")
    return cleaned[:300] or "Erro inesperado ao consultar o Strava."


def format_activity(activity):
    average_speed = activity.get("average_speed")
    return {
        "id": activity.get("id"),
        "name": activity.get("name"),
        "type": activity.get("type"),
        "sport_type": activity.get("sport_type"),
        "start_date_local": activity.get("start_date_local"),
        "distance_km": round((activity.get("distance") or 0) / 1000, 2),
        "moving_time_min": round((activity.get("moving_time") or 0) / 60, 1),
        "average_speed_kmh": round(average_speed * 3.6, 2) if average_speed else None,
        "total_elevation_gain": activity.get("total_elevation_gain") or 0,
        "calories": activity.get("calories"),
    }


def render_page(message="", connected=False, language="pt"):
    escaped_message = html.escape(message)
    connect_class = "hidden" if connected else ""
    refresh_class = "" if connected else "hidden"
    subtitle = ""
    return f'''<!doctype html>
<html lang="{language}">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>RunToU Strava</title>
  <style>
    :root {{ font-family: Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; color: #151515; background: #f5f7f8; }}
    body {{ margin: 0; }}
    main {{ max-width: 1040px; margin: 0 auto; padding: 32px 20px 48px; }}
    header {{ display: flex; align-items: center; justify-content: space-between; gap: 16px; margin-bottom: 28px; }}
    .header-actions {{ display: flex; align-items: center; gap: 10px; margin-left: auto; }}
    .refresh-button {{ width: 44px; min-width: 44px; height: 44px; min-height: 44px; padding: 0; border-color: #d4d9dd; background: white; color: #fc4c02; }}
    .refresh-button img {{ width: 27px; height: 27px; object-fit: contain; display: block; }}
    .refresh-button:hover {{ border-color: #fc4c02; box-shadow: 0 2px 8px rgba(252,76,2,.14); }}
    .refresh-button.loading img {{ animation: spin .9s linear infinite; }}
    .refresh-status {{ color: #9a3412; font-size: 13px; font-weight: 700; min-width: 0; white-space: nowrap; }}
    @keyframes spin {{ to {{ transform: rotate(360deg); }} }}
    h1 {{ margin: 0; font-size: 30px; line-height: 1.2; }}
    p {{ margin: 8px 0 0; color: #4b5563; }}
    button, a.button {{ display: inline-flex; align-items: center; justify-content: center; min-height: 40px; padding: 0 14px; border: 1px solid #fc4c02; border-radius: 6px; background: #fc4c02; color: white; font-weight: 700; text-decoration: none; cursor: pointer; }}
    button.secondary {{ background: white; color: #151515; border-color: #d4d9dd; }}
    .hidden {{ display: none !important; }}
    .control-group {{ margin: 22px 0; }}
    .control-label {{ color: #4b5563; font-size: 12px; font-weight: 700; margin-bottom: 8px; text-transform: uppercase; }}
    .filters {{ display: flex; gap: 10px; flex-wrap: wrap; }}
    .filter, .period {{ background: white; color: #151515; border-color: #d4d9dd; }}
    .filter.active, .period.active {{ background: #151515; border-color: #151515; color: white; }}
    .period {{ gap: 8px; }}
    .period .count {{ border-radius: 999px; background: #edf0f2; color: #4b5563; font-size: 12px; font-weight: 700; line-height: 1; padding: 4px 7px; }}
    .period.active .count {{ background: rgba(255,255,255,.18); color: white; }}
    .drilldown {{ display: grid; gap: 14px; margin: 22px 0; }}
    .drill-title {{ display: flex; align-items: baseline; justify-content: space-between; gap: 12px; }}
    .drill-title strong {{ font-size: 15px; }}
    .drill-title span {{ color: #4b5563; font-size: 13px; }}
    .summary, .run-metrics {{ display: grid; grid-template-columns: repeat(6, minmax(110px, 1fr)); gap: 10px; margin: 0 0 18px; }}
    .level-card {{ display: grid; grid-template-columns: 142px 1fr; align-items: center; gap: 20px; background: #ffffff; border: 1px solid #dfe3e6; border-left: 6px solid #fc4c02; border-radius: 8px; padding: 18px 20px; margin: 0 0 24px; box-shadow: 0 10px 24px rgba(21,21,21,.06); }}
    .level-icon {{ width: 124px; aspect-ratio: 1; border-radius: 50%; overflow: hidden; position: relative; background: #fff7df; }}
    .level-icon img {{ position: absolute; width: 300%; height: 200%; object-fit: cover; max-width: none; }}
    .level-card strong {{ display: block; font-size: 28px; line-height: 1.1; margin: 4px 0 8px; }}
    .level-card span {{ color: #4b5563; font-size: 14px; }}
    .summary div, .run-metrics div {{ background: white; border: 1px solid #dfe3e6; border-radius: 8px; padding: 12px; }}
    .summary strong, .run-metrics strong {{ display: block; font-size: 18px; }}
    .summary span, .run-metrics span {{ color: #4b5563; font-size: 12px; text-transform: uppercase; }}
    .run-metrics {{ grid-template-columns: repeat(4, minmax(140px, 1fr)); }}
    .notice {{ min-height: 20px; color: #9a3412; margin-bottom: 16px; }}
    .charts {{ display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 14px; margin: 0 0 18px; }}
    .chart-panel {{ background: white; border: 1px solid #dfe3e6; border-radius: 8px; padding: 14px; }}
    .chart-panel.wide {{ grid-column: 1 / -1; }}
    .chart-head {{ display: flex; align-items: center; justify-content: space-between; gap: 12px; margin-bottom: 8px; }}
    .share-chart {{ width: 36px; min-width: 36px; height: 36px; min-height: 36px; padding: 0; border-color: #d4d9dd; background: white; color: #151515; }}
    .share-chart svg {{ display: block; width: 20px; height: 20px; stroke: currentColor; }}
    .share-chart:hover {{ border-color: #151515; }}
    .chart-panel h2, .month-header h2 {{ margin: 0; font-size: 18px; }}
    canvas {{ display: block; width: 100%; height: 260px; }}
    .month-section {{ margin: 0 0 22px; }}
    .month-header {{ display: flex; align-items: baseline; justify-content: space-between; gap: 12px; margin: 0 0 8px; color: #4b5563; }}
    table {{ width: 100%; border-collapse: collapse; background: white; border: 1px solid #dfe3e6; border-radius: 8px; overflow: hidden; }}
    th, td {{ padding: 12px 10px; border-bottom: 1px solid #edf0f2; text-align: left; font-size: 14px; }}
    .activity-link {{ color: #151515; font-weight: 700; text-decoration: none; }}
    .activity-link:hover {{ color: #fc4c02; text-decoration: underline; }}
    th {{ background: #eef2f3; color: #374151; font-size: 12px; text-transform: uppercase; }}
    tr:last-child td {{ border-bottom: 0; }}
    .empty {{ background: white; border: 1px solid #dfe3e6; border-radius: 8px; padding: 20px; color: #4b5563; }}
    @media (max-width: 900px) {{
      .charts {{ grid-template-columns: 1fr; }}
      .chart-panel, .chart-panel.wide {{ grid-column: 1 / -1; }}
    }}
    @media (max-width: 760px) {{
      main {{ padding: 24px 16px 40px; }}
      header {{ align-items: flex-start; flex-direction: column; }}
      .header-actions {{ margin-left: 0; width: 100%; justify-content: space-between; }}
      .refresh-status {{ white-space: normal; text-align: right; }}
      .summary, .run-metrics {{ grid-template-columns: repeat(2, minmax(0, 1fr)); }}
      .level-card {{ grid-template-columns: 92px 1fr; padding: 14px; }}
      .level-icon {{ width: 78px; }}
      .level-card strong {{ font-size: 22px; }}
      table {{ display: block; overflow-x: auto; white-space: nowrap; }}
    }}
    @media (max-width: 520px) {{
      .charts {{ grid-template-columns: 1fr; }}
      .chart-panel {{ padding: 12px; }}
      .chart-panel h2 {{ font-size: 18px; }}
      canvas {{ min-height: 180px; }}
    }}
  </style>
</head>
<body>
  <main>
    <header>
      <div><h1>RunToU Strava</h1><p id="subtitle" data-connected="{str(connected).lower()}">{subtitle}</p></div>
      <div class="header-actions">
        <a class="button {connect_class}" id="connectButton" href="/api/strava/login">Conectar com Strava</a>
        <span class="refresh-status" id="refreshStatus" aria-live="polite"></span>
        <button class="refresh-button {refresh_class}" id="refreshButton" type="button" aria-label="Atualizar informações" title="Atualizar informações"><img src="/assets/recarregar.png" alt="" /></button>
      </div>
    </header>
    <section class="level-card hidden" id="activityLevel"></section>
    <div class="control-group">
      <div class="control-label" data-i18n="activitiesLabel">Atividades</div>
      <div class="filters" aria-label="Filtros de atividade">
        <button class="filter active" data-filter="all" data-i18n="all">Todas</button>
      </div>
    </div>
    <section class="drilldown" aria-label="Navegacao por periodo">
      <div class="control-group" id="yearGroup">
        <div class="drill-title"><strong data-i18n="years">Anos</strong></div>
        <div class="filters" id="yearControls"></div>
      </div>
      <div class="control-group hidden" id="monthGroup">
        <div class="drill-title"><strong data-i18n="months">Meses</strong><span data-i18n="chooseMonth">Escolha um mês para ver as semanas</span></div>
        <div class="filters" id="monthControls"></div>
      </div>
      <div class="control-group hidden" id="weekGroup">
        <div class="drill-title"><strong data-i18n="weeks">Semanas</strong><span data-i18n="chooseWeek">Escolha uma semana para detalhar</span></div>
        <div class="filters" id="weekControls"></div>
      </div>
    </section>
    <div class="summary" id="summary"></div>
    <div class="run-metrics" id="runMetrics"></div>
    <div class="notice" id="notice">{escaped_message}</div>
    <section id="content" class="empty" data-i18n="connectToLoad">Conecte sua conta Strava para carregar as atividades.</section>
  </main>
  <script>
    const initialLanguage = "{language}";
    const translations = {{
      pt: {{
        connectedSubtitle: "Acompanhe suas atividades e evolução no Strava.", disconnectedSubtitle: "Conecte sua conta para ver suas atividades recentes.",
        activitiesLabel: "Atividades", all: "Todas", runs: "Corridas", walks: "Caminhadas", pilates: "Pilates",
        years: "Anos", months: "Meses", weeks: "Semanas", chooseYear: "Escolha um ano para ver os meses", chooseMonth: "Escolha um mês para ver as semanas", chooseWeek: "Escolha uma semana para detalhar", connectToLoad: "Conecte sua conta Strava para carregar as atividades.",
        loadingMonth: "Buscando atividades do mês...", loadingYear: "Buscando atividades do ano...", retry: "Tentar novamente", downloaded: "O navegador baixou o gráfico PNG com descrição. Publique o arquivo no Instagram.", stravaUnavailable: "O Strava está temporariamente indisponível. Tente novamente em alguns minutos.", refreshData: "Atualizar informações", refreshing: "Atualizando as informações...", refreshed: "Informações atualizadas.",
        activities: "atividades", activity: "atividade", distance: "distância", time: "tempo", averagePace: "pace médio", weekKm: "km da semana", bestPace: "melhor ritmo", evolution: "evolução", activityLevel: "Nível de atividade", avgWeekly: "média semanal", sedentary: "Sedentário", lightlyActive: "Pouco ativo", activeLevel: "Ativo", veryActive: "Muito ativo", athlete: "Atleta", elite: "Elite", noFilter: "Nenhuma atividade neste filtro.", noneFound: "Nenhuma atividade encontrada.",
        date: "Data", name: "Nome", type: "Tipo", types: "tipos", calories: "calorias estimadas", avgSpeed: "Vel. média", elevation: "Elevação", weekOf: "Semana de", to: "a", byWeek: "por semana", byMonth: "por mês", distanceTitle: "Distância", timeTitle: "Tempo", averageRunPaceTitle: "Pace médio de corrida", runKmTitle: "Km de corrida", typeTitle: "Atividades por tipo", shareChart: "Compartilhar gráfico", generatedBy: "Gerado pelo RunToU", runLabel: "corridas", walkLabel: "caminhadas"
      }},
      en: {{
        connectedSubtitle: "Track your Strava activities and progress.", disconnectedSubtitle: "Connect your account to view your recent activities.",
        activitiesLabel: "Activities", all: "All", runs: "Runs", walks: "Walks", pilates: "Pilates",
        years: "Years", months: "Months", weeks: "Weeks", chooseYear: "Choose a year to view months", chooseMonth: "Choose a month to view weeks", chooseWeek: "Choose a week to inspect", connectToLoad: "Connect your Strava account to load activities.",
        loadingMonth: "Loading month activities...", loadingYear: "Loading year activities...", retry: "Try again", downloaded: "The browser downloaded the chart PNG with description. Publish the file on Instagram.", stravaUnavailable: "Strava is temporarily unavailable. Try again in a few minutes.", refreshData: "Refresh information", refreshing: "Refreshing information...", refreshed: "Information refreshed.",
        activities: "activities", activity: "activity", distance: "distance", time: "time", averagePace: "average pace", weekKm: "week mi", bestPace: "best pace", evolution: "progress", activityLevel: "Activity level", avgWeekly: "weekly average", sedentary: "Sedentary", lightlyActive: "Lightly active", activeLevel: "Active", veryActive: "Very active", athlete: "Athlete", elite: "Elite", noFilter: "No activity in this filter.", noneFound: "No activity found.",
        date: "Date", name: "Name", type: "Type", types: "types", calories: "estimated calories", avgSpeed: "Avg. speed", elevation: "Elevation", weekOf: "Week of", to: "to", byWeek: "by week", byMonth: "by month", distanceTitle: "Distance", timeTitle: "Time", averageRunPaceTitle: "Average running pace", runKmTitle: "Running distance", typeTitle: "Activities by type", shareChart: "Share chart", generatedBy: "Generated by RunToU", runLabel: "runs", walkLabel: "walks"
      }},
      es: {{
        connectedSubtitle: "Sigue tus actividades y evolución en Strava.", disconnectedSubtitle: "Conecta tu cuenta para ver tus actividades recientes.",
        activitiesLabel: "Actividades", all: "Todas", runs: "Carreras", walks: "Caminatas", pilates: "Pilates",
        years: "Años", months: "Meses", weeks: "Semanas", chooseYear: "Elige un año para ver los meses", chooseMonth: "Elige un mes para ver las semanas", chooseWeek: "Elige una semana para detallar", connectToLoad: "Conecta tu cuenta de Strava para cargar actividades.",
        loadingMonth: "Buscando actividades del mes...", loadingYear: "Buscando actividades del año...", retry: "Intentar de nuevo", downloaded: "El navegador descargó el gráfico PNG con descripción. Publica el archivo en Instagram.", stravaUnavailable: "Strava no está disponible temporalmente. Inténtalo de nuevo en unos minutos.", refreshData: "Actualizar información", refreshing: "Actualizando la información...", refreshed: "Información actualizada.",
        activities: "actividades", activity: "actividad", distance: "distancia", time: "tiempo", averagePace: "ritmo medio", weekKm: "km de la semana", bestPace: "mejor ritmo", evolution: "evolución", activityLevel: "Nivel de actividad", avgWeekly: "media semanal", sedentary: "Sedentario", lightlyActive: "Poco activo", activeLevel: "Activo", veryActive: "Muy activo", athlete: "Atleta", elite: "Élite", noFilter: "No hay actividades en este filtro.", noneFound: "No se encontraron actividades.",
        date: "Fecha", name: "Nombre", type: "Tipo", types: "tipos", calories: "calorías estimadas", avgSpeed: "Vel. media", elevation: "Elevación", weekOf: "Semana de", to: "a", byWeek: "por semana", byMonth: "por mes", distanceTitle: "Distancia", timeTitle: "Tiempo", averageRunPaceTitle: "Ritmo medio de carrera", runKmTitle: "Km de carrera", typeTitle: "Actividades por tipo", shareChart: "Compartir gráfico", generatedBy: "Generado por RunToU", runLabel: "carreras", walkLabel: "caminatas"
      }},
      fr: {{
        connectedSubtitle: "Suivez vos activités et votre progression sur Strava.", disconnectedSubtitle: "Connectez votre compte pour voir vos activités récentes.",
        activitiesLabel: "Activités", all: "Toutes", runs: "Courses", walks: "Marches", pilates: "Pilates",
        years: "Années", months: "Mois", weeks: "Semaines", chooseYear: "Choisissez une année pour voir les mois", chooseMonth: "Choisissez un mois pour voir les semaines", chooseWeek: "Choisissez une semaine à détailler", connectToLoad: "Connectez votre compte Strava pour charger les activités.",
        loadingMonth: "Chargement des activités du mois...", loadingYear: "Chargement des activités de l’année...", retry: "Réessayer", downloaded: "Le navigateur a téléchargé le graphique PNG avec description. Publiez le fichier sur Instagram.", stravaUnavailable: "Strava est temporairement indisponible. Réessayez dans quelques minutes.", refreshData: "Actualiser les informations", refreshing: "Actualisation des informations...", refreshed: "Informations actualisées.",
        activities: "activités", activity: "activité", distance: "distance", time: "temps", averagePace: "allure moyenne", weekKm: "km de la semaine", bestPace: "meilleure allure", evolution: "évolution", activityLevel: "Niveau d’activité", avgWeekly: "moyenne hebdomadaire", sedentary: "Sédentaire", lightlyActive: "Peu actif", activeLevel: "Actif", veryActive: "Très actif", athlete: "Athlète", elite: "Élite", noFilter: "Aucune activité dans ce filtre.", noneFound: "Aucune activité trouvée.",
        date: "Date", name: "Nom", type: "Type", types: "types", calories: "calories estimées", avgSpeed: "Vit. moy.", elevation: "Dénivelé", weekOf: "Semaine du", to: "au", byWeek: "par semaine", byMonth: "par mois", distanceTitle: "Distance", timeTitle: "Temps", averageRunPaceTitle: "Allure moyenne de course", runKmTitle: "Km de course", typeTitle: "Activités par type", shareChart: "Partager le graphique", generatedBy: "Généré par RunToU", runLabel: "courses", walkLabel: "marches"
      }},
      de: {{
        connectedSubtitle: "Verfolge deine Strava-Aktivitäten und Entwicklung.", disconnectedSubtitle: "Verbinde dein Konto, um aktuelle Aktivitäten zu sehen.",
        activitiesLabel: "Aktivitäten", all: "Alle", runs: "Läufe", walks: "Spaziergänge", pilates: "Pilates",
        years: "Jahre", months: "Monate", weeks: "Wochen", chooseYear: "Wähle ein Jahr, um Monate zu sehen", chooseMonth: "Wähle einen Monat, um Wochen zu sehen", chooseWeek: "Wähle eine Woche für Details", connectToLoad: "Verbinde dein Strava-Konto, um Aktivitäten zu laden.",
        loadingMonth: "Aktivitäten des Monats werden geladen...", loadingYear: "Aktivitäten des Jahres werden geladen...", retry: "Erneut versuchen", downloaded: "Der Browser hat die PNG-Grafik mit Beschreibung heruntergeladen. Veröffentliche die Datei auf Instagram.", stravaUnavailable: "Strava ist vorübergehend nicht verfügbar. Versuche es in einigen Minuten erneut.", refreshData: "Informationen aktualisieren", refreshing: "Informationen werden aktualisiert...", refreshed: "Informationen aktualisiert.",
        activities: "Aktivitäten", activity: "Aktivität", distance: "Distanz", time: "Zeit", averagePace: "Ø Pace", weekKm: "Wochen-km", bestPace: "beste Pace", evolution: "Entwicklung", activityLevel: "Aktivitätsniveau", avgWeekly: "Wochendurchschnitt", sedentary: "Sitzend", lightlyActive: "Wenig aktiv", activeLevel: "Aktiv", veryActive: "Sehr aktiv", athlete: "Athlet", elite: "Elite", noFilter: "Keine Aktivität in diesem Filter.", noneFound: "Keine Aktivitäten gefunden.",
        date: "Datum", name: "Name", type: "Typ", types: "Typen", calories: "geschätzte Kalorien", avgSpeed: "Ø Geschw.", elevation: "Höhenmeter", weekOf: "Woche vom", to: "bis", byWeek: "pro Woche", byMonth: "pro Monat", distanceTitle: "Distanz", timeTitle: "Zeit", averageRunPaceTitle: "Durchschnittliche Laufpace", runKmTitle: "Lauf-km", typeTitle: "Aktivitäten nach Typ", shareChart: "Grafik teilen", generatedBy: "Erstellt mit RunToU", runLabel: "Läufe", walkLabel: "Spaziergänge"
      }}
    }};
    function normalizeLanguage(value) {{
      const code = String(value || "").toLowerCase().split("-")[0];
      return translations[code] ? code : "pt";
    }}
    const appLanguage = normalizeLanguage(navigator.language || initialLanguage);
    const i18n = translations[appLanguage];
    const activityTypeTranslations = {{
      pt: {{ run: "Corrida", trailrun: "Corrida em trilha", virtualrun: "Corrida virtual", walk: "Caminhada", hike: "Trilha", ride: "Pedalada", virtualride: "Pedalada virtual", mountainbikeride: "Mountain bike", gravelride: "Gravel", ebikeride: "E-bike", swim: "Natação", weighttraining: "Treino de força", workout: "Treino", pilates: "Pilates", yoga: "Yoga", rowing: "Remo", kayaking: "Caiaque", canoeing: "Canoagem", soccer: "Futebol", tennis: "Tênis", golf: "Golfe", elliptical: "Elíptico", stairstepper: "Escada", crossfit: "Crossfit", other: "Outro" }},
      en: {{ run: "Run", trailrun: "Trail run", virtualrun: "Virtual run", walk: "Walk", hike: "Hike", ride: "Ride", virtualride: "Virtual ride", mountainbikeride: "Mountain bike", gravelride: "Gravel ride", ebikeride: "E-bike ride", swim: "Swim", weighttraining: "Weight training", workout: "Workout", pilates: "Pilates", yoga: "Yoga", rowing: "Rowing", kayaking: "Kayaking", canoeing: "Canoeing", soccer: "Soccer", tennis: "Tennis", golf: "Golf", elliptical: "Elliptical", stairstepper: "Stair stepper", crossfit: "CrossFit", other: "Other" }},
      es: {{ run: "Carrera", trailrun: "Carrera de trail", virtualrun: "Carrera virtual", walk: "Caminata", hike: "Senderismo", ride: "Ciclismo", virtualride: "Ciclismo virtual", mountainbikeride: "Mountain bike", gravelride: "Gravel", ebikeride: "E-bike", swim: "Natación", weighttraining: "Entrenamiento de fuerza", workout: "Entrenamiento", pilates: "Pilates", yoga: "Yoga", rowing: "Remo", kayaking: "Kayak", canoeing: "Piragüismo", soccer: "Fútbol", tennis: "Tenis", golf: "Golf", elliptical: "Elíptica", stairstepper: "Escaladora", crossfit: "CrossFit", other: "Otro" }},
      fr: {{ run: "Course", trailrun: "Trail", virtualrun: "Course virtuelle", walk: "Marche", hike: "Randonnée", ride: "Vélo", virtualride: "Vélo virtuel", mountainbikeride: "VTT", gravelride: "Gravel", ebikeride: "Vélo électrique", swim: "Natation", weighttraining: "Musculation", workout: "Entraînement", pilates: "Pilates", yoga: "Yoga", rowing: "Aviron", kayaking: "Kayak", canoeing: "Canoë", soccer: "Football", tennis: "Tennis", golf: "Golf", elliptical: "Elliptique", stairstepper: "Escalier", crossfit: "CrossFit", other: "Autre" }},
      de: {{ run: "Lauf", trailrun: "Trailrun", virtualrun: "Virtueller Lauf", walk: "Spaziergang", hike: "Wanderung", ride: "Radfahrt", virtualride: "Virtuelle Radfahrt", mountainbikeride: "Mountainbike", gravelride: "Gravel", ebikeride: "E-Bike", swim: "Schwimmen", weighttraining: "Krafttraining", workout: "Training", pilates: "Pilates", yoga: "Yoga", rowing: "Rudern", kayaking: "Kajak", canoeing: "Kanufahren", soccer: "Fußball", tennis: "Tennis", golf: "Golf", elliptical: "Crosstrainer", stairstepper: "Stepper", crossfit: "CrossFit", other: "Andere" }}
    }};
    document.documentElement.lang = appLanguage;
    function t(key) {{ return i18n[key] || translations.pt[key] || key; }}
    function applyI18n() {{
      document.querySelectorAll("[data-i18n]").forEach((node) => {{ node.textContent = t(node.dataset.i18n); }});
      const subtitleNode = document.querySelector("#subtitle");
      if (subtitleNode) subtitleNode.textContent = subtitleNode.dataset.connected === "true" ? t("connectedSubtitle") : t("disconnectedSubtitle");
      const contentNode = document.querySelector("#content[data-i18n]");
      if (contentNode) contentNode.textContent = t(contentNode.dataset.i18n);
      const refreshNode = document.querySelector("#refreshButton");
      if (refreshNode) {{ refreshNode.title = t("refreshData"); refreshNode.setAttribute("aria-label", t("refreshData")); }}
    }}

    const usesImperial = appLanguage === "en";
    function distanceUnit() {{ return usesImperial ? "mi" : "km"; }}
    function speedUnit() {{ return usesImperial ? "mph" : "km/h"; }}
    function elevationUnit() {{ return usesImperial ? "ft" : "m"; }}
    function paceUnit() {{ return usesImperial ? "min/mi" : "min/km"; }}
    function distanceValue(km) {{ return usesImperial ? Number(km || 0) * 0.621371 : Number(km || 0); }}
    function speedValue(kmh) {{ return kmh == null ? null : usesImperial ? Number(kmh) * 0.621371 : Number(kmh); }}
    function elevationValue(meters) {{ return usesImperial ? Number(meters || 0) * 3.28084 : Number(meters || 0); }}
    function paceValue(minutesPerKm) {{ return usesImperial ? Number(minutesPerKm || 0) / 0.621371 : Number(minutesPerKm || 0); }}
    function formatDistance(km) {{ return `${{distanceValue(km).toFixed(2)}} ${{distanceUnit()}}`; }}
    function formatSpeed(kmh) {{ const value = speedValue(kmh); return value == null ? "-" : `${{value.toFixed(2)}} ${{speedUnit()}}`; }}
    function formatElevation(meters) {{ return `${{elevationValue(meters).toFixed(0)}} ${{elevationUnit()}}`; }}

    const notice = document.querySelector("#notice");
    const content = document.querySelector("#content");
    const summary = document.querySelector("#summary");
    const activityLevel = document.querySelector("#activityLevel");
    const runMetrics = document.querySelector("#runMetrics");
    const connectButton = document.querySelector("#connectButton");
    const refreshButton = document.querySelector("#refreshButton");
    const refreshStatus = document.querySelector("#refreshStatus");
    let refreshStatusTimer = null;
    const subtitle = document.querySelector("#subtitle");
    const filterContainer = document.querySelector(".filters");
    let filterButtons = [...document.querySelectorAll(".filter")];
    const yearGroup = document.querySelector("#yearGroup");
    const monthGroup = document.querySelector("#monthGroup");
    const weekGroup = document.querySelector("#weekGroup");
    const yearControls = document.querySelector("#yearControls");
    const monthControls = document.querySelector("#monthControls");
    const weekControls = document.querySelector("#weekControls");
    applyI18n();
    let yearActivities = [];
    let monthActivities = [];
    let activeFilter = "all";
    let selectedYear = String(new Date().getFullYear());
    let selectedMonth = null;
    let selectedWeek = null;
    const availableYears = Array.from({{ length: 10 }}, (_, index) => String(new Date().getFullYear() - index));

    renderPeriodControls([]);
    if (refreshButton) refreshButton.addEventListener("click", () => loadActivities(true));
    loadActivities();

    async function loadActivities(forceRefresh = false) {{
      const loadingMonth = Boolean(selectedMonth);
      notice.textContent = forceRefresh ? t("refreshing") : (loadingMonth ? t("loadingMonth") : t("loadingYear"));
      if (forceRefresh) setRefreshStatus(t("refreshing"));
      if (refreshButton) {{ refreshButton.disabled = true; refreshButton.classList.toggle("loading", forceRefresh); }}
      try {{
        const response = await fetch(buildActivitiesUrl(forceRefresh));
        const data = await response.json();
        if (!response.ok) {{
          if (connectButton) connectButton.classList.remove("hidden");
          if (refreshButton) refreshButton.classList.add("hidden");
          notice.textContent = humanError(data.error || JSON.stringify(data));
          showRetry();
          return;
        }}
        if (connectButton) connectButton.classList.add("hidden");
        if (refreshButton) refreshButton.classList.remove("hidden");
        if (subtitle) {{ subtitle.dataset.connected = "true"; subtitle.textContent = t("connectedSubtitle"); }}
        if (loadingMonth) {{
          monthActivities = data;
          notice.textContent = "";
          if (forceRefresh) setRefreshStatus(t("refreshed"), true);
          renderCurrentView();
        }} else {{
          yearActivities = data;
          monthActivities = [];
          selectedMonth = null;
          selectedWeek = null;
          notice.textContent = "";
          if (forceRefresh) setRefreshStatus(t("refreshed"), true);
          renderCurrentView();
        }}
      }} catch (error) {{
        notice.textContent = humanError(error.message);
        showRetry();
      }} finally {{
        if (refreshButton) {{ refreshButton.disabled = false; refreshButton.classList.remove("loading"); }}
      }}
    }}

    function setRefreshStatus(message, autoClear = false) {{
      if (!refreshStatus) return;
      window.clearTimeout(refreshStatusTimer);
      refreshStatus.textContent = message || "";
      if (autoClear) refreshStatusTimer = window.setTimeout(() => {{ refreshStatus.textContent = ""; }}, 2400);
    }}

    function humanError(message) {{
      const text = String(message || "");
      if (text.includes("Strava is temporarily unavailable") || text.includes("<!DOCTYPE")) {{
        return t("stravaUnavailable");
      }}
      if (text.includes("Conecte sua conta Strava")) return t("connectToLoad");
      return text;
    }}

    function showRetry() {{
      content.className = "empty";
      content.innerHTML = `<button id="retryLoad">${{t("retry")}}</button>`;
      document.querySelector("#retryLoad").addEventListener("click", loadActivities);
    }}

    attachFilterHandlers();

    function attachFilterHandlers() {{
      filterButtons = [...document.querySelectorAll(".filter")];
      filterButtons.forEach((button) => {{
        button.addEventListener("click", () => {{
          activeFilter = button.dataset.filter;
          selectedMonth = null;
          selectedWeek = null;
          filterButtons.forEach((item) => item.classList.toggle("active", item === button));
          renderCurrentView();
        }});
      }});
    }}

    function renderCurrentView() {{
      const filterSource = selectedMonth && monthActivities.length ? monthActivities : yearActivities;
      renderActivityFilters(filterSource);
      const yearBaseActivities = filterActivities(yearActivities, activeFilter);
      const monthBaseActivities = filterActivities(monthActivities, activeFilter);
      ensureSelectionStillExists(yearBaseActivities, monthBaseActivities);
      renderPeriodControls(yearBaseActivities, monthBaseActivities);

      const selectedActivities = getSelectedActivities(yearBaseActivities, monthBaseActivities);
      const groups = getVisibleGroups(yearBaseActivities, monthBaseActivities);
      renderSummary(selectedActivities);
      renderActivityLevel(selectedActivities);
      renderRunMetrics(selectedActivities, yearBaseActivities, monthBaseActivities);
      renderPeriodView(groups);
      renderGroups(groups);
    }}

    function buildActivitiesUrl(forceRefresh = false) {{
      const params = new URLSearchParams();
      if (selectedYear) params.set("year", selectedYear);
      if (selectedMonth) params.set("month", selectedMonth.slice(5, 7));
      if (forceRefresh) params.set("refresh", "1");
      return `/api/strava/activities?${{params}}`;
    }}

    function renderActivityFilters(activities) {{
      const types = uniqueActivityTypes(activities);
      if (activeFilter !== "all" && !types.some((type) => type.key === activeFilter)) activeFilter = "all";
      filterContainer.innerHTML = `<button class="filter ${{activeFilter === "all" ? "active" : ""}}" data-filter="all">${{t("all")}}</button>` +
        types.map((type) => `<button class="filter ${{type.key === activeFilter ? "active" : ""}}" data-filter="${{type.key}}">${{escapeHtml(type.label)}}</button>`).join("");
      attachFilterHandlers();
    }}

    function uniqueActivityTypes(activities) {{
      const map = new Map();
      activities.forEach((activity) => {{
        const key = activityTypeKey(activity);
        if (!map.has(key)) map.set(key, {{ key, label: activityTypeLabel(activity) }});
      }});
      return [...map.values()].sort((a, b) => a.label.localeCompare(b.label, appLanguage));
    }}

    function filterActivities(activities, filter) {{
      if (filter === "all") return activities;
      return activities.filter((activity) => activityTypeKey(activity) === filter);
    }}

    function activityTypeKey(activity) {{
      return normalizeActivityType(activity.sport_type || activity.type || "other");
    }}

    function normalizeActivityType(value) {{
      return String(value || "other").trim().replace(/([a-z])([A-Z])/g, "$1 $2").toLowerCase().replace(/[^a-z0-9]+/g, "");
    }}

    function activityTypeLabel(activityOrKey) {{
      const raw = typeof activityOrKey === "string" ? activityOrKey : String(activityOrKey.sport_type || activityOrKey.type || "Other");
      const key = normalizeActivityType(raw);
      const labels = activityTypeTranslations[appLanguage] || activityTypeTranslations.pt;
      if (labels[key]) return labels[key];
      return raw.replaceAll("_", " ").replace(/([a-z])([A-Z])/g, "$1 $2").replace(/^./, (char) => char.toUpperCase());
    }}

    function isRun(activity) {{
      return String(activity.sport_type || activity.type || "").toLowerCase().includes("run");
    }}

    function ensureSelectionStillExists(yearBaseActivities, monthBaseActivities) {{
      if (!selectedYear) selectedYear = String(new Date().getFullYear());
      if (!selectedMonth) return;
      const months = groupActivities(yearBaseActivities, "month");
      if (!months.some((group) => group.key === selectedMonth)) {{
        selectedMonth = null;
        selectedWeek = null;
        monthActivities = [];
      }}
      if (!selectedWeek) return;
      const weeks = groupActivities(monthBaseActivities, "week");
      if (!weeks.some((group) => group.key === selectedWeek)) selectedWeek = null;
    }}

    function renderPeriodControls(yearBaseActivities, monthBaseActivities) {{
      renderButtons(yearControls, availableYears.map((year) => ({{ key: year, shortLabel: year, activities: [] }})), selectedYear, (key) => {{
        selectedYear = key;
        selectedMonth = null;
        selectedWeek = null;
        loadActivities();
      }});

      const monthGroups = groupActivities(yearBaseActivities, "month");
      monthGroup.classList.toggle("hidden", !selectedYear || !yearBaseActivities.length);
      renderButtons(monthControls, monthGroups, selectedMonth, (key) => {{
        selectedMonth = selectedMonth === key ? null : key;
        selectedWeek = null;
        if (selectedMonth) loadActivities();
        else {{ monthActivities = []; renderCurrentView(); }}
      }});

      const weekGroups = selectedMonth ? groupActivities(monthBaseActivities, "week") : [];
      weekGroup.classList.toggle("hidden", !selectedMonth || !monthBaseActivities.length);
      renderButtons(weekControls, weekGroups, selectedWeek, (key) => {{
        selectedWeek = selectedWeek === key ? null : key;
        renderCurrentView();
      }});
    }}

    function renderButtons(container, groups, selectedKey, onClick) {{
      container.innerHTML = groups.map((group) => `
        <button class="period ${{group.key === selectedKey ? "active" : ""}}" data-key="${{group.key}}">
          <span>${{group.shortLabel}}</span>${{group.activities.length ? `<span class="count">${{group.activities.length}}</span>` : ""}}
        </button>
      `).join("");
      [...container.querySelectorAll("button")].forEach((button) => {{
        button.addEventListener("click", () => onClick(button.dataset.key));
      }});
    }}

    function getSelectedActivities(yearBaseActivities, monthBaseActivities) {{
      let selected = selectedMonth ? monthBaseActivities : yearBaseActivities;
      if (selectedWeek) selected = selected.filter((activity) => getPeriodInfo(new Date(activity.start_date_local), "week").key === selectedWeek);
      return selected;
    }}

    function getVisibleGroups(yearBaseActivities, monthBaseActivities) {{
      const selected = getSelectedActivities(yearBaseActivities, monthBaseActivities);
      if (selectedMonth || selectedWeek) return groupActivities(selected, "week");
      return groupActivities(selected, "month");
    }}

    function renderSummary(activities) {{
      if (!activities.length) {{ summary.innerHTML = ""; activityLevel.innerHTML = ""; activityLevel.classList.add("hidden"); runMetrics.innerHTML = ""; removeChartsArea(); return; }}
      const totalDistance = activities.reduce((sum, activity) => sum + distanceValue(activity.distance_km), 0);
      const totalTime = activities.reduce((sum, activity) => sum + Number(activity.moving_time_min || 0), 0);
      const typeCount = uniqueActivityTypes(activities).length;
      const totalCalories = activities.reduce((sum, activity) => sum + estimatedCalories(activity), 0);
      const typeCard = activeFilter === "all" ? `<div><strong>${{typeCount}}</strong><span>${{t("types")}}</span></div>` : "";
      summary.innerHTML = `<div><strong>${{activities.length}}</strong><span>${{t("activities")}}</span></div><div><strong>${{totalDistance.toFixed(2)}} ${{distanceUnit()}}</strong><span>${{t("distance")}}</span></div><div><strong>${{totalTime.toFixed(1)}} min</strong><span>${{t("time")}}</span></div><div><strong>${{Math.round(totalCalories)}} kcal</strong><span>${{t("calories")}}</span></div>${{typeCard}}`;
    }}

    function renderActivityLevel(activities) {{
      if (!activities.length) {{ activityLevel.classList.add("hidden"); return; }}
      const level = getActivityLevel(activities);
      activityLevel.classList.remove("hidden");
      activityLevel.innerHTML = `<div class="level-icon"><img src="/assets/icones.png" alt="" style="left: ${{level.left}}; top: ${{level.top}};"></div><div><span>${{t("activityLevel")}}</span><strong>${{t(level.key)}}</strong><span>${{t("avgWeekly")}}: ${{level.weeklyMinutes.toFixed(0)}} min · ${{formatDistance(level.weeklyDistanceKm)}}</span></div>`;
    }}

    function getActivityLevel(activities) {{
      const totalMinutes = activities.reduce((sum, activity) => sum + Number(activity.moving_time_min || 0), 0);
      const totalKm = activities.reduce((sum, activity) => sum + Number(activity.distance_km || 0), 0);
      const dates = activities.map((activity) => new Date(activity.start_date_local)).filter((date) => !Number.isNaN(date.getTime()));
      const minDate = dates.length ? new Date(Math.min(...dates)) : new Date();
      const maxDate = dates.length ? new Date(Math.max(...dates)) : new Date();
      const days = Math.max(7, (maxDate - minDate) / 86400000 + 1);
      const weeks = Math.max(1, days / 7);
      const weeklyMinutes = totalMinutes / weeks;
      const weeklyDistanceKm = totalKm / weeks;
      const levels = [
        {{ key: "sedentary", limit: 60, left: "0%", top: "0%" }},
        {{ key: "lightlyActive", limit: 150, left: "-100%", top: "0%" }},
        {{ key: "activeLevel", limit: 300, left: "-200%", top: "0%" }},
        {{ key: "veryActive", limit: 450, left: "0%", top: "-100%" }},
        {{ key: "athlete", limit: 600, left: "-100%", top: "-100%" }},
        {{ key: "elite", limit: Infinity, left: "-200%", top: "-100%" }},
      ];
      return {{ ...levels.find((level) => weeklyMinutes < level.limit), weeklyMinutes, weeklyDistanceKm }};
    }}

    function renderRunMetrics(activities, yearBaseActivities, monthBaseActivities) {{
      const runs = activities.filter((activity) => isRun(activity) && Number(activity.distance_km || 0) > 0);
      if (!runs.length) {{ runMetrics.innerHTML = ""; return; }}

      const totalRunDistance = runs.reduce((sum, activity) => sum + Number(activity.distance_km || 0), 0);
      const totalRunTime = runs.reduce((sum, activity) => sum + Number(activity.moving_time_min || 0), 0);
      const averagePace = totalRunDistance ? totalRunTime / totalRunDistance : null;
      const bestPace = Math.min(...runs.map((activity) => Number(activity.moving_time_min || 0) / Number(activity.distance_km || 1)).filter(Boolean));
      const weekContext = selectedMonth ? monthBaseActivities : yearBaseActivities;
      const weeklyRuns = groupActivities(weekContext.filter((activity) => isRun(activity)), "week").reverse();
      const currentWeek = selectedWeek
        ? weeklyRuns.find((group) => group.key === selectedWeek)
        : weeklyRuns[weeklyRuns.length - 1];
      const previousWeek = currentWeek ? weeklyRuns[weeklyRuns.findIndex((group) => group.key === currentWeek.key) - 1] : null;
      const currentWeekKm = currentWeek ? currentWeek.distance : 0;
      const previousWeekKm = previousWeek ? previousWeek.distance : 0;
      const evolution = formatEvolution(currentWeekKm, previousWeekKm);

      runMetrics.innerHTML = `
        <div><strong>${{formatPace(paceValue(averagePace))}}</strong><span>${{t("averagePace")}}</span></div>
        <div><strong>${{distanceValue(currentWeekKm).toFixed(2)}} ${{distanceUnit()}}</strong><span>${{t("weekKm")}}</span></div>
        <div><strong>${{formatPace(paceValue(bestPace))}}</strong><span>${{t("bestPace")}}</span></div>
        <div><strong>${{evolution}}</strong><span>${{t("evolution")}}</span></div>
      `;
    }}

    function formatPace(minutesPerKm) {{
      if (!Number.isFinite(minutesPerKm) || minutesPerKm <= 0) return "-";
      const minutes = Math.floor(minutesPerKm);
      const seconds = Math.round((minutesPerKm - minutes) * 60);
      return `${{minutes}}:${{String(seconds).padStart(2, "0")}}/${{usesImperial ? "mi" : "km"}}`;
    }}

    function formatEvolution(currentKm, previousKm) {{
      if (!previousKm && currentKm) return "+100%";
      if (!previousKm) return "-";
      const change = ((currentKm - previousKm) / previousKm) * 100;
      const sign = change > 0 ? "+" : "";
      return `${{sign}}${{change.toFixed(1)}}%`;
    }}

    function activityLink(activity) {{
      const name = escapeHtml(activity.name || t("activity"));
      if (!activity.id) return name;
      return `<a class="activity-link" href="https://www.strava.com/activities/${{encodeURIComponent(activity.id)}}" target="_blank" rel="noopener noreferrer">${{name}}</a>`;
    }}

    function renderGroups(groups) {{
      if (!groups.length) {{ content.className = "empty"; content.textContent = (yearActivities.length || monthActivities.length) ? t("noFilter") : t("noneFound"); return; }}
      content.className = "";
      content.innerHTML = groups.map((group) => `
        <section class="month-section">
          <div class="month-header">
            <h2>${{group.label}}</h2>
            <div>${{group.activities.length}} ${{t("activities")}} · ${{formatDistance(group.distance)}} · ${{group.time.toFixed(1)}} min</div>
          </div>
          <table>
            <thead><tr><th>${{t("date")}}</th><th>${{t("name")}}</th><th>${{t("type")}}</th><th>${{t("distance")}}</th><th>${{t("time")}}</th><th>${{t("avgSpeed")}}</th><th>${{t("elevation")}}</th></tr></thead>
            <tbody>${{group.activities.map((activity) => `<tr><td>${{formatDate(activity.start_date_local)}}</td><td>${{activityLink(activity)}}</td><td>${{escapeHtml(activityTypeLabel(activity))}}</td><td>${{formatDistance(activity.distance_km)}}</td><td>${{activity.moving_time_min}} min</td><td>${{formatSpeed(activity.average_speed_kmh)}}</td><td>${{formatElevation(activity.total_elevation_gain)}}</td></tr>`).join("")}}</tbody>
          </table>
        </section>
      `).join("");
    }}

    function groupActivities(activities, period) {{
      const groups = new Map();
      for (const activity of activities) {{
        const date = new Date(activity.start_date_local);
        const info = getPeriodInfo(date, period);
        if (!groups.has(info.key)) {{
          groups.set(info.key, {{
            key: info.key,
            label: info.label,
            shortLabel: info.shortLabel,
            activities: [],
            distance: 0,
            time: 0,
            typeCounts: {{}},
          }});
        }}
        const group = groups.get(info.key);
        group.activities.push(activity);
        group.distance += Number(activity.distance_km || 0);
        group.time += Number(activity.moving_time_min || 0);
        const type = activityTypeKey(activity);
        group.typeCounts[type] = (group.typeCounts[type] || 0) + 1;
      }}
      return [...groups.values()].sort((a, b) => b.key.localeCompare(a.key));
    }}

    function getPeriodInfo(date, period) {{
      const year = date.getFullYear();
      if (period === "year") {{
        return {{ key: `${{year}}`, label: `${{year}}`, shortLabel: `${{year}}` }};
      }}
      if (period === "week") {{
        const start = startOfWeek(date);
        const end = new Date(start);
        end.setDate(start.getDate() + 6);
        const key = `${{start.getFullYear()}}-${{String(start.getMonth() + 1).padStart(2, "0")}}-${{String(start.getDate()).padStart(2, "0")}}`;
        return {{
          key,
          label: `${{t("weekOf")}} ${{formatDateOnly(start)}} ${{t("to")}} ${{formatDateOnly(end)}}`,
          shortLabel: formatShortDate(start),
        }};
      }}
      const key = `${{year}}-${{String(date.getMonth() + 1).padStart(2, "0")}}`;
      return {{
        key,
        label: new Intl.DateTimeFormat(appLanguage, {{ month: "long", year: "numeric" }}).format(date),
        shortLabel: `${{new Intl.DateTimeFormat(appLanguage, {{ month: "short" }}).format(date).replace(".", "")}}/${{String(year).slice(-2)}}`,
      }};
    }}

    function startOfWeek(date) {{
      const start = new Date(date.getFullYear(), date.getMonth(), date.getDate());
      const day = start.getDay();
      const diff = day === 0 ? -6 : 1 - day;
      start.setDate(start.getDate() + diff);
      return start;
    }}

    function renderPeriodView(groups) {{
      if (!groups.length) return;
      const charts = document.querySelector("#charts") || createChartsArea();
      const suffix = selectedMonth ? t("byWeek") : t("byMonth");
      charts.innerHTML = `
        ${{chartPanel("distanceChart", `${{t("distanceTitle")}} ${{suffix}} (${{distanceUnit()}})`)}}
        ${{chartPanel("timeChart", `${{t("timeTitle")}} ${{suffix}} (min)`)}}
        ${{chartPanel("paceChart", `${{t("averageRunPaceTitle")}} ${{suffix}} (${{paceUnit()}})`)}}
        ${{chartPanel("runDistanceChart", `${{t("runKmTitle")}} ${{suffix}} (${{distanceUnit()}})`)}}
        ${{chartPanel("typeChart", t("typeTitle"), true)}}
      `;
      const chronological = [...groups].reverse();
      drawBarChart("distanceChart", chronological.map((group) => ({{ label: group.shortLabel, value: distanceValue(group.distance) }})), distanceUnit(), "#fc4c02");
      drawBarChart("timeChart", chronological.map((group) => ({{ label: group.shortLabel, value: group.time }})), "min", "#2563eb");
      drawBarChart("paceChart", chronological.map((group) => ({{ label: group.shortLabel, value: paceValue(averageRunPace(group.activities) || 0) }})), "pace", "#7c3aed");
      drawBarChart("runDistanceChart", chronological.map((group) => ({{ label: group.shortLabel, value: distanceValue(runDistance(group.activities)) }})), distanceUnit(), "#16a34a");
      drawStackedTypeChart("typeChart", chronological);
      attachChartShareButtons();
    }}

    function chartPanel(id, title, wide = false) {{
      return `<div class="chart-panel ${{wide ? "wide" : ""}}"><div class="chart-head"><h2>${{title}}</h2><button class="share-chart" data-chart="${{id}}" data-title="${{escapeHtml(title)}}" aria-label="${{t("shareChart")}}" title="${{t("shareChart")}}">${{shareIcon()}}</button></div><canvas id="${{id}}"></canvas></div>`;
    }}

    function shareIcon() {{
      return `<svg viewBox="0 0 24 24" fill="none" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><circle cx="6" cy="12" r="3.2"></circle><circle cx="18" cy="5" r="3.2"></circle><circle cx="18" cy="19" r="3.2"></circle><path d="M8.9 10.5 15.1 6.5"></path><path d="M8.9 13.5 15.1 17.5"></path></svg>`;
    }}

    function attachChartShareButtons() {{
      [...document.querySelectorAll(".share-chart")].forEach((button) => {{
        button.addEventListener("click", () => shareChart(button.dataset.chart, button.dataset.title));
      }});
    }}

    async function shareChart(chartId, title) {{
      const canvas = document.getElementById(chartId);
      if (!canvas) return;
      const description = buildShareDescription();
      const blob = await createShareImageBlob(canvas, title, description);
      const fileName = `${{title.toLowerCase().replaceAll(" ", "-").replaceAll("/", "-")}}.png`;
      const file = new File([blob], fileName, {{ type: "image/png" }});

      if (navigator.canShare && navigator.canShare({{ files: [file] }})) {{
        await navigator.share({{ files: [file], title, text: description }});
        return;
      }}

      const url = URL.createObjectURL(blob);
      const link = document.createElement("a");
      link.href = url;
      link.download = fileName;
      link.click();
      URL.revokeObjectURL(url);
      notice.textContent = t("downloaded");
    }}

    async function createShareImageBlob(sourceCanvas, title, description) {{
      const padding = 36;
      const headerHeight = 132;
      const footerHeight = 34;
      const output = document.createElement("canvas");
      output.width = 1080;
      output.height = 1080;
      const ctx = output.getContext("2d");

      ctx.fillStyle = "#f5f7f8";
      ctx.fillRect(0, 0, output.width, output.height);
      ctx.fillStyle = "#ffffff";
      roundRect(ctx, padding, padding, output.width - padding * 2, output.height - padding * 2, 18);
      ctx.fill();

      ctx.fillStyle = "#fc4c02";
      ctx.font = "700 28px system-ui, sans-serif";
      ctx.fillText("RunToU Strava", padding + 28, padding + 48);
      ctx.fillStyle = "#151515";
      ctx.font = "700 42px system-ui, sans-serif";
      ctx.fillText(title, padding + 28, padding + 96);
      ctx.fillStyle = "#4b5563";
      ctx.font = "24px system-ui, sans-serif";
      wrapText(ctx, description, padding + 28, padding + 132, output.width - padding * 2 - 56, 30);

      const chartX = padding + 28;
      const chartY = padding + headerHeight + 58;
      const chartW = output.width - padding * 2 - 56;
      const chartH = output.height - chartY - footerHeight - padding;
      ctx.drawImage(sourceCanvas, chartX, chartY, chartW, chartH);

      ctx.fillStyle = "#6b7280";
      ctx.font = "20px system-ui, sans-serif";
      ctx.fillText(t("generatedBy"), padding + 28, output.height - padding - 8);

      return new Promise((resolve) => output.toBlob(resolve, "image/png"));
    }}

    function buildShareDescription() {{
      const selected = getSelectedActivities(filterActivities(yearActivities, activeFilter), filterActivities(monthActivities, activeFilter));
      const distance = selected.reduce((sum, activity) => sum + distanceValue(activity.distance_km), 0);
      const calories = selected.reduce((sum, activity) => sum + estimatedCalories(activity), 0);
      const runs = selected.filter((activity) => isRun(activity)).length;
      const period = selectedWeek
        ? `${{t("weekOf")}} ${{formatDateOnly(startOfWeek(new Date(selected[0]?.start_date_local || Date.now())))}}`
        : selectedMonth
          ? getPeriodInfo(new Date(selected[0]?.start_date_local || Date.now()), "month").label
          : selectedYear;
      return `${{period}} · ${{selected.length}} ${{t("activities")}} · ${{distance.toFixed(2)}} ${{distanceUnit()}} · ${{Math.round(calories)}} kcal · ${{runs}} ${{t("runLabel")}}`;
    }}

    function roundRect(ctx, x, y, width, height, radius) {{
      ctx.beginPath();
      ctx.moveTo(x + radius, y);
      ctx.lineTo(x + width - radius, y);
      ctx.quadraticCurveTo(x + width, y, x + width, y + radius);
      ctx.lineTo(x + width, y + height - radius);
      ctx.quadraticCurveTo(x + width, y + height, x + width - radius, y + height);
      ctx.lineTo(x + radius, y + height);
      ctx.quadraticCurveTo(x, y + height, x, y + height - radius);
      ctx.lineTo(x, y + radius);
      ctx.quadraticCurveTo(x, y, x + radius, y);
      ctx.closePath();
    }}

    function wrapText(ctx, text, x, y, maxWidth, lineHeight) {{
      const words = text.split(" ");
      let line = "";
      for (const word of words) {{
        const testLine = `${{line}}${{word}} `;
        if (ctx.measureText(testLine).width > maxWidth && line) {{
          ctx.fillText(line, x, y);
          line = `${{word}} `;
          y += lineHeight;
        }} else {{
          line = testLine;
        }}
      }}
      ctx.fillText(line, x, y);
    }}

    function estimatedCalories(activity) {{
      const stravaCalories = Number(activity.calories || 0);
      if (stravaCalories > 0) return stravaCalories;
      const minutes = Number(activity.moving_time_min || 0);
      if (!minutes) return 0;
      const type = activityTypeKey(activity);
      const metByType = {{
        run: 9.8, trailrun: 10.5, virtualrun: 9.8, walk: 3.8, hike: 6.0, ride: 7.5, virtualride: 7.5, ebikeride: 5.5, mountainbikeride: 8.5,
        swim: 8.0, weighttraining: 3.5, workout: 5.0, pilates: 3.0, yoga: 2.5, rowing: 7.0, kayaking: 5.0, soccer: 7.0, tennis: 7.0
      }};
      const met = metByType[type] || 4.5;
      const referenceWeightKg = 75;
      return met * referenceWeightKg * (minutes / 60);
    }}

    function runDistance(activities) {{
      return activities
        .filter((activity) => isRun(activity))
        .reduce((sum, activity) => sum + Number(activity.distance_km || 0), 0);
    }}

    function averageRunPace(activities) {{
      const runs = activities.filter((activity) => isRun(activity) && Number(activity.distance_km || 0) > 0);
      const distance = runs.reduce((sum, activity) => sum + Number(activity.distance_km || 0), 0);
      const time = runs.reduce((sum, activity) => sum + Number(activity.moving_time_min || 0), 0);
      return distance ? time / distance : null;
    }}

    function createChartsArea() {{
      const charts = document.createElement("section");
      charts.id = "charts";
      charts.className = "charts";
      summary.after(charts);
      return charts;
    }}

    function removeChartsArea() {{
      const charts = document.querySelector("#charts");
      if (charts) charts.remove();
    }}


    function resizeCanvas(canvas) {{
      const rect = canvas.getBoundingClientRect();
      canvas.width = Math.max(320, Math.floor(rect.width));
      canvas.height = Math.max(220, Math.floor(rect.height || 260));
    }}

    function drawBarChart(id, data, unit, color) {{
      const canvas = document.getElementById(id);
      resizeCanvas(canvas);
      const ctx = canvas.getContext("2d");
      const width = canvas.width;
      const height = canvas.height;
      const paddingLeft = 42;
      const paddingBottom = unit === "pace" ? 58 : 42;
      const max = Math.max(...data.map((item) => item.value), 1);
      const labelEvery = data.length > 10 ? Math.ceil(data.length / 8) : 1;
      ctx.clearRect(0, 0, width, height);
      ctx.fillStyle = "#ffffff";
      ctx.fillRect(0, 0, width, height);
      ctx.strokeStyle = "#dfe3e6";
      ctx.beginPath();
      ctx.moveTo(paddingLeft, 18);
      ctx.lineTo(paddingLeft, height - paddingBottom);
      ctx.lineTo(width - 16, height - paddingBottom);
      ctx.stroke();
      const barWidth = Math.max(10, Math.min(34, (width - paddingLeft - 34) / data.length - 8));
      data.forEach((item, index) => {{
        const gap = 8;
        const x = paddingLeft + 12 + index * (barWidth + gap);
        const barHeight = ((height - paddingBottom - 30) * item.value) / max;
        const y = height - paddingBottom - barHeight;
        ctx.fillStyle = color;
        ctx.fillRect(x, y, barWidth, barHeight);
        ctx.fillStyle = "#374151";
        ctx.font = "12px sans-serif";
        ctx.textAlign = "center";
        if (index % labelEvery === 0 || index === data.length - 1) {{
          ctx.save();
          ctx.translate(x + barWidth / 2, height - 18);
          if (unit === "pace" && data.length > 6) ctx.rotate(-Math.PI / 5);
          ctx.fillText(item.label, 0, 0);
          ctx.restore();
        }}
        if (barWidth > 16 && item.value > 0) {{
          const valueLabel = unit === "pace" ? formatPace(item.value).replace("/km", "").replace("/mi", "") : item.value.toFixed(1);
          ctx.fillText(valueLabel, x + barWidth / 2, Math.max(14, y - 8));
        }}
      }});
    }}

    function drawStackedTypeChart(id, groups) {{
      const canvas = document.getElementById(id);
      resizeCanvas(canvas);
      const ctx = canvas.getContext("2d");
      const width = canvas.width;
      const height = canvas.height;
      const padding = 42;
      const typeKeys = [...new Set(groups.flatMap((group) => Object.keys(group.typeCounts || {{}})))];
      const palette = ["#fc4c02", "#16a34a", "#7c3aed", "#2563eb", "#eab308", "#db2777", "#0891b2", "#64748b"];
      const colors = Object.fromEntries(typeKeys.map((type, index) => [type, palette[index % palette.length]]));
      const max = Math.max(...groups.map((group) => typeKeys.reduce((sum, type) => sum + (group.typeCounts?.[type] || 0), 0)), 1);
      ctx.clearRect(0, 0, width, height);
      ctx.fillStyle = "#ffffff";
      ctx.fillRect(0, 0, width, height);
      const maxBarWidth = 48;
      const slotWidth = (width - padding - 34) / Math.max(groups.length, 1);
      const barWidth = Math.max(12, Math.min(maxBarWidth, slotWidth * 0.55));
      const chartWidth = groups.length * slotWidth;
      const startX = padding + Math.max(12, ((width - padding - 16) - chartWidth) / 2);
      groups.forEach((group, index) => {{
        const x = startX + index * slotWidth + (slotWidth - barWidth) / 2;
        let y = height - padding;
        for (const type of typeKeys) {{
          const value = group.typeCounts?.[type] || 0;
          const h = ((height - padding - 52) * value) / max;
          y -= h;
          ctx.fillStyle = colors[type];
          ctx.fillRect(x, y, barWidth, h);
        }}
        ctx.fillStyle = "#374151";
        ctx.font = "12px sans-serif";
        ctx.textAlign = "center";
        ctx.fillText(group.shortLabel, x + barWidth / 2, height - 16);
      }});
      typeKeys.slice(0, 6).forEach((type, index) => {{
        const x = padding + (index % 3) * 170;
        const y = 12 + Math.floor(index / 3) * 20;
        ctx.fillStyle = colors[type];
        ctx.fillRect(x, y, 16, 10);
        ctx.fillStyle = "#374151";
        ctx.textAlign = "left";
        ctx.fillText(activityTypeLabel(type), x + 22, y + 10);
      }});
    }}

    function formatDateOnly(value) {{
      return new Intl.DateTimeFormat(appLanguage, {{ day: "2-digit", month: "2-digit", year: "numeric" }}).format(value);
    }}

    function formatShortDate(value) {{
      return new Intl.DateTimeFormat(appLanguage, {{ day: "2-digit", month: "2-digit" }}).format(value);
    }}

    function formatDate(value) {{ return new Intl.DateTimeFormat(appLanguage, {{ dateStyle: "short", timeStyle: "short" }}).format(new Date(value)); }}
    function escapeHtml(value) {{ return String(value ?? "").replaceAll("&", "&amp;").replaceAll("<", "&lt;").replaceAll(">", "&gt;").replaceAll('"', "&quot;").replaceAll("'", "&#039;"); }}
  </script>
</body>
</html>'''


def first(values):
    return values[0] if values else None


if __name__ == "__main__":
    server = ThreadingHTTPServer(("localhost", PORT), Handler)
    print(f"RunToU Strava: http://localhost:{PORT}")
    server.serve_forever()
