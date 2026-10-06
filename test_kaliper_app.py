import os
import tempfile
import unittest

from kaliper_app import KaliperStandalone, QCDatabase, analisa_kolom


class FakeDB:
    def __init__(self):
        self.rows, self.fail = [], False

    def insert_line(self, line, tipe, mold, sides, avg, ts):
        if self.fail:
            raise RuntimeError("DB mati (simulasi)")
        self.rows.append((line, tipe, mold, sides, avg))


class KaliperTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.csv = os.path.join(self.tmp, "t.csv")
        self.db = FakeDB()
        self.app = KaliperStandalone(filename=self.csv, db=self.db)
        self.app.set_context("m-01", "grid a")

    def _isi(self, nilai):
        for v in nilai:
            self.assertTrue(self.app.process_input(v))

    def test_koma_desimal_dan_insert_setelah_8_sisi(self):
        self._isi(["1,10", "1,20", "1,30", "1,40", "1,50", "1,60", "1,70", "1,80"])
        self.assertEqual(len(self.db.rows), 1)
        line, tipe, mold, sides, avg = self.db.rows[0]
        self.assertEqual((line, tipe, mold), ("C01", "GRID A", "M-01"))
        self.assertEqual(sides, [1.1, 1.2, 1.3, 1.4, 1.5, 1.6, 1.7, 1.8])
        self.assertAlmostEqual(avg, 1.45)
        self.assertEqual(self.app.line_name, "C02")      # pindah line otomatis
        self.assertEqual(self.app.current_sisi, 0)

    def test_tidak_insert_sebelum_8_sisi(self):
        self._isi(["1.0"] * 7)
        self.assertEqual(self.db.rows, [])

    def test_input_tidak_valid_ditolak(self):
        for bad in ["abc", "nan", "inf", ""]:
            self.assertFalse(self.app.process_input(bad))
        self.assertEqual(self.app.current_sisi, 0)

    def test_undo(self):
        self._isi(["1.0", "2.0"])
        self.app.undo()
        self.assertEqual(self.app.buffer, [1.0])

    def test_db_gagal_ditahan_lalu_dikirim_ulang(self):
        self.db.fail = True
        self._isi(["1.0"] * 8)
        self.assertEqual(len(self.app.pending), 1)
        self.assertEqual(self.db.rows, [])
        self.db.fail = False
        self.app.set_context("M-02", "GRID B")
        self._isi(["2.0"] * 8)
        self.assertEqual([r[0] for r in self.db.rows], ["C01", "C02"])
        self.assertEqual(self.app.pending, [])

    def test_tanpa_mold_ditolak(self):
        self.app.mold = ""
        self.assertFalse(self.app.process_input("1.0"))

    def test_tabel_dan_connection_string(self):
        self.assertEqual(QCDatabase.table_for_line("c05"), "tbl_c05")
        with self.assertRaises(ValueError):
            QCDatabase.table_for_line("C23")
        cs = QCDatabase(server="h", port=1, database="d", user="u-x", password="p@;}!")._conn_str("DRV")
        self.assertIn("SERVER=h,1;", cs)
        self.assertIn("PWD={p@;}}!};", cs)


def _rows(table, spec):
    return [(table, c, dt, ln, None, None, nl, df, idn, 0) for (c, dt, ln, nl, df, idn) in spec]


BASE = [("id", "int", None, "NO", None, 1), ("timestamp", "datetime", None, "NO", None, 0),
        ("tipe", "nvarchar", 50, "NO", None, 0), ("no_mold", "nvarchar", 50, "NO", None, 0)] + \
       [(f"sisi_{c}", "float", None, "YES", None, 0) for c in "abcdefgh"] + [("avg", "float", None, "YES", None, 0)]


class AnalisaKolomTest(unittest.TestCase):
    def test_cocok(self):
        out = analisa_kolom(_rows("tbl_c01", BASE))
        self.assertFalse([l for l in out if l.startswith(("MASALAH", "PERHATIAN"))])

    def test_kolom_hilang_dan_not_null_tanpa_default(self):
        spec = [r for r in BASE if r[0] != "avg"] + [("shift", "nvarchar", 5, "NO", None, 0)]
        out = "\n".join(analisa_kolom(_rows("tbl_c01", spec)))
        self.assertIn("TIDAK ADA: avg", out)
        self.assertIn("'shift' NOT NULL tanpa default", out)

    def test_kolom_tambahan_boleh_null_aman(self):
        spec = BASE + [("catatan", "nvarchar", 100, "YES", None, 0), ("shift", "nvarchar", 5, "NO", "('A')", 0)]
        out = analisa_kolom(_rows("tbl_c01", spec))
        self.assertFalse([l for l in out if l.startswith("MASALAH")])

    def test_tipe_bulat_dan_beda_antar_tabel(self):
        spec = [("sisi_a", "int", None, "YES", None, 0) if r[0] == "sisi_a" else r for r in BASE]
        rows = _rows("tbl_c01", spec) + _rows("tbl_c02", BASE)
        out = "\n".join(analisa_kolom(rows))
        self.assertIn("dibulatkan", out)
        self.assertIn("Struktur berbeda", out)


if __name__ == "__main__":
    unittest.main()