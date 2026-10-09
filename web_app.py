"""
Kaliper QC - tampilan WEB (Flask) di atas logika kaliper_app.py

Alur data tetap sama dengan aplikasi tkinter:
  Kaliper -> MarCom (Keyboard code) -> browser (kolom input tersembunyi) -> Flask
          -> CSV lokal + SQL Server (tbl_cNN) lewat KaliperStandalone / QCDatabase

Jalankan : python web_app.py          (lalu buka http://localhost:5000)
Env      : WEB_HOST (default 0.0.0.0), WEB_PORT (default 5000) + DB_* seperti kaliper_app.py
"""
import csv
import os
import threading
import time

import re
from datetime import datetime

from flask import Flask, jsonify, request, send_file, send_from_directory

import excel_export
import qc_queries
from kaliper_app import (LINE_COUNT, SISI_COUNT, DB_NAME, DB_PORT, DB_SERVER, KaliperStandalone,
                         make_db, short_err)

WEB_HOST = os.getenv("WEB_HOST", "0.0.0.0")
WEB_PORT = int(os.getenv("WEB_PORT", "5000"))
DB_CHECK_EVERY_SEC = 5.0
AUTO_RETRY_EVERY_SEC = 30.0
MOLD_CACHE_TTL_SEC = 15.0
EXPORT_MAX_ROWS = 50000

LINES = [f"C{i:02d}" for i in range(1, LINE_COUNT + 1)]


