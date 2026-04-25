import sqlite3
import uuid
import os
import ssl
import certifi
import time
import urllib.request
import urllib.parse
import json
from collections import defaultdict
from flask import Flask, request, jsonify, send_from_directory

SSL_CTX = ssl.create_default_context(cafile=certifi.where())

# ── RATE LIMITING ──
# Max 5 flight searches per IP per minute, 30 per IP per day
_rate_store = defaultdict(list)

def is_rate_limited(ip):
    now = time.time()
    minute_ago = now - 60
    day_ago    = now - 86400
    calls = _rate_store[ip]
    _rate_store[ip] = [t for t in calls if t > day_ago]
    per_minute = sum(1 for t in _rate_store[ip] if t > minute_ago)
    per_day    = len(_rate_store[ip])
    if per_minute >= 5:
        return 'Too many searches. Wait a minute and try again.'
    if per_day >= 30:
        return 'Daily search limit reached (30/day). Try again tomorrow.'
    _rate_store[ip].append(now)
    return None

app = Flask(__name__, static_folder='public', static_url_path='')

DB_PATH = os.path.join(os.path.dirname(__file__), 'travel.db')

def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn

def init_db():
    with get_db() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS trips (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                destination TEXT DEFAULT '',
                created_at INTEGER DEFAULT (strftime('%s', 'now'))
            );
            CREATE TABLE IF NOT EXISTS members (
                id TEXT PRIMARY KEY,
                trip_id TEXT NOT NULL,
                name TEXT NOT NULL,
                color TEXT NOT NULL,
                FOREIGN KEY (trip_id) REFERENCES trips(id)
            );
            CREATE TABLE IF NOT EXISTS expenses (
                id TEXT PRIMARY KEY,
                trip_id TEXT NOT NULL,
                name TEXT NOT NULL,
                total_amount REAL NOT NULL,
                paid_by TEXT,
                created_at INTEGER DEFAULT (strftime('%s', 'now')),
                FOREIGN KEY (trip_id) REFERENCES trips(id)
            );
            CREATE TABLE IF NOT EXISTS expense_splits (
                id TEXT PRIMARY KEY,
                expense_id TEXT NOT NULL,
                member_id TEXT NOT NULL,
                amount REAL NOT NULL,
                paid INTEGER DEFAULT 0,
                FOREIGN KEY (expense_id) REFERENCES expenses(id),
                FOREIGN KEY (member_id) REFERENCES members(id)
            );
        """)

init_db()

COLORS = ['#FF6B6B','#4ECDC4','#45B7D1','#96CEB4','#FFEAA7','#DDA0DD','#98D8C8','#F7DC6F','#BB8FCE','#85C1E9']

# ── SERPAPI GOOGLE FLIGHTS SEARCH ──

try:
    from config import SERPAPI_KEY as _CONFIG_KEY, GOOGLE_MAPS_KEY as _CONFIG_MAPS_KEY
except ImportError:
    _CONFIG_KEY      = ''
    _CONFIG_MAPS_KEY = ''
SERPAPI_KEY      = os.environ.get('SERPAPI_KEY', _CONFIG_KEY)
GOOGLE_MAPS_KEY  = os.environ.get('GOOGLE_MAPS_KEY', _CONFIG_MAPS_KEY)
SERPAPI_BASE     = 'https://serpapi.com/search'

@app.get('/api/maps-key')
def get_maps_key():
    return jsonify(key=GOOGLE_MAPS_KEY)

def format_minutes(total_mins):
    h, m = divmod(int(total_mins), 60)
    parts = []
    if h: parts.append(f'{h}h')
    if m: parts.append(f'{m}m')
    return ' '.join(parts) or '—'

def parse_serpapi_flights(data, adults):
    results = []
    all_offers = data.get('best_flights', []) + data.get('other_flights', [])
    for offer in all_offers[:8]:
        flights = offer.get('flights', [])
        if not flights:
            continue
        first = flights[0]
        last  = flights[-1]

        airline     = first.get('airline', '—')
        flight_num  = first.get('flight_number', '—')
        origin      = first['departure_airport'].get('id', '—')
        destination = last['arrival_airport'].get('id', '—')
        depart_time = first['departure_airport'].get('time', '')
        arrive_time = last['arrival_airport'].get('time', '')
        duration    = format_minutes(offer.get('total_duration', 0))
        stops       = max(0, len(flights) - 1)
        price_total = float(offer.get('price', 0))
        price_pp    = round(price_total / adults, 2) if adults > 1 else price_total
        total_price = round(price_total, 2)

        # Layover details
        layovers = []
        for lw in offer.get('layovers', []):
            layovers.append({
                'airport': lw.get('name', ''),
                'id': lw.get('id', ''),
                'duration': format_minutes(lw.get('duration', 0)),
                'overnight': lw.get('overnight', False),
            })

        # Per-segment details
        segments = []
        for seg in flights:
            segments.append({
                'airline': seg.get('airline', ''),
                'flight_number': seg.get('flight_number', ''),
                'airplane': seg.get('airplane', ''),
                'travel_class': seg.get('travel_class', ''),
                'legroom': seg.get('legroom', ''),
                'extensions': seg.get('extensions', []),
                'from': seg['departure_airport'].get('id', ''),
                'from_name': seg['departure_airport'].get('name', ''),
                'to': seg['arrival_airport'].get('id', ''),
                'to_name': seg['arrival_airport'].get('name', ''),
                'depart': seg['departure_airport'].get('time', ''),
                'arrive': seg['arrival_airport'].get('time', ''),
                'duration': format_minutes(seg.get('duration', 0)),
                'overnight': seg.get('overnight', False),
            })

        carbon = offer.get('carbon_emissions', {})

        results.append({
            'id': offer.get('departure_token', flight_num),
            'airline': airline,
            'airline_logo': offer.get('airline_logo', ''),
            'flight_num': flight_num,
            'origin': origin,
            'destination': destination,
            'depart': depart_time,
            'arrive': arrive_time,
            'duration': duration,
            'stops': stops,
            'price_per_person': round(price_pp, 2),
            'total_price': total_price,
            'currency': 'USD',
            'adults': adults,
            'segments': segments,
            'layovers': layovers,
            'carbon_kg': carbon.get('this_flight', 0) // 1000 if carbon else 0,
        })
    return results

@app.get('/api/flights/search')
def search_flights():
    key = SERPAPI_KEY
    if not key:
        return jsonify(error='no_key'), 503

    limit_msg = is_rate_limited(request.remote_addr)
    if limit_msg:
        return jsonify(error=limit_msg), 429

    origin      = request.args.get('origin', '').upper().strip()
    destination = request.args.get('destination', '').upper().strip()
    date        = request.args.get('date', '').strip()
    adults      = int(request.args.get('adults', 1))

    if not origin or not destination or not date:
        return jsonify(error='origin, destination, and date are required'), 400

    try:
        qs = urllib.parse.urlencode({
            'engine': 'google_flights',
            'departure_id': origin,
            'arrival_id': destination,
            'outbound_date': date,
            'adults': adults,
            'currency': 'USD',
            'hl': 'en',
            'type': '2',  # one-way
            'api_key': key,
        })
        req = urllib.request.Request(f'{SERPAPI_BASE}?{qs}', method='GET')
        with urllib.request.urlopen(req, timeout=15, context=SSL_CTX) as resp:
            data = json.loads(resp.read())

        if 'error' in data:
            return jsonify(error=data['error']), 502

        return jsonify(flights=parse_serpapi_flights(data, adults))
    except urllib.error.HTTPError as e:
        body = e.read().decode()
        return jsonify(error=f'SerpAPI error {e.code}', detail=body), 502
    except Exception as e:
        return jsonify(error=str(e)), 500

# ── LODGING SEARCH ──

@app.get('/api/lodging/search')
def search_lodging():
    key = SERPAPI_KEY
    if not key:
        return jsonify(error='no_key'), 503

    limit_msg = is_rate_limited(request.remote_addr)
    if limit_msg:
        return jsonify(error=limit_msg), 429

    destination = request.args.get('destination', '').strip()
    check_in    = request.args.get('check_in', '').strip()
    check_out   = request.args.get('check_out', '').strip()
    adults      = int(request.args.get('adults', 2))
    max_price   = request.args.get('max_price', '').strip()

    if not destination or not check_in or not check_out:
        return jsonify(error='destination, check_in, and check_out are required'), 400

    try:
        params = {
            'engine': 'google_hotels',
            'q': destination + ' hotels',
            'check_in_date': check_in,
            'check_out_date': check_out,
            'adults': adults,
            'currency': 'USD',
            'hl': 'en',
            'api_key': key,
        }
        if max_price:
            params['max_price'] = int(max_price)

        qs = urllib.parse.urlencode(params)
        req = urllib.request.Request(f'{SERPAPI_BASE}?{qs}', method='GET')
        with urllib.request.urlopen(req, timeout=20, context=SSL_CTX) as resp:
            data = json.loads(resp.read())

        if 'error' in data:
            return jsonify(error=data['error']), 502

        results = []
        for prop in data.get('properties', [])[:15]:
            rate  = prop.get('rate_per_night', {})
            total = prop.get('total_rate', {})
            gps   = prop.get('gps_coordinates', {})
            imgs  = prop.get('images', [])
            thumb = prop.get('thumbnail', '') or (imgs[0].get('thumbnail', '') if imgs else '')
            # Collect full-size images for slideshow
            all_images = []
            for img in imgs[:10]:
                src = img.get('original_image') or img.get('thumbnail', '')
                if src:
                    all_images.append(src)
            if not all_images and thumb:
                all_images = [thumb]

            results.append({
                'name':               prop.get('name', ''),
                'link':               prop.get('link', ''),
                'lat':                gps.get('latitude'),
                'lng':                gps.get('longitude'),
                'price_per_night':    rate.get('extracted_lowest', 0),
                'price_per_night_str':rate.get('lowest', ''),
                'total_price':        total.get('extracted_lowest', 0),
                'total_price_str':    total.get('lowest', ''),
                'rating':             prop.get('overall_rating'),
                'reviews':            prop.get('reviews', 0),
                'hotel_class':        prop.get('hotel_class', ''),
                'amenities':          (prop.get('amenities') or []),
                'thumbnail':          thumb,
                'images':             all_images,
                'description':        prop.get('description', ''),
                'phone':              prop.get('phone', ''),
                'check_in_time':      prop.get('check_in_time', ''),
                'check_out_time':     prop.get('check_out_time', ''),
            })

        return jsonify(lodging=results)
    except urllib.error.HTTPError as e:
        body = e.read().decode()
        return jsonify(error=f'SerpAPI error {e.code}', detail=body), 502
    except Exception as e:
        return jsonify(error=str(e)), 500

# ── TRIPS ──

@app.post('/api/trips')
def create_trip():
    data = request.json
    name = (data.get('name') or '').strip()
    if not name:
        return jsonify(error='Trip name required'), 400
    trip_id = str(uuid.uuid4())[:8]
    with get_db() as conn:
        conn.execute('INSERT INTO trips (id, name, destination) VALUES (?, ?, ?)',
                     (trip_id, name, data.get('destination') or ''))
    return jsonify(id=trip_id, name=name, destination=data.get('destination') or '')

@app.get('/api/trips/<trip_id>')
def get_trip(trip_id):
    with get_db() as conn:
        trip = conn.execute('SELECT * FROM trips WHERE id = ?', (trip_id,)).fetchone()
        if not trip:
            return jsonify(error='Trip not found'), 404
        members = [dict(r) for r in conn.execute('SELECT * FROM members WHERE trip_id = ?', (trip_id,)).fetchall()]
        expenses_raw = conn.execute(
            'SELECT * FROM expenses WHERE trip_id = ? ORDER BY created_at DESC', (trip_id,)
        ).fetchall()
        expenses = []
        for exp in expenses_raw:
            splits = [dict(r) for r in conn.execute("""
                SELECT es.*, m.name as member_name, m.color as member_color
                FROM expense_splits es
                JOIN members m ON es.member_id = m.id
                WHERE es.expense_id = ?
            """, (exp['id'],)).fetchall()]
            expenses.append({**dict(exp), 'splits': splits})
    return jsonify({**dict(trip), 'members': members, 'expenses': expenses})

# ── MEMBERS ──

@app.post('/api/trips/<trip_id>/members')
def add_member(trip_id):
    name = (request.json.get('name') or '').strip()
    if not name:
        return jsonify(error='Name required'), 400
    with get_db() as conn:
        if not conn.execute('SELECT id FROM trips WHERE id = ?', (trip_id,)).fetchone():
            return jsonify(error='Trip not found'), 404
        count = conn.execute('SELECT COUNT(*) FROM members WHERE trip_id = ?', (trip_id,)).fetchone()[0]
        color = COLORS[count % len(COLORS)]
        member_id = str(uuid.uuid4())
        conn.execute('INSERT INTO members (id, trip_id, name, color) VALUES (?, ?, ?, ?)',
                     (member_id, trip_id, name, color))
        expenses = conn.execute('SELECT * FROM expenses WHERE trip_id = ?', (trip_id,)).fetchall()
        for exp in expenses:
            current_count = conn.execute(
                'SELECT COUNT(*) FROM expense_splits WHERE expense_id = ?', (exp['id'],)
            ).fetchone()[0]
            new_count = current_count + 1
            equal_share = exp['total_amount'] / new_count
            conn.execute('UPDATE expense_splits SET amount = ? WHERE expense_id = ?', (equal_share, exp['id']))
            conn.execute('INSERT INTO expense_splits (id, expense_id, member_id, amount, paid) VALUES (?,?,?,?,0)',
                         (str(uuid.uuid4()), exp['id'], member_id, equal_share))
    return jsonify(id=member_id, name=name, color=color)

# ── EXPENSES ──

@app.post('/api/trips/<trip_id>/expenses')
def add_expense(trip_id):
    data = request.json
    name = (data.get('name') or '').strip()
    total_amount = data.get('total_amount')
    if not name or not total_amount:
        return jsonify(error='Name and amount required'), 400
    with get_db() as conn:
        if not conn.execute('SELECT id FROM trips WHERE id = ?', (trip_id,)).fetchone():
            return jsonify(error='Trip not found'), 404
        exp_id = str(uuid.uuid4())
        conn.execute('INSERT INTO expenses (id, trip_id, name, total_amount, paid_by) VALUES (?,?,?,?,?)',
                     (exp_id, trip_id, name, float(total_amount), data.get('paid_by')))
        splits = data.get('splits', [])
        if splits:
            for sp in splits:
                conn.execute('INSERT INTO expense_splits (id, expense_id, member_id, amount, paid) VALUES (?,?,?,?,?)',
                             (str(uuid.uuid4()), exp_id, sp['member_id'], float(sp['amount']), 1 if sp.get('paid') else 0))
        else:
            members = conn.execute('SELECT * FROM members WHERE trip_id = ?', (trip_id,)).fetchall()
            if members:
                share = float(total_amount) / len(members)
                for m in members:
                    is_paid = 1 if m['id'] == data.get('paid_by') else 0
                    conn.execute('INSERT INTO expense_splits (id, expense_id, member_id, amount, paid) VALUES (?,?,?,?,?)',
                                 (str(uuid.uuid4()), exp_id, m['id'], share, is_paid))
    return jsonify(id=exp_id)

@app.delete('/api/expenses/<exp_id>')
def delete_expense(exp_id):
    with get_db() as conn:
        conn.execute('DELETE FROM expense_splits WHERE expense_id = ?', (exp_id,))
        conn.execute('DELETE FROM expenses WHERE id = ?', (exp_id,))
    return jsonify(ok=True)

# ── SPLITS ──

@app.route('/api/splits/<split_id>/toggle', methods=['PATCH'])
def toggle_split(split_id):
    with get_db() as conn:
        row = conn.execute('SELECT paid FROM expense_splits WHERE id = ?', (split_id,)).fetchone()
        if not row:
            return jsonify(error='Not found'), 404
        new_paid = 0 if row['paid'] else 1
        conn.execute('UPDATE expense_splits SET paid = ? WHERE id = ?', (new_paid, split_id))
    return jsonify(paid=bool(new_paid))

# ── STATIC / CATCH-ALL ──

@app.get('/')
@app.get('/trip/<path:subpath>')
def index(subpath=None):
    return send_from_directory('public', 'index.html')

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 3000))
    keys_ok = bool(SERPAPI_KEY)
    print(f'\n✈️  TripSplit running at http://localhost:{port}')
    print(f'   Flight search: {"✅ SerpAPI connected" if keys_ok else "⚠️  No SerpAPI key — set SERPAPI_KEY env var"}\n')
    app.run(host='0.0.0.0', port=port, debug=False)
