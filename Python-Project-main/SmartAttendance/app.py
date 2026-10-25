"""Smart Attendance Pro - macOS-friendly Tkinter + OpenCV LBPH demonstration."""
import csv
import hashlib
import hmac
import os
import re
import secrets
import sqlite3
import threading
import time
import tkinter as tk
from collections import Counter, defaultdict
from datetime import date, datetime
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

import cv2
import numpy as np
from PIL import Image, ImageTk

ROOT = Path(__file__).resolve().parent
DB = ROOT / 'attendance.db'
FACES = ROOT / 'student_faces'
MODEL = ROOT / 'face_model.yml'
LABELS = ROOT / 'face_labels.npz'  # legacy-compatible mapping
FACES.mkdir(exist_ok=True)
SAMPLES = 25
THRESHOLD = 60.0  # LBPH distance, not a percentage or probability
STABLE_FRAMES = 5
FRAME_DELAY_MS = 65



# Clean native-Tk modern theme; no extra UI framework is required.
BG = '#0B101B'
WHITE = '#161F30'   # panels
NAVY = '#101827'    # sidebar
NAVY2 = '#1E293B'
TEXT = '#F5F7FC'
MUTED = '#AAB7CA'
ACCENT = '#FF8A24'
ACCENT_LIGHT = '#402919'
BORDER = '#344157'
GREEN = '#49D5A5'


def apply_theme(root):
    root.configure(bg=BG)
    s = ttk.Style(root)
    s.theme_use('clam')
    s.configure('.', font=('Helvetica Neue', 11), background=BG, foreground=TEXT)
    s.configure('TFrame', background=BG)
    s.configure('TLabel', background=BG, foreground=TEXT)
    s.configure('TButton', padding=(13, 10), font=('Helvetica Neue', 10, 'bold'),
                background='#293447', foreground=TEXT, borderwidth=0, relief='flat')
    s.map('TButton', background=[('active', '#38465D')], foreground=[('active', TEXT)])
    s.configure('Primary.TButton', background=ACCENT, foreground='#1C1109', borderwidth=0)
    s.map('Primary.TButton', background=[('active', '#FFAA55')], foreground=[('active', '#1C1109')])
    s.configure('Danger.TButton', background='#672735', foreground='#FFFFFF', borderwidth=0)
    s.map('Danger.TButton', background=[('active', '#913C4F')], foreground=[('active', '#FFFFFF')])
    s.configure('TEntry', fieldbackground='#202C40', foreground='#FFFFFF',
                insertcolor='#FFFFFF', bordercolor='#344157', lightcolor='#344157', darkcolor='#344157', borderwidth=0, padding=9, relief='flat')
    s.map('TEntry', fieldbackground=[('focus', '#24344C')], foreground=[('focus', TEXT)])
    s.configure('Modern.Treeview', background=WHITE, fieldbackground=WHITE,
                foreground=TEXT, rowheight=38, borderwidth=0, relief='flat',
                lightcolor=WHITE, darkcolor=WHITE, bordercolor=WHITE,
                font=('Helvetica Neue', 11))
    # A borderless Treeview layout prevents the native macOS/clam white frame.
    s.layout('Modern.Treeview', [('Treeview.padding', {'sticky': 'nsew', 'children':
        [('Treeview.treearea', {'sticky': 'nsew'})]})])
    s.configure('Modern.Treeview.Heading', background='#263348', foreground='#FFFFFF',
                padding=(10, 12), relief='flat', borderwidth=0,
                bordercolor='#263348', lightcolor='#263348', darkcolor='#263348',
                font=('Helvetica Neue', 11, 'bold'))
    s.map('Modern.Treeview', background=[('selected', '#B35A13')],
          foreground=[('selected', '#FFFFFF')])
    s.configure('Chart.TRadiobutton', background=WHITE, foreground=TEXT, font=('Helvetica Neue', 10, 'bold'))
    s.map('Chart.TRadiobutton', background=[('active', WHITE)], foreground=[('active', ACCENT)])
    s.configure('Modern.TNotebook', background=BG, borderwidth=0, relief='flat',
                bordercolor=BG, lightcolor=BG, darkcolor=BG, tabmargins=0)
    # macOS/clam can paint a thin off-white client border even with borderwidth=0.
    # Remove the notebook's native frame element while keeping its child pages.
    s.layout('Modern.TNotebook', [('Notebook.client', {'sticky': 'nswe'})])
    # Hide the ugly native tabs: the custom pop-out sidebar owns navigation.
    s.layout('Modern.TNotebook.Tab', [])
    s.configure('Modern.TNotebook.Tab', padding=0)


class SidebarItem(tk.Frame):
    """Fully painted Tk controls; macOS native Button overrides are avoided."""
    def __init__(self, parent, text, command, *, kind='nav', active=False):
        self.kind = kind
        self.active = active
        self.hover = False
        self.command = command
        self.title = text
        super().__init__(parent, bg=NAVY, padx=5, pady=3, cursor='hand2')
        self.indicator = tk.Frame(self, width=5, bg=NAVY)
        self.indicator.pack(side='left', fill='y')
        self.text_widget = tk.Label(self, text=text, anchor='w', padx=16, pady=12,
                                    font=('Helvetica Neue', 11, 'bold'), cursor='hand2')
        self.text_widget.pack(side='left', fill='both', expand=True)
        for widget in (self, self.indicator, self.text_widget):
            widget.bind('<Button-1>', lambda _event: self.command())
            widget.bind('<Enter>', self._enter)
            widget.bind('<Leave>', self._leave)
        self.paint()

    def _enter(self, _event):
        self.hover = True
        self.paint()

    def _leave(self, _event):
        self.after(10, self._check_leave)

    def _check_leave(self):
        x, y = self.winfo_pointerxy()
        rx, ry = self.winfo_rootx(), self.winfo_rooty()
        self.hover = rx <= x < rx + self.winfo_width() and ry <= y < ry + self.winfo_height()
        self.paint()

    def set_active(self, active):
        self.active = active
        self.paint()

    def paint(self):
        if self.active:
            fill, foreground, bar = ACCENT, '#17110B', '#FFD2A0'
        elif self.kind == 'danger':
            fill, foreground, bar = ('#873349' if self.hover else '#4A2634'), '#FFFFFF', NAVY
        elif self.kind == 'action':
            fill, foreground, bar = ('#A35418' if self.hover else '#49321F'), '#FFFFFF', NAVY
        else:
            fill, foreground, bar = ('#344157' if self.hover else '#1B273A'), '#F5F7FC', NAVY
        self.indicator.configure(bg=bar)
        self.text_widget.configure(bg=fill, fg=foreground)


def conn():
    db = sqlite3.connect(DB, timeout=15)
    db.execute('PRAGMA foreign_keys=ON')
    return db


def init_db():
    with conn() as db:
        db.execute('''CREATE TABLE IF NOT EXISTS students (
            student_id TEXT PRIMARY KEY, name TEXT NOT NULL, created_at TEXT NOT NULL)''')
        db.execute('''CREATE TABLE IF NOT EXISTS attendance (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            student_id TEXT NOT NULL, attendance_date TEXT NOT NULL,
            attendance_time TEXT NOT NULL,
            UNIQUE(student_id, attendance_date),
            FOREIGN KEY(student_id) REFERENCES students(student_id))''')
        db.execute('''CREATE TABLE IF NOT EXISTS admin_auth (
            username TEXT PRIMARY KEY, salt TEXT NOT NULL, hash TEXT NOT NULL,
            iterations INTEGER NOT NULL)''')
        db.execute('''CREATE TABLE IF NOT EXISTS user_auth (
            username TEXT PRIMARY KEY, student_id TEXT NOT NULL UNIQUE,
            salt TEXT NOT NULL, hash TEXT NOT NULL, iterations INTEGER NOT NULL,
            FOREIGN KEY(student_id) REFERENCES students(student_id) ON DELETE CASCADE)''')
        db.execute('''CREATE TABLE IF NOT EXISTS role_student_links (
            username TEXT PRIMARY KEY, student_id TEXT NOT NULL UNIQUE,
            FOREIGN KEY(username) REFERENCES admin_auth(username) ON DELETE CASCADE,
            FOREIGN KEY(student_id) REFERENCES students(student_id) ON DELETE CASCADE)''')
        db.execute('''CREATE TABLE IF NOT EXISTS signup_invites (
            student_id TEXT PRIMARY KEY, token_hash TEXT NOT NULL,
            created_at TEXT NOT NULL,
            FOREIGN KEY(student_id) REFERENCES students(student_id) ON DELETE CASCADE)''')
        # New sign-ups may introduce students who are not yet in the students table.
        # Migrate the earlier foreign-key constrained request table without losing requests.
        db.execute('''CREATE TABLE IF NOT EXISTS signup_requests (
            student_id TEXT PRIMARY KEY, username TEXT NOT NULL UNIQUE,
            student_name TEXT NOT NULL DEFAULT '',
            salt TEXT NOT NULL, hash TEXT NOT NULL, iterations INTEGER NOT NULL,
            requested_at TEXT NOT NULL)''')
        cols = {row[1] for row in db.execute('PRAGMA table_info(signup_requests)')}
        if 'student_name' not in cols:
            db.execute("ALTER TABLE signup_requests ADD COLUMN student_name TEXT NOT NULL DEFAULT ''")
        if db.execute('PRAGMA foreign_key_list(signup_requests)').fetchall():
            db.execute('''CREATE TABLE signup_requests_migrated (
                student_id TEXT PRIMARY KEY, username TEXT NOT NULL UNIQUE,
                student_name TEXT NOT NULL DEFAULT '',
                salt TEXT NOT NULL, hash TEXT NOT NULL, iterations INTEGER NOT NULL,
                requested_at TEXT NOT NULL)''')
            db.execute('''INSERT INTO signup_requests_migrated
                (student_id,username,student_name,salt,hash,iterations,requested_at)
                SELECT r.student_id,r.username,COALESCE(s.name,r.student_name),
                       r.salt,r.hash,r.iterations,r.requested_at
                FROM signup_requests r LEFT JOIN students s ON s.student_id=r.student_id''')
            db.execute('DROP TABLE signup_requests')
            db.execute('ALTER TABLE signup_requests_migrated RENAME TO signup_requests')
        db.execute('''CREATE TABLE IF NOT EXISTS app_settings (
            key TEXT PRIMARY KEY, value TEXT NOT NULL)''')
        db.execute('''CREATE TABLE IF NOT EXISTS password_change_required (
            username TEXT PRIMARY KEY,
            FOREIGN KEY(username) REFERENCES user_auth(username) ON DELETE CASCADE)''')


def hash_password(password, salt=None, iterations=360_000):
    salt = salt or secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac('sha256', password.encode('utf-8'), salt, iterations)
    return salt.hex(), digest.hex(), iterations


def authenticate(user, password):
    with conn() as db:
        row = db.execute('SELECT salt,hash,iterations FROM admin_auth WHERE username=?', (user,)).fetchone()
    if not row:
        return False
    salt, expected, iterations = row
    actual = hashlib.pbkdf2_hmac('sha256', password.encode(), bytes.fromhex(salt), iterations)
    return hmac.compare_digest(actual, bytes.fromhex(expected))


def authenticate_account(username, password):
    # Existing administrator accounts remain compatible.
    if authenticate(username, password):
        return ('admin', None)
    with conn() as db:
        row = db.execute('SELECT student_id,salt,hash,iterations FROM user_auth WHERE username=?',
                         (username,)).fetchone()
    if not row:
        return None
    student_id, salt, expected, iterations = row
    actual = hashlib.pbkdf2_hmac('sha256', password.encode('utf-8'),
                                bytes.fromhex(salt), iterations)
    if hmac.compare_digest(actual, bytes.fromhex(expected)):
        return ('user', student_id)
    return None


DEFAULT_ADMIN_REGISTER_PASSWORD = 'Changeme@123'


def create_top_right_student_login(student_id):
    """Create a student login only for the completed top-right enrollment.

    Never overwrite any existing account/password; callers must be admins.
    """
    with conn() as db:
        db.execute('BEGIN IMMEDIATE')
        if not db.execute('SELECT 1 FROM students WHERE student_id=?', (student_id,)).fetchone():
            raise ValueError('Student registration has not completed.')
        existing = db.execute('SELECT username FROM user_auth WHERE student_id=?',
                              (student_id,)).fetchone()
        if existing:
            return existing[0], False
        if db.execute('SELECT 1 FROM role_student_links WHERE student_id=?',
                      (student_id,)).fetchone():
            raise ValueError('This student already has an administrator-linked account.')
        # Student ID is the initial username. Never shadow a current admin account.
        if db.execute('SELECT 1 FROM admin_auth WHERE username=?', (student_id,)).fetchone():
            raise ValueError('Student ID conflicts with an administrator username.')
        salt, digest, iterations = hash_password(DEFAULT_ADMIN_REGISTER_PASSWORD)
        db.execute("""INSERT INTO user_auth (username,student_id,salt,hash,iterations)
                      VALUES (?,?,?,?,?)""", (student_id, student_id, salt, digest, iterations))
        db.execute('INSERT INTO password_change_required(username) VALUES (?)', (student_id,))
    return student_id, True


