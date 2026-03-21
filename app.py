from flask import Flask, render_template, request, jsonify
import pandas as pd
import sqlite3
import requests
from datetime import datetime, timedelta
import os
import re
from dotenv import load_dotenv

load_dotenv()

app = Flask(__name__)
DB_NAME = 'productivity.db'


def get_db_connection():
    conn = sqlite3.connect(DB_NAME)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    try:
        conn = get_db_connection()
        conn.execute('''
            CREATE TABLE IF NOT EXISTS logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                start_date TEXT, start_time TEXT,
                end_date TEXT, end_time TEXT,
                task TEXT, duration INTEGER, tags TEXT, urg INTEGER, imp INTEGER
            )
        ''')
        conn.commit()
        conn.close()
    except Exception as e:
        print(f"DB Error: {e}")


class TimeLogParser:
    def __init__(self):
        self.time_pattern = re.compile(r'^(\d{1,2})(?:[:\s]?(\d{2}))?\s*([ap]m)?\s*', re.IGNORECASE)

    def parse_time_string(self, text_str, ref_date):
        if not isinstance(text_str, str) or not text_str: return None, text_str
        match = self.time_pattern.match(text_str)
        if match:
            hour, minute = int(match.group(1)), int(match.group(2) or 0)
            ampm = match.group(3).lower() if match.group(3) else None
            if ampm == 'pm' and hour < 12: hour += 12
            if ampm == 'am' and hour == 12: hour = 0
            dt = ref_date.replace(hour=hour, minute=minute, second=0, microsecond=0)
            return dt, text_str[match.end():].strip()
        return None, text_str

    def parse_row(self, col_a, col_b, client_now_str, previous_end_dt):
        try:
            client_now = pd.to_datetime(client_now_str)
        except:
            client_now = datetime.now()

        explicit_time_a, _ = self.parse_time_string(col_a, client_now)
        explicit_time_b, remaining_text = self.parse_time_string(col_b, client_now)

        raw_text = remaining_text if remaining_text else (col_b if col_b else "Unspecified")
        task_name, tag = raw_text.strip(), "General"
        is_urg, is_imp = False, False

        if '.' in raw_text:
            parts = raw_text.split('.', 1)
            task_name = parts[0].strip()
            meta_part = parts[1].strip()
            is_urg = 'urg' in meta_part.lower()
            is_imp = 'imp' in meta_part.lower()
            if meta_part:
                words = meta_part.split()
                if words: tag = words[0].capitalize()

        # LOGIC: End Time
        # Rule: Explicit time in text IS ALWAYS THE END TIME.
        end_dt = explicit_time_b if explicit_time_b else client_now

        # Correction: If explicit end is > 1 hour in future vs client_now, assume yesterday
        # (e.g. logging 1AM task at 9AM)
        if end_dt > client_now + timedelta(hours=1):
            end_dt -= timedelta(days=1)

        # LOGIC: Start Time
        start_dt = None

        # Rule 1: Explicit Start (Only if Column A is used)
        if explicit_time_a:
            start_dt = explicit_time_a

        # Rule 2: Chaining (Default)
        if not start_dt:
            if previous_end_dt:
                # If gap is MASSIVE (> 16h), break chain. Otherwise, simple chain.
                gap_hours = (end_dt - previous_end_dt).total_seconds() / 3600
                if gap_hours > 16:
                    start_dt = end_dt - timedelta(minutes=30)
                else:
                    start_dt = previous_end_dt
            else:
                start_dt = end_dt - timedelta(minutes=30)

        return {
            "start_dt": start_dt, "end_dt": end_dt,
            "task": task_name, "tag": tag,
            "urg": is_urg, "imp": is_imp
        }