class WebKaliper:
    """Membungkus KaliperStandalone: state untuk UI, kirim DB di thread terpisah, status koneksi DB."""

    def __init__(self, core):
        self.core = core
        self.lock = threading.RLock()
        core.auto_flush = False                      # kirim DB lewat thread, jangan memblok request
        core.on_line_complete = self._on_complete
        core.on_flush_result = self._on_flush

        self.line_state = {l: {"state": "idle", "avg": None, "msg": ""} for l in LINES}
        self.complete_seq = 0
        self.last_complete = None
        self.flush_msg = ""

        self._db_ok = None if core.db is not None else False
        self._db_msg = "" if core.db is not None else "Mode CSV saja (DB_PASSWORD belum diisi)"
        self._molds = {"ts": 0.0, "data": []}

        if core.db is not None:
            threading.Thread(target=self._db_monitor, daemon=True).start()
            threading.Thread(target=self._auto_retry, daemon=True).start()

    # ------------------------------ callback -----------------------------
    def _on_complete(self, rec):
        with self.lock:
            self.complete_seq += 1
            self.last_complete = {"seq": self.complete_seq, "line": rec["line"], "mold": rec["mold"],
                                  "tipe": rec["tipe"], "avg": rec["avg"], "sides": rec["sides"]}
            self.line_state[rec["line"]] = {"state": "send", "avg": rec["avg"],
                                            "msg": "Mengirim ke DB..." if self.core.db else "Offline - hanya CSV"}
        self.kick_flush()

    def _on_flush(self, line, ok, msg):
        with self.lock:
            if ok:
                st = self.line_state[line]
                st.update(state="ok", msg="Tersimpan di DB")
                self._db_ok, self._db_msg = True, ""
                self.flush_msg = ""
            else:
                for p in self.core.pending:           # semua yang menunggu ditandai gagal
                    self.line_state[p["line"]].update(state="fail", msg="GAGAL - menunggu kirim ulang")
                if self.core.db is not None:
                    self._db_ok, self._db_msg = False, msg
                self.flush_msg = msg

    # ----------------------------- kirim ke DB ---------------------------
    def kick_flush(self):
        if self.core.pending:
            threading.Thread(target=self.core.flush_pending, daemon=True).start()

    def _auto_retry(self):
        while True:
            time.sleep(AUTO_RETRY_EVERY_SEC)
            if self.core.pending:
                self.core.flush_pending()

    def _db_monitor(self):
        while True:
            try:
                self.core.db.connect().close()
                ok, msg = True, ""
            except Exception as e:
                ok, msg = False, short_err(e)
            with self.lock:
                self._db_ok, self._db_msg = ok, msg
            time.sleep(DB_CHECK_EVERY_SEC)

    # -------------------------------- state ------------------------------
    def db_label(self):
        if self.core.db is None:
            return "csv only"
        return {True: "online", False: "not online", None: "checking"}[self._db_ok]

    def state(self):
        with self.lock:
            c = self.core
            buf = list(c.buffer)
            titik = {f"titik{i + 1}": (buf[i] if i < len(buf) else "-") for i in range(SISI_COUNT)}
            lines = {}
            for l in LINES:
                s = dict(self.line_state[l])
                if l == c.line_name and s["state"] in ("idle", "ok"):   # "ok" = sisa siklus sebelumnya
                    s["state"] = "active"
                lines[l] = s
            return {
                "error": False, "ts": time.time(), "active_line": c.line_name, "count": len(buf), "sisi_total": SISI_COUNT,
                "avg": (sum(buf) / len(buf)) if buf else "-", **titik,
                "mold": c.mold, "tipe": c.tipe, "last_mold": c.last_mold,
                "pending": len(c.pending), "db_server": self.db_label(), "db_message": self._db_msg,
                "db_target": f"{DB_SERVER}:{DB_PORT}/{DB_NAME}", "flush_msg": self.flush_msg,
                "lines": lines, "last_complete": self.last_complete,
            }

    # ------------------------------- perintah ----------------------------
    def set_context(self, mold, tipe):
        mold, tipe = (mold or "").strip().upper(), (tipe or "").strip().upper()
        if not mold or not tipe:
            return False, "No. Mold dan Tipe Grid wajib diisi."
        with self.lock:
            if self.core.buffer and (mold != self.core.mold or tipe != self.core.tipe):
                return False, "Selesaikan atau batalkan nilai di line aktif sebelum mengganti Mold/Tipe."
            self.core.set_context(mold, tipe)
        return True, "OK"

    def add_input(self, value):
        with self.lock:
            if not self.core.mold or not self.core.tipe:
                return False, "Konfirmasi No. Mold dan Tipe Grid dulu."
            if self.core.process_input(str(value)):
                return True, "OK"
            return False, "Input bukan angka valid. Tembak ulang kaliper."

    def undo(self):
        with self.lock:
            if not self.core.buffer:
                return False, "Tidak ada nilai untuk dibatalkan."
            self.core.undo()
            return True, "OK"

    def clear_line(self):
        with self.lock:
            self.core.buffer.clear()          # nilai mentah tetap tercatat di CSV (log), tidak masuk DB
            return True, "Nilai line aktif dikosongkan."

    def goto_line(self, line):
        line = (line or "").strip().upper()
        if line not in LINES:
            return False, f"Line harus C01-C{LINE_COUNT:02d}."
        with self.lock:
            if self.core.buffer:
                return False, "Selesaikan/batalkan nilai di line aktif sebelum pindah line."
            if line != self.core.line_name:
                self.core.current_line = LINES.index(line)
                self.core.mold = self.core.tipe = ""
        return True, "OK"

    def reset_cycle(self):
        with self.lock:
            if self.core.buffer:
                return False, "Selesaikan/batalkan nilai di line aktif dulu."
            self.core.current_line = 0
            self.core.mold = self.core.tipe = ""
            for l in LINES:
                if self.line_state[l]["state"] != "fail":   # yang gagal kirim tetap terlihat
                    self.line_state[l] = {"state": "idle", "avg": None, "msg": ""}
        return True, "Siklus baru dimulai dari C01."

    def molds(self):
        db = self.core.db
        if db is None:
            return []
        now = time.time()
        if now - self._molds["ts"] > MOLD_CACHE_TTL_SEC:
            try:
                self._molds["data"] = qc_queries.list_molds(db)
                self._molds["ts"] = now
            except Exception:
                pass                           # pakai cache lama bila DB sedang gangguan
        return self._molds["data"]

    def invalidate_molds(self):
        self._molds["ts"] = 0.0


