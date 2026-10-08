"""
Query tambahan untuk tampilan web (Riwayat, Grafik, daftar Mold).
Hanya memakai QCDatabase.connect() dari kaliper_app.py - struktur DB tidak diubah.
Semua nilai filter memakai parameter (?), nama tabel hanya dari tbl_c01..tbl_c22.
"""
import re
from datetime import datetime

from kaliper_app import LINE_COUNT, QCDatabase

LINES = [f"C{i:02d}" for i in range(1, LINE_COUNT + 1)]
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _norm_date(s):
    s = (s or "").strip()
    if not _DATE_RE.match(s):
        return None
    try:
        datetime.strptime(s, "%Y-%m-%d")
        return s
    except ValueError:
        return None


def _norm_time(s):
    s = (s or "").strip()
    for fmt in ("%H:%M", "%H:%M:%S"):
        try:
            return datetime.strptime(s, fmt).strftime("%H:%M:%S")
        except ValueError:
            continue
    return None


def list_molds(db):
    """[{'mold':..., 'tipe':...}] dari tabel mold_mapping."""
    conn = db.connect()
    try:
        cur = conn.cursor()
        cur.execute("SELECT mold_number, tipe_grid FROM mold_mapping")
        return [{"mold": str(r[0]).strip().upper(), "tipe": str(r[1]).strip().upper() if r[1] else ""}
                for r in cur.fetchall() if r[0]]
    finally:
        conn.close()


def add_mold(db, mold, tipe):
    """Tambah baris ke mold_mapping bila belum ada. Return (ok, pesan)."""
    mold, tipe = (mold or "").strip().upper(), (tipe or "").strip().upper()
    if not mold or not tipe:
        return False, "No. Mold dan Tipe wajib diisi."
    conn = db.connect()
    try:
        cur = conn.cursor()
        cur.execute("SELECT 1 FROM mold_mapping WHERE UPPER(LTRIM(RTRIM(mold_number))) = ?", mold)
        if cur.fetchone():
            return True, "Mold sudah ada di database."
        cur.execute("INSERT INTO mold_mapping (mold_number, tipe_grid) VALUES (?, ?)", mold, tipe)
        return True, "Mold baru tersimpan ke database."
    finally:
        conn.close()


def _union(line, mold, tipe, date_from, date_to, time_from, time_to):
    parts, params = [], []
    for label in LINES:
        if line and label != line:
            continue
        table = f"tbl_c{label[1:]}"
        cond = ["1=1"]
        if mold:
            cond.append("UPPER(LTRIM(RTRIM(no_mold))) = ?"); params.append(mold)
        if tipe:
            cond.append("UPPER(LTRIM(RTRIM(tipe))) = ?"); params.append(tipe)
        if date_from:
            cond.append("CAST([timestamp] AS date) >= ?"); params.append(date_from)
        if date_to:
            cond.append("CAST([timestamp] AS date) <= ?"); params.append(date_to)
        if time_from:
            cond.append("CAST([timestamp] AS time) >= ?"); params.append(time_from)
        if time_to:
            cond.append("CAST([timestamp] AS time) <= ?"); params.append(time_to)
        parts.append(
            f"SELECT '{label}' AS line, [timestamp] AS ts, tipe, no_mold, sisi_a, sisi_b, sisi_c, sisi_d, "
            f"sisi_e, sisi_f, sisi_g, sisi_h, [avg] AS avg_value FROM dbo.{table} WHERE {' AND '.join(cond)}")
    return " UNION ALL ".join(parts), params


def _clean_args(args):
    line = (args.get("line") or "").strip().upper()
    if line in ("ALL", "SEMUA"):
        line = ""
    if line and line not in LINES:
        raise ValueError("line_invalid")
    try:
        limit = max(1, min(int(args.get("limit") or 200), 5000))
    except ValueError:
        raise ValueError("limit_invalid")
    return dict(
        line=line,
        mold=(args.get("mold") or "").strip().upper(),
        tipe=(args.get("tipe") or "").strip().upper(),
        date_from=_norm_date(args.get("date_from")), date_to=_norm_date(args.get("date_to")),
        time_from=_norm_time(args.get("time_from")), time_to=_norm_time(args.get("time_to")),
        limit=limit)


def history(db, args):
    """Riwayat pengukuran (terbaru dulu). Tanpa filter apa pun -> kosong (sama seperti UI acuan)."""
    f = _clean_args(args)
    if not (f["line"] or f["mold"] or f["tipe"]):
        return {"rows": [], "tipe": "-"}
    union_sql, params = _union(f["line"], f["mold"], f["tipe"], f["date_from"], f["date_to"],
                               f["time_from"], f["time_to"])
    sql = (f"SELECT TOP {f['limit']} q.line, q.ts, q.tipe, q.no_mold, q.sisi_a, q.sisi_b, q.sisi_c, q.sisi_d, "
           f"q.sisi_e, q.sisi_f, q.sisi_g, q.sisi_h, q.avg_value FROM ({union_sql}) AS q ORDER BY q.ts DESC")
    conn = db.connect()
    try:
        cur = conn.cursor()
        cur.execute(sql, params)
        rows, latest_tipe = [], "-"
        for r in cur.fetchall():
            ts = r.ts
            tipe = str(r.tipe).strip() if r.tipe else "-"
            if latest_tipe == "-" and tipe != "-":
                latest_tipe = tipe
            row = {"line": r.line, "date": ts.strftime("%Y-%m-%d") if ts else "-",
                   "time": ts.strftime("%H:%M:%S") if ts else "-", "tipe": tipe,
                   "mold": str(r.no_mold).strip() if r.no_mold else "-", "avg": r.avg_value}
            for i, k in enumerate("abcdefgh", 1):
                row[f"titik{i}"] = getattr(r, f"sisi_{k}")
            rows.append(row)
        return {"rows": rows, "tipe": latest_tipe}
    finally:
        conn.close()


def signature(db, args):
    """'jumlah|timestamp-terbaru' - dipakai UI untuk mendeteksi data baru tanpa menarik semua baris."""
    f = _clean_args(args)
    if not (f["line"] or f["mold"] or f["tipe"]):
        return {"signature": "0|-"}
    union_sql, params = _union(f["line"], f["mold"], f["tipe"], f["date_from"], f["date_to"],
                               f["time_from"], f["time_to"])
    conn = db.connect()
    try:
        cur = conn.cursor()
        cur.execute(f"SELECT COUNT(1), MAX(q.ts) FROM ({union_sql}) AS q", params)
        n, latest = cur.fetchone()
        latest = latest.strftime("%Y-%m-%d %H:%M:%S") if latest else "-"
        return {"signature": f"{int(n or 0)}|{latest}"}
    finally:
        conn.close()
