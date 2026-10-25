# Smart Attendance — top-right registration default login

This update targets **only** the orange **Register student** button at the **top-right of the admin dashboard**.

## What changes

1. Admin clicks the top-right Register student button.
2. Enter Student ID and Full name; camera captures 25 face samples.
3. Once capture completes, the application adds the student record (if needed), creates a **Student** account if no account for that student exists, and starts model retraining.
4. Newly created login credentials:
   - Username: **Student ID** (as entered)
   - Temporary password: **Changeme@123**
5. First time that account signs in, it must replace the default password before viewing attendance.

The **Students-page Register student** and **Manage users** workflows are unchanged and do not receive any automatic default password. If the student already has a login, the password is **never overwritten**.

## Install on macOS

1. Quit the running application.
2. Back up your existing `app.py` and `attendance.db`.
3. Replace only `app.py` with this package's version in your existing SmartAttendance folder.
4. Keep `attendance.db`, `student_faces/`, `face_model.yml`, and `face_labels.npz` untouched.
5. Use your existing virtual environment:

```bash
cd "$HOME/Documents/python programs/SmartAttendance"
source .venv/bin/activate
python app.py
```

## Notes

- Student IDs must satisfy the existing input validation.
- The account is created only after face capture completes. Cancelling capture does not create an account.
- If an existing username collides with the new student ID, the student record still saves and the app reports the conflict instead of changing an existing account.
- The default password is a known shared credential: distribute it privately and change it immediately on first login. For production, use unique temporary credentials.
- New schema: `password_change_required` table, created automatically by `init_db()`.
- Tested: syntax compilation, simulated database account creation, repeat-registration protection, authentication and required-change flag. Actual camera GUI must be tested on macOS.