def sync_cloud_data():
    url = os.getenv("SHEETY_ENDPOINT")
    if not url: return

    try:
        response = requests.get(url)
        data = response.json()
        if 'sheet1' not in data: return
        cloud_df = pd.DataFrame(data['sheet1'])
        if 'id' in cloud_df.columns: cloud_df = cloud_df.sort_values('id')

        # 1. Parse Raw Data
        parser = TimeLogParser()
        parsed_rows = []
        previous_end = None

        for _, row in cloud_df.iterrows():
            col_a = str(row.get('colA', '') or '')
            col_b = str(row.get('colB', '') or str(row.get('rawTask', '')))
            client_now = row.get('loggedTime')

            parsed = parser.parse_row(col_a, col_b, client_now, previous_end)
            parsed_rows.append(parsed)
            previous_end = parsed['end_dt']

        # 2. Trim Overlaps
        for i in range(1, len(parsed_rows)):
            current = parsed_rows[i]
            prev = parsed_rows[i - 1]
            if current['start_dt'] < prev['end_dt']:
                parsed_rows[i - 1]['end_dt'] = current['start_dt']

        # 3. Midnight Splitter
        final_rows = []
        for p in parsed_rows:
            if p['start_dt'].date() < p['end_dt'].date():
                midnight = datetime.combine(p['end_dt'].date(), datetime.min.time())

                part1 = p.copy()
                part1['end_dt'] = midnight

                part2 = p.copy()
                part2['start_dt'] = midnight

                final_rows.append(part1)
                final_rows.append(part2)
            else:
                final_rows.append(p)

        # 4. Save to DB
        conn = get_db_connection()
        conn.execute("DROP TABLE IF EXISTS logs")
        conn.execute('''
            CREATE TABLE logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                start_date TEXT, start_time TEXT,
                end_date TEXT, end_time TEXT,
                task TEXT, duration INTEGER, tags TEXT, urg INTEGER, imp INTEGER
            )
        ''')

        for p in final_rows:
            duration = int((p['end_dt'] - p['start_dt']).total_seconds() / 60)
            if duration <= 0: continue

            conn.execute('''
                INSERT INTO logs (start_date, start_time, end_date, end_time, task, duration, tags, urg, imp)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ''', (
                p['start_dt'].strftime("%Y-%m-%d"),
                p['start_dt'].strftime("%H:%M:%S"),
                p['end_dt'].strftime("%Y-%m-%d"),
                p['end_dt'].strftime("%H:%M:%S"),
                p['task'], duration, p['tag'],
                1 if p['urg'] else 0, 1 if p['imp'] else 0
            ))

        conn.commit()
        conn.close()
        print("Sync Complete: Strict End Times Enforced.")

    except Exception as e:
        print(f"Sync Error: {e}")


def fetch_local_data():
    try:
        conn = get_db_connection()
        df = pd.read_sql_query("SELECT * FROM logs ORDER BY start_date ASC, start_time ASC", conn)
        conn.close()
        if df.empty: return pd.DataFrame()

        final_rows = []
        for _, row in df.iterrows():
            start = pd.to_datetime(f"{row['start_date']} {row['start_time']}")
            end = pd.to_datetime(f"{row['end_date']} {row['end_time']}")
            final_rows.append({
                'id': row['id'],
                'date': start.date(),
                'start_time': start.strftime("%H:%M"),
                'end_time': end.strftime("%H:%M"),
                'start_datetime': start,
                'end_datetime': end,
                'task': row['task'],
                'duration': row['duration'],
                'tag': row['tags'],
                'urgent': bool(row['urg']),
                'important': bool(row['imp'])
            })
        return pd.DataFrame(final_rows)
    except Exception as e:
        print(f"Fetch error: {e}")
        return pd.DataFrame()


def get_matrix_stats(df):
    stats = {"q1": {"hours": 0, "pct": 0}, "q2": {"hours": 0, "pct": 0},
             "q3": {"hours": 0, "pct": 0}, "q4": {"hours": 0, "pct": 0}, "total_hours": 0}
    if df.empty: return stats

    total_min = df['duration'].sum() or 1
    q2 = df[~df['urgent'] & df['important']]['duration'].sum()
    q1 = df[df['urgent'] & df['important']]['duration'].sum()
    q3 = df[df['urgent'] & ~df['important']]['duration'].sum()
    q4 = df[~df['urgent'] & ~df['important']]['duration'].sum()

    return {
        "q1": {"hours": round(q1 / 60, 1), "pct": round((q1 / total_min) * 100, 1)},
        "q2": {"hours": round(q2 / 60, 1), "pct": round((q2 / total_min) * 100, 1)},
        "q3": {"hours": round(q3 / 60, 1), "pct": round((q3 / total_min) * 100, 1)},
        "q4": {"hours": round(q4 / 60, 1), "pct": round((q4 / total_min) * 100, 1)},
        "total_hours": round(total_min / 60, 1)
    }


