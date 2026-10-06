import time
import random
from kaliper_app import KaliperGateway

def run_test_simulation():
    gateway = KaliperGateway()
    print("=== Memulai Simulasi Pengukuran Kaliper ===")
    
    # Pengujian pengiriman 5 data acak
    for i in range(5):
        # Generate angka acak (contoh: 20.000 mm - 50.000 mm)
        simulated_val = round(random.uniform(20.0, 50.0), 3)
        print(f"\n[TEST {i+1}] Mengirim data pengukuran: {simulated_val} mm")
        
        success = gateway.send_measurement(simulated_val)
        if not success:
            print("Uji coba terhenti karena koneksi ke server gagal.")
            break
            
        time.sleep(1)

if __name__ == "__main__":
    run_test_simulation()