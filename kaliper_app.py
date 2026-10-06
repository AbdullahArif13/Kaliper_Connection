"""
Kaliper -> MarCom (Keyboard code) -> Python -> CSV lokal + SQL Server (DB qc)

Jalankan pengukuran :  python kaliper_app.py
Cek koneksi DB      :  python kaliper_app.py --cek-db
Password DB TIDAK ditulis di kode; isi lewat environment variable DB_PASSWORD
(lihat start_kaliper.bat).
"""
import csv
import math
import os
import queue
import re
import socket
import sys
import threading
import time
from datetime import datetime

try:
    import pyodbc
except ImportError:  # biar program tetap jalan (mode CSV saja)
    pyodbc = None

# ============================ KONFIGURASI DB ============================
DB_SERVER = os.getenv("DB_SERVER", "gsportal-DEV01")
DB_PORT = int(os.getenv("DB_PORT", "1443"))
DB_NAME = os.getenv("DB_DATABASE", "qc")
DB_USER = os.getenv("DB_USER", "dev01-bedul")
DB_PASSWORD = os.getenv("DB_PASSWORD", "")
DB_DRIVERS = [
    os.getenv("DB_DRIVER_1", "ODBC Driver 18 for SQL Server"),
    os.getenv("DB_DRIVER_2", "ODBC Driver 17 for SQL Server"),
    os.getenv("DB_DRIVER_3", "SQL Server"),
]

LINE_COUNT = 22
SISI_COUNT = 8
LINE_RE = re.compile(r"^C(\d{2})$")


def short_err(e):
    lines = str(e).strip().splitlines()
    return (lines[0] if lines else repr(e))[:220]


EXPECTED_COLS = ["timestamp", "tipe", "no_mold"] + [f"sisi_{c}" for c in "abcdefgh"] + ["avg"]

COLUMNS_SQL = (
    "SELECT c.TABLE_NAME, c.COLUMN_NAME, c.DATA_TYPE, c.CHARACTER_MAXIMUM_LENGTH, c.NUMERIC_PRECISION, "
    "c.NUMERIC_SCALE, c.IS_NULLABLE, c.COLUMN_DEFAULT, "
    "COLUMNPROPERTY(OBJECT_ID(QUOTENAME(c.TABLE_SCHEMA)+'.'+QUOTENAME(c.TABLE_NAME)), c.COLUMN_NAME, 'IsIdentity'), "
    "COLUMNPROPERTY(OBJECT_ID(QUOTENAME(c.TABLE_SCHEMA)+'.'+QUOTENAME(c.TABLE_NAME)), c.COLUMN_NAME, 'IsComputed') "
    "FROM INFORMATION_SCHEMA.COLUMNS c WHERE c.TABLE_NAME LIKE 'tbl[_]c[0-9][0-9]' "
    "ORDER BY c.TABLE_NAME, c.ORDINAL_POSITION"
)


def _type_desc(dt, clen, prec, scale):
    dt = str(dt).lower()
    if clen not in (None, 0):
        return f"{dt}({'MAX' if clen == -1 else clen})"
    if dt in ("decimal", "numeric") and prec is not None:
        return f"{dt}({prec},{scale})"
    return dt


