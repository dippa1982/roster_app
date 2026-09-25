# Simon's Roster Dashboard

A Flask app for your Ocado/Morrisons roster PDFs.

## What it does
- Uploads a new roster PDF.
- Extracts the roster period, employee details, shifts, start times and 10-hour shifts.
- Recognises the Ocado and Morrisons icons used in the Spoke Leeds roster.
- Shows total shifts and hours.
- Includes a full month-by-month calendar with Ocado/Morrisons/Holiday events.
- Calendar has previous/next month navigation and a Today button.
- Breaks hours down by Ocado and Morrisons.
- Shows holidays.
- Keeps previous uploaded rosters so you can switch back to an older roster.

## Run it

### Windows
1. Install Python 3.11+.
2. Open a terminal in this folder.
3. Create a virtual environment:

   `python -m venv .venv`

4. Activate it:

   `.venv\Scripts\activate`

5. Install packages:

   `pip install -r requirements.txt`

6. Start the app:

   `python app.py`

7. Open `http://127.0.0.1:5000`

### Mac/Linux

`python3 -m venv .venv`
`source .venv/bin/activate`
`pip install -r requirements.txt`
`python app.py`

Then open `http://127.0.0.1:5000`.

## Important
The parser is deliberately built around the Spoke Leeds roster format in the supplied PDF. If Ocado changes the roster template, the parser may need a small update.


### Added: manual shifts
Click any day in Calendar and choose Ocado/Morrisons, start time and hours to add an extra shift. Existing PDF upload/history functionality is unchanged.