@app.route('/')
def dashboard():
    sync_cloud_data()
    date_str = request.args.get('date')
    period = request.args.get('period', 'day')  # day, week, month
    
    if date_str:
        selected_date = datetime.strptime(date_str, '%Y-%m-%d').date()
    else:
        selected_date = datetime.now().date()

    df = fetch_local_data()
    
    # Determine date range based on period
    if period == 'day':
        start_date = selected_date
        end_date = selected_date
    elif period == 'week':
        idx = (selected_date.weekday() + 1) % 7
        start_date = selected_date - timedelta(days=idx)
        end_date = start_date + timedelta(days=6)
    else:  # month
        start_date = selected_date.replace(day=1)
        if start_date.month == 12:
            end_date = start_date.replace(year=start_date.year + 1, month=1) - timedelta(days=1)
        else:
            end_date = start_date.replace(month=start_date.month + 1) - timedelta(days=1)
    
    # Filter data for selected period
    period_df = df[(df['date'] >= start_date) & (df['date'] <= end_date)] if not df.empty else pd.DataFrame()
    matrix = get_matrix_stats(period_df)

    # Week days for week selector
    idx = (selected_date.weekday() + 1) % 7
    start_of_week = selected_date - timedelta(days=idx)
    week_days = []
    for i in range(7):
        day = start_of_week + timedelta(days=i)
        day_data = df[df['date'] == day] if not df.empty else pd.DataFrame()
        day_hours = round(day_data['duration'].sum() / 60, 1) if not day_data.empty else 0
        week_days.append({
            'date_obj': day,
            'day_name': day.strftime("%a")[0],
            'day_num': day.day,
            'full_str': day.strftime("%Y-%m-%d"),
            'hours': day_hours,
            'is_selected': (day == selected_date)
        })
    
    week_total = sum(day['hours'] for day in week_days)

    # Tags for selected period
    tag_labels, tag_data = [], []
    if not period_df.empty:
        tag_counts = period_df.groupby('tag')['duration'].sum().sort_values(ascending=False)
        tag_labels = tag_counts.index.tolist()
        tag_data = [round(x / 60, 1) for x in tag_counts.values]

    # Format period label
    if period == 'day':
        period_label = selected_date.strftime("%B %Y")
    elif period == 'week':
        period_label = f"{start_of_week.strftime('%b %d')} - {(start_of_week + timedelta(days=6)).strftime('%b %d, %Y')}"
    else:
        period_label = selected_date.strftime("%B %Y")

    return render_template('dashboard.html',
                           matrix=matrix,
                           tags={'labels': tag_labels, 'data': tag_data},
                           week_days=week_days,
                           week_total=round(week_total, 1),
                           current_month=period_label,
                           selected_date=selected_date,
                           period=period,
                           start_date=start_date,
                           end_date=end_date)