def list_accounts():
    """Show account roles without disclosing password hashes."""
    with conn() as db:
        return db.execute("""SELECT username, 'Administrator', NULL
                FROM admin_auth
                UNION ALL
                SELECT username, 'Student', student_id FROM user_auth
                ORDER BY username""").fetchall()


def change_account_role(actor_username, target_username, target_role, student_id=None):
    """Change role transactionally. Call ONLY from an authenticated admin action.

    The actor is checked against admin_auth again to enforce authorization at
    the data-operation layer, not merely by hiding a button in the UI.
    """
    if target_role not in ('admin', 'user'):
        raise ValueError('Invalid target role.')
    with conn() as db:
        db.execute('BEGIN IMMEDIATE')
        if not db.execute('SELECT 1 FROM admin_auth WHERE username=?', (actor_username,)).fetchone():
            raise PermissionError('Administrator access is required.')
        if actor_username == target_username:
            raise ValueError('You cannot change your own role while signed in.')
        admin = db.execute('SELECT salt,hash,iterations FROM admin_auth WHERE username=?',
                           (target_username,)).fetchone()
        student = db.execute('SELECT student_id,salt,hash,iterations FROM user_auth WHERE username=?',
                             (target_username,)).fetchone()
        if target_role == 'admin':
            if admin:
                raise ValueError('This account is already an administrator.')
            if not student:
                raise ValueError('Student account not found.')
            sid, salt, digest, iters = student
            db.execute('DELETE FROM user_auth WHERE username=?', (target_username,))
            db.execute('INSERT INTO admin_auth(username,salt,hash,iterations) VALUES(?,?,?,?)',
                       (target_username,salt,digest,iters))
            db.execute('INSERT INTO role_student_links(username,student_id) VALUES(?,?)',
                       (target_username,sid))
        else:
            if student:
                raise ValueError('This account is already a student.')
            if not admin:
                raise ValueError('Administrator account not found.')
            if db.execute('SELECT COUNT(*) FROM admin_auth').fetchone()[0] <= 1:
                raise ValueError('Cannot demote the last administrator.')
            link = db.execute('SELECT student_id FROM role_student_links WHERE username=?',
                              (target_username,)).fetchone()
            sid = link[0] if link else student_id
            if not sid or not db.execute('SELECT 1 FROM students WHERE student_id=?', (sid,)).fetchone():
                raise ValueError('Choose a registered student ID to link with this account.')
            if db.execute('SELECT 1 FROM user_auth WHERE student_id=?', (sid,)).fetchone():
                raise ValueError('This student already has a student login.')
            if db.execute('SELECT 1 FROM role_student_links WHERE student_id=? AND username<>?',
                          (sid, target_username)).fetchone():
                raise ValueError('This student is linked to another administrator.')
            salt, digest, iters = admin
            db.execute('DELETE FROM role_student_links WHERE username=?', (target_username,))
            db.execute('DELETE FROM admin_auth WHERE username=?', (target_username,))
            db.execute('INSERT INTO user_auth(username,student_id,salt,hash,iterations) VALUES(?,?,?,?,?)',
                       (target_username,sid,salt,digest,iters))


def get_last_username():
    """Remember only the last successful username, never a password or session."""
    with conn() as db:
        row = db.execute("SELECT value FROM app_settings WHERE key='last_username'").fetchone()
    return row[0] if row else ''


def save_last_username(username):
    with conn() as db:
        db.execute("""INSERT INTO app_settings(key, value) VALUES('last_username', ?)
            ON CONFLICT(key) DO UPDATE SET value=excluded.value""", (username,))


def valid_student_id(value):
    return bool(re.fullmatch(r'[A-Za-z0-9_-]{1,40}', value))


def face_detector():
    path = Path(cv2.data.haarcascades) / 'haarcascade_frontalface_default.xml'
    if not path.is_file():
        raise RuntimeError(f'OpenCV face detector is missing: {path}')
    detector = cv2.CascadeClassifier(str(path))
    if detector.empty():
        raise RuntimeError('OpenCV cannot load the face detection model.')
    return detector


def face_crops(gray, detector):
    boxes = detector.detectMultiScale(gray, scaleFactor=1.15, minNeighbors=6, minSize=(90, 90))
    return sorted(boxes, key=lambda b: b[2]*b[3], reverse=True)


def normalize(gray, rect):
    x,y,w,h = map(int, rect)
    face = gray[y:y+h, x:x+w]
    return cv2.equalizeHist(cv2.resize(face, (200, 200)))


def train_model():
    with conn() as db:
        students = db.execute('SELECT student_id,name FROM students ORDER BY student_id').fetchall()
    images, labels, mapping = [], [], []
    for sid, name in students:
        folder = FACES / sid
        if not folder.is_dir():
            continue
        samples = []
        for path in sorted(folder.glob('*.jpg')):
            img = cv2.imread(str(path), cv2.IMREAD_GRAYSCALE)
            if img is not None:
                samples.append(cv2.equalizeHist(cv2.resize(img, (200,200))))
        if samples:
            label = len(mapping)
            mapping.append(f'{label}|{sid}|{name}')
            images.extend(samples)
            labels.extend([label]*len(samples))
    if not images:
        MODEL.unlink(missing_ok=True)
        LABELS.unlink(missing_ok=True)
        return 'No registered face samples remain. Model cleared.'
    model = cv2.face.LBPHFaceRecognizer_create()
    model.train(images, np.array(labels, dtype=np.int32))
    model.write(str(MODEL))
    np.savez(str(LABELS), mapping=np.array(mapping, dtype=str))
    return f'Trained {len(mapping)} student(s), {len(images)} samples.'


def load_model():
    if not MODEL.is_file() or not LABELS.is_file():
        raise RuntimeError('Register a student and train the model first.')
    model = cv2.face.LBPHFaceRecognizer_create()
    model.read(str(MODEL))
    with np.load(str(LABELS), allow_pickle=False) as data:
        entries = data['mapping']
    mapping = {}
    for item in entries:
        label,sid,name = str(item).split('|', 2)
        mapping[int(label)] = (sid,name)
    return model,mapping


def mark_attendance(sid):
    stamp = datetime.now()
    with conn() as db:
        cursor = db.execute('''INSERT OR IGNORE INTO attendance
            (student_id,attendance_date,attendance_time) VALUES (?,?,?)''',
            (sid, stamp.strftime('%Y-%m-%d'), stamp.strftime('%H:%M:%S')))
    return cursor.rowcount == 1