def analisa_kolom(rows):
    """rows = hasil COLUMNS_SQL. Mengembalikan daftar baris laporan (tanpa menyentuh DB)."""
    tables = {}
    for (tbl, col, dt, clen, prec, scale, nullable, default, ident, comp) in rows:
        tables.setdefault(str(tbl).lower(), []).append(
            dict(col=str(col).lower(), dt=str(dt).lower(), clen=clen, prec=prec, scale=scale,
                 null=str(nullable).upper() == "YES", default=default,
                 ident=(ident == 1), comp=(comp == 1)))
    out = []
    if not tables:
        return ["Tidak ada tabel tbl_cNN yang ditemukan."]

    first = sorted(tables)[0]
    out.append(f"Struktur {first} (acuan):")
    for c in tables[first]:
        out.append(f"   {c['col']:<10} {_type_desc(c['dt'], c['clen'], c['prec'], c['scale']):<16}"
                   f"{'NULL' if c['null'] else 'NOT NULL'}{'  (identity)' if c['ident'] else ''}"
                   f"{'  (computed)' if c['comp'] else ''}")

    masalah, perhatian = [], []
    ref_sig = [(c["col"], c["dt"]) for c in tables[first]]
    beda = [t for t in sorted(tables) if [(c["col"], c["dt"]) for c in tables[t]] != ref_sig]
    for t in sorted(tables):
        cols = {c["col"]: c for c in tables[t]}
        hilang = [e for e in EXPECTED_COLS if e not in cols]
        if hilang:
            masalah.append(f"{t}: kolom yang dipakai program TIDAK ADA: {', '.join(hilang)}")
        for c in tables[t]:
            if c["col"] in EXPECTED_COLS or c["ident"] or c["comp"] or c["dt"] in ("timestamp", "rowversion"):
                continue
            if not c["null"] and c["default"] is None:
                masalah.append(f"{t}: kolom '{c['col']}' NOT NULL tanpa default -> INSERT akan GAGAL")
        if "timestamp" in cols and cols["timestamp"]["dt"] in ("timestamp", "rowversion"):
            masalah.append(f"{t}: kolom 'timestamp' bertipe rowversion -> tidak bisa diisi")
    c0 = {c["col"]: c for c in tables[first]}
    for k in [f"sisi_{x}" for x in "abcdefgh"] + ["avg"]:
        c = c0.get(k)
        if not c:
            continue
        if c["dt"] in ("int", "bigint", "smallint", "tinyint", "bit"):
            perhatian.append(f"'{k}' bertipe {c['dt']} -> angka desimal akan dibulatkan")
        elif c["dt"] in ("decimal", "numeric") and (c["scale"] or 0) < 3:
            perhatian.append(f"'{k}' {c['dt']}({c['prec']},{c['scale']}) -> kurang dari 3 desimal, nilai dibulatkan")
    for k in ("tipe", "no_mold"):
        c = c0.get(k)
        if c and c["clen"] not in (None, -1) and c["clen"] < 10:
            perhatian.append(f"'{k}' hanya {c['clen']} karakter -> nilai panjang bisa ditolak/terpotong")
    if beda:
        perhatian.append(f"Struktur berbeda dari {first}: {', '.join(beda)}")

    out.append("")
    out += [f"MASALAH   : {m}" for m in masalah]
    out += [f"PERHATIAN : {m}" for m in perhatian]
    if not masalah and not perhatian:
        out.append("Tidak ada ketidakcocokan terdeteksi untuk INSERT yang dipakai program.")
    return out


