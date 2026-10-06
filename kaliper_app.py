import csv
import time

class KaliperStandalone:
    def __init__(self, filename="hasil_pengukuran.csv"):
        self.current_line = 0  # Index 0 = C01, Index 21 = C22
        self.current_sisi = 0  # Index 0 = Sisi 1, Index 7 = Sisi 8
        self.filename = filename
        self._init_csv()

    def _init_csv(self):
        # Buat header CSV jika file belum ada
        try:
            with open(self.filename, mode='x', newline='') as f:
                writer = csv.writer(f)
                writer.writerow(["Baris", "Sisi", "Nilai (mm)", "Waktu"])
        except FileExistsError:
            pass

    def process_input(self, val_str):
        try:
            # Ubah koma desimal dari MarCom menjadi titik
            val_clean = val_str.replace(',', '.').strip()
            if not val_clean:
                return

            val_float = float(val_clean)
            line_name = f"C{self.current_line + 1:02d}"
            sisi_num = self.current_sisi + 1

            # Simpan ke CSV lokal
            timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
            with open(self.filename, mode='a', newline='') as f:
                writer = csv.writer(f)
                writer.writerow([line_name, f"Sisi {sisi_num}", val_float, timestamp])

            print(f"[OK] {line_name} | Sisi {sisi_num} -> {val_float:.3f} mm (Tersimpan di {self.filename})")

            # Pindah sel otomatis
            self._auto_advance()

        except ValueError:
            print("[ERROR] Input bukan angka valid! Silakan tembak ulang kaliper.")

    def _auto_advance(self):
        self.current_sisi += 1
        if self.current_sisi > 7:
            self.current_sisi = 0
            self.current_line += 1
            if self.current_line > 21:
                self.current_line = 0
                print("\n=== SEMUA BARIS (C01-C22) SELESAI! MENGULANG DARI C01 ===\n")

if __name__ == "__main__":
    app = KaliperStandalone()
    print("=== PENGUJIAN KALIPER PYTHON (STANDALONE) ===")
    print("1. Pastikan MarCom disetel ke 'Keyboard code' dengan End marker ''.")
    print("2. Pastikan kursor aktif/fokus di terminal ini.")
    print("3. Tekan tombol DATA pada kaliper untuk mengirim angka.")
    print("4. Ketik 'q' lalu Enter untuk keluar.\n")

    while True:
        target = f"[C{app.current_line+1:02d} - Sisi {app.current_sisi+1}]"
        user_in = input(f"Arahkan kaliper ke {target}: ")

        if user_in.lower() == 'q':
            print("Pengujian selesai.")
            break

        app.process_input(user_in)