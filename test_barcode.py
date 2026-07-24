import sys
import glob
from PIL import Image
from pyzbar import pyzbar

def test(path):
    print(f"Файл: {path}")
    try:
        img = Image.open(path)
        decoded = pyzbar.decode(img)
        if not decoded:
            print("  Штрихкод НЕ найден")
            return
        for bc in decoded:
            barcode = bc.data.decode("utf-8")
            pl = barcode[4:12] if len(barcode) >= 12 else "слишком короткий"
            print(f"  Штрихкод:       {barcode}")
            print(f"  Номер путевого: {pl}")
    except Exception as e:
        print(f"  Ошибка: {e}")

folder = r"C:\Users\molod"
files = glob.glob(f"{folder}\\*.jpg") + glob.glob(f"{folder}\\*.png")

if not files:
    print("Файлы jpg/png не найдены в папке", folder)
else:
    print(f"Найдено файлов: {len(files)}\n")
    for path in files:
        test(path)
        print()

input("Нажмите Enter для выхода...")
