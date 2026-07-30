"""
Ручная проверка AI-детектирования полей на конкретном скане.

Использование:
    python check_scan.py "путь\к\скану.pdf"

Открывает браузер с аннотированным изображением:
  - Зелёная рамка = AI считает ЗАПОЛНЕННЫМ (не будет перезаписывать)
  - Красная рамка  = AI считает ПУСТЫМ (будет заполнять из 1С)
  - Также сохраняет вырезки каждого поля отдельно для детальной проверки
"""
import sys, os, webbrowser, io, base64, json, time
sys.path.insert(0, r"C:\Users\molod")

if len(sys.argv) < 2:
    print("Укажи путь к PDF: python check_scan.py \"путь\\к\\скану.pdf\"")
    sys.exit(1)

pdf_path = sys.argv[1]
if not os.path.exists(pdf_path):
    print("Файл не найден:", pdf_path)
    sys.exit(1)

import waybill_app as w
import pypdfium2 as pdfium
from PIL import ImageDraw

print(f"Файл: {os.path.basename(pdf_path)}")
print("Запускаю AI-детектирование...")
t0 = time.time()
result = w.detect_fields_with_ai(pdf_path)
elapsed = time.time() - t0
print(f"Готово за {elapsed:.1f}с")
print()

# --- Рендер аннотированной страницы ---
doc = pdfium.PdfDocument(pdf_path)
page = doc[0]
pw, ph = page.get_width(), page.get_height()
scale = 2
img_orig = page.render(scale=scale).to_pil().convert("RGB")

# --- Вырезки из оригинала (до рисования рамок) ---
crops = {}
for field, (x0, y0, x1, y1, *_) in w.SCAN_FIELDS.items():
    sx0, sy0, sx1, sy1 = w._tmpl_region_to_scan(x0, y0, x1, y1, pw, ph)
    pad = 10
    box = (max(0, int(sx0 * scale) - pad), max(0, int(sy0 * scale) - pad),
           min(img_orig.width, int(sx1 * scale) + pad),
           min(img_orig.height, int(sy1 * scale) + pad))
    crop = img_orig.crop(box)
    if w._scan_is_portrait(pw, ph):
        crop = crop.rotate(90, expand=True)
    buf = io.BytesIO()
    crop.save(buf, format="JPEG", quality=92)
    crops[field] = base64.b64encode(buf.getvalue()).decode()

# --- Аннотированная страница (рамки поверх копии) ---
img = img_orig.copy()
draw = ImageDraw.Draw(img)
for field, (x0, y0, x1, y1, *_) in w.SCAN_FIELDS.items():
    sx0, sy0, sx1, sy1 = w._tmpl_region_to_scan(x0, y0, x1, y1, pw, ph)
    px0, py0 = int(sx0 * scale), int(sy0 * scale)
    px1, py1 = int(sx1 * scale), int(sy1 * scale)
    filled = result.get(field, False)
    color = (0, 180, 0) if filled else (220, 0, 0)
    draw.rectangle([px0, py0, px1, py1], outline=color, width=4)
    draw.text((min(px0, px1) + 3, min(py0, py1) + 3), field, fill=color)

buf_full = io.BytesIO()
img.save(buf_full, format="JPEG", quality=85)
full_b64 = base64.b64encode(buf_full.getvalue()).decode()

# --- HTML-отчёт ---
html_path = pdf_path.replace(".pdf", "_check.html").replace(".PDF", "_check.html")
rows = []
for field, filled in result.items():
    status = "✅ заполнено" if filled else "❌ пусто"
    color = "#1a7a1a" if filled else "#c00"
    bg = "#efffef" if filled else "#fff0f0"
    crop_img = f"<img src='data:image/jpeg;base64,{crops[field]}' style='max-height:80px;max-width:500px;display:block;image-rendering:crisp-edges'>"
    rows.append(f"""
      <tr style="background:{bg}">
        <td style="font-family:monospace;padding:6px 10px;white-space:nowrap">{field}</td>
        <td style="color:{color};font-weight:bold;padding:6px 10px;font-size:15px;white-space:nowrap">{status}</td>
        <td style="padding:6px 10px;border-left:4px solid {color}">{crop_img}</td>
      </tr>""")

html = f"""<!DOCTYPE html>
<html><head><meta charset="utf-8">
<title>Проверка: {os.path.basename(pdf_path)}</title>
<style>
  body{{font-family:sans-serif;margin:20px;background:#f5f5f5}}
  h2{{margin-bottom:4px}}
  .wrap{{display:flex;gap:24px;align-items:flex-start;flex-wrap:wrap}}
  .left{{flex:0 0 auto}}
  .right{{flex:1 1 320px}}
  img.full{{max-width:560px;border:1px solid #ccc;display:block}}
  table{{border-collapse:collapse;background:white;border-radius:6px;overflow:hidden;box-shadow:0 1px 4px rgba(0,0,0,.1)}}
  tr:nth-child(even){{background:#fafafa}}
  th{{background:#333;color:white;padding:6px 8px;text-align:left}}
  .legend{{font-size:13px;margin:8px 0}}
</style>
</head><body>
<h2>Детектирование полей: {os.path.basename(pdf_path)}</h2>
<p style="color:#666">Модель: {w.AI_MODEL} | Время: {elapsed:.1f}с | API ключ: {'✓ задан' if w.OPENROUTER_API_KEY else '✗ не задан'}</p>
<div class="legend">
  <span style="color:green">■</span> Зелёная рамка = заполнено (не трогаем) &nbsp;
  <span style="color:red">■</span> Красная рамка = пусто (заполним из 1С)
</div>
<div class="wrap">
  <div class="left">
    <b>Скан с зонами:</b><br>
    <img class="full" src="data:image/jpeg;base64,{full_b64}">
  </div>
  <div class="right">
    <b>Детально по полям (вырезки):</b>
    <table>
      <tr><th>Поле</th><th>Вердикт AI</th><th>Вырезка зоны</th></tr>
      {"".join(rows)}
    </table>
  </div>
</div>
</body></html>"""

with open(html_path, "w", encoding="utf-8") as f:
    f.write(html)

print("Rezultat AI:")
for field, filled in result.items():
    mark = "[+] zapolneno" if filled else "[ ] pusto    "
    print(f"  {mark}  {field}")

print()
print("Отчёт:", html_path)
webbrowser.open(html_path)
