# START HERE

---

## Mac

1. Double-click **`start.command`**
2. Open **http://localhost:8000/api/v1/admin/ui** in your browser
3. Upload the Excel availability file
4. Fill in the schedule details and click **Generate Schedule**
5. Click **Download Schedule Workbook**

> First time only — if a security dialog appears saying the file "cannot be opened because it is from an unidentified developer":  
> Right-click `start.command` → **Open** → **Open** again.

---

## Windows

1. Double-click **`start_windows.bat`**
2. Open **http://localhost:8000/api/v1/admin/ui** in your browser
3. Upload the Excel availability file
4. Fill in the schedule details and click **Generate Schedule**
5. Click **Download Schedule Workbook**

---

## First-time setup (if the startup script fails)

The `.venv` virtual environment must be created once on each machine.  
See **README.md → Setup** for step-by-step instructions.

**Python 3.11 is required.** OR-Tools (the scheduling solver) may not install correctly on Python 3.13+.
