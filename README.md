# Babson Library Student Worker Scheduler — MVP

A constraint-aware scheduling backend that imports the library's Excel availability matrix, runs a solver, and produces an annotated Excel workbook ready for staff review.

---

## Contents

```
Babson-Library-Scheduler-MVP/
  babson-scheduler/       Backend source code (FastAPI + OR-Tools solver)
  sample-input/           De-identified Spring 2026 availability input
  sample-output/          Sample generated schedule workbook
  README.md               This file
  START_HERE.md           Shortest possible run instructions
  start.command           Mac one-click launcher
  start_windows.bat       Windows one-click launcher
```

---

## What it does

1. You upload the library's Excel availability matrix through a browser form.
2. The solver reads student preferences, seniority, and hour targets.
3. It generates a recurring weekly schedule for the semester.
4. You download a multi-sheet Excel workbook: Dashboard, Schedule Grid, Schedule List, Student Summary, Violations, Technical Details, and Run Info.

---

## MVP Scope

### Capabilities

- Imports the library's existing Excel availability matrix: preferred / available / cannot-work
- Generates a recurring weekly semester schedule in one click
- Enforces no overlapping shift assignments for the same student
- Enforces a maximum of 6 consecutive hours per student
- Respects preferred / available / cannot-work availability values
- Uses seniority dates and target weekly hours as soft objectives
- Prioritises hard shifts such as opening and late-night windows
- Exports: Dashboard, Schedule Grid, Schedule List, Student Summary, Violations, Technical Details, and Run Info

### Limitations

- Does not automatically handle holidays, semester breaks, exam periods, one-off absences, or late schedule changes
- Does not include authentication or login. Anyone with the local URL can access the admin page while the server is running
- Free-text comments in the availability file are surfaced for staff review but are not automatically interpreted as constraints
- Staff should review the final workbook before distributing it to student workers
- This is an MVP/prototype, not a hardened production deployment

---

## Requirements

- **Python 3.11 is recommended**. Python 3.12 may also work, but Python 3.13 can cause OR-Tools installation issues.
- The `.venv` virtual environment must be created once per machine.
- Do not type commands with square brackets. For example, type `python -m pip install -r requirements.txt`, not `[python -m pip install -r requirements.txt]`.

---

## Quick Start — Mac

### Step 1: Unzip the folder

Move the folder somewhere easy to find, such as the Desktop:

```
/Users/YOUR_USERNAME/Desktop/Babson-Library-Scheduler-MVP
```

### Step 2: Open Terminal in the correct folder

Run this, replacing `YOUR_USERNAME` if needed:

```bash
cd "/Users/YOUR_USERNAME/Desktop/Babson-Library-Scheduler-MVP/babson-scheduler"
```

If the folder is on your Desktop and your Terminal is already using your account, this shorter version also works:

```bash
cd ~/Desktop/Babson-Library-Scheduler-MVP/babson-scheduler
```

You must be inside the `babson-scheduler` folder before running setup commands. This is the folder that contains `requirements.txt`.

To confirm you are in the right folder, run:

```bash
ls
```

You should see files/folders such as:

```
app
requirements.txt
README.md
```

### Step 3: Create the virtual environment one time

First check which Python versions are installed:

```bash
python3.11 --version
python3.12 --version
python3 --version
```

If Python 3.11 exists, use:

```bash
python3.11 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
```

If `python3.11` says `command not found`, try Python 3.12:

```bash
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
```

If Python 3.12 fails during `requirements.txt`, install Python 3.11 from https://www.python.org/downloads/ and repeat the Python 3.11 setup.

### Step 4: Run the server

After setup, run:

```bash
source .venv/bin/activate
python -m uvicorn app.main:app --reload --port 8000
```

Leave this Terminal window open while using the app.

### Step 5: Open the admin UI

Open this in your browser:

```
http://localhost:8000/api/v1/admin/ui
```

---

## Mac One-Click Launcher

After the one-time setup is complete, you can try double-clicking:

```
start.command
```

If macOS says **"start.command is damaged and can't be opened"**, this is usually Gatekeeper blocking the script because it was downloaded or sent through WhatsApp.

Do **not** click **Move to Trash**. Click **Cancel**.

Then run this in Terminal:

```bash
cd ~/Desktop/Babson-Library-Scheduler-MVP
chmod +x start.command
xattr -d com.apple.quarantine start.command
```

If it still fails, remove quarantine from the whole folder:

```bash
cd ~/Desktop
xattr -dr com.apple.quarantine Babson-Library-Scheduler-MVP
```

Then double-click `start.command` again.

If the one-click launcher still does not work, use the manual Terminal commands in the Mac Quick Start section.

---

## Quick Start — Windows

### Step 1: Unzip the folder

Place the folder somewhere easy to find, such as the Desktop:

```
C:\Users\YOUR_USERNAME\Desktop\Babson-Library-Scheduler-MVP
```

### Step 2: Open Command Prompt in the correct folder

Open **Command Prompt** and run:

```cmd
cd %USERPROFILE%\Desktop\Babson-Library-Scheduler-MVP\babson-scheduler
```

You must be inside the `babson-scheduler` folder before running setup commands. This is the folder that contains `requirements.txt`.

To confirm you are in the right folder, run:

```cmd
dir
```

You should see files/folders such as:

```
app
requirements.txt
README.md
```

### Step 3: Install Python 3.11 if needed

Download Python 3.11 from https://www.python.org/downloads/.

During installation, check:

```
Add Python to PATH
```

Verify in Command Prompt:

```cmd
py -3.11 --version
```

### Step 4: Create the virtual environment one time

```cmd
py -3.11 -m venv .venv
.venv\Scripts\activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
```

This takes 1–3 minutes. You only need to do it once.

### Step 5: Run the server

After setup, run:

```cmd
.venv\Scripts\activate
python -m uvicorn app.main:app --reload --port 8000
```

Leave this Command Prompt window open while using the app.

### Step 6: Open the admin UI

Open this in your browser:

```
http://localhost:8000/api/v1/admin/ui
```

---

## Windows One-Click Launcher

After the one-time setup is complete, you can try double-clicking:

```
start_windows.bat
```

If Windows blocks it, right-click the file and choose **Run as administrator**, or use the manual Command Prompt commands above.

---

## Using the scheduler

1. Open **http://localhost:8000/api/v1/admin/ui**
2. Select **Schedule Type**: use **Recurring Semester Schedule** for a full term
3. Enter the **Representative Week Start**. This must be a Monday date, for example `2026-04-27`
4. For term mode, optionally enter **Term Start** and **Term End** dates
5. Upload the Excel availability file
6. Click **Generate Schedule**
7. Click **Download Schedule Workbook** when it appears
8. Review the workbook before distributing the schedule. Start with the **Dashboard**, **Schedule Grid**, **Student Summary**, and **Violations** sheets

---

## Troubleshooting

### `cd: no such file or directory`

You are using the wrong folder path.

Do not type placeholder paths like:

```bash
cd path/to/Babson-Library-Scheduler-MVP/babson-scheduler
```

Use the actual location of the folder. For example, on Mac if the folder is on the Desktop:

```bash
cd ~/Desktop/Babson-Library-Scheduler-MVP/babson-scheduler
```

On Windows:

```cmd
cd %USERPROFILE%\Desktop\Babson-Library-Scheduler-MVP\babson-scheduler
```

### `requirements.txt` not found

You are probably inside the outer folder instead of the backend folder.

Wrong folder:

```
Babson-Library-Scheduler-MVP/
```

Correct folder:

```
Babson-Library-Scheduler-MVP/babson-scheduler/
```

Run:

```bash
cd ~/Desktop/Babson-Library-Scheduler-MVP/babson-scheduler
ls
```

You should see `requirements.txt`.

### `python3.11: command not found`

Python 3.11 is not installed.

On Mac, check whether Python 3.12 exists:

```bash
python3.12 --version
```

If it exists, you can try:

```bash
python3.12 -m venv .venv
```

If dependency installation fails, install Python 3.11 from https://www.python.org/downloads/.

### `source: no such file or directory: .venv/bin/activate`

The virtual environment was not created successfully. Create it first:

```bash
python3.11 -m venv .venv
source .venv/bin/activate
```

If `python3.11` is not available, see the previous section.

### `No module named uvicorn`

The dependencies were not installed in the active virtual environment.

Run this from the `babson-scheduler` folder:

```bash
source .venv/bin/activate
python -m pip install -r requirements.txt
python -m uvicorn app.main:app --reload --port 8000
```

### Port 8000 is already in use

**Mac:**

```bash
lsof -i :8000
kill -9 <PID>
```

Replace `<PID>` with the number shown by the first command.

**Windows:**

```cmd
netstat -ano | findstr :8000
taskkill /PID <PID_NUMBER> /F
```

Replace `<PID_NUMBER>` with the number shown by the first command.

### App starts but page does not load

Check the health endpoint first:

```
http://localhost:8000/health
```

Then try the admin UI:

```
http://localhost:8000/api/v1/admin/ui
```

If the Terminal/Command Prompt window shows an error, copy the error text. It identifies the problem.

### Upload fails

- Confirm the file is `.xlsx`, not `.xls` or `.csv`
- Confirm the file matches the expected availability matrix format: rows = students, columns = time windows, cells = preferred / available / cannot work
- See `sample-input/Deidentified_Spring_2026_Availability_Input.xlsx` for the expected layout

### OR-Tools installation fails

This usually means the Python version is unsupported. Confirm:

```bash
python --version
```

Use Python 3.11 if possible. Python 3.13 is not recommended for this project.

---

## API documentation — developer use only

The full REST API is self-documented at:

```
http://localhost:8000/docs
```

The client does not need Swagger for normal use. Use the admin UI instead:

```
http://localhost:8000/api/v1/admin/ui
```

---

## Stopping the server

Press **Ctrl+C** in the Terminal / Command Prompt window where the server is running.