@app.route('/api/tasks')
def get_tasks():
    """API endpoint to get filtered tasks"""
    date_str = request.args.get('date')
    period = request.args.get('period', 'day')
    filter_type = request.args.get('filter', 'all')
    tag_name = request.args.get('tag', '')

    if date_str:
        selected_date = datetime.strptime(date_str, '%Y-%m-%d').date()
    else:
        selected_date = datetime.now().date()

    df = fetch_local_data()

    # Safety Check: Empty Data
    if df.empty:
        return jsonify({
            'tasks': [],
            'total_hours': 0,
            'total_minutes': 0,
            'count': 0
        })

    # Determine date range
    if period == 'day':
        start_date = selected_date
        end_date = selected_date
    elif period == 'week':
        idx = (selected_date.weekday() + 1) % 7
        start_date = selected_date - timedelta(days=idx)
        end_date = start_date + timedelta(days=6)
    else:  # month
        start_date = selected_date.replace(day=1)
        if start_date.month == 12:
            end_date = start_date.replace(year=start_date.year + 1, month=1) - timedelta(days=1)
        else:
            end_date = start_date.replace(month=start_date.month + 1) - timedelta(days=1)

    period_df = df[(df['date'] >= start_date) & (df['date'] <= end_date)] if not df.empty else pd.DataFrame()

    # Apply filters
    if period_df.empty:
        filtered = period_df
    elif filter_type == 'q1':
        filtered = period_df[period_df['urgent'] & period_df['important']]
    elif filter_type == 'q2':
        filtered = period_df[~period_df['urgent'] & period_df['important']]
    elif filter_type == 'q3':
        filtered = period_df[period_df['urgent'] & ~period_df['important']]
    elif filter_type == 'q4':
        filtered = period_df[~period_df['urgent'] & ~period_df['important']]
    elif filter_type == 'imp':
        filtered = period_df[period_df['important']]
    elif filter_type == 'urg':
        filtered = period_df[period_df['urgent']]
    elif filter_type == 'imp_and_urg':
        filtered = period_df[period_df['urgent'] & period_df['important']]
    elif filter_type == 'imp_not_urg':
        filtered = period_df[~period_df['urgent'] & period_df['important']]
    elif filter_type == 'urg_not_imp':
        filtered = period_df[period_df['urgent'] & ~period_df['important']]
    elif filter_type == 'not_imp':
        filtered = period_df[~period_df['important']]
    elif filter_type == 'tag' and tag_name:
        filtered = period_df[period_df['tag'] == tag_name]
    else:
        filtered = period_df

    tasks = []
    if not filtered.empty:
        for _, row in filtered.iterrows():
            tasks.append({
                'task': row['task'],
                'start_time': row['start_time'],
                'end_time': row['end_time'],
                'date': str(row['date']),
                'duration': float(round(row['duration'] / 60, 2)),  # Explicit float conversion
                'tag': row['tag'],
                'urgent': bool(row['urgent']),  # Explicit bool conversion
                'important': bool(row['important'])  # Explicit bool conversion
            })

    # Calculate stats with explicit types
    if not filtered.empty:
        total_hours = float(round(filtered['duration'].sum() / 60, 2))
        total_minutes = int(filtered['duration'].sum())
    else:
        total_hours = 0.0
        total_minutes = 0

    return jsonify({
        'tasks': tasks,
        'total_hours': total_hours,
        'total_minutes': total_minutes,
        'count': len(tasks)
    })


@app.route('/api/tags')
def get_tags():
    """API endpoint to get all tags with stats"""
    date_str = request.args.get('date')
    period = request.args.get('period', 'day')
    
    if date_str:
        selected_date = datetime.strptime(date_str, '%Y-%m-%d').date()
    else:
        selected_date = datetime.now().date()
    
    df = fetch_local_data()
    
    # Determine date range
    if period == 'day':
        start_date = selected_date
        end_date = selected_date
    elif period == 'week':
        idx = (selected_date.weekday() + 1) % 7
        start_date = selected_date - timedelta(days=idx)
        end_date = start_date + timedelta(days=6)
    else:  # month
        start_date = selected_date.replace(day=1)
        if start_date.month == 12:
            end_date = start_date.replace(year=start_date.year + 1, month=1) - timedelta(days=1)
        else:
            end_date = start_date.replace(month=start_date.month + 1) - timedelta(days=1)
    
    period_df = df[(df['date'] >= start_date) & (df['date'] <= end_date)] if not df.empty else pd.DataFrame()
    
    if period_df.empty:
        return jsonify({'tags': []})
    
    tag_stats = []
    for tag, group in period_df.groupby('tag'):
        total_minutes = group['duration'].sum()
        tag_stats.append({
            'name': tag,
            'hours': round(total_minutes / 60, 2),
            'minutes': int(total_minutes),
            'count': len(group)
        })
    
    tag_stats.sort(key=lambda x: x['hours'], reverse=True)
    return jsonify({'tags': tag_stats})


if __name__ == '__main__':
    init_db()
    app.run(debug=True, port=5001)