from flask import Flask, request, Response, jsonify
import requests
import re
import json
import os
import shutil
from datetime import datetime
from zoneinfo import ZoneInfo
from collections import deque

app = Flask(__name__)

PROWLARR_URL = os.getenv("PROWLARR_URL", "http://your-prowlarr-host:9696")
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
RULES_FILE = os.getenv("RULES_FILE", "/config/rules.json")
DEFAULT_RULES_SOURCE = os.getenv("DEFAULT_RULES_SOURCE", os.path.join(BASE_DIR, "rules.json"))
LOG_BUFFER_MAX_LINES = 500
LOG_BUFFER = deque(maxlen=LOG_BUFFER_MAX_LINES)
BERLIN_TZ = ZoneInfo("Europe/Berlin")


def current_time():
    return datetime.now(BERLIN_TZ)


def log_event(message):
    """Speichert Log-Einträge im internen Buffer und leitet sie an stdout weiter."""
    timestamp = current_time().strftime("%Y-%m-%d %H:%M:%S")
    entry = f"[{timestamp}] {message}"
    LOG_BUFFER.append(entry)
    print(entry, flush=True)


def check_prowlarr_health():
    """Prüft ob Prowlarr erreichbar ist und gibt Status zurück."""
    try:
        # Nutze Root-Endpoint statt API (braucht kein Auth)
        response = requests.get(
            PROWLARR_URL,
            timeout=5
        )
        # Akzeptiere alle erfolgreichen Status-Codes (2xx)
        if 200 <= response.status_code < 300:
            return True, None
        else:
            return False, f"HTTP {response.status_code}"
    except requests.exceptions.ConnectionError as e:
        return False, f"Connection Error: {str(e)}"
    except requests.exceptions.Timeout:
        return False, "Timeout: Prowlarr antwortet nicht"
    except Exception as e:
        return False, f"Error: {str(e)}"


def ensure_rules_file():
    """Stellt sicher, dass die Konfigurationsdatei im Mount-Pfad vorhanden ist."""
    global RULES_FILE

    config_dir = os.path.dirname(RULES_FILE)
    if config_dir:
        try:
            os.makedirs(config_dir, exist_ok=True)
        except PermissionError as e:
            fallback_path = os.path.join(BASE_DIR, "config", "rules.json")
            fallback_dir = os.path.dirname(fallback_path)
            os.makedirs(fallback_dir, exist_ok=True)
            RULES_FILE = fallback_path
            config_dir = fallback_dir
            log_event(f"WARNING: Konnte {RULES_FILE} nicht anlegen: {e}. Verwende Fallback {fallback_path}")

    if os.path.exists(RULES_FILE):
        return

    if os.path.exists(DEFAULT_RULES_SOURCE):
        try:
            shutil.copyfile(DEFAULT_RULES_SOURCE, RULES_FILE)
            log_event(f"Initialized {RULES_FILE} from {DEFAULT_RULES_SOURCE}")
            return
        except Exception as e:
            log_event(f"ERROR: Konnte {DEFAULT_RULES_SOURCE} nach {RULES_FILE} kopieren: {str(e)}")

    with open(RULES_FILE, 'w', encoding='utf-8') as f:
        json.dump(get_default_rules(), f, indent=2)

    log_event(f"WARNING: {RULES_FILE} nicht gefunden. Erstelle Standard-Regeln.")


def load_rules():
    """Lädt die rules.json Datei. Falls nicht vorhanden, wird eine Default-Konfiguration verwendet."""
    ensure_rules_file()

    if os.path.exists(RULES_FILE):
        try:
            with open(RULES_FILE, 'r', encoding='utf-8') as f:
                return json.load(f)
        except Exception as e:
            log_event(f"ERROR: Konnte {RULES_FILE} nicht laden: {str(e)}")
            return get_default_rules()
    else:
        log_event(f"WARNING: {RULES_FILE} nicht gefunden. Verwende Standard-Regeln.")
        return get_default_rules()


def save_rules_to_disk(rules_data):
    """Schreibt Regeln atomar auf die Konfigurationsdatei."""
    ensure_rules_file()
    temp_path = f"{RULES_FILE}.tmp"
    with open(temp_path, 'w', encoding='utf-8') as f:
        json.dump(rules_data, f, indent=2)
    os.replace(temp_path, RULES_FILE)
    return rules_data


