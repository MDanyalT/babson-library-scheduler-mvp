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
- Imports the library's existing Excel availability matrix (preferred / available / cannot-work)
- Generates a recurring weekly semester schedule in one click
- Enforces no overlapping shift assignments for the same student
- Enforces a maximum of 6 consecutive hours per student
- Respects preferred / available / cannot-work availability values
- Uses seniority dates and target weekly hours as soft objectives
- Prioritises hard shifts (opening 07:30–09:00 and late-night 23:00–02:00 windows)
- Exports: Dashboard, Schedule Grid, Schedule List, Student Summary, Violations, Technical Details, Run Info

### Limitations
- Does not automatically handle holidays, semester breaks, exam periods, one-off absences, or late schedule changes
- Does not include authentication or login — anyone with the URL can access the API
- Free-text comments in the availability file are surfaced for staff review but are not automatically interpreted as constraints
- Staff should review the final workbook before distributing it to student workers
- This is an MVP/prototype, not a hardened production deployment

---

## Requirements

- **Python 3.11** (recommended — OR-Tools may not install correctly on Python 3.13+)
- The `.venv` virtual environment must be created once per machine (see Setup below)

---

## Setup — Mac

### 1. Install Python 3.11

Download from https://www.python.org/downloads/ and install.  
Verify in Terminal:

```bash
python3.11 --version
```

### 2. Create the virtual environment (one time only)

Open **Terminal**, navigate to the `babson-scheduler` folder, and run:

```bash
cd path/to/Babson-Library-Scheduler-MVP/babson-scheduler
python3.11 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
```

This takes 1–3 minutes. You only need to do it once.

### 3. Run the server

After setup, you can use the one-click launcher:

```
Double-click start.command
```

Or run manually in Terminal:

```bash
cd path/to/Babson-Library-Scheduler-MVP/babson-scheduler
source .venv/bin/activate
python -m uvicorn app.main:app --reload --port 8000
```

### 4. Open the admin UI

```
http://localhost:8000/api/v1/admin/ui
```

---

## Setup — Windows

### 1. Install Python 3.11

Download from https://www.python.org/downloads/ and install.  
During installation, check **"Add Python to PATH"**.  
Verify in Command Prompt:

```cmd
py -3.11 --version
```

### 2. Create the virtual environment (one time only)

Open **Command Prompt**, navigate to the `babson-scheduler` folder, and run:

```cmd
cd path\to\Babson-Library-Scheduler-MVP\babson-scheduler
py -3.11 -m venv .venv
.venv\Scripts\activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
```

This takes 1–3 minutes. You only need to do it once.

### 3. Run the server

After setup, you can use the one-click launcher:

```
Double-click start_windows.bat
```

Or run manually in Command Prompt:

```cmd
cd path\to\Babson-Library-Scheduler-MVP\babson-scheduler
.venv\Scripts\activate
python -m uvicorn app.main:app --reload --port 8000
```

### 4. Open the admin UI

```
http://localhost:8000/api/v1/admin/ui
```

---

## Using the scheduler

1. Open **http://localhost:8000/api/v1/admin/ui**
2. Select **Schedule Type** (Recurring Semester Schedule for a full term)
3. Enter the **Representative Week Start** (Monday date, e.g. `2026-09-07`)
4. For term mode, enter **Term Start** and **Term End** dates
5. Upload the Excel availability file
6. Click **Generate Schedule**
7. Click **Download Schedule Workbook** when it appears
8. Review the workbook — check the **Violations** sheet before distributing

---

## Troubleshooting

### Port 8000 is already in use

**Mac:**
```bash
lsof -i :8000
kill -9 <PID>
```

**Windows:**
```cmd
netstat -ano | findstr :8000
taskkill /PID <PID_NUMBER> /F
```

### Virtual environment missing

The startup scripts require `.venv` to already exist. If you see an activation error:

**Mac:**
```bash
cd path/to/Babson-Library-Scheduler-MVP/babson-scheduler
python3.11 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
```

**Windows:**
```cmd
cd path\to\Babson-Library-Scheduler-MVP\babson-scheduler
py -3.11 -m venv .venv
.venv\Scripts\activate
python -m pip install -r requirements.txt
```

### Dependencies not installed

```bash
python -m pip install -r requirements.txt
```

### App starts but page does not load

Check the health endpoint first:

```
http://localhost:8000/health
```

Then try the admin UI:

```
http://localhost:8000/api/v1/admin/ui
```

If the Terminal/Command Prompt window shows an error, copy the error text — it will identify the problem.

### Upload fails

- Confirm the file is `.xlsx` (not `.xls` or `.csv`)
- Confirm the file matches the expected availability matrix format (rows = students, columns = time windows, cells = preferred / available / cannot work)
- See `sample-input/Deidentified_Spring_2026_Availability_Input.xlsx` for the expected layout

### OR-Tools installation fails

This usually means Python is not version 3.11. Confirm:

```bash
python --version
```

If it shows 3.12 or 3.13, uninstall and install Python 3.11, then recreate the virtual environment.

---

## API documentation (Swagger UI)

The full REST API is self-documented at:

```
http://localhost:8000/docs
```

This can be used for IBM Orchestrate integration or manual API testing.

---

## Stopping the server

Press **Ctrl+C** in the Terminal / Command Prompt window where the server is running.