class App:
    def __init__(self, root):
        self.root = root
        self.root.title('Smart Attendance Pro')
        screen_w, screen_h = self.root.winfo_screenwidth(), self.root.winfo_screenheight()
        self.root.geometry(f'{min(1240, max(760, screen_w - 90))}x{min(790, max(540, screen_h - 110))}')
        self.root.minsize(760,540)
        self.root.protocol('WM_DELETE_WINDOW', self.close)
        self.status = tk.StringVar(value='Welcome.')
        self.camera = None
        self.camera_dialog = None
        self.camera_label = None
        self.camera_job = None
        self.camera_mode = None
        self.samples = 0
        self.last_sample = 0
        self.hits = Counter()
        self.marked_this_session = set()
        self.camera_sid = None
        self.camera_name = None
        self.detector = None
        self.model = None
        self.mapping = {}
        self.logged_in = False
        self.current_user = None
        self.current_role = None
        self.current_student_id = None
        apply_theme(self.root)
        self.login_screen()

    def clear(self):
        for child in self.root.winfo_children():
            child.destroy()

    def label_entry(self, parent, title, *, secret=False):
        ttk.Label(parent, text=title).pack(anchor='w', pady=(10,3))
        ent = ttk.Entry(parent, width=36, show='●' if secret else '')
        ent.pack(fill='x')
        return ent

    def login_screen(self):
        """Always require authentication at startup and after logout."""
        self.clear()
        self.logged_in = False
        self.current_user = None
        self.current_role = None
        self.current_student_id = None
        self.root.unbind('<Configure>')
        for wheel_event in ('<MouseWheel>', '<Button-4>', '<Button-5>'):
            self.root.unbind_all(wheel_event)
        for wheel_event in ('<MouseWheel>', '<Button-4>', '<Button-5>'):
            self.root.unbind(wheel_event)
        self.root.configure(bg=BG)

        outer = tk.Frame(self.root, bg=BG, highlightthickness=0, bd=0)
        outer.pack(fill='both', expand=True)

        # Two-column layout collapses into a single centered login card for small windows.
        wrap = tk.Frame(outer, bg=BG, highlightthickness=0, bd=0)
        wrap.place(relx=.5, rely=.5, anchor='center')
        branding = tk.Frame(wrap, bg=BG, width=300)
        branding.pack(side='left', fill='both', padx=(0, 35))
        tk.Label(branding, text='◉', bg=BG, fg=ACCENT,
                 font=('Helvetica Neue', 42, 'bold')).pack(anchor='w', pady=(0, 20))
        tk.Label(branding, text='SMART\nATTENDANCE', justify='left', bg=BG,
                 fg=TEXT, font=('Helvetica Neue', 27, 'bold')).pack(anchor='w')
        tk.Label(branding, text='Secure campus access', bg=BG, fg=MUTED,
                 font=('Helvetica Neue', 13)).pack(anchor='w', pady=(12, 0))
        tk.Label(branding, text='Dark interface  •  Orange accents', bg=BG,
                 fg=MUTED, font=('Helvetica Neue', 10)).pack(anchor='w', pady=(8,0))

        card = tk.Frame(wrap, bg=WHITE, padx=29, pady=29, bd=0, relief='flat', highlightthickness=0)
        card.pack(side='left', fill='both')
        tk.Label(card, text='Welcome back', bg=WHITE, fg=TEXT,
                 font=('Helvetica Neue', 23, 'bold')).pack(anchor='w')
        tk.Label(card, text='Sign in to continue', bg=WHITE, fg=MUTED,
                 font=('Helvetica Neue', 11)).pack(anchor='w', pady=(6, 23))

        with conn() as db:
            has_admin = bool(db.execute('SELECT 1 FROM admin_auth LIMIT 1').fetchone())

        def field(title, *, secret=False):
            tk.Label(card, text=title, bg=WHITE, fg=TEXT,
                     font=('Helvetica Neue', 11, 'bold')).pack(anchor='w', pady=(13, 6))
            # A separate padded frame provides REAL text inset. Tk Entry ipadx
            # expands the widget but does not reliably pad its text on macOS.
            field_box = tk.Frame(card, bg='#263348', highlightthickness=1,
                                 highlightbackground='#4B5970', bd=0)
            field_box.pack(fill='x')
            ent = tk.Entry(field_box, bg='#263348', fg=TEXT,
                           insertbackground=TEXT, bd=0, relief='flat',
                           highlightthickness=0, justify='left',
                           font=('Helvetica Neue', 13), width=28,
                           show='•' if secret else '')
            ent.pack(fill='x', padx=14, pady=13)
            ent.bind('<FocusIn>', lambda e: field_box.configure(highlightbackground=ACCENT))
            ent.bind('<FocusOut>', lambda e: field_box.configure(highlightbackground='#4B5970'))
            return ent

        # Standard Tk buttons are rendered differently by macOS; a label is reliable.
        def action_button(text, command):
            btn = tk.Label(card, text=text, bg=ACCENT, fg='#19120D',
                           font=('Helvetica Neue', 12, 'bold'),
                           cursor='hand2', pady=14)
            btn.pack(fill='x', pady=(25, 0))
            btn.bind('<Button-1>', lambda event: command())
            btn.bind('<Enter>', lambda event: btn.configure(bg='#FFAC55'))
            btn.bind('<Leave>', lambda event: btn.configure(bg=ACCENT))
            return btn

        if not has_admin:
            tk.Label(card, text='FIRST RUN · SET UP ADMINISTRATOR', bg=WHITE,
                     fg=ACCENT, font=('Helvetica Neue', 10, 'bold')).pack(anchor='w', pady=(0, 6))
            user = field('Admin username')
            user.insert(0, 'admin')
            pw = field('Password (at least 10 characters)', secret=True)
            confirm = field('Confirm password', secret=True)
            feedback = tk.Label(card, text='', bg=WHITE, fg='#FFB2B2', wraplength=360)
            feedback.pack(anchor='w', pady=(9, 0))
            def create():
                u = user.get().strip()
                if not u or len(pw.get()) < 10 or pw.get() != confirm.get():
                    feedback.configure(text='Enter a username and matching passwords of 10+ characters.')
                    return
                salt, digest, iterations = hash_password(pw.get())
                try:
                    with conn() as db:
                        db.execute('INSERT INTO admin_auth VALUES (?,?,?,?)',
                                   (u, salt, digest, iterations))
                except sqlite3.IntegrityError:
                    feedback.configure(text='That admin username is already registered.')
                    return
                save_last_username(u)
                self.login_screen()
            action_button('Create administrator  →', create)
            confirm.bind('<Return>', lambda event: create())
            pw.focus_set()
        else:
            role = tk.StringVar(value='admin')
            # Animated canvas pill: avoids the abrupt flash of two Tk labels.
            toggle = tk.Canvas(card, height=51, bg='#263348', bd=0,
                               highlightthickness=0, cursor='hand2')
            toggle.pack(fill='x', pady=(0, 8))
            role_anim = {'x': 4.0, 'job': None, 'target': 'admin'}

            def paint_toggle():
                if not toggle.winfo_exists():
                    return
                w = max(toggle.winfo_width(), 260)
                h = 51
                half = w / 2
                toggle.delete('all')
                toggle.create_rectangle(0, 0, w, h, fill='#263348', outline='')
                toggle.create_rectangle(role_anim['x'], 4, role_anim['x'] + half - 8,
                                        h - 4, fill=ACCENT, outline='')
                toggle.create_text(half / 2, h / 2, text='Administrator',
                                   fill='#19120D' if role.get() == 'admin' else TEXT,
                                   font=('Helvetica Neue', 11, 'bold'))
                toggle.create_text(half + half / 2, h / 2, text='Student',
                                   fill='#19120D' if role.get() == 'user' else TEXT,
                                   font=('Helvetica Neue', 11, 'bold'))

            def animate_role(target):
                if not toggle.winfo_exists():
                    return
                half = max(toggle.winfo_width(), 260) / 2
                goal = 4 if target == 'admin' else half + 4
                role_anim['x'] += (goal - role_anim['x']) * .32
                paint_toggle()
                if abs(goal - role_anim['x']) >= .6:
                    role_anim['job'] = toggle.after(16, lambda: animate_role(target))
                else:
                    role_anim['x'] = goal
                    paint_toggle()
                    role_anim['job'] = None

            def choose_role(selected, animated=True):
                if role_anim['job'] is not None:
                    toggle.after_cancel(role_anim['job'])
                    role_anim['job'] = None
                role.set(selected)
                role_anim['target'] = selected
                if animated:
                    animate_role(selected)
                else:
                    role_anim['x'] = 4 if selected == 'admin' else max(toggle.winfo_width(), 260) / 2 + 4
                    paint_toggle()
                feedback.configure(text='')

            toggle.bind('<Button-1>', lambda event: choose_role(
                'admin' if event.x < toggle.winfo_width() / 2 else 'user'))
            toggle.bind('<Configure>', lambda event: (
                role_anim.update(x=4 if role.get() == 'admin' else event.width / 2 + 4),
                paint_toggle()))
            user = field('Username')
            user.insert(0, get_last_username())
            pw = field('Password', secret=True)
            feedback = tk.Label(card, text='', bg=WHITE, fg='#FFB2B2', wraplength=360,
                                font=('Helvetica Neue', 10))
            feedback.pack(anchor='w', pady=(9, 0))
            choose_role('admin', animated=False)
            def login():
                username = user.get().strip()
                account = authenticate_account(username, pw.get())
                if not account or account[0] != role.get():
                    feedback.configure(text='Incorrect credentials for the selected account type.')
                    pw.delete(0, 'end')
                    pw.focus_set()
                    return
                self.current_role, self.current_student_id = account
                self.current_user = username
                save_last_username(username)
                self.logged_in = True
                if self.current_role == 'admin':
                    self.main_screen()
                else:
                    with conn() as db:
                        needs_change = db.execute(
                            'SELECT 1 FROM password_change_required WHERE username=?',
                            (username,)).fetchone()
                    if needs_change:
                        self.required_student_password_change()
                    else:
                        self.user_screen()
            action_button('Sign in  →', login)
            signup_link = tk.Label(card, text='New student? Create an account', bg=WHITE,
                                   fg=ACCENT, font=('Helvetica Neue', 11, 'bold'),
                                   cursor='hand2', pady=12)
            signup_link.pack(anchor='center')
            signup_link.bind('<Button-1>', lambda _event: self.student_signup())
            pw.bind('<Return>', lambda event: login())
            pw.focus_set()

        def responsive(event=None):
            if not outer.winfo_exists():
                return
            narrow = outer.winfo_width() < 920
            if getattr(self, '_login_narrow', None) == narrow:
                return
            self._login_narrow = narrow
            branding.pack_forget()
            card.pack_forget()
            if narrow:
                branding.pack(side='top', anchor='w', fill='x', padx=(10, 10), pady=(0, 8))
                card.pack(side='top', fill='x', padx=(10, 10))
                # Retain access to the login controls in a short window.
                wrap.place_configure(rely=.02, anchor='n')
            else:
                branding.pack(side='left', fill='both', padx=(0, 35))
                card.pack(side='left', fill='both')
                wrap.place_configure(rely=.5, anchor='center')
        outer.bind('<Configure>', responsive)
        self.root.after_idle(responsive)

    def required_student_password_change(self):
        """Gate the first login until the known temporary password is replaced."""
        self.clear()
        self.root.configure(bg=BG)
        panel = tk.Frame(self.root, bg=WHITE, padx=30, pady=30)
        panel.place(relx=.5, rely=.5, anchor='center', relwidth=.65)
        tk.Label(panel, text='Set your private password', bg=WHITE, fg=TEXT,
                 font=('Helvetica Neue', 22, 'bold')).pack(anchor='w')
        tk.Label(panel, text='Your temporary password must be changed before accessing attendance.',
                 bg=WHITE, fg=MUTED, wraplength=520).pack(anchor='w', pady=(8, 18))
        old = self.label_entry(panel, 'Temporary password', secret=True)
        new = self.label_entry(panel, 'New password (10+ characters)', secret=True)
        confirm = self.label_entry(panel, 'Confirm new password', secret=True)
        feedback = tk.Label(panel, text='', bg=WHITE, fg='#FFB2B2')
        feedback.pack(anchor='w', pady=(10, 0))

        def save():
            if not authenticate_account(self.current_user, old.get()):
                feedback.configure(text='Incorrect temporary password.')
                return
            if len(new.get()) < 10 or new.get() != confirm.get():
                feedback.configure(text='New passwords must match and contain at least 10 characters.')
                return
            if new.get() == DEFAULT_ADMIN_REGISTER_PASSWORD:
                feedback.configure(text='Choose a password different from the temporary password.')
                return
            salt, digest, iterations = hash_password(new.get())
            with conn() as db:
                db.execute('UPDATE user_auth SET salt=?, hash=?, iterations=? WHERE username=?',
                           (salt, digest, iterations, self.current_user))
                db.execute('DELETE FROM password_change_required WHERE username=?',
                           (self.current_user,))
            self.user_screen()

        ttk.Button(panel, text='Change password and continue',
                   style='Primary.TButton', command=save).pack(fill='x', pady=(15, 6))
        ttk.Button(panel, text='Log out', command=self.logout).pack(fill='x')
        confirm.bind('<Return>', lambda _e: save())
        old.focus_set()

    def student_signup(self):
        "Collect a pending student request; an admin approves access later."
        dialog = tk.Toplevel(self.root)
        dialog.title('Student sign up')
        dialog.configure(bg=BG)
        dialog.geometry('540x705')
        dialog.minsize(440, 630)
        dialog.transient(self.root)
        dialog.grab_set()

        panel = tk.Frame(dialog, bg=WHITE, bd=0, highlightthickness=0)
        panel.pack(fill='both', expand=True, padx=14, pady=14)
        body = tk.Frame(panel, bg=WHITE, bd=0, highlightthickness=0)
        body.pack(fill='both', expand=True, padx=25, pady=22)
        tk.Label(body, text='Create student account', bg=WHITE, fg=TEXT,
                 font=('Helvetica Neue', 23, 'bold'), anchor='w').pack(fill='x')
        tk.Label(body, text='Submit a request. An administrator will approve your account.',
                 bg=WHITE, fg=MUTED, font=('Helvetica Neue', 11),
                 wraplength=405, justify='left', anchor='w').pack(fill='x', pady=(6, 14))

        def field(title, hide=False):
            tk.Label(body, text=title, bg=WHITE, fg=TEXT,
                     font=('Helvetica Neue', 12, 'bold'), anchor='w').pack(fill='x', pady=(8, 5))
            container = tk.Frame(body, bg='#263348', height=48,
                                 bd=0, highlightthickness=0)
            container.pack(fill='x')
            container.pack_propagate(False)
            entry = tk.Entry(container, bg='#263348', fg=TEXT,
                             insertbackground=TEXT, insertwidth=2,
                             selectbackground=ACCENT, selectforeground='#14100B',
                             relief='flat', bd=0, highlightthickness=0,
                             justify='left', font=('Helvetica Neue', 13),
                             show='•' if hide else '')
            entry.pack(fill='both', expand=True, padx=14, pady=10)
            return entry

        sid = field('Student ID')
        full_name = field('Full name')
        username = field('New username')
        password = field('Password (10+ characters)', True)
        confirm = field('Confirm password', True)
        info = tk.Label(body, text='', bg=WHITE, fg='#FFB3B3',
                        font=('Helvetica Neue', 10), wraplength=405,
                        justify='left', anchor='w')
        info.pack(fill='x', pady=(8, 2))

        def submit():
            student_id = sid.get().strip()
            student_name = full_name.get().strip()
            u = username.get().strip()
            pw = password.get()
            if not valid_student_id(student_id) or not re.fullmatch(r'[A-Za-z0-9_.-]{3,40}', u):
                info.configure(text='Enter a valid registered student ID and username (3–40 characters).')
                return
            if not (2 <= len(student_name) <= 100) or '|' in student_name:
                info.configure(text='Enter your full name (2–100 characters).')
                return
            if len(pw) < 10 or pw != confirm.get():
                info.configure(text='Passwords must match and contain at least 10 characters.')
                return
            salt, digest, iterations = hash_password(pw)
            try:
                with conn() as db:
                    registered = db.execute('SELECT name FROM students WHERE student_id=?',
                                            (student_id,)).fetchone()
                    if registered and registered[0].strip().casefold() != student_name.casefold():
                        raise ValueError('The name does not match the existing student record.')
                    if db.execute('SELECT 1 FROM admin_auth WHERE username=?', (u,)).fetchone() or \
                       db.execute('SELECT 1 FROM user_auth WHERE username=?', (u,)).fetchone():
                        raise ValueError('This username is already in use.')
                    if db.execute('SELECT 1 FROM user_auth WHERE student_id=?',
                                  (student_id,)).fetchone() or \
                       db.execute('SELECT 1 FROM role_student_links WHERE student_id=?',
                                  (student_id,)).fetchone():
                        raise ValueError('This student already has an account.')
                    db.execute('''INSERT INTO signup_requests
                                  (student_id,username,student_name,salt,hash,iterations,requested_at)
                                  VALUES (?,?,?,?,?,?,?)''',
                               (student_id, u, student_name, salt, digest, iterations,
                                datetime.now().isoformat(timespec='seconds')))
            except sqlite3.IntegrityError:
                info.configure(text='A sign-up request already exists for this student or username.')
                return
            except ValueError as exc:
                info.configure(text=str(exc))
                return
            dialog.destroy()
            messagebox.showinfo('Request submitted',
                                'Your request is pending administrator approval. You can sign in after approval.',
                                parent=self.root)

        button = tk.Label(body, text='Request student account  →', bg=ACCENT,
                          fg='#19120D', cursor='hand2', bd=0, highlightthickness=0,
                          font=('Helvetica Neue', 12, 'bold'), pady=11)
        button.pack(fill='x', pady=(8, 0))
        button.bind('<Button-1>', lambda _event: submit())
        confirm.bind('<Return>', lambda _event: submit())
        sid.focus_set()

    def main_screen(self):
        if self.current_role != 'admin':
            self.user_screen()
            return
        self.clear()
        shell = tk.Frame(self.root, bg=BG)
        shell.pack(fill='both', expand=True)
        self.sidebar = tk.Frame(shell, bg=NAVY, width=238)
        self.sidebar.pack(side='left', fill='y')
        self.sidebar.pack_propagate(False)
        # Scrollable sidebar prevents controls disappearing on smaller Mac screens.
        side_canvas = tk.Canvas(self.sidebar, bg=NAVY, highlightthickness=0, bd=0)
        # Native trackpad/mouse-wheel scrolling, without on-screen arrow buttons.
        side_canvas.pack(fill='both', expand=True)
        side_inner = tk.Frame(side_canvas, bg=NAVY)
        side_window = side_canvas.create_window((0, 0), window=side_inner, anchor='nw')
        side_inner.bind('<Configure>', lambda e: side_canvas.configure(scrollregion=side_canvas.bbox('all')))
        side_canvas.bind('<Configure>', lambda e: side_canvas.itemconfigure(side_window, width=e.width))
        # Scroll the navigation panel via mouse wheel / Mac trackpad. No visible
        # scrollbar is required; Logout stays reachable in short windows.
        def sidebar_wheel(event):
            if not side_canvas.winfo_exists():
                return
            if side_canvas.bbox('all') is None:
                return
            total_height = side_canvas.bbox('all')[3]
            if total_height <= side_canvas.winfo_height():
                return
            delta = getattr(event, 'delta', 0)
            if delta:
                # macOS sends small trackpad deltas; Windows often sends 120.
                step = -max(1, round(abs(delta) / 12)) if delta > 0 else max(1, round(abs(delta) / 12))
                side_canvas.yview_scroll(step, 'units')
            elif getattr(event, 'num', None) in (4, 5):
                side_canvas.yview_scroll(-2 if event.num == 4 else 2, 'units')
            return 'break'

        def pointer_in_sidebar():
            px, py = self.root.winfo_pointerxy()
            x, y = self.sidebar.winfo_rootx(), self.sidebar.winfo_rooty()
            return x <= px < x + self.sidebar.winfo_width() and y <= py < y + self.sidebar.winfo_height()

        def wheel_from_root(event):
            if pointer_in_sidebar():
                return sidebar_wheel(event)

        # Root binding catches wheel events above nested navigation labels too.
        # bind_all catches events over nested labels and custom sidebar items.
        # The pointer test above prevents intercepting scrolling in the main view.
        for wheel_event in ('<MouseWheel>', '<Button-4>', '<Button-5>'):
            self.root.bind_all(wheel_event, wheel_from_root, add='+')
        self.sidebar_canvas = side_canvas

        tk.Label(side_inner, text='●', bg=NAVY, fg=ACCENT,
                 font=('Helvetica Neue', 26, 'bold')).pack(anchor='w', padx=22, pady=(18, 0))
        tk.Label(side_inner, text='ATTENDANCE', bg=NAVY, fg=TEXT,
                 font=('Helvetica Neue', 15, 'bold')).pack(anchor='w', padx=22)
        tk.Label(side_inner, text='Smart Campus / Pro', bg=NAVY, fg=MUTED,
                 font=('Helvetica Neue', 10)).pack(anchor='w', padx=22, pady=(4, 23))
        tk.Label(side_inner, text='WORKSPACE', bg=NAVY, fg='#D8E2F2',
                 font=('Helvetica Neue', 10, 'bold')).pack(anchor='w', padx=22, pady=(0, 8))
        content = tk.Frame(shell, bg=BG)
        content.pack(side='left', fill='both', expand=True)
        top = tk.Frame(content, bg=BG)
        top.pack(fill='x', padx=20, pady=(18, 12))
        self.page_heading = tk.Label(top, text='Overview', bg=BG, fg=TEXT,
                                     font=('Helvetica Neue', 23, 'bold'))
        self.page_heading.pack(side='left')
        ttk.Button(top, text='＋ Register student', style='Primary.TButton', command=lambda: self.register(create_default_login=True)).pack(side='right', padx=(8,0))
        ttk.Button(top, text='↻ Refresh', command=self.refresh_all).pack(side='right')
        main = tk.Frame(content, bg=BG)
        main.pack(fill='both', expand=True, padx=20, pady=(0, 10))
        self.tabs = ttk.Notebook(main, style='Modern.TNotebook')
        self.tabs.pack(fill='both', expand=True, padx=0, pady=0)
        self.dashboard = tk.Frame(self.tabs, bg=BG)
        self.records = tk.Frame(self.tabs, bg=BG)
        self.students = tk.Frame(self.tabs, bg=BG)
        for page, title in [(self.dashboard, 'Overview'), (self.records, 'Attendance reports'), (self.students, 'Students')]:
            self.tabs.add(page, text=title)
        self.nav_buttons = []
        for label, index in [('▦  Overview',0), ('▤  Attendance reports',1), ('♙  Students',2)]:
            item = SidebarItem(side_inner, label, lambda i=index: self.tabs.select(i))
            item.pack(fill='x', padx=10, pady=2)
            self.nav_buttons.append(item)
        tk.Frame(side_inner, bg=BORDER, height=1).pack(fill='x', padx=19, pady=18)
        tk.Label(side_inner, text='QUICK ACTIONS', bg=NAVY, fg='#D8E2F2',
                 font=('Helvetica Neue', 10, 'bold')).pack(anchor='w', padx=22, pady=(0, 8))
        for label, action in [('▶  Start recognition', lambda: self.open_camera('recognition')),
                              ('◎  Train face model', self.train_async),
                              ('↓  Export CSV', self.export_csv)]:
            SidebarItem(side_inner, label, action, kind='action').pack(fill='x', padx=10, pady=2)
        tk.Frame(side_inner, bg=BORDER, height=1).pack(fill='x', padx=19, pady=18)
        tk.Label(side_inner, text='SIGNED IN', bg=NAVY, fg=MUTED,
                 font=('Helvetica Neue', 9, 'bold')).pack(anchor='w', padx=22)
        tk.Label(side_inner, text=self.current_user or 'Administrator', bg=NAVY,
                 fg=TEXT, font=('Helvetica Neue', 11, 'bold')).pack(anchor='w', padx=22, pady=(5, 12))
        SidebarItem(side_inner, '♙  Manage users', self.manage_users, kind='action').pack(fill='x', padx=10, pady=2)
        SidebarItem(side_inner, '⇄  Manage authority', self.manage_roles, kind='action').pack(fill='x', padx=10, pady=2)
        SidebarItem(side_inner, '⚙  Change password', self.change_password).pack(fill='x', padx=10, pady=2)
        SidebarItem(side_inner, '↪  Log out', self.logout, kind='danger').pack(fill='x', padx=10, pady=(2, 15))
        statusbar = tk.Frame(content, bg=WHITE, bd=0, highlightthickness=0, relief="flat")
        statusbar.pack(side='bottom', fill='x')
        tk.Label(statusbar, text='●', bg=WHITE, fg=GREEN, font=('Helvetica Neue', 11)).pack(side='left', padx=(20, 8), pady=11)
        tk.Label(statusbar, textvariable=self.status, bg=WHITE, fg=MUTED,
                 font=('Helvetica Neue', 10), anchor='w').pack(side='left', fill='x', expand=True)
        tk.Label(statusbar, text='LOCAL · SQLITE', bg=WHITE, fg=MUTED,
                 font=('Helvetica Neue', 9, 'bold')).pack(side='right', padx=18)
        def on_tab_changed(_event=None):
            current = self.tabs.index(self.tabs.select())
            self.page_heading.configure(text=['Overview','Attendance reports','Students'][current])
            for i, item in enumerate(self.nav_buttons):
                item.set_active(i == current)
        self.tabs.bind('<<NotebookTabChanged>>', on_tab_changed)
        on_tab_changed()
        self.make_dashboard()
        self.make_records()
        self.make_students()
        self.refresh_all()
        # Adapt card columns and navigation width to window size.
        self.root.bind('<Configure>', self.on_window_resize)

    def on_window_resize(self, event):
        if event.widget is not self.root or not hasattr(self, 'sidebar') or not self.sidebar.winfo_exists():
            return
        w = event.width
        desired = 205 if w < 1000 else 238
        if int(self.sidebar.cget('width')) != desired:
            self.sidebar.configure(width=desired)
        if hasattr(self, 'stats_frame') and self.stats_frame.winfo_exists():
            cols = 2 if w < 1140 else 4
            if getattr(self, '_metric_columns', None) != cols:
                self._metric_columns = cols
                self.refresh_dashboard()

    def logout(self):
        if self.camera: self.stop_camera()
        self.logged_in=False
        self.current_user=None
        self.current_role=None
        self.current_student_id=None
        self.login_screen()

    def change_password(self):
        dialog=tk.Toplevel(self.root)
        dialog.title('Change password')
        dialog.transient(self.root)
        frame=ttk.Frame(dialog,padding=20)
        frame.pack()
        username=self.label_entry(frame,'Username')
        username.insert(0, self.current_user or get_last_username())
        old=self.label_entry(frame,'Current password',secret=True)
        new=self.label_entry(frame,'New password (10+ characters)',secret=True)
        def save():
            if username.get() != self.current_user or not authenticate_account(username.get(),old.get()):
                messagebox.showerror('Error','Current credentials incorrect.',parent=dialog)
                return
            if len(new.get()) < 10:
                messagebox.showerror('Error','Use at least 10 characters.',parent=dialog)
                return
            salt,digest,iterations=hash_password(new.get())
            with conn() as db:
                table = 'admin_auth' if self.current_role == 'admin' else 'user_auth'
                db.execute(f'UPDATE {table} SET salt=?,hash=?,iterations=? WHERE username=?',
                           (salt,digest,iterations,username.get()))
                if self.current_role == 'user':
                    db.execute('DELETE FROM password_change_required WHERE username=?',
                               (username.get(),))
            dialog.destroy()
            self.status.set('Password updated.')
        ttk.Button(frame,text='Update password',command=save).pack(fill='x',pady=15)

    def manage_users(self):
        # Administrator-only account manager and sign-up review.
        if self.current_role != 'admin' or not self.current_user:
            messagebox.showerror('Access denied', 'Administrator access required.')
            return
        dialog = tk.Toplevel(self.root)
        dialog.title('Manage users and sign-up requests')
        dialog.geometry('960x690')
        dialog.minsize(720, 520)
        dialog.configure(bg=BG)
        dialog.transient(self.root)
        dialog.grab_set()

        wrap = tk.Frame(dialog, bg=BG, padx=22, pady=18)
        wrap.pack(fill='both', expand=True)
        tk.Label(wrap, text='User management', bg=BG, fg=TEXT,
                 font=('Helvetica Neue', 23, 'bold')).pack(anchor='w')
        tk.Label(wrap, text='Create accounts, review sign-ups, reset passwords and manage permissions.',
                 bg=BG, fg=MUTED, font=('Helvetica Neue', 11)).pack(anchor='w', pady=(3, 12))

        stats = tk.Label(wrap, bg=BG, fg=ACCENT, font=('Helvetica Neue', 11, 'bold'))
        stats.pack(anchor='w', pady=(0, 8))
        notebook = ttk.Notebook(wrap)
        notebook.pack(fill='both', expand=True)
        accounts_tab = tk.Frame(notebook, bg=WHITE, padx=12, pady=12)
        requests_tab = tk.Frame(notebook, bg=WHITE, padx=12, pady=12)
        notebook.add(accounts_tab, text='Accounts')
        notebook.add(requests_tab, text='Pending sign-ups')

        form = tk.Frame(accounts_tab, bg=WHITE)
        form.pack(fill='x')
        def field(parent, label, password=False):
            tk.Label(parent, text=label, bg=WHITE, fg=TEXT,
                     font=('Helvetica Neue', 10, 'bold')).pack(anchor='w', pady=(0,4))
            entry = tk.Entry(parent, bg='#263348', fg=TEXT, insertbackground=TEXT,
                             highlightthickness=0, relief='flat', bd=0,
                             font=('Helvetica Neue', 11), show='•' if password else '')
            entry.pack(fill='x', ipady=9, padx=0)
            return entry
        left = tk.Frame(form, bg=WHITE)
        right = tk.Frame(form, bg=WHITE)
        left.pack(side='left', fill='x', expand=True, padx=(0, 10))
        right.pack(side='left', fill='x', expand=True)
        username = field(left, 'Username')
        password = field(left, 'Password (10+ characters)', True)
        tk.Label(right, text='Linked student', bg=WHITE, fg=TEXT,
                 font=('Helvetica Neue', 10, 'bold')).pack(anchor='w', pady=(0,4))
        student_choice = tk.StringVar()
        student_box = ttk.Combobox(right, state='readonly', textvariable=student_choice)
        student_box.pack(fill='x', ipady=7)
        notice = tk.Label(right, text='Select a registered student before creating an account.',
                          bg=WHITE, fg=MUTED, wraplength=310, justify='left')
        notice.pack(anchor='w', pady=(9, 0))

        columns = ('username','role','student_id')
        accounts = ttk.Treeview(accounts_tab, columns=columns, show='headings',
                                selectmode='browse', style='Modern.Treeview', height=7)
        for key, title, width in [('username','Username',200),('role','Role',155),('student_id','Student ID',180)]:
            accounts.heading(key, text=title)
            accounts.column(key, width=width, stretch=True, anchor='w')
        actions = tk.Frame(accounts_tab, bg=WHITE)
        actions.pack(fill='x', pady=(12, 0))
        actions.columnconfigure(0, weight=1)
        actions.columnconfigure(1, weight=1)
        accounts.pack(fill='both', expand=True, pady=(12, 9))

        req_cols = ('student_id','username','name','requested_at')
        requests = ttk.Treeview(requests_tab, columns=req_cols, show='headings',
                                selectmode='browse', style='Modern.Treeview')
        for key, title, width in [('student_id','Student ID',140),('username','Requested username',175),
                                  ('name','Student name',190),('requested_at','Requested on',180)]:
            requests.heading(key, text=title)
            requests.column(key, width=width, stretch=True, anchor='w')
        requests.pack(fill='both', expand=True, pady=(5, 12))
        request_actions = tk.Frame(requests_tab, bg=WHITE)
        request_actions.pack(fill='x')

        def refresh():
            for tree in (accounts, requests):
                for item in tree.get_children():
                    tree.delete(item)
            with conn() as db:
                data = db.execute('''SELECT username,'Administrator',NULL FROM admin_auth
                     UNION ALL SELECT username,'Student',student_id FROM user_auth ORDER BY username''').fetchall()
                pending = db.execute('''SELECT student_id,username,student_name,requested_at
                     FROM signup_requests ORDER BY requested_at''').fetchall()
                students = db.execute('SELECT student_id,name FROM students ORDER BY name').fetchall()
            for account in data:
                accounts.insert('', 'end', values=(account[0], account[1], account[2] or '—'))
            for req in pending:
                requests.insert('', 'end', values=req)
            student_box['values'] = [f'{sid} — {name}' for sid, name in students]
            stats.configure(text=f'{len(data)} accounts  •  {len(pending)} pending requests  •  {len(students)} students')
            notebook.tab(1, text=f'Pending sign-ups ({len(pending)})')
            return dict((f'{sid} — {name}',sid) for sid,name in students)

        def selected_account():
            ids = accounts.selection()
            if not ids:
                messagebox.showinfo('Select account', 'Select a user in the accounts table.', parent=dialog)
                return None
            return accounts.item(ids[0], 'values')

        def create():
            u, p = username.get().strip(), password.get()
            student_map = refresh_students()
            sid = student_map.get(student_choice.get())
            if not 3 <= len(u) <= 40 or not p or len(p) < 10 or not sid:
                messagebox.showerror('Incomplete form', 'Enter a 3–40 character username, a 10+ character password, and select a student.', parent=dialog)
                return
            salt, digest, iters = hash_password(p)
            try:
                with conn() as db:
                    if db.execute('SELECT 1 FROM admin_auth WHERE username=?', (u,)).fetchone():
                        raise ValueError('Username is already assigned to an administrator.')
                    if db.execute('SELECT 1 FROM signup_requests WHERE username=? OR student_id=?', (u, sid)).fetchone():
                        raise ValueError('A sign-up request already exists for that student/username.')
                    db.execute('INSERT INTO user_auth(username,student_id,salt,hash,iterations) VALUES(?,?,?,?,?)',
                               (u, sid, salt, digest, iters))
            except (sqlite3.IntegrityError, ValueError) as err:
                messagebox.showerror('Could not create account', str(err), parent=dialog)
                return
            username.delete(0, 'end')
            password.delete(0, 'end')
            refresh()
            if messagebox.askyesno('Face enrollment',
                f'Student login {u} is ready.\nCapture their face now for automatic attendance?', parent=dialog):
                dialog.destroy()
                self.enroll_student_face(sid, '')
            else:
                messagebox.showinfo('Enrollment pending',
                    'Account created. The student will not be recognized until face enrollment is completed.', parent=dialog)

        def refresh_students():
            return {f'{sid} — {name}': sid for sid,name in self.student_list()}

        def capture_selected_face():
            selected = selected_account()
            if not selected:
                return
            user, role, student_id = selected
            if student_id in ('—', '', None):
                messagebox.showwarning('No student link',
                    'Select a student account linked to a student ID.', parent=dialog)
                return
            with conn() as db:
                student = db.execute('SELECT name FROM students WHERE student_id=?', (student_id,)).fetchone()
            if not student:
                messagebox.showerror('Student not found', 'The linked student no longer exists.', parent=dialog)
                return
            if not messagebox.askyesno('Capture face',
                f'Capture face samples for {student[0]} ({student_id})?\n\nEnsure the student is present and agrees.', parent=dialog):
                return
            dialog.destroy()
            self.enroll_student_face(student_id, student[0])

        def reset_password():
            row = selected_account()
            if not row: return
            user, role, _ = row
            secret = password.get()
            if len(secret) < 10:
                messagebox.showerror('Password required', 'Enter a new password of at least 10 characters in the password field.', parent=dialog)
                return
            if not messagebox.askyesno('Confirm password reset', f'Reset password for {user}?', parent=dialog):
                return
            table = 'admin_auth' if role == 'Administrator' else 'user_auth'
            salt, digest, iterations = hash_password(secret)
            with conn() as db:
                if not db.execute('SELECT 1 FROM admin_auth WHERE username=?', (self.current_user,)).fetchone():
                    messagebox.showerror('Access denied', 'Administrator permission no longer valid.', parent=dialog)
                    return
                db.execute(f'UPDATE {table} SET salt=?,hash=?,iterations=? WHERE username=?',
                           (salt, digest, iterations, user))
            password.delete(0, 'end')
            messagebox.showinfo('Password updated', f'Password updated for {user}.', parent=dialog)

        def remove():
            row = selected_account()
            if not row: return
            user, role, _ = row
            if role == 'Administrator':
                messagebox.showwarning('Protected', 'Use Manage authority to change administrator roles. Admin accounts cannot be deleted here.', parent=dialog)
                return
            if not messagebox.askyesno('Remove login', f'Remove login for {user}? Attendance and student records will remain.', parent=dialog):
                return
            with conn() as db:
                if not db.execute('SELECT 1 FROM admin_auth WHERE username=?', (self.current_user,)).fetchone():
                    raise PermissionError('Administrator access required.')
                db.execute('DELETE FROM user_auth WHERE username=?', (user,))
            refresh()

        def decide(approve):
            ids = requests.selection()
            if not ids:
                messagebox.showinfo('Select request', 'Select a request in the Pending sign-ups tab.', parent=dialog)
                return
            sid, user, name, requested = requests.item(ids[0], 'values')
            verb = 'Approve' if approve else 'Reject'
            if not messagebox.askyesno(f'{verb} request', f'{verb} account request for {name} ({user})?\nVerify student identity before approval.', parent=dialog):
                return
            try:
                with conn() as db:
                    db.execute('BEGIN IMMEDIATE')
                    if not db.execute('SELECT 1 FROM admin_auth WHERE username=?', (self.current_user,)).fetchone():
                        raise PermissionError('Administrator access required.')
                    row = db.execute('SELECT salt,hash,iterations,student_name FROM signup_requests WHERE student_id=? AND username=?',
                                     (sid, user)).fetchone()
                    if not row: raise ValueError('Request no longer exists.')
                    if approve:
                        if db.execute('SELECT 1 FROM admin_auth WHERE username=?', (user,)).fetchone():
                            raise ValueError('Username already belongs to an administrator.')
                        if not db.execute('SELECT 1 FROM students WHERE student_id=?', (sid,)).fetchone():
                            db.execute('INSERT INTO students(student_id,name,created_at) VALUES(?,?,?)',
                                       (sid,row[3],datetime.now().isoformat(timespec='seconds')))
                        db.execute('INSERT INTO user_auth(username,student_id,salt,hash,iterations) VALUES(?,?,?,?,?)',
                                   (user, sid, *row[:3]))
                    db.execute('DELETE FROM signup_requests WHERE student_id=? AND username=?', (sid, user))
            except (ValueError, PermissionError, sqlite3.IntegrityError) as err:
                messagebox.showerror('Could not process request', str(err), parent=dialog)
                return
            refresh()
            if approve:
                if messagebox.askyesno('Approved — enroll face',
                    f'Account approved for {name} ({sid}).\n\nCapture face images now for future attendance?', parent=dialog):
                    dialog.destroy()
                    self.enroll_student_face(sid, name)
                else:
                    messagebox.showinfo('Face enrollment pending',
                        'Account approved. Attendance recognition requires face capture before first use.', parent=dialog)
            else:
                messagebox.showinfo('Completed', f'Request rejected for {user}.', parent=dialog)

        ttk.Button(actions, text='Create student login', style='Primary.TButton', command=create).grid(row=0, column=0, sticky='ew', padx=(0, 5), pady=(0, 6))
        ttk.Button(actions, text='Capture selected face', command=capture_selected_face).grid(row=0, column=1, sticky='ew', padx=(5, 0), pady=(0, 6))
        ttk.Button(actions, text='Reset selected password', command=reset_password).grid(row=1, column=0, sticky='ew', padx=(0, 5))
        ttk.Button(actions, text='Remove student login', style='Danger.TButton', command=remove).grid(row=1, column=1, sticky='ew', padx=(5, 0))
        ttk.Button(request_actions, text='Approve selected', style='Primary.TButton', command=lambda:decide(True)).pack(side='left', padx=(0,7))
        ttk.Button(request_actions, text='Reject selected', style='Danger.TButton', command=lambda:decide(False)).pack(side='left')
        ttk.Button(request_actions, text='Refresh', command=refresh).pack(side='right')
        footer = tk.Frame(wrap, bg=BG)
        footer.pack(fill='x', pady=(9,0))
        ttk.Button(footer, text='Manage authority (admin only)', command=self.manage_roles).pack(side='left')
        ttk.Button(footer, text='Refresh lists', command=refresh).pack(side='right')
        refresh()

    def manage_roles(self):
        """Admin-only role management with ALWAYS-VISIBLE controls above the table."""
        if self.current_role != 'admin' or not self.current_user:
            messagebox.showerror('Not authorized', 'Administrator access required.')
            return
        dialog = tk.Toplevel(self.root)
        dialog.title('Manage account authority')
        dialog.geometry('800x570')
        dialog.minsize(630, 430)
        dialog.configure(bg=BG)
        dialog.transient(self.root)
        dialog.grab_set()

        wrap = tk.Frame(dialog, bg=BG, padx=20, pady=15)
        wrap.pack(fill='both', expand=True)
        tk.Label(wrap, text='Account authority', bg=BG, fg=TEXT,
                 font=('Helvetica Neue', 21, 'bold')).pack(anchor='w')
        tk.Label(wrap, text='Select an account and choose an action below. Only admins may change roles.',
                 bg=BG, fg=MUTED, font=('Helvetica Neue', 11),
                 wraplength=730, justify='left').pack(anchor='w', pady=(5, 12))

        # Place action controls BEFORE the expanding table. On macOS Tkinter can
        # otherwise allocate all height to the table, hiding buttons below it.
        action_panel = tk.Frame(wrap, bg=WHITE, padx=12, pady=10)
        action_panel.pack(fill='x', pady=(0, 10))
        actions = tk.Frame(action_panel, bg=action_panel.cget('bg'))
        actions.pack(fill='x')
        promote = tk.Button(actions, text='↑  Promote to Admin',
                            bg='#FF8A24', fg='#17110B', activebackground='#FFAA55',
                            activeforeground='#17110B', relief='flat', bd=0,
                            cursor='hand2', padx=14, pady=10,
                            font=('Helvetica Neue', 12, 'bold'))
        promote.pack(side='left', padx=(0, 9))
        demote = tk.Button(actions, text='↓  Change to Student',
                           bg='#293A52', fg='#FFFFFF', activebackground='#3A506B',
                           activeforeground='#FFFFFF', relief='flat', bd=0,
                           cursor='hand2', padx=14, pady=10,
                           font=('Helvetica Neue', 12, 'bold'))
        demote.pack(side='left', padx=(0, 9))
        tk.Button(actions, text='Refresh', bg='#253247', fg='#FFFFFF',
                  activebackground='#34465D', activeforeground='#FFFFFF',
                  relief='flat', bd=0, padx=12, pady=10,
                  font=('Helvetica Neue', 11)).pack(side='right')

        linkrow = tk.Frame(action_panel, bg=action_panel.cget('bg'))
        linkrow.pack(fill='x', pady=(10, 0))
        tk.Label(linkrow, text='Student ID for demotion:',
                 bg=linkrow.cget('bg'), fg=TEXT,
                 font=('Helvetica Neue', 10, 'bold')).pack(side='left', padx=(0, 9))
        students = {f'{sid} — {name}': sid for sid, name in self.student_list()}
        chosen = tk.StringVar()
        combo = ttk.Combobox(linkrow, values=list(students), textvariable=chosen,
                             state='readonly', width=37)
        combo.pack(side='left', fill='x', expand=True)
        tk.Label(action_panel, text='Demotion uses an existing student link if available; otherwise choose a student above.',
                 bg=action_panel.cget('bg'), fg=MUTED,
                 font=('Helvetica Neue', 10), anchor='w').pack(fill='x', pady=(7, 0))

        table_frame = tk.Frame(wrap, bg=BG)
        table_frame.pack(fill='both', expand=True)
        cols = ('username', 'role', 'student')
        tree = ttk.Treeview(table_frame, columns=cols, show='headings',
                            style='Modern.Treeview', selectmode='browse')
        for col, title, width in [('username', 'Username', 210),
                                  ('role', 'Current role', 165),
                                  ('student', 'Linked student ID', 210)]:
            tree.heading(col, text=title)
            tree.column(col, width=width, stretch=True)
        tree.pack(fill='both', expand=True)

        def refresh():
            selected = tree.selection()
            old_user = tree.item(selected[0], 'values')[0] if selected else None
            for item in tree.get_children():
                tree.delete(item)
            for username, role, sid in list_accounts():
                iid = tree.insert('', 'end', values=(username, role, sid or '—'))
                if old_user == username:
                    tree.selection_set(iid)
            info.set(f'{len(tree.get_children())} accounts. Select one to update authority.')

        def update(target):
            selected = tree.selection()
            if not selected:
                messagebox.showwarning('Select an account',
                                       'First click an account in the table.', parent=dialog)
                return
            username, role, _ = tree.item(selected[0], 'values')
            action = 'promote to Administrator' if target == 'admin' else 'change to Student'
            if not messagebox.askyesno('Confirm authority change',
                                       f'Are you sure you want to {action} for {username}?',
                                       parent=dialog):
                return
            try:
                change_account_role(self.current_user, username, target,
                                    students.get(chosen.get()))
            except (ValueError, PermissionError, sqlite3.IntegrityError) as error:
                messagebox.showerror('Role change failed', str(error), parent=dialog)
                return
            refresh()
            self.status.set(f'Updated authority for {username}.')
            messagebox.showinfo('Role updated',
                                f'{username} now has {"Administrator" if target == "admin" else "Student"} access.',
                                parent=dialog)

        promote.configure(command=lambda: update('admin'))
        demote.configure(command=lambda: update('user'))
        # Enable the Refresh button by assigning the action on the existing widget.
        for widget in actions.winfo_children():
            if isinstance(widget, tk.Button) and widget.cget('text') == 'Refresh':
                widget.configure(command=refresh)

        info = tk.StringVar(value='Select an account to update its role.')
        tk.Label(wrap, textvariable=info, bg=BG, fg=MUTED,
                 font=('Helvetica Neue', 10), anchor='w').pack(fill='x', pady=(7, 0))
        refresh()

    def student_list(self):
        with conn() as db:
            return db.execute('SELECT student_id,name FROM students ORDER BY name').fetchall()

    def user_screen(self):
        # No administrative widgets are constructed for student users.
        if self.current_role != 'user' or not self.current_student_id:
            self.logout()
            return
        self.clear()
        shell = tk.Frame(self.root, bg=BG)
        shell.pack(fill='both', expand=True)
        bar = tk.Frame(shell, bg=NAVY, padx=24, pady=18)
        bar.pack(fill='x')
        tk.Label(bar, text='●  SMART ATTENDANCE  /  STUDENT PORTAL',
                 bg=NAVY, fg=ACCENT, font=('Helvetica Neue', 15, 'bold')).pack(side='left')
        ttk.Button(bar, text='Log out', command=self.logout).pack(side='right')
        ttk.Button(bar, text='Change password', command=self.change_password).pack(side='right', padx=10)
        body = tk.Frame(shell, bg=BG, padx=30, pady=25)
        body.pack(fill='both', expand=True)
        with conn() as db:
            student = db.execute('SELECT name FROM students WHERE student_id=?',
                                 (self.current_student_id,)).fetchone()
        tk.Label(body, text=f'Welcome, {student[0] if student else self.current_user}',
                 bg=BG, fg=TEXT, font=('Helvetica Neue', 24, 'bold')).pack(anchor='w')
        tk.Label(body, text=f'Student ID: {self.current_student_id}  •  Personal attendance records',
                 bg=BG, fg=MUTED).pack(anchor='w', pady=(6, 22))
        count_var = tk.StringVar()
        tk.Label(body, textvariable=count_var, bg=WHITE, fg=TEXT,
                 font=('Helvetica Neue', 15, 'bold'), padx=18, pady=16).pack(fill='x')
        table = ttk.Treeview(body, columns=('date','time'), show='headings', style='Modern.Treeview')
        table.heading('date', text='Attendance date')
        table.heading('time', text='Check-in time')
        table.pack(fill='both', expand=True, pady=18)
        def refresh():
            with conn() as db:
                rows = db.execute('''SELECT attendance_date,attendance_time FROM attendance
                     WHERE student_id=? ORDER BY attendance_date DESC,attendance_time DESC''',
                     (self.current_student_id,)).fetchall()
            for item in table.get_children():
                table.delete(item)
            for row in rows:
                table.insert('', 'end', values=row)
            count_var.set(f'Total recorded check-ins: {len(rows)}')
        def export():
            with conn() as db:
                rows = db.execute('''SELECT attendance_date,attendance_time FROM attendance
                        WHERE student_id=? ORDER BY attendance_date DESC''',
                        (self.current_student_id,)).fetchall()
            path = filedialog.asksaveasfilename(parent=self.root, defaultextension='.csv',
                initialfile='my_attendance.csv', filetypes=[('CSV files', '*.csv')])
            if path:
                with open(path, 'w', newline='', encoding='utf-8-sig') as file:
                    writer = csv.writer(file)
                    writer.writerow(['Attendance date', 'Check-in time'])
                    writer.writerows(rows)
        actions = tk.Frame(body, bg=BG)
        actions.pack(fill='x')
        ttk.Button(actions, text='Refresh my attendance', command=refresh).pack(side='left')
        ttk.Button(actions, text='Export my CSV', style='Primary.TButton', command=export).pack(side='left', padx=12)
        refresh()

    def make_dashboard(self):
        intro = tk.Frame(self.dashboard, bg=BG)
        intro.pack(fill='x', pady=(10, 16))
        tk.Label(intro, text='Your campus at a glance', bg=BG, fg=TEXT,
                 font=('Helvetica Neue', 17, 'bold')).pack(anchor='w')
        tk.Label(intro, text='Live attendance insights and student activity', bg=BG,
                 fg=MUTED, font=('Helvetica Neue', 11)).pack(anchor='w', pady=(4,0))
        self.stats_frame = tk.Frame(self.dashboard, bg=BG)
        self.stats_frame.pack(fill='x', pady=(0, 20))
        panel = tk.Frame(self.dashboard, bg=WHITE, bd=0, highlightthickness=0, relief="flat")
        panel.pack(fill='both', expand=True, pady=(0, 12))
        tk.Label(panel, text='Attendance activity', bg=WHITE, fg=TEXT,
                 font=('Helvetica Neue', 16, 'bold')).pack(anchor='w', padx=24, pady=(20,2))
        tk.Label(panel, text='Daily check-ins for the last 7 days', bg=WHITE,
                 fg=MUTED, font=('Helvetica Neue', 10)).pack(anchor='w', padx=24)
        chart_controls = tk.Frame(panel, bg=WHITE)
        chart_controls.pack(fill='x', padx=22, pady=(8, 0))
        tk.Label(chart_controls, text='VIEW:', bg=WHITE, fg=MUTED, font=('Helvetica Neue', 10, 'bold')).pack(side='left', padx=(0, 10))
        self.chart_mode = tk.StringVar(value='Bars')
        for mode in ('Bars', 'Trend line', 'Today split'):
            ttk.Radiobutton(chart_controls, text=mode, variable=self.chart_mode,
                            value=mode, command=self.redraw_chart, style='Chart.TRadiobutton').pack(side='left', padx=(0, 14))
        self.chart = tk.Canvas(panel, height=300, bg=WHITE, highlightthickness=0)
        self.chart.pack(fill='both', expand=True, padx=16, pady=(12, 12))
        self.chart.bind('<Configure>', lambda event: self.redraw_chart())
        tk.Label(self.dashboard,
                 text='Attendance rate compares today’s check-ins with all registered students; it is not a class-specific absence report.',
                 bg=BG, fg=MUTED, font=('Helvetica Neue', 9)).pack(anchor='w', pady=(0, 8))

    def redraw_chart(self):
        if hasattr(self, 'chart_values') and self.chart.winfo_exists():
            self.draw_chart(self.chart_values)

    def make_records(self):
        filters = tk.Frame(self.records, bg=WHITE, padx=13, pady=12)
        filters.pack(fill='x', pady=(0, 12))
        row1 = tk.Frame(filters, bg=WHITE)
        row1.pack(fill='x', pady=(0, 8))
        tk.Label(row1, text='FROM', bg=WHITE, fg=MUTED, font=('Helvetica Neue', 10, 'bold')).pack(side='left', padx=(0,6))
        self.from_date=ttk.Entry(row1, width=13)
        self.from_date.pack(side='left', padx=(0,17))
        self.from_date.insert(0,date.today().replace(day=1).isoformat())
        tk.Label(row1, text='TO', bg=WHITE, fg=MUTED, font=('Helvetica Neue', 10, 'bold')).pack(side='left', padx=(0,6))
        self.to_date=ttk.Entry(row1, width=13)
        self.to_date.pack(side='left')
        self.to_date.insert(0,date.today().isoformat())
        row2 = tk.Frame(filters, bg=WHITE)
        row2.pack(fill='x')
        ttk.Button(row2,text='Apply dates',command=self.refresh_records).pack(side='left',padx=(0,8))
        ttk.Button(row2,text='This month',command=self.this_month).pack(side='left',padx=(0,8))
        ttk.Button(row2,text='↓ Export filtered CSV',style='Primary.TButton',command=self.export_csv).pack(side='left')
        self.report_total=ttk.Label(self.records,text='')
        self.report_total.pack(anchor='w',pady=(4,7))
        self.monthly_summary=ttk.Label(self.records,text='')
        self.monthly_summary.pack(anchor='w',pady=(0,9))
        self.att_tree=self.tree_table(self.records,[('student_id','Student ID',140),('name','Name',230),('date','Date',130),('time','Time',110)])

    def make_students(self):
        bar = tk.Frame(self.students, bg=BG)
        bar.pack(fill='x', pady=(0, 10))
        ttk.Button(bar, text='＋ Register student', style='Primary.TButton',
                   command=self.register).pack(side='left', padx=(0, 8))
        ttk.Button(bar, text='Delete selected', style='Danger.TButton',
                   command=self.delete_student).pack(side='left')
        ttk.Button(bar, text='Export student history',
                   command=self.export_student_history).pack(side='right')

        tk.Label(self.students, text='Select a student to view their individual attendance records and trends.',
                 bg=BG, fg=MUTED, font=('Helvetica Neue', 10)).pack(anchor='w', pady=(0, 10))

        # Top student list; lower pane is per-student reporting.
        upper = tk.Frame(self.students, bg=BG, height=205)
        upper.pack(fill='x')
        upper.pack_propagate(False)
        self.student_tree = self.tree_table(upper, [
            ('student_id', 'Student ID', 150), ('name', 'Name', 245),
            ('created', 'Registered', 180)])
        self.student_tree.bind('<<TreeviewSelect>>', self.show_student_history)

        self.student_detail = tk.Frame(self.students, bg=WHITE, bd=0,
                                       highlightthickness=0, relief='flat')
        self.student_detail.pack(fill='both', expand=True, pady=(12, 0))
        self.student_heading = tk.Label(self.student_detail, text='Select a student',
                                        bg=WHITE, fg=TEXT,
                                        font=('Helvetica Neue', 15, 'bold'))
        self.student_heading.pack(anchor='w', padx=16, pady=(12, 3))
        self.student_summary = tk.Label(self.student_detail,
                                        text='Individual attendance records will appear here.',
                                        bg=WHITE, fg=MUTED,
                                        font=('Helvetica Neue', 10))
        self.student_summary.pack(anchor='w', padx=16, pady=(0, 8))
        self.student_chart = tk.Canvas(self.student_detail, bg=WHITE, height=105,
                                       highlightthickness=0)
        self.student_chart.pack(fill='x', padx=14, pady=(0, 8))
        self.student_chart.bind('<Configure>', lambda e: self.redraw_student_chart())
        history_container = tk.Frame(self.student_detail, bg=WHITE)
        history_container.pack(fill='both', expand=True, padx=14, pady=(0, 12))
        self.student_history_tree = self.tree_table(history_container, [
            ('date', 'Attendance date', 170), ('time', 'Check-in time', 170)])
        self.student_history_tree.column('date', stretch=True)
        self.student_history_tree.column('time', stretch=True)
        self.student_chart_data = []

    def selected_student(self):
        selection = self.student_tree.selection()
        return self.student_tree.item(selection[0], 'values') if selection else None

    def show_student_history(self, event=None):
        selected = self.selected_student()
        for row in self.student_history_tree.get_children():
            self.student_history_tree.delete(row)
        self.student_chart_data = []
        if not selected:
            self.student_heading.config(text='Select a student')
            self.student_summary.config(text='Individual attendance records will appear here.')
            self.redraw_student_chart()
            return
        sid, name = selected[:2]
        with conn() as db:
            records = db.execute("SELECT attendance_date,attendance_time FROM attendance WHERE student_id=? ORDER BY attendance_date DESC",
                                 (sid,)).fetchall()
        for item in records:
            self.student_history_tree.insert('', 'end', values=item)
        self.student_heading.config(text=f'{name}  •  {sid}')
        today = date.today().isoformat()
        last30 = sum(1 for d,t in records if d >= (date.today() - __import__('datetime').timedelta(days=29)).isoformat())
        today_marked = any(d == today for d,t in records)
        self.student_summary.config(text=(
            f'Total check-ins: {len(records)}    |    Last 30 days: {last30}    |    '
            f'Today: {"Present" if today_marked else "Not marked"}'))
        self.student_chart_data = [d for d,t in records]
        self.redraw_student_chart()

    def redraw_student_chart(self):
        if not hasattr(self, 'student_chart') or not self.student_chart.winfo_exists():
            return
        from datetime import timedelta
        canvas = self.student_chart
        canvas.delete('all')
        width = max(240, canvas.winfo_width())
        days = [date.today() - timedelta(days=i) for i in range(13,-1,-1)]
        marked = set(getattr(self, 'student_chart_data', []))
        padding = 20
        step = (width - padding*2) / len(days)
        for i,d in enumerate(days):
            x1 = padding + i*step + 2
            fill = ACCENT if d.isoformat() in marked else '#344157'
            canvas.create_rectangle(x1, 12, x1+max(4,step-5), 60, fill=fill, outline='')
            if i in (0, 3, 6, 9, 13):
                canvas.create_text(x1, 80, text=d.strftime('%d %b'), fill=MUTED,
                                   anchor='w', font=('Helvetica Neue', 9))

    def export_student_history(self):
        selected = self.selected_student()
        if not selected:
            messagebox.showinfo('Select student', 'Select a student first.')
            return
        sid, name = selected[:2]
        with conn() as db:
            rows = db.execute("SELECT attendance_date,attendance_time FROM attendance WHERE student_id=? ORDER BY attendance_date DESC",
                              (sid,)).fetchall()
        filename = re.sub(r'[^A-Za-z0-9_-]', '_', sid)
        path = filedialog.asksaveasfilename(defaultextension='.csv',
                                            initialfile=f'attendance_{filename}.csv',
                                            filetypes=[('CSV files','*.csv')])
        if not path: return
        with open(path, 'w', newline='', encoding='utf-8-sig') as f:
            writer = csv.writer(f)
            writer.writerow(['Student ID', 'Name', 'Date', 'Check-in time'])
            writer.writerows([(sid, name, d, t) for d,t in rows])
        self.status.set(f'Exported {len(rows)} attendance records for {name}.')

    def tree_table(self, parent, columns):
        # Remove the native scrollbar and white border entirely. The table still
        # scrolls with a mouse wheel / Mac trackpad and keyboard navigation.
        frame = tk.Frame(parent, bg=WHITE, bd=0, relief='flat', highlightthickness=0)
        frame.pack(fill='both', expand=True)
        tree = ttk.Treeview(frame, columns=[c[0] for c in columns],
                            show='headings', style='Modern.Treeview',
                            selectmode='browse', padding=0)
        for key, label, width in columns:
            tree.heading(key, text=label)
            tree.column(key, width=width, anchor='w')
        tree.pack(fill='both', expand=True, padx=0, pady=0)

        # macOS trackpads dispatch MouseWheel events; Linux may send Button-4/5.
        # Binding on each table (rather than globally) leaves other panes intact.
        def on_wheel(event):
            if event.num == 4:
                delta = -1
            elif event.num == 5:
                delta = 1
            else:
                delta = -1 if event.delta > 0 else 1
            tree.yview_scroll(delta, 'units')
            return 'break'

        tree.bind('<MouseWheel>', on_wheel)
        tree.bind('<Button-4>', on_wheel)
        tree.bind('<Button-5>', on_wheel)
        return tree

    def refresh_all(self):
        self.refresh_dashboard()
        self.refresh_records()
        self.refresh_students()

    def refresh_dashboard(self):
        today=date.today().isoformat()
        with conn() as db:
            students=db.execute('SELECT COUNT(*) FROM students').fetchone()[0]
            present=db.execute('SELECT COUNT(*) FROM attendance WHERE attendance_date=?',(today,)).fetchone()[0]
            rows=db.execute('''SELECT attendance_date, COUNT(*) FROM attendance
                               WHERE attendance_date >= date('now','-6 days')
                               GROUP BY attendance_date''').fetchall()
        for child in self.stats_frame.winfo_children():
            child.destroy()
        stats = [
            ('REGISTERED STUDENTS', students, '♙', ACCENT),
            ('PRESENT TODAY', present, '✓', GREEN),
            ('NOT MARKED TODAY', max(0, students-present), '◷', '#D18A27'),
            ('ATTENDANCE RATE', f'{present/students:.0%}' if students else 'N/A', '◉', '#805AD5'),
        ]
        for j,(label,value,symbol,color) in enumerate(stats):
            card = tk.Frame(self.stats_frame, bg=WHITE, bd=0, highlightthickness=0, relief="flat")
            columns = getattr(self, '_metric_columns', 4)
            card.grid(row=j//columns, column=j%columns, sticky='nsew', padx=5, pady=5, ipady=7)
            tk.Label(card, text=symbol, bg=WHITE, fg=color,
                     font=('Helvetica Neue', 19, 'bold')).pack(anchor='w', padx=18, pady=(5,4))
            tk.Label(card, text=str(value), bg=WHITE, fg=TEXT,
                     font=('Helvetica Neue', 27, 'bold')).pack(anchor='w', padx=18)
            tk.Label(card, text=label, bg=WHITE, fg=MUTED,
                     font=('Helvetica Neue', 9, 'bold')).pack(anchor='w', padx=18, pady=(2,7))
            self.stats_frame.columnconfigure(j%columns, weight=1, uniform='metrics')
        self.chart_values = dict(rows)
        self.root.after(80, self.redraw_chart)

    def draw_chart(self, values):
        if not self.chart.winfo_exists():
            return
        from datetime import timedelta
        self.chart.delete('all')
        width = max(self.chart.winfo_width(), 300)
        height = max(self.chart.winfo_height(), 265)
        days = [date.today()-timedelta(days=i) for i in range(6,-1,-1)]
        counts = [values.get(d.isoformat(), 0) for d in days]
        mode = self.chart_mode.get() if hasattr(self, 'chart_mode') else 'Bars'
        if mode == 'Today split':
            with conn() as db:
                total = db.execute('SELECT COUNT(*) FROM students').fetchone()[0]
                present = db.execute('SELECT COUNT(*) FROM attendance WHERE attendance_date=?',
                                     (date.today().isoformat(),)).fetchone()[0]
            absent = max(0, total - present)
            x, y = width / 2, height / 2 - 8
            radius = min(width * 0.18, height * 0.34, 115)
            bounds = (x-radius, y-radius, x+radius, y+radius)
            # Use ARC outlines rather than filled pie slices: these are reliable
            # on macOS Tk and leave the center transparent for the labels.
            # Tk's exact 360-degree extent can disappear on some builds,
            # so use 359.9 degrees for a visually complete full ring.
            ring_width = max(12, int(radius * 0.18))
            self.chart.create_arc(*bounds, start=90, extent=-359.9,
                                  style=tk.ARC, outline='#344157', width=ring_width)
            if total:
                if present:
                    portion = min(1.0, max(0.0, present / total))
                    angle = min(359.9, 360.0 * portion)
                    self.chart.create_arc(*bounds, start=90, extent=-angle,
                                          style=tk.ARC, outline=ACCENT, width=ring_width)
                self.chart.create_text(x, y-8, text=f'{present}/{total}', fill=TEXT,
                                       font=('Helvetica Neue', 24, 'bold'))
                self.chart.create_text(x, y+23, text='present today', fill=MUTED,
                                       font=('Helvetica Neue', 11))
            else:
                self.chart.create_text(x,y,text='No registered students',fill=TEXT,
                                       font=('Helvetica Neue', 14))
            self.chart.create_text(x, height-26, text=f'Present: {present}      Not marked: {absent}',
                                   fill=MUTED, font=('Helvetica Neue', 11))
            return
        max_count = max(counts+[1])
        left, right, top, bottom = 40, 20, 34, 55
        plot_h = height-top-bottom
        plot_w = width-left-right
        for j in range(5):
            y = top+plot_h*j/4
            self.chart.create_line(left,y,width-right,y,fill='#344157')
            self.chart.create_text(left-13,y,text=f'{max_count*(4-j)/4:g}',
                                   fill=MUTED,font=('Helvetica Neue',9))
        gap = plot_w/7
        points = []
        for i,(d,n) in enumerate(zip(days,counts)):
            bar_w = min(55,gap*.55)
            x = left+gap*(i+.5)
            bar_h = plot_h*n/max_count
            y = top+plot_h-bar_h
            if mode == 'Bars' and n:
                self.chart.create_rectangle(x-bar_w/2,y,x+bar_w/2,top+plot_h,
                                            fill=ACCENT,outline='')
            points.append((x,y))
            self.chart.create_text(x,max(top+10,y-14),text=str(n),fill=TEXT,
                                   font=('Helvetica Neue',10,'bold'))
            self.chart.create_text(x,height-31,text=d.strftime('%d %b'),
                                   fill=MUTED,font=('Helvetica Neue',10))
        if mode == 'Trend line':
            if len(points) > 1:
                self.chart.create_line(*[v for p in points for v in p], fill=ACCENT,
                                       width=3, smooth=False)
            for x,y in points:
                self.chart.create_oval(x-5,y-5,x+5,y+5,fill=ACCENT,outline=WHITE,width=1)

    def check_dates(self):
        try:
            start=date.fromisoformat(self.from_date.get().strip())
            end=date.fromisoformat(self.to_date.get().strip())
            if start>end: raise ValueError('Start date exceeds end date')
            return start.isoformat(),end.isoformat()
        except ValueError:
            messagebox.showerror('Invalid dates','Use YYYY-MM-DD and ensure From is before To.')
            return None

    def this_month(self):
        self.from_date.delete(0,'end')
        self.from_date.insert(0,date.today().replace(day=1).isoformat())
        self.to_date.delete(0,'end')
        self.to_date.insert(0,date.today().isoformat())
        self.refresh_records()

    def report_rows(self):
        interval=self.check_dates()
        if not interval: return None
        with conn() as db:
            return db.execute('''SELECT a.student_id,s.name,a.attendance_date,a.attendance_time
                FROM attendance a JOIN students s ON s.student_id=a.student_id
                WHERE a.attendance_date BETWEEN ? AND ?
                ORDER BY a.attendance_date DESC,a.attendance_time DESC''',interval).fetchall()

    def refresh_records(self):
        rows=self.report_rows()
        if rows is None: return
        for item in self.att_tree.get_children():self.att_tree.delete(item)
        for row in rows:self.att_tree.insert('','end',values=row)
        self.report_total.config(text=f'{len(rows)} attendance records in selected date range')
        per_student=Counter(row[0] for row in rows)
        self.monthly_summary.config(text=f'Unique students present: {len(per_student)}  |  Most days recorded: {max(per_student.values(), default=0)}  |  Total daily check-ins: {len(rows)}')

    def refresh_students(self):
        with conn() as db:
            rows=db.execute('SELECT student_id,name,created_at FROM students ORDER BY name').fetchall()
        for item in self.student_tree.get_children():self.student_tree.delete(item)
        for row in rows:self.student_tree.insert('','end',values=(row[0], row[1], row[2].replace('T', ' ')[:19]))
        if rows and not self.student_tree.selection():
            first = self.student_tree.get_children()[0]
            self.student_tree.selection_set(first)
        self.show_student_history()

    def export_csv(self):
        rows=self.report_rows()
        if rows is None:return
        if not rows:
            messagebox.showinfo('No records','No attendance records match the selected date range.')
            return
        path=filedialog.asksaveasfilename(defaultextension='.csv',initialfile='attendance_report.csv',
                                            filetypes=[('CSV files','*.csv')])
        if not path:return
        with open(path,'w',newline='',encoding='utf-8-sig') as f:
            writer=csv.writer(f)
            writer.writerow(['Student ID','Student name','Date','Time'])
            writer.writerows(rows)
        self.status.set(f'Exported {len(rows)} records to {path}')

    def enroll_student_face(self, sid, name):
        """Enroll an existing or newly approved student; never overwrite saved faces."""
        if self.current_role != 'admin':
            messagebox.showerror('Access denied', 'Only administrators can enroll faces.')
            return
        if self.camera:
            messagebox.showwarning('Camera active', 'Stop the current camera session first.')
            return
        with conn() as db:
            student = db.execute('SELECT name FROM students WHERE student_id=?', (sid,)).fetchone()
        if not student:
            messagebox.showerror('Unknown student', 'Student must be approved or registered first.')
            return
        folder = FACES / sid
        if folder.exists() and any(folder.glob('*.jpg')):
            messagebox.showinfo('Already enrolled',
                'Face samples already exist for this student. To replace them, use a separate supervised re-enrollment procedure.')
            return
        self.root.after(120, lambda: self.open_camera('register', sid, student[0]))

    def register(self, create_default_login=False):
        if self.current_role != 'admin':
            messagebox.showerror('Access denied', 'Only an administrator can register students.')
            return
        if self.camera:
            messagebox.showwarning('Camera active','Stop the camera before registering another student.')
            return
        dlg=tk.Toplevel(self.root)
        dlg.title('Register Student')
        dlg.transient(self.root)
        dlg.grab_set()
        frame=ttk.Frame(dlg,padding=20)
        frame.pack()
        sid_ent=self.label_entry(frame,'Student ID (letters, numbers, _ or -)')
        name_ent=self.label_entry(frame,'Full name')
        def start():
            sid,name=sid_ent.get().strip(),name_ent.get().strip()
            if not valid_student_id(sid) or not name or '|' in name:
                messagebox.showerror('Invalid input','Use an ID of 1-40 letters/numbers/_/-, and a nonempty name without |.',parent=dlg)
                return
            with conn() as db:
                exists=db.execute('SELECT 1 FROM students WHERE student_id=?',(sid,)).fetchone()
            if exists:
                face_dir = FACES / sid
                if face_dir.is_dir() and any(face_dir.glob('*.jpg')):
                    messagebox.showerror('Already enrolled',
                        'This student already has face samples. Remove their existing samples only if you intend to re-enroll.',parent=dlg)
                    return
                with conn() as db:
                    registered_name = db.execute('SELECT name FROM students WHERE student_id=?',(sid,)).fetchone()[0]
                if registered_name.strip().casefold() != name.casefold():
                    messagebox.showerror('Name mismatch','Enter the name stored for this student.',parent=dlg)
                    return
            dlg.destroy()
            # Store this only after validation. Other enrollment entry points never set it.
            self._top_right_default_login = bool(create_default_login)
            self.root.after(150,lambda:self.open_camera('register',sid,name))
        ttk.Button(frame,text='Start face capture',command=start).pack(fill='x',pady=14)

    def open_camera(self,mode,sid=None,name=None):
        if self.camera:
            messagebox.showwarning('Busy','A camera session is already running.')
            return
        try:
            self.detector=face_detector()
            if mode=='recognition':
                self.model,self.mapping=load_model()
            cam=cv2.VideoCapture(0,cv2.CAP_AVFOUNDATION if os.sys.platform=='darwin' else cv2.CAP_ANY)
            if not cam.isOpened():
                cam.release()
                raise RuntimeError('Camera unavailable. Allow camera access in macOS Privacy & Security settings, then restart the editor/terminal.')
            cam.set(cv2.CAP_PROP_FRAME_WIDTH,640)
            cam.set(cv2.CAP_PROP_FRAME_HEIGHT,480)
            self.camera=cam
            self._new_face_capture = (mode == 'register')
            self.camera_mode=mode
            self.camera_sid=sid
            self.camera_name=name
            self.samples=0
            self.last_sample=0
            self.hits=Counter()
            self.marked_this_session=set()
            self.camera_dialog=tk.Toplevel(self.root)
            self.camera_dialog.title('Capture student faces' if mode=='register' else 'Attendance recognition')
            self.camera_dialog.protocol('WM_DELETE_WINDOW',self.stop_camera)
            self.camera_label=ttk.Label(self.camera_dialog)
            self.camera_label.pack(padx=12,pady=12)
            ttk.Label(self.camera_dialog,text='Registration: keep one face visible and move slightly. Recognition: hold still for several frames.').pack(pady=3)
            ttk.Button(self.camera_dialog,text='Stop camera',command=self.stop_camera).pack(pady=12)
            self.status.set('Camera running. Use Stop camera to close it.')
            self.camera_tick()
        except Exception as exc:
            if self.camera:self.stop_camera()
            messagebox.showerror('Camera error',str(exc))
            self.status.set(f'Camera error: {exc}')

    def camera_tick(self):
        if self.camera is None:return
        try:
            ok,frame=self.camera.read()
            if not ok:raise RuntimeError('Camera stopped returning frames.')
            frame=cv2.flip(frame,1)
            gray=cv2.cvtColor(frame,cv2.COLOR_BGR2GRAY)
            faces=face_crops(gray,self.detector)
            if self.camera_mode=='register':
                if len(faces)==1:
                    rect=faces[0]
                    x,y,w,h=map(int,rect)
                    cv2.rectangle(frame,(x,y),(x+w,y+h),(0,180,40),2)
                    if time.monotonic()-self.last_sample>=.25 and self.samples<SAMPLES:
                        out=normalize(gray,rect)
                        folder=FACES/self.camera_sid
                        folder.mkdir(parents=True,exist_ok=True)
                        saved=cv2.imwrite(str(folder/f'{self.samples:03d}.jpg'),out)
                        if saved:self.samples+=1
                        self.last_sample=time.monotonic()
                caption=f'{self.camera_name}: captured {self.samples}/{SAMPLES} samples'
                if self.samples>=SAMPLES:
                    self.finish_registration()
                    return
            else:
                caption='Keep your face visible - multiple stable frames required'
                seen_now=set()
                if len(faces)>1:
                    caption='For safe check-in, show one face at a time'
                for rect in (faces if len(faces)==1 else []):
                    x,y,w,h=map(int,rect)
                    label,dist=self.model.predict(normalize(gray,rect))
                    if dist<=THRESHOLD and label in self.mapping:
                        sid,name=self.mapping[label]
                        seen_now.add(sid)
                        self.hits[sid]+=1
                        color=(0,180,40)
                        shown=f'{name}  distance {dist:.0f}'
                        if self.hits[sid]>=STABLE_FRAMES and sid not in self.marked_this_session:
                            created=mark_attendance(sid)
                            self.marked_this_session.add(sid)
                            self.status.set(f'{name}: attendance marked' if created else f'{name}: already marked today')
                    else:
                        color=(0,0,220)
                        shown='Unknown'
                    cv2.rectangle(frame,(x,y),(x+w,y+h),color,2)
                    cv2.putText(frame,shown,(x,max(22,y-8)),cv2.FONT_HERSHEY_SIMPLEX,.55,color,2)
                for sid in list(self.hits):
                    if sid not in seen_now:self.hits[sid]=0
            cv2.putText(frame,caption,(10,25),cv2.FONT_HERSHEY_SIMPLEX,.58,(0,255,255),2)
            rgb=cv2.cvtColor(frame,cv2.COLOR_BGR2RGB)
            image=ImageTk.PhotoImage(Image.fromarray(rgb))
            if self.camera_label and self.camera_label.winfo_exists():
                self.camera_label.configure(image=image)
                self.camera_label.image=image
            self.camera_job=self.root.after(FRAME_DELAY_MS,self.camera_tick)
        except Exception as exc:
            self.status.set(f'Camera stopped: {exc}')
            self.stop_camera()
            messagebox.showerror('Camera error',str(exc))

    def finish_registration(self):
        sid, name = self.camera_sid, self.camera_name
        default_login = getattr(self, '_top_right_default_login', False)
        self._new_face_capture = False  # Completed samples are kept.
        self.stop_camera()
        with conn() as db:
            db.execute('INSERT OR IGNORE INTO students(student_id,name,created_at) VALUES(?,?,?)',
                       (sid,name,datetime.now().isoformat(timespec='seconds')))
        account_message = ''
        if default_login:
            try:
                username, created = create_top_right_student_login(sid)
                if created:
                    account_message = (f'\nStudent login: {username}'
                                       f'\nTemporary password: {DEFAULT_ADMIN_REGISTER_PASSWORD}'
                                       '\nPassword change required on first login.')
                else:
                    account_message = (f'\nExisting login: {username}'
                                       '\nExisting password was NOT changed.')
            except (ValueError, sqlite3.IntegrityError) as exc:
                account_message = f'\nStudent registered, but login could not be created: {exc}'
        self.refresh_all()
        self.train_async()
        messagebox.showinfo('Student registered',
                            f'{name} registered with {SAMPLES} face samples. Training started.'
                            + account_message)

    def stop_camera(self):
        # Don't leave incomplete face sets that could be mistaken for enrollment.
        if getattr(self, '_new_face_capture', False) and self.camera_mode == 'register' and self.camera_sid:
            folder = FACES / self.camera_sid
            for sample in folder.glob('[0-9][0-9][0-9].jpg'):
                sample.unlink(missing_ok=True)
            if folder.exists() and not any(folder.iterdir()):
                folder.rmdir()
        self._new_face_capture = False
        self._top_right_default_login = False
        if self.camera_job:
            try:self.root.after_cancel(self.camera_job)
            except tk.TclError:pass
            self.camera_job=None
        if self.camera:
            self.camera.release()
            self.camera=None
        if self.camera_dialog:
            try:self.camera_dialog.destroy()
            except tk.TclError:pass
            self.camera_dialog=None
        self.camera_label=None
        self.camera_mode=None
        self.refresh_all() if self.logged_in else None

    def train_async(self):
        if self.camera:
            messagebox.showinfo('Camera active','Stop the camera before retraining.')
            return
        self.status.set('Training the face model...')
        def worker():
            try:result=train_model()
            except Exception as exc:result=f'Training failed: {exc}'
            self.root.after(0,lambda:self.status.set(result))
        threading.Thread(target=worker,daemon=True).start()

    def delete_student(self):
        selected=self.student_tree.selection()
        if not selected:
            messagebox.showinfo('Select student','Select a student on the Students tab.')
            return
        sid,name,*_=self.student_tree.item(selected[0],'values')
        if not messagebox.askyesno('Confirm permanent deletion',f'Delete {name} ({sid}), their face images, and attendance records?'):
            return
        with conn() as db:
            db.execute('DELETE FROM attendance WHERE student_id=?',(sid,))
            db.execute('DELETE FROM students WHERE student_id=?',(sid,))
        import shutil
        shutil.rmtree(FACES/sid,ignore_errors=True)
        self.refresh_all()
        self.train_async()

    def close(self):
        if self.camera:self.stop_camera()
        self.root.destroy()


if __name__=='__main__':
    init_db()
    root=tk.Tk()
    apply_theme(root)
    App(root)
    root.mainloop()