def get_rules_state():
    """Gibt die aktuell gespeicherten Regeln zurück und aktualisiert den globalen Zustand."""
    global RULES
    RULES = load_rules()
    return RULES


def get_default_rules():
    """Gibt Standard-Regeln zurück, falls rules.json nicht existiert."""
    return {
        "search_rules": [
            {
                "name": "Search",
                "pattern": "",
                "enabled": False,
                "case_insensitive": True,
                "action": "extract"
            }
        ],
        "release_rules": [],
        "settings": {
            "prowlarr_url": PROWLARR_URL,
            "rewrite_enabled": True
        }
    }


# Lade Regeln beim Start
RULES = load_rules()

log_event("="*60)
log_event("🚀 REWRITE PROXY STARTED")
log_event("="*60)
log_event(f"Loaded {len(RULES.get('search_rules', []))} search rules")
log_event(f"Rewrite enabled: {RULES.get('settings', {}).get('rewrite_enabled', True)}")
log_event("="*60)


def apply_umlaut_adaptarr(text):
    """Ersetzt deutsche Umlaute und entfernt führende Artikel."""
    if not text:
        return text

    transformations = {
        "ä": "ae",
        "ö": "oe",
        "ü": "ue",
        "Ä": "Ae",
        "Ö": "Oe",
        "Ü": "Ue",
        "ß": "ss"
    }

    for old, new in transformations.items():
        text = text.replace(old, new)

    text = re.sub(r"^\s*(der|die|das|the|an|a)\s+", "", text, flags=re.IGNORECASE)
    return text.strip()


def apply_replace_rule(text, pattern, replacement, flags):
    """Ersetzt das Pattern durch replacement im Text."""
    if not pattern or replacement is None:
        return text
    try:
        return re.sub(pattern, replacement, text, flags)
    except re.error as e:
        log_event(f"ERROR: Ungültiger Regex in replace rule: {str(e)}")
        return text


def apply_skip_rule(text, pattern, flags):
    """Entfernt den Bereich, der dem Pattern entspricht."""
    if not pattern:
        return text
    try:
        new_text = re.sub(pattern, "", text, flags)
        new_text = re.sub(r"\s+", " ", new_text).strip()
        return new_text
    except re.error as e:
        log_event(f"ERROR: Ungültiger Regex in skip rule: {str(e)}")
        return text


