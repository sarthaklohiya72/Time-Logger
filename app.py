from flask import Flask, render_template, request
import pandas as pd
import requests
from datetime import datetime, timedelta, date

app = Flask(__name__)

# --- CONFIG ---
SHEETY_API_URL = "https://api.sheety.co/332ad00039a2b49efc712475559e8035/timeTracker/sheet1"


def fetch_and_process_data():
    """Fetches and cleans data."""
    try:
        response = requests.get(SHEETY_API_URL)
        data = response.json()
        if 'sheet1' not in data: return pd.DataFrame()

        df = pd.DataFrame(data['sheet1'])

        if 'id' in df.columns: df = df.sort_values('id')

        processed_rows = []
        previous_end = None

        for _, row in df.iterrows():
            try:
                ref_dt = pd.to_datetime(row.get('loggedTime'))
            except:
                ref_dt = datetime.now()

            raw_task = str(row.get('rawTask', ''))
            is_urg = 'urg' in raw_task.lower()
            is_imp = 'imp' in raw_task.lower()

            parts = raw_task.split('.')
            tag = "General"
            if len(parts) > 1:
                tag = parts[1].strip().split(' ')[0]

            end_dt = ref_dt
            start_dt = previous_end if previous_end else end_dt - timedelta(minutes=30)
            previous_end = end_dt

            duration = (end_dt - start_dt).total_seconds() / 60
            if duration < 0: duration = 30

            processed_rows.append({
                'date': start_dt.date(),
                'duration': int(duration),
                'tag': tag,
                'urgent': is_urg,
                'important': is_imp
            })

        return pd.DataFrame(processed_rows)

    except Exception as e:
        print(f"Error: {e}")
        return pd.DataFrame()


def get_matrix_stats(df):
    """Calculates Matrix Quadrants (Safe Version)."""
    stats = {
        "q1": {"hours": 0, "pct": 0},
        "q2": {"hours": 0, "pct": 0},
        "q3": {"hours": 0, "pct": 0},
        "q4": {"hours": 0, "pct": 0},
        "total_hours": 0
    }
    if df.empty: return stats

    total_min = df['duration'].sum()
    if total_min == 0: total_min = 1

    # Logic:
    # Q2 (Growth) = Imp, Not Urg
    # Q1 (Critical) = Imp, Urg
    # Q3 (Drudgery) = Urg, Not Imp
    # Q4 (Waste) = Not Urg, Not Imp

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
    # 1. Determine Selected Date
    date_str = request.args.get('date')
    if date_str:
        selected_date = datetime.strptime(date_str, '%Y-%m-%d').date()
    else:
        selected_date = datetime.now().date()

    # 2. Generate Week Strip (Sunday to Saturday around selected date)
    # Find the Sunday of this week
    # (weekday: Mon=0, Sun=6) -> We want Sunday start.
    idx = (selected_date.weekday() + 1) % 7
    start_of_week = selected_date - timedelta(days=idx)

    week_days = []
    for i in range(7):
        day = start_of_week + timedelta(days=i)
        week_days.append({
            'date_obj': day,
            'day_name': day.strftime("%a")[0],  # S, M, T...
            'day_num': day.day,
            'full_str': day.strftime("%Y-%m-%d"),
            'is_selected': (day == selected_date)
        })

    # 3. Fetch & Filter Data
    df = fetch_and_process_data()

    if not df.empty:
        filtered_df = df[df['date'] == selected_date]
    else:
        filtered_df = pd.DataFrame()

    # 4. Metrics
    matrix = get_matrix_stats(filtered_df)

    tag_labels = []
    tag_data = []
    if not filtered_df.empty:
        tag_counts = filtered_df.groupby('tag')['duration'].sum().sort_values(ascending=False)
        tag_labels = tag_counts.index.tolist()
        tag_data = [round(x / 60, 1) for x in tag_counts.values]

    return render_template('dashboard.html',
                           matrix=matrix,
                           tags={'labels': tag_labels, 'data': tag_data},
                           week_days=week_days,
                           current_month=selected_date.strftime("%B %Y"),
                           selected_date=selected_date)


if __name__ == '__main__':
    app.run(debug=True, port=5001)