def create_app(core=None):
    app = Flask(__name__)
    app.config["JSON_AS_ASCII"] = False
    kal = WebKaliper(core or KaliperStandalone(db=make_db()))
    app.kal = kal

    @app.after_request
    def no_cache(resp):
        resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
        return resp

    def reply(result, **extra):
        ok, msg = result
        body = {"ok": ok, "message": msg, **extra}
        body["state"] = kal.state()
        return jsonify(body), (200 if ok else 400)

    @app.route("/")
    def index():
        return send_from_directory(os.path.join(app.root_path, "templates"), "index.html")

    @app.route("/health")
    def health():
        return jsonify({"ok": True, "db_server": kal.db_label()})

    @app.route("/api/state")
    def api_state():
        return jsonify(kal.state())

    @app.route("/api/context", methods=["POST"])
    def api_context():
        p = request.get_json(silent=True) or {}
        return reply(kal.set_context(p.get("mold"), p.get("tipe")))

    @app.route("/api/input", methods=["POST"])
    def api_input():
        p = request.get_json(silent=True) or {}
        return reply(kal.add_input(p.get("value", "")))

    @app.route("/api/undo", methods=["POST"])
    def api_undo():
        return reply(kal.undo())

    @app.route("/api/clear_line", methods=["POST"])
    def api_clear():
        return reply(kal.clear_line())

    @app.route("/api/goto_line", methods=["POST"])
    def api_goto():
        p = request.get_json(silent=True) or {}
        return reply(kal.goto_line(p.get("line")))

    @app.route("/api/flush", methods=["POST"])
    def api_flush():
        kal.kick_flush()
        return reply((True, "Mengirim ulang..." if kal.core.pending else "Tidak ada data yang menunggu."))

    @app.route("/api/reset_cycle", methods=["POST"])
    def api_reset():
        return reply(kal.reset_cycle())

    # ---- master mold -------------------------------------------------
    @app.route("/api/molds")
    def api_molds():
        return jsonify({"molds": kal.molds()})

    @app.route("/api/tipe")
    def api_tipe():
        mold = (request.args.get("mold") or "").strip().upper()
        if not mold:
            return jsonify({"tipe": "", "found": False})
        for m in kal.molds():
            if m["mold"] == mold and m["tipe"]:
                return jsonify({"tipe": m["tipe"], "found": True})
        db = kal.core.db
        if db is not None:                       # cache bisa basi -> cek langsung sekali
            try:
                t = db.get_tipe(mold)
                if t:
                    return jsonify({"tipe": t.upper(), "found": True})
            except Exception as e:
                return jsonify({"tipe": "", "found": False, "error": short_err(e)})
        return jsonify({"tipe": "", "found": False})

    @app.route("/api/register_mold", methods=["POST"])
    def api_register_mold():
        p = request.get_json(silent=True) or {}
        if kal.core.db is None:
            return jsonify({"ok": False, "message": "DB belum terhubung (mode CSV saja)."}), 400
        try:
            ok, msg = qc_queries.add_mold(kal.core.db, p.get("mold"), p.get("tipe"))
        except Exception as e:
            return jsonify({"ok": False, "message": short_err(e)}), 500
        kal.invalidate_molds()
        return jsonify({"ok": ok, "message": msg}), (200 if ok else 400)

    # ---- riwayat & grafik --------------------------------------------
    def _db_query(fn):
        base = {"db_server": kal.db_label()}
        if kal.core.db is None:
            return jsonify({"error": True, "message": "Mode CSV saja - riwayat DB tidak tersedia.", **base}), 200
        try:
            return jsonify({"error": False, **fn(kal.core.db, request.args), **base}), 200
        except ValueError as e:
            return jsonify({"error": True, "message": str(e), **base}), 400
        except Exception as e:
            return jsonify({"error": True, "message": short_err(e), **base}), 500

    @app.route("/api/history")
    def api_history():
        return _db_query(qc_queries.history)

    @app.route("/api/history_signature")
    def api_history_signature():
        return _db_query(qc_queries.signature)


    @app.route("/api/export_history")
    def api_export_history():
        """Unduh riwayat (sesuai filter di layar) sebagai file Excel."""
        if kal.core.db is None:
            return jsonify({"error": True, "message": "Mode CSV saja - ekspor dari DB tidak tersedia."}), 400
        args = request.args.to_dict()
        args["limit"] = str(EXPORT_MAX_ROWS)
        try:
            data = qc_queries.history(kal.core.db, args, max_limit=EXPORT_MAX_ROWS)
        except ValueError as e:
            return jsonify({"error": True, "message": str(e)}), 400
        except Exception as e:
            return jsonify({"error": True, "message": short_err(e)}), 500
        rows = data["rows"]
        if not rows:
            return jsonify({"error": True, "message": "Tidak ada data untuk diunduh. Pilih Line, Mold, atau Tipe dulu."}), 404
        filters = {k: (args.get(k) or "").strip() for k in
                   ("line", "mold", "tipe", "date_from", "date_to", "time_from", "time_to")}
        buf = excel_export.build_history_workbook(rows, filters, truncated=len(rows) >= EXPORT_MAX_ROWS)
        label = re.sub(r"[^A-Za-z0-9_-]+", "_", filters["mold"] or filters["line"] or filters["tipe"] or "ALL")
        name = f"QC_History_{label}_{datetime.now():%Y%m%d_%H%M%S}.xlsx"
        return send_file(buf, as_attachment=True, download_name=name,
                         mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")

    @app.route("/api/export_latest")
    def api_export_latest():
        """Unduh data pengukuran terbaru yang sudah masuk sebagai file Excel (.xlsx)."""
        line = (request.args.get("line") or "").strip().upper()
        if line and line not in LINES:
            line = ""

        rows = []
        source_label = "Database"

        if kal.core.db is not None:
            try:
                union_sql, params = qc_queries._union(line, "", "", None, None, None, None)
                limit = int(request.args.get("limit") or 1000)
                sql = (f"SELECT TOP {limit} q.line, q.ts, q.tipe, q.no_mold, q.sisi_a, q.sisi_b, q.sisi_c, q.sisi_d, "
                       f"q.sisi_e, q.sisi_f, q.sisi_g, q.sisi_h, q.avg_value FROM ({union_sql}) AS q ORDER BY q.ts DESC")
                conn = kal.core.db.connect()
                try:
                    cur = conn.cursor()
                    cur.execute(sql, params)
                    for r in cur.fetchall():
                        ts = r.ts
                        row = {
                            "line": r.line,
                            "date": ts.strftime("%Y-%m-%d") if ts else "-",
                            "time": ts.strftime("%H:%M:%S") if ts else "-",
                            "tipe": str(r.tipe).strip() if r.tipe else "-",
                            "mold": str(r.no_mold).strip() if r.no_mold else "-",
                            "avg": r.avg_value
                        }
                        for i, k in enumerate("abcdefgh", 1):
                            row[f"titik{i}"] = getattr(r, f"sisi_{k}")
                        rows.append(row)
                finally:
                    conn.close()
            except Exception as e:
                print(f"[export_latest] DB query error: {short_err(e)}")

        # Fallback ke CSV bila DB belum ada / kosong
        if not rows and os.path.exists(kal.core.filename):
            source_label = "CSV Lokal"
            try:
                with open(kal.core.filename, "r", encoding="utf-8") as f:
                    reader = csv.reader(f)
                    next(reader, None)
                    grouped = {}
                    for row in reader:
                        if len(row) >= 4:
                            b, s, val, w = row[0].strip(), row[1].strip(), row[2].strip(), row[3].strip()
                            if line and b.upper() != line:
                                continue
                            key = (b, w[:16])
                            if key not in grouped:
                                grouped[key] = {
                                    "line": b, "date": w[:10] if len(w) >= 10 else "-",
                                    "time": w[11:] if len(w) >= 19 else "-",
                                    "mold": "-", "tipe": "-", "sides": {}, "w": w
                                }
                            try:
                                s_idx = int(re.search(r"\d+", s).group(0))
                                grouped[key]["sides"][s_idx] = float(val)
                            except Exception:
                                pass
                    for g in sorted(grouped.values(), key=lambda x: x["w"], reverse=True):
                        pts = {f"titik{i}": g["sides"].get(i) for i in range(1, 9)}
                        vals = [v for v in pts.values() if v is not None]
                        avg_val = round(sum(vals) / len(vals), 3) if vals else None
                        rows.append({
                            "line": g["line"], "date": g["date"], "time": g["time"],
                            "mold": g["mold"], "tipe": g["tipe"], "avg": avg_val, **pts
                        })
            except Exception as e:
                print(f"[export_latest] CSV parse error: {short_err(e)}")

        if not rows:
            return jsonify({"error": True, "message": "Belum ada data pengukuran yang masuk untuk diunduh."}), 404

        filters = {
            "Line": line or "Semua Line",
            "Sumber Data": source_label,
            "Waktu Unduh": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        }
        buf = excel_export.build_history_workbook(rows, filters, truncated=False)
        label = line or "SemuaLine"
        name = f"QC_Pengukuran_{label}_{datetime.now():%Y%m%d_%H%M%S}.xlsx"
        return send_file(buf, as_attachment=True, download_name=name,
                         mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")

    @app.route("/api/download_csv")
    def api_download_csv():
        """Unduh file mentah hasil_pengukuran.csv."""
        if not os.path.exists(kal.core.filename):
            kal.core._init_csv()
        return send_file(os.path.abspath(kal.core.filename), as_attachment=True,
                         download_name=f"hasil_pengukuran_{datetime.now():%Y%m%d_%H%M%S}.csv",
                         mimetype="text/csv")

    return app


if __name__ == "__main__":
    application = create_app()
    print(f"Kaliper QC web berjalan di http://localhost:{WEB_PORT}  (DB: {application.kal.db_label()})")
    application.run(host=WEB_HOST, port=WEB_PORT, debug=False, use_reloader=False, threaded=True)