def rewrite_query(q):
    """Rewritten Suchanfrage basierend auf den aktuell gespeicherten Regeln."""
    rules = get_rules_state()

    # Debug: Eingabe loggen
    log_event(f"[rewrite_query] Input: {q}")
    log_event(f"[rewrite_query] RULES is None: {rules is None}")
    log_event(f"[rewrite_query] Rewrite enabled: {rules.get('settings', {}).get('rewrite_enabled', True) if rules else 'N/A'}")
    
    if not q or not rules.get("settings", {}).get("rewrite_enabled", True):
        log_event("[rewrite_query] Early return - returning unchanged")
        return q

    original = q
    search_rules = rules.get("search_rules", [])
    matched_rule_name = None
    log_event(f"[rewrite_query] Found {len(search_rules)} search rules")

    # Durchlaufe alle aktivierten Regeln
    for rule in search_rules:
        if not rule.get("enabled", True):
            log_event(f"[rewrite_query] Skipping disabled rule: {rule.get('name')}")
            continue

        pattern = rule.get("pattern")
        flags = re.IGNORECASE if rule.get("case_insensitive", False) else 0
        action = rule.get("action", "extract").lower()

        try:
            if action == "extract":
                if not pattern:
                    log_event(f"[rewrite_query] Skipping extract rule without pattern: {rule.get('name')}")
                    continue

                log_event(f"[rewrite_query] Testing rule: {rule.get('name')} with pattern: {pattern}")
                match = re.search(pattern, q, flags)
                log_event(f"[rewrite_query] Regex match result: {match}")
                if match:
                    q = match.group(1)
                    matched_rule_name = rule.get('name', 'Unknown')
                    log_event(f"[rewrite_query] Match found! New value: {q}")
                    break  # Erste Regel, die matched, wird verwendet
            elif action in ("umlaut_adaptarr", "normalize_umlauts"):
                if pattern and not re.search(pattern, q, flags):
                    log_event(f"[rewrite_query] Skipping rule because pattern did not match: {rule.get('name')}")
                    continue

                new_q = apply_umlaut_adaptarr(q)
                log_event(f"[rewrite_query] Umlaut adaptation result: {new_q}")
                if new_q != q:
                    q = new_q
                    matched_rule_name = rule.get('name', 'Unknown')
            elif action == "replace":
                if not pattern:
                    log_event(f"[rewrite_query] Skipping replace rule without pattern: {rule.get('name')}")
                    continue

                replacement = rule.get("replacement", "")
                if replacement == "":
                    log_event(f"[rewrite_query] Skipping replace rule without replacement text: {rule.get('name')}")
                    continue

                new_q = apply_replace_rule(q, pattern, replacement, flags)
                log_event(f"[rewrite_query] Replace rule result: {new_q}")
                if new_q != q:
                    q = new_q
                    matched_rule_name = rule.get('name', 'Unknown')
            elif action == "skip":
                if not pattern:
                    log_event(f"[rewrite_query] Skipping skip rule without pattern: {rule.get('name')}")
                    continue

                new_q = apply_skip_rule(q, pattern, flags)
                log_event(f"[rewrite_query] Skip rule result: {new_q}")
                if new_q != q:
                    q = new_q
                    matched_rule_name = rule.get('name', 'Unknown')
        except Exception as e:
            log_event(f"ERROR in Regel '{rule.get('name')}': {str(e)}")
            continue

    q = q.strip()

    if q != original:
        rule_info = f" (Regel: {matched_rule_name})" if matched_rule_name else ""
        log_event(f"REWRITE SEARCH: {original} -> {q}{rule_info}")
    else:
        log_event("[rewrite_query] No rewrite: q == original")

    return q


def rewrite_release_title(xml, original_query):
    """
    Fügt den originalen Suchnamen in den Release-Titel ein.
    """

    if not original_query:
        return xml

    def replace_title(match):
        old_title = match.group(1)

        new_title = (
            original_query
            + " "
            + old_title
        )

        log_event(
            f"REWRITE RELEASE: {old_title} -> {new_title}"
        )

        return "<title>" + new_title + "</title>"


    xml = re.sub(
        r"<title>(.*?)</title>",
        replace_title,
        xml,
        flags=re.IGNORECASE | re.DOTALL
    )

    return xml


@app.route("/<int:indexer>/api")
def torznab(indexer):

    params = request.args.to_dict()

    original_query = None

    if params.get("t") == "search" and "q" in params:
        original_query = params["q"]
        params["q"] = rewrite_query(params["q"])


    target = f"{PROWLARR_URL}/{indexer}/api"

    log_event(
        f"FORWARD: {target} {params}"
    )

    try:
        r = requests.get(
            target,
            params=params,
            timeout=30
        )
    except requests.exceptions.ConnectionError as e:
        log_event(f"ERROR: Prowlarr Verbindungsfehler: {str(e)}")
        return Response(
            '<error code="3" description="Prowlarr ist offline" />',
            status=503,
            content_type="application/xml"
        )
    except requests.exceptions.Timeout:
        log_event("ERROR: Prowlarr Timeout")
        return Response(
            '<error code="3" description="Prowlarr Timeout" />',
            status=504,
            content_type="application/xml"
        )
    except Exception as e:
        log_event(f"ERROR: Prowlarr Fehler: {str(e)}")
        return Response(
            f'<error code="3" description="Prowlarr Fehler: {str(e)}" />',
            status=500,
            content_type="application/xml"
        )

    content = r.text


    if original_query:
        content = rewrite_release_title(
            content,
            original_query
        )


    return Response(
        content.encode("utf-8"),
        status=r.status_code,
        content_type=r.headers.get(
            "content-type",
            "application/xml"
        )
    )