# ============================== DATABASE ================================
class QCDatabase:
    def __init__(self, server=DB_SERVER, port=DB_PORT, database=DB_NAME,
                 user=DB_USER, password=DB_PASSWORD):
        self.server, self.port, self.database = server, port, database
        self.user, self.password = user, password

    @staticmethod
    def _q(v):
        # Bungkus nilai agar karakter spesial (; { } @ !) aman di connection string ODBC
        return "{" + str(v).replace("}", "}}") + "}"

    def _conn_str(self, driver):
        return (f"DRIVER={{{driver}}};SERVER={self.server},{self.port};"
                f"DATABASE={self.database};UID={self._q(self.user)};"
                f"PWD={self._q(self.password)};TrustServerCertificate=yes;")

    def connect(self):
        if pyodbc is None:
            raise RuntimeError("pyodbc belum terpasang (pip install pyodbc)")
        if not self.password:
            raise RuntimeError("DB_PASSWORD belum diisi")
        last = None
        for drv in DB_DRIVERS:
            try:
                return pyodbc.connect(self._conn_str(drv), autocommit=True, timeout=5)
            except Exception as e:
                last = e
        raise last

    @staticmethod
    def table_for_line(line):
        m = LINE_RE.fullmatch(str(line).strip().upper())
        if not m or not 1 <= int(m.group(1)) <= LINE_COUNT:
            raise ValueError(f"Line tidak valid: {line}")
        return f"tbl_c{int(m.group(1)):02d}"

    def get_tipe(self, mold):
        # Query SAMA dengan server_test.py lama: SELECT mold_number, tipe_grid FROM mold_mapping
        conn = self.connect()
        try:
            cur = conn.cursor()
            cur.execute("SELECT mold_number, tipe_grid FROM mold_mapping")
            target = mold.strip().upper()
            for r in cur.fetchall():
                if r[0] and str(r[0]).strip().upper() == target:
                    return str(r[1]).strip() if r[1] else None
            return None
        finally:
            conn.close()

    def insert_line(self, line, tipe, mold, sides, avg, ts):
        table = self.table_for_line(line)
        conn = self.connect()
        try:
            conn.cursor().execute(
                f"INSERT INTO {table} (timestamp, tipe, no_mold, sisi_a, sisi_b, sisi_c, sisi_d, "
                "sisi_e, sisi_f, sisi_g, sisi_h, [avg]) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                ts, tipe, mold, *sides[:8], avg)
        finally:
            conn.close()

    def diagnose(self):
        print(f"Server   : {self.server}  Port: {self.port}")
        print(f"Database : {self.database}  User: {self.user}")
        print(f"Password : {'terisi' if self.password else 'KOSONG (set DB_PASSWORD)'}\n")

        print("[1] Driver ODBC")
        if pyodbc is None:
            print("    GAGAL - pyodbc belum terpasang: pip install pyodbc"); return
        drivers = [d for d in pyodbc.drivers() if "SQL Server" in d]
        print("    Terpasang:", ", ".join(drivers) or "tidak ada driver SQL Server!")

        print("[2] Jaringan (TCP)")
        open_ports = []
        for p in dict.fromkeys([self.port, 1433, 1443]):
            try:
                socket.create_connection((self.server, p), timeout=3).close()
                print(f"    Port {p}: TERBUKA"); open_ports.append(p)
            except socket.gaierror:
                print(f"    Nama host '{self.server}' tidak ditemukan (DNS/VPN/jaringan?)"); break
            except Exception as e:
                print(f"    Port {p}: tertutup/timeout ({short_err(e)})")
        if open_ports and self.port not in open_ports:
            print(f"    >> Port {self.port} tertutup tapi {open_ports[0]} terbuka. "
                  f"Coba set DB_PORT={open_ports[0]}")

        print("[3] Login & database")
        try:
            conn = self.connect()
        except Exception as e:
            print("    GAGAL:", short_err(e)); return
        try:
            cur = conn.cursor()
            cur.execute("SELECT DB_NAME(), SUSER_SNAME()")
            db, usr = cur.fetchone()
            print(f"    OK - terhubung ke DB '{db}' sebagai '{usr}'")

            print("[4] Tabel yang dibutuhkan")
            cur.execute("SELECT TABLE_NAME FROM INFORMATION_SCHEMA.TABLES")
            ada = {r[0].lower() for r in cur.fetchall()}
            butuh = [f"tbl_c{i:02d}" for i in range(1, LINE_COUNT + 1)]
            hilang = [t for t in butuh if t not in ada]
            print(f"    tbl_c01..tbl_c22 : {len(butuh)-len(hilang)}/{len(butuh)} ada"
                  + (f"  (hilang: {', '.join(hilang)})" if hilang else ""))
            print(f"    mold_mapping     : {'ada' if 'mold_mapping' in ada else 'TIDAK ADA'}")
            if hilang:
                print("    >> Tabel yang hilang harus disiapkan DBA/Anda; program ini TIDAK mengubah struktur DB.")

            ada_tbl = next((t for t in butuh if t in ada), None)
            if ada_tbl:
                cur.execute("SELECT HAS_PERMS_BY_NAME(?, 'OBJECT', 'INSERT')", ada_tbl)
                ok = cur.fetchone()[0]
                print(f"[5] Izin INSERT ke {ada_tbl}: {'YA' if ok == 1 else 'TIDAK'}")

                print("[6] Struktur kolom tbl_cNN (hanya membaca)")
                try:
                    cur.execute(COLUMNS_SQL)
                    for ln in analisa_kolom(cur.fetchall()):
                        print("    " + ln)
                except Exception as e:
                    print("    GAGAL membaca struktur:", short_err(e))

                print(f"[7] Contoh 3 data terakhir di {ada_tbl} (untuk dibandingkan formatnya)")
                try:
                    cur.execute(f"SELECT TOP 3 * FROM {ada_tbl} ORDER BY [timestamp] DESC")
                    names = [d[0] for d in cur.description]
                    rows = cur.fetchall()
                    print("    " + " | ".join(names))
                    for r in rows:
                        print("    " + " | ".join(str(v) for v in r))
                    if not rows:
                        print("    (tabel kosong)")
                except Exception as e:
                    print("    GAGAL membaca contoh data:", short_err(e))

            if "mold_mapping" in ada:
                print("[8] mold_mapping")
                try:
                    cur.execute("SELECT COLUMN_NAME, DATA_TYPE FROM INFORMATION_SCHEMA.COLUMNS "
                                "WHERE TABLE_NAME='mold_mapping' ORDER BY ORDINAL_POSITION")
                    cols = [(r[0], r[1]) for r in cur.fetchall()]
                    print("    kolom:", ", ".join(f"{n} ({t})" for n, t in cols))
                    nm = {n.lower() for n, _ in cols}
                    kurang = [k for k in ("mold_number", "tipe_grid") if k not in nm]
                    if kurang:
                        print("    MASALAH: kolom dipakai program tidak ada:", ", ".join(kurang))
                    cur.execute("SELECT COUNT(*) FROM mold_mapping")
                    print("    jumlah baris:", cur.fetchone()[0])
                except Exception as e:
                    print("    GAGAL:", short_err(e))
        except Exception as e:
            print("    GAGAL:", short_err(e))
        finally:
            conn.close()


# ============================== APLIKASI ================================
class KaliperStandalone:
    def __init__(self, filename="hasil_pengukuran.csv", db=None):
        self.current_line = 0          # 0 = C01 ... 21 = C22
        self.filename = filename
        self.db = db
        self.buffer = []               # nilai sisi yang sudah masuk untuk line aktif
        self.mold = ""
        self.tipe = ""
        self.last_mold = ""
        self.pending = []              # baris yang gagal dikirim ke DB, dicoba lagi nanti
        self.auto_flush = True         # GUI mematikan ini & mengirim lewat thread terpisah
        self.on_line_complete = None   # callback(record) saat 8 sisi lengkap
        self.on_flush_result = None    # callback(line, ok, pesan) hasil kirim ke DB
        self._flush_lock = threading.Lock()
        self._init_csv()

    @property
    def current_sisi(self):
        return len(self.buffer)

    @property
    def line_name(self):
        return f"C{self.current_line + 1:02d}"

    def _init_csv(self):
        try:
            with open(self.filename, mode='x', newline='') as f:
                csv.writer(f).writerow(["Baris", "Sisi", "Nilai (mm)", "Waktu"])
        except FileExistsError:
            pass

    def set_context(self, mold, tipe):
        self.mold, self.tipe = mold.strip().upper(), tipe.strip().upper()

    def process_input(self, val_str):
        """Return True bila nilai diterima."""
        val_clean = val_str.replace(',', '.').strip()
        if not val_clean:
            return False
        try:
            val = float(val_clean)
            if not math.isfinite(val):
                raise ValueError
        except ValueError:
            print("[ERROR] Input bukan angka valid! Silakan tembak ulang kaliper.")
            return False
        if not self.mold or not self.tipe:
            print("[ERROR] No. Mold / Tipe belum diisi untuk line ini.")
            return False

        sisi_num = self.current_sisi + 1
        timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
        with open(self.filename, mode='a', newline='') as f:
            csv.writer(f).writerow([self.line_name, f"Sisi {sisi_num}", val, timestamp])
        self.buffer.append(val)
        print(f"[OK] {self.line_name} | Sisi {sisi_num} -> {val:.3f} mm")

        if len(self.buffer) == SISI_COUNT:
            self._commit_line()
        return True

    def undo(self):
        if not self.buffer:
            print("Tidak ada nilai untuk dibatalkan pada line ini.")
            return
        v = self.buffer.pop()
        print(f"[UNDO] Sisi {len(self.buffer) + 1} ({v:.3f}) dibatalkan. Tembak ulang sisi tersebut.")

    def goto_line(self, n):
        if self.buffer:
            print("Selesaikan/undo dulu nilai di line aktif sebelum pindah line.")
            return False
        if not 1 <= n <= LINE_COUNT:
            print(f"Line harus 1-{LINE_COUNT}.")
            return False
        self.current_line = n - 1
        return True

    def _commit_line(self):
        sides = list(self.buffer)
        rec = {
            "line": self.line_name, "tipe": self.tipe, "mold": self.mold,
            "sides": sides, "avg": sum(sides) / SISI_COUNT, "ts": datetime.now(),
        }
        self.pending.append(rec)
        print(f"[LINE {rec['line']} LENGKAP] Mold {rec['mold']} | Tipe {rec['tipe']} | "
              f"Rata-rata {rec['avg']:.3f} mm")
        self.last_mold = self.mold
        self.buffer, self.mold, self.tipe = [], "", ""
        if self.on_line_complete:
            self.on_line_complete(rec)
        if self.auto_flush:
            self.flush_pending()
        self._auto_advance()

    def _notify(self, line, ok, msg):
        if self.on_flush_result:
            self.on_flush_result(line, ok, msg)

    def flush_pending(self):
        if not self._flush_lock.acquire(blocking=False):
            return                      # sudah ada proses kirim yang berjalan
        try:
            while self.pending:
                r = self.pending[0]
                if self.db is None:
                    print(f"[DB] Mode offline - {len(self.pending)} baris menunggu (data mentah ada di {self.filename}).")
                    self._notify(r["line"], False, "Mode offline (DB_PASSWORD kosong) - data ada di CSV")
                    return
                try:
                    self.db.insert_line(r["line"], r["tipe"], r["mold"], r["sides"], r["avg"], r["ts"])
                except Exception as e:
                    msg = short_err(e)
                    print(f"[DB GAGAL] {r['line']}: {msg}")
                    print(f"           {len(self.pending)} baris menunggu, dicoba lagi saat line berikutnya selesai.")
                    self._notify(r["line"], False, msg)
                    return
                print(f"[DB OK] {r['line']} tersimpan di SQL Server.")
                self.pending.pop(0)
                self._notify(r["line"], True, "tersimpan")
        finally:
            self._flush_lock.release()

    def _auto_advance(self):
        self.current_line += 1
        if self.current_line >= LINE_COUNT:
            self.current_line = 0
            print("\n=== SEMUA BARIS (C01-C22) SELESAI! MENGULANG DARI C01 ===\n")


# ============================ TAMPILAN TABEL ============================
class KaliperGUI:
    """Tabel ala Excel: Enter / tembak kaliper -> kursor pindah ke sel sebelah kanan."""
    COL_MOLD, COL_TIPE, COL_SISI0 = 0, 1, 2
    FONT = ("Consolas", 12)
    C_ACTIVE, C_IDLE, C_OK, C_FAIL, C_SEND = "#FFF2A8", "#FFFFFF", "#DDF3DD", "#F8D7DA", "#E3EEFF"

    def __init__(self, app):
        import tkinter as tk
        self.tk = tk
        self.app = app
        app.auto_flush = False
        app.on_line_complete = self._on_line_complete
        app.on_flush_result = lambda line, ok, msg: self.ui_queue.put(lambda: self._apply_flush(line, ok, msg))

        self.ui_queue = queue.Queue()
        self.row, self.col = 0, self.COL_MOLD
        self.finished = False
        self.tipe_cache = {}
        self._lookup_token = 0
        self.locked = [False] * LINE_COUNT
        self.row_state = ["idle"] * LINE_COUNT
        self.entries, self.line_lbls, self.avg_lbls, self.status_lbls = [], [], [], []

        self.root = tk.Tk()
        self.root.title("Kaliper QC - Input Pengukuran")
        self.root.geometry("1280x700")
        self._build()
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self.root.after(100, self._drain)
        self._check_db()

    # ------------------------------ UI -----------------------------------
    def _build(self):
        tk = self.tk
        top = tk.Frame(self.root, padx=8, pady=6)
        top.pack(fill="x")
        self.lbl_db = tk.Label(top, text="DB: memeriksa...", font=("Segoe UI", 10, "bold"), anchor="w")
        self.lbl_db.pack(side="left")
        self.lbl_pending = tk.Label(top, text="", font=("Segoe UI", 10), padx=16)
        self.lbl_pending.pack(side="left")
        tk.Button(top, text="Mulai ulang tabel", command=self.reset_table).pack(side="right", padx=3)
        tk.Button(top, text="Kirim ulang ke DB", command=self._kick_flush).pack(side="right", padx=3)
        tk.Button(top, text="Undo (Ctrl+Z)", command=self.undo).pack(side="right", padx=3)

        self.lbl_info = tk.Label(
            self.root, anchor="w", padx=8, font=("Segoe UI", 10), fg="#333",
            text="Isi No. Mold (Enter) -> tembak kaliper, tiap Enter pindah ke sel kanan. "
                 "Setelah Sisi H, baris otomatis masuk DB dan turun ke line berikutnya.  Esc = hapus sel, Ctrl+Z = undo.")
        self.lbl_info.pack(fill="x")

        wrap = tk.Frame(self.root)
        wrap.pack(fill="both", expand=True, padx=8, pady=6)
        self.canvas = tk.Canvas(wrap, highlightthickness=0)
        sb = tk.Scrollbar(wrap, orient="vertical", command=self.canvas.yview)
        self.canvas.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        self.canvas.pack(side="left", fill="both", expand=True)
        self.gf = tk.Frame(self.canvas)
        self.canvas.create_window((0, 0), window=self.gf, anchor="nw")
        self.gf.bind("<Configure>", lambda e: self.canvas.configure(scrollregion=self.canvas.bbox("all")))
        self.canvas.bind_all("<MouseWheel>", lambda e: self.canvas.yview_scroll(int(-e.delta / 120), "units"))

        heads = ["Line", "No. Mold", "Tipe"] + [f"Sisi {c.upper()}" for c in "abcdefgh"] + ["Rata-rata", "Status"]
        for j, h in enumerate(heads):
            tk.Label(self.gf, text=h, font=("Segoe UI", 10, "bold"), bg="#2F3E55", fg="white",
                     padx=4, pady=4).grid(row=0, column=j, sticky="nsew", padx=1, pady=1)

        for i in range(LINE_COUNT):
            ll = tk.Label(self.gf, text=f"C{i + 1:02d}", font=self.FONT, width=5)
            ll.grid(row=i + 1, column=0, sticky="nsew", padx=1, pady=1)
            self.line_lbls.append(ll)
            row_entries = []
            for j in range(2 + SISI_COUNT):
                e = tk.Entry(self.gf, width=11 if j < 2 else 8, font=self.FONT, justify="center",
                             relief="solid", bd=1, highlightthickness=2, highlightcolor="#1E6FFF")
                e.grid(row=i + 1, column=j + 1, sticky="nsew", padx=1, pady=1)
                e.bind("<Return>", lambda ev, r=i, c=j: self._on_enter(r, c))
                e.bind("<KP_Enter>", lambda ev, r=i, c=j: self._on_enter(r, c))
                e.bind("<FocusIn>", lambda ev, r=i, c=j: self._on_focus(r, c))
                e.bind("<Escape>", lambda ev: self._clear_current())
                e.bind("<Control-z>", lambda ev: self.undo())
                row_entries.append(e)
            self.entries.append(row_entries)
            al = tk.Label(self.gf, text="", font=self.FONT, width=8)
            al.grid(row=i + 1, column=2 + SISI_COUNT + 1, sticky="nsew", padx=1, pady=1)
            sl = tk.Label(self.gf, text="", font=("Segoe UI", 10), width=18, anchor="w")
            sl.grid(row=i + 1, column=2 + SISI_COUNT + 2, sticky="nsew", padx=1, pady=1)
            self.avg_lbls.append(al)
            self.status_lbls.append(sl)
            self._paint_row(i)

        self._prepare_row(0)
        self.root.after(200, self._first_focus)

    def _first_focus(self):
        self.root.focus_force()
        self.focus_current()

    # --------------------------- util tampilan ---------------------------
    @staticmethod
    def _set(entry, text, readonly=False):
        entry.config(state="normal")
        entry.delete(0, "end")
        entry.insert(0, text)
        if readonly:
            entry.config(state="readonly")

    def _paint_row(self, r):
        st = self.row_state[r]
        if st == "ok":
            color = self.C_OK
        elif st == "fail":
            color = self.C_FAIL
        elif st == "send":
            color = self.C_SEND
        elif r == self.row and not self.finished:
            color = self.C_ACTIVE
        else:
            color = self.C_IDLE
        for e in self.entries[r]:
            e.config(bg=color, readonlybackground=color, highlightbackground=color)
        for lab in (self.line_lbls[r], self.avg_lbls[r], self.status_lbls[r]):
            lab.config(bg=color)

    def _set_status(self, r, text, fg="black"):
        self.status_lbls[r].config(text=text, fg=fg)

    def _update_pending_label(self):
        n = len(self.app.pending)
        self.lbl_pending.config(text=f"Menunggu kirim ke DB: {n}", fg=("#B00020" if n else "#2E7D32"))

    def focus_current(self):
        e = self.entries[self.row][self.col]
        e.focus_set()
        e.select_range(0, "end")
        e.icursor("end")
        self._ensure_visible(e)

    def _ensure_visible(self, e):
        try:
            self.root.update_idletasks()
            h = max(1, self.gf.winfo_height())
            top, bottom = self.canvas.yview()
            y0, y1 = e.winfo_y() / h, (e.winfo_y() + e.winfo_height()) / h
            if y0 < top:
                self.canvas.yview_moveto(max(0.0, y0 - 0.02))
            elif y1 > bottom:
                self.canvas.yview_moveto(min(1.0, y1 - (bottom - top) + 0.02))
        except Exception:
            pass

    def _flash(self, e):
        e.config(bg="#FF9C9C")
        self.root.after(350, lambda: self._paint_row(self.row))

    def _bell(self):
        try:
            self.root.bell()
        except Exception:
            pass

    # ------------------------------ alur ---------------------------------
    def _prepare_row(self, r):
        """Mold diisi default dari mold sebelumnya (tinggal Enter)."""
        self.row, self.col = r, self.COL_MOLD
        m = self.entries[r][self.COL_MOLD]
        if not m.get().strip() and self.app.last_mold:
            self._set(m, self.app.last_mold)
        for i in range(LINE_COUNT):
            self._paint_row(i)

    def _on_focus(self, r, c):
        if (r, c) == (self.row, self.col) or self.finished:
            return
        if r != self.row:
            if (not self.locked[r]) and (not self.app.buffer) and c <= self.COL_TIPE:
                self._prepare_row(r)          # pindah ke baris lain (klik) selama belum ada sisi terisi
                self.col = c
        elif c < self.COL_SISI0 and not self.app.buffer:
            self.col = c                      # boleh ubah mold/tipe sebelum mulai menembak
        self.root.after_idle(self.focus_current)

    def _on_enter(self, r, c):
        if (r, c) == (self.row, self.col) and not self.finished:
            if c == self.COL_MOLD:
                self._enter_mold()
            elif c == self.COL_TIPE:
                self._enter_tipe()
            else:
                self._enter_sisi(c)
        return "break"

    def _enter_mold(self):
        r = self.row
        mold = self.entries[r][self.COL_MOLD].get().strip().upper() or self.app.last_mold
        if not mold:
            self._bell()
            return
        self._set(self.entries[r][self.COL_MOLD], mold)
        tipe = self.tipe_cache.get(mold)
        te = self.entries[r][self.COL_TIPE]
        if tipe:
            self._set(te, tipe)
            self._begin_sisi()
            return
        self._set(te, "")
        self.col = self.COL_TIPE
        self._lookup_token += 1
        token = self._lookup_token
        if self.app.db is None:
            self._set_status(r, "Ketik tipe grid", "#B26A00")
        else:
            self._set_status(r, "Mencari tipe di DB...", "#555")

            def work():
                try:
                    tp, err = self.app.db.get_tipe(mold), ""
                except Exception as ex:
                    tp, err = None, short_err(ex)
                self.ui_queue.put(lambda: self._lookup_done(token, r, mold, tp, err))
            threading.Thread(target=work, daemon=True).start()
        self.focus_current()

    def _lookup_done(self, token, r, mold, tipe, err):
        te = self.entries[r][self.COL_TIPE]
        if token != self._lookup_token or (self.row, self.col) != (r, self.COL_TIPE) or te.get().strip():
            return                            # sudah berpindah / operator mengetik sendiri
        if tipe:
            self.tipe_cache[mold] = tipe.upper()
            self._set(te, tipe.upper())
            self._begin_sisi()
        else:
            self._set_status(r, "Tipe tidak ketemu - ketik manual" if not err else "DB error - ketik tipe manual", "#B26A00")
            self.focus_current()

    def _enter_tipe(self):
        r = self.row
        tipe = self.entries[r][self.COL_TIPE].get().strip().upper()
        if not tipe:
            self._bell()
            return
        self._set(self.entries[r][self.COL_TIPE], tipe)
        self.tipe_cache[self.entries[r][self.COL_MOLD].get().strip().upper()] = tipe
        self._begin_sisi()

    def _begin_sisi(self):
        r = self.row
        mold = self.entries[r][self.COL_MOLD].get().strip().upper()
        tipe = self.entries[r][self.COL_TIPE].get().strip().upper()
        self.app.current_line = r
        self.app.set_context(mold, tipe)
        self.col = self.COL_SISI0 + len(self.app.buffer)
        self._set_status(r, "Menunggu kaliper...", "#555")
        self.focus_current()

    def _enter_sisi(self, c):
        r = self.row
        e = self.entries[r][c]
        ok = self.app.process_input(e.get())
        if not ok:
            self._flash(e)
            self.focus_current()
            return
        if self.locked[r]:                    # sisi ke-8 -> baris sudah dikunci oleh _on_line_complete
            return
        val = float(e.get().replace(",", ".").strip())
        self._set(e, f"{val:.3f}", readonly=True)
        self.col = c + 1
        self._set_status(r, f"Terisi {self.col - self.COL_SISI0}/{SISI_COUNT}", "#555")
        self.focus_current()

    def _on_line_complete(self, rec):
        r = int(rec["line"][1:]) - 1
        self.locked[r] = True
        for k, v in enumerate(rec["sides"]):
            self._set(self.entries[r][self.COL_SISI0 + k], f"{v:.3f}")
        for e in self.entries[r]:
            e.config(state="readonly")
        self.avg_lbls[r].config(text=f"{rec['avg']:.3f}")
        self.row_state[r] = "send"
        self._set_status(r, "Mengirim ke DB..." if self.app.db is not None else "Offline - hanya CSV",
                         "#1E5FBF" if self.app.db is not None else "#B00020")
        self._paint_row(r)
        self._update_pending_label()
        self._kick_flush()
        if r + 1 < LINE_COUNT:
            self._prepare_row(r + 1)
            self.focus_current()
        else:
            self.finished = True
            self._paint_row(r)
            self.lbl_info.config(text="SEMUA LINE C01-C22 SELESAI. Klik 'Mulai ulang tabel' untuk siklus berikutnya.",
                                 fg="#2E7D32", font=("Segoe UI", 10, "bold"))

    # --------------------------- kirim ke DB -----------------------------
    def _kick_flush(self):
        if self.app.pending:
            threading.Thread(target=self.app.flush_pending, daemon=True).start()

    def _apply_flush(self, line, ok, msg):
        r = int(line[1:]) - 1
        if ok:
            self.row_state[r] = "ok"
            self._set_status(r, "Tersimpan di DB", "#2E7D32")
            self._paint_row(r)
            self._show_db(True, "")            # DB terbukti hidup -> label atas kembali hijau
        else:
            for p in list(self.app.pending):
                pr = int(p["line"][1:]) - 1
                self.row_state[pr] = "fail"
                self._set_status(pr, "GAGAL - menunggu kirim ulang", "#B00020")
                self._paint_row(pr)
            self.lbl_db.config(text=f"DB: gagal kirim ({msg[:80]})", fg="#B00020")
        self._update_pending_label()

    def _check_db(self):
        if self.app.db is None:
            self.lbl_db.config(text="DB: MODE CSV SAJA (DB_PASSWORD belum diisi - jalankan lewat start_kaliper.bat)", fg="#B26A00")
            return

        def work():
            try:
                self.app.db.connect().close()
                res = (True, "")
            except Exception as ex:
                res = (False, short_err(ex))
            self.ui_queue.put(lambda: self._show_db(*res))
        threading.Thread(target=work, daemon=True).start()

    def _show_db(self, ok, msg):
        if ok:
            self.lbl_db.config(text=f"DB: terhubung ({DB_SERVER}:{DB_PORT} / {DB_NAME})", fg="#2E7D32")
        else:
            self.lbl_db.config(text=f"DB: belum terhubung ({msg[:90]}) - data ditahan & dikirim ulang otomatis", fg="#B00020")

    # ----------------------------- perintah ------------------------------
    def undo(self):
        if self.finished or self.col <= self.COL_SISI0 or not self.app.buffer:
            self._bell()
            return "break"
        self.app.undo()
        self.col -= 1
        self._set(self.entries[self.row][self.col], "")
        self._set_status(self.row, f"Terisi {self.col - self.COL_SISI0}/{SISI_COUNT}", "#555")
        self.focus_current()
        return "break"

    def _clear_current(self):
        if not self.finished and self.col < self.COL_SISI0 + SISI_COUNT:
            e = self.entries[self.row][self.col]
            if str(e.cget("state")) == "normal":
                e.delete(0, "end")
        return "break"

    def reset_table(self):
        from tkinter import messagebox
        extra = f"\n\nPERINGATAN: {len(self.app.pending)} baris belum masuk DB (masih ditahan di memori & ada di CSV)." if self.app.pending else ""
        if not messagebox.askyesno("Mulai ulang tabel", "Kosongkan tabel dan mulai lagi dari C01?" + extra, parent=self.root):
            return
        for r in range(LINE_COUNT):
            for e in self.entries[r]:
                self._set(e, "")
            self.avg_lbls[r].config(text="")
            self._set_status(r, "")
            self.locked[r], self.row_state[r] = False, "idle"
        self.app.buffer, self.app.mold, self.app.tipe = [], "", ""
        self.finished = False
        self.lbl_info.config(text="Isi No. Mold (Enter) -> tembak kaliper, tiap Enter pindah ke sel kanan.",
                             fg="#333", font=("Segoe UI", 10))
        self._prepare_row(0)
        self.focus_current()

    def _drain(self):
        try:
            while True:
                self.ui_queue.get_nowait()()
        except queue.Empty:
            pass
        except Exception as e:
            print("[GUI] error:", short_err(e))
        self.root.after(100, self._drain)

    def _on_close(self):
        from tkinter import messagebox
        if self.app.pending and not messagebox.askyesno(
                "Keluar", f"{len(self.app.pending)} baris belum masuk DB (data mentah ada di CSV).\nTetap keluar?", parent=self.root):
            return
        self.root.destroy()

    def run(self):
        self._update_pending_label()
        self.root.mainloop()


# ================================ CLI ===================================
def make_db():
    if not DB_PASSWORD:
        print("[DB] DB_PASSWORD belum diisi -> mode CSV saja (jalankan lewat start_kaliper.bat).")
        return None
    return QCDatabase()


def prompt_context(app):
    default = app.last_mold
    while True:
        hint = f" (Enter = {default})" if default else ""
        mold = input(f"[{app.line_name}] No. Mold{hint}: ").strip().upper() or default
        if mold:
            break
    tipe = None
    if app.db is not None:
        try:
            tipe = app.db.get_tipe(mold)
        except Exception as e:
            print(f"[DB] Gagal cek tipe: {short_err(e)}")
    if tipe:
        print(f"Tipe grid dari DB: {tipe}")
    else:
        while not tipe:
            tipe = input("Tipe grid tidak ditemukan otomatis. Ketik tipe grid: ").strip().upper()
    app.set_context(mold, tipe)


def main_terminal():
    db = make_db()
    if db is not None:
        try:
            db.connect().close()
            print(f"[DB] Terhubung ke {DB_SERVER}:{DB_PORT} / {DB_NAME}")
        except Exception as e:
            print(f"[DB] Belum bisa terhubung: {short_err(e)}")
            print("     Pengukuran tetap jalan; data dicoba dikirim ulang otomatis. Cek: python kaliper_app.py --cek-db")

    app = KaliperStandalone(db=db)
    print("\n=== PENGUKURAN KALIPER -> DATABASE (mode terminal) ===")
    print("1. MarCom: 'Keyboard code'. Kursor harus aktif di terminal ini.")
    print("2. Perintah: 'u' = batalkan nilai terakhir | 'l 5' = pindah ke C05 | 'q' = keluar.\n")

    while True:
        if app.current_sisi == 0 and not app.mold:
            prompt_context(app)
        target = f"[{app.line_name} - Sisi {app.current_sisi + 1}/{SISI_COUNT}]"
        user_in = input(f"Arahkan kaliper ke {target}: ").strip()
        low = user_in.lower()
        if low == 'q':
            if app.pending:
                print(f"PERINGATAN: {len(app.pending)} baris belum masuk DB (data mentah ada di CSV).")
            print("Pengukuran selesai.")
            break
        if low == 'u':
            app.undo()
        elif low.startswith('l ') and low[2:].strip().isdigit():
            if app.goto_line(int(low[2:])):
                app.mold = app.tipe = ""
        else:
            app.process_input(user_in)


def main():
    if "--cek-db" in sys.argv:
        QCDatabase().diagnose()
        return
    if "--terminal" in sys.argv:
        main_terminal()
        return
    try:
        import tkinter  # noqa: F401
    except ImportError:
        print("[INFO] tkinter tidak tersedia -> memakai mode terminal.")
        main_terminal()
        return
    app = KaliperStandalone(db=make_db())
    KaliperGUI(app).run()


if __name__ == "__main__":
    main()