@app.route("/info")
def info():
    """Healthcheck Endpoint mit Prowlarr-Status"""
    is_healthy, error_msg = check_prowlarr_health()
    
    if is_healthy:
        return jsonify({
            "status": "ok",
            "message": "Rewrite Proxy OK",
            "prowlarr": "online",
            "timestamp": current_time().isoformat()
        }), 200
    else:
        return jsonify({
            "status": "error",
            "message": "Prowlarr ist offline oder nicht erreichbar",
            "prowlarr": "offline",
            "error": error_msg,
            "timestamp": current_time().isoformat()
        }), 503


@app.route("/")
def admin_panel():
    """Admin Panel - Hauptseite mit Regelbearbeitung und Healthcheck"""
    try:
        with open('admin.html', 'r', encoding='utf-8') as f:
            return f.read()
    except FileNotFoundError:
        return jsonify({
            "error": "admin.html nicht gefunden"
        }), 500


@app.route("/api/rules", methods=["GET"])
def get_rules():
    """GET /api/rules - Gibt alle Regeln zurück"""
    return jsonify(get_rules_state()), 200


@app.route("/api/logs")
def get_logs():
    """Gibt die letzten Log-Zeilen zurück, die auch in docker logs erscheinen."""
    return jsonify({
        "logs": list(LOG_BUFFER),
        "count": len(LOG_BUFFER)
    }), 200


@app.route("/api/rules", methods=["POST"])
def update_rules():
    """POST /api/rules - Aktualisiert die Regeln und speichert sie in rules.json"""
    try:
        new_rules = request.get_json()
        
        if not new_rules:
            return jsonify({
                "error": "Keine Daten erhalten"
            }), 400
        
        # Validiere die neue Konfiguration
        if "search_rules" not in new_rules or "settings" not in new_rules:
            return jsonify({
                "error": "Ungültige Konfiguration: search_rules und settings erforderlich"
            }), 400
        
        # Speichere in rules.json
        save_rules_to_disk(new_rules)
        
        # Laden Sie die neuen Regeln in den Speicher
        global RULES
        RULES = load_rules()
        
        log_event(f"DEBUG: Regeln aktualisiert: {RULES_FILE}")
        
        return jsonify({
            "status": "success",
            "message": "Regeln wurden aktualisiert",
            "rules": RULES
        }), 200
        
    except Exception as e:
        log_event(f"ERROR: Fehler beim Aktualisieren der Regeln: {str(e)}")
        return jsonify({
            "error": f"Fehler beim Aktualisieren: {str(e)}"
        }), 500


@app.route("/api/rules/reload", methods=["POST"])
def reload_rules():
    """POST /api/rules/reload - Lädt Regeln neu aus rules.json"""
    try:
        global RULES
        RULES = load_rules()
        
        log_event(f"DEBUG: Regeln neu geladen aus {RULES_FILE}")
        
        return jsonify({
            "status": "success",
            "message": "Regeln wurden neu geladen",
            "rules": RULES
        }), 200
        
    except Exception as e:
        log_event(f"ERROR: Fehler beim Neu-Laden der Regeln: {str(e)}")
        return jsonify({
            "error": f"Fehler beim Neu-Laden: {str(e)}"
        }), 500


@app.route('/favicon.ico')
def favicon():
    """Proxy favicon from configured Prowlarr URL."""
    rules = get_rules_state()
    prowlarr_url = rules.get('settings', {}).get('prowlarr_url', PROWLARR_URL)
    prowlarr_url = prowlarr_url.rstrip('/')
    favicon_paths = ['/favicon.ico', '/favicon-32x32.png', '/favicon.png', '/favicon.svg']

    for path in favicon_paths:
        try:
            target = f"{prowlarr_url}{path}"
            r = requests.get(target, timeout=10, stream=True)
            if r.status_code == 200 and r.headers.get('content-type'):
                content_type = r.headers.get('content-type')
                return Response(r.content, status=200, content_type=content_type)
        except requests.exceptions.RequestException as e:
            log_event(f"ERROR: favicon proxy request failed for {target}: {str(e)}")
            continue

    return Response(status=204)


if __name__ == "__main__":
    app.run(
        host="0.0.0.0",
        port=8080
    )
