"""
Безопасный тест записи штрихкодов в 1С.

1. Берёт заказы без путевых из 1С (zakaz_no_pl)
2. Читает штрихкод с каждого скана (pyzbar)
3. Открывает браузер: видишь скан + штрихкод
4. Жмёшь «Записать» только по тем строкам которые проверил
   — пока не нажал, в 1С ничего не пишется

python test_pl_barcode.py
"""
import sys, os, base64, io, json, webbrowser, threading
from http.server import HTTPServer, BaseHTTPRequestHandler
import requests
from PIL import Image
from pyzbar import pyzbar as _pyzbar

sys.path.insert(0, r"C:\Users\molod")
from monitor_pl import (
    get_orders_without_pl, unc_to_windows_path,
    API_BASE, PL_BARCODE_USER, PL_BARCODE_PASS,
)

PORT = 8766

# ---------------------------------------------------------------------------

def open_scan_image(file_path: str) -> Image.Image:
    if file_path.lower().endswith(".pdf"):
        import pypdfium2 as pdfium
        doc = pdfium.PdfDocument(file_path)
        return doc[0].render(scale=2).to_pil().convert("RGB")
    return Image.open(file_path).convert("RGB")


def decode_barcode(img: Image.Image):
    """Вернуть (строка_баркода, rect) или (None, None)."""
    from PIL import ImageEnhance, ImageFilter
    from pyzbar.pyzbar import Rect

    def _first(decoded, scale=1):
        if not decoded:
            return None, None
        r = decoded[0].rect
        return decoded[0].data.decode("utf-8"), Rect(
            r.left // scale, r.top // scale,
            r.width // scale, r.height // scale,
        )

    gray = img.convert("L")

    # 1. оригинал цветной
    d = _pyzbar.decode(img)
    if d: return _first(d)

    # 2. grayscale
    d = _pyzbar.decode(gray)
    if d: return _first(d)

    # 3. x2
    g2 = gray.resize((gray.width * 2, gray.height * 2), Image.LANCZOS)
    d = _pyzbar.decode(g2)
    if d: return _first(d, 2)

    # 4. x2 + контраст
    g2c = ImageEnhance.Contrast(gray).enhance(2.0).resize(
        (gray.width * 2, gray.height * 2), Image.LANCZOS)
    d = _pyzbar.decode(g2c)
    if d: return _first(d, 2)

    # 5. x3 + контраст
    g3c = ImageEnhance.Contrast(gray).enhance(2.0).resize(
        (gray.width * 3, gray.height * 3), Image.LANCZOS)
    d = _pyzbar.decode(g3c)
    if d: return _first(d, 3)

    # 6. бинаризация x2
    bw = gray.point(lambda p: 255 if p > 128 else 0, '1').convert('L')
    bw2 = bw.resize((bw.width * 2, bw.height * 2), Image.LANCZOS)
    d = _pyzbar.decode(bw2)
    if d: return _first(d, 2)

    # 7. кроп нижней половины x3 + контраст
    h = gray.height
    bottom = ImageEnhance.Contrast(gray.crop((0, h // 2, gray.width, h))).enhance(2.0)
    bottom3 = bottom.resize((bottom.width * 3, bottom.height * 3), Image.LANCZOS)
    d = _pyzbar.decode(bottom3)
    if d:
        bcode, rect = _first(d, 3)
        if rect:
            from pyzbar.pyzbar import Rect as _Rect
            rect = _Rect(rect.left, rect.top + h // 2, rect.width, rect.height)
        return bcode, rect

    # 8. резкость x3
    sharp3 = ImageEnhance.Sharpness(gray).enhance(3.0).resize(
        (gray.width * 3, gray.height * 3), Image.LANCZOS)
    d = _pyzbar.decode(sharp3)
    if d: return _first(d, 3)

    # 9. x4 + контраст
    g4c = ImageEnhance.Contrast(gray).enhance(2.0).resize(
        (gray.width * 4, gray.height * 4), Image.LANCZOS)
    d = _pyzbar.decode(g4c)
    if d: return _first(d, 4)

    # 10–13. повороты ±5° и ±10° — спасает наклонённые штрихкоды
    base = ImageEnhance.Contrast(gray).enhance(2.0).resize(
        (gray.width * 2, gray.height * 2), Image.LANCZOS)
    for angle in (5, -5, 10, -10):
        rotated = base.rotate(angle, expand=True, fillcolor=255)
        d = _pyzbar.decode(rotated)
        if d: return _first(d, 2)

    return None, None


def crop_barcode_area(img: Image.Image, rect) -> Image.Image:
    pad_h = rect.height
    box = (
        max(0, rect.left - 10),
        max(0, rect.top - 10),
        min(img.width, rect.left + rect.width + 10),
        min(img.height, rect.top + rect.height + pad_h),
    )
    return img.crop(box)


def img_to_b64(img: Image.Image, max_w=600) -> str:
    if img.width > max_w:
        ratio = max_w / img.width
        img = img.resize((max_w, int(img.height * ratio)), Image.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=85)
    return base64.b64encode(buf.getvalue()).decode()


def call_pl_barcode(order_id: str, shk: str) -> dict:
    payload = {"ШК": shk, "ИДСмены": order_id}
    token = base64.b64encode(
        f"{PL_BARCODE_USER}:{PL_BARCODE_PASS}".encode("utf-8")
    ).decode("ascii")
    headers = {"Authorization": f"Basic {token}",
               "Content-Type": "application/json"}
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    resp = requests.post(f"{API_BASE}/pl_barcode", data=body,
                         headers=headers, timeout=20)
    resp.raise_for_status()
    return resp.json()


# ---------------------------------------------------------------------------
# Сбор данных
# ---------------------------------------------------------------------------

print("Получаю заказы из 1С...")
try:
    orders = get_orders_without_pl()
except Exception as exc:
    print("Ошибка:", exc)
    sys.exit(1)

if not orders:
    print("Заказов без путевого не найдено.")
    sys.exit(0)

print(f"Найдено заказов: {len(orders)}")
rows = []

for i, order in enumerate(orders):
    order_id  = order.get("id", "")
    order_num = order.get("num", "?")
    namef_raw = order.get("namef", "")
    file_path = unc_to_windows_path(namef_raw)

    print(f"  [{i+1}/{len(orders)}] {order_num} — {file_path}")

    row = {
        "order_id":  order_id,
        "order_num": order_num,
        "file_path": file_path,
        "shk":       None,
        "thumb_b64": None,
        "crop_b64":  None,
        "error":     None,
    }

    if not file_path:
        row["error"] = "Пустой путь к файлу"
        rows.append(row)
        continue

    if not os.path.exists(file_path):
        row["error"] = f"Файл не найден: {file_path}"
        rows.append(row)
        continue

    try:
        img = open_scan_image(file_path)
        row["thumb_b64"] = img_to_b64(img, max_w=400)

        shk, rect = decode_barcode(img)
        if not shk:
            row["error"] = "Штрихкод не найден"
            rows.append(row)
            continue

        row["shk"] = shk
        print(f"       pyzbar: {shk}")

        if rect:
            crop = crop_barcode_area(img, rect)
            row["crop_b64"] = img_to_b64(crop, max_w=500)

    except Exception as exc:
        row["error"] = str(exc)

    rows.append(row)

print(f"\nГотово. Запускаю браузер...")

# ---------------------------------------------------------------------------
# HTTP сервер
# ---------------------------------------------------------------------------

RESULTS = {}  # order_id -> результат записи

def generate_html():
    found_count = sum(1 for r in rows if r["shk"])
    err_count   = sum(1 for r in rows if r["error"])

    table_rows = []
    for r in rows:
        oid = r["order_id"]
        res = RESULTS.get(oid)

        if r["error"]:
            status_cell = f'<td class="err" colspan="3">{r["error"]}</td>'
        else:
            crop_img = (f'<img src="data:image/jpeg;base64,{r["crop_b64"]}" '
                        f'style="max-height:60px">'
                        if r["crop_b64"] else "—")

            if res:
                wrote_ok  = res.get("ok")
                btn_label = "✓ Записано" if wrote_ok else f"✗ Ошибка: {res.get('error','')}"
                btn_cls   = "ok" if wrote_ok else "bad"
                btn = f'<button class="{btn_cls}" disabled>{btn_label}</button>'
            else:
                disabled = "" if r["shk"] else "disabled"
                btn = (f'<button onclick="doWrite(this,\'{oid}\',\'{r["shk"]}\','
                       f'\'{r["order_num"]}\')" {disabled}>Записать ШК в 1С</button>')

            status_cell = (
                f'<td>{crop_img}</td>'
                f'<td class="mono">{r["shk"] or "—"}</td>'
                f'<td>{btn}</td>'
            )

        thumb = (f'<img src="data:image/jpeg;base64,{r["thumb_b64"]}" '
                 f'style="max-height:120px;cursor:pointer" '
                 f'onclick="this.style.maxHeight=this.style.maxHeight==\'none\'?\'120px\':\'none\'">'
                 if r["thumb_b64"] else "нет файла")

        table_rows.append(f"""
        <tr>
          <td class="mono small">{r["order_num"]}</td>
          <td class="small gray">{r["file_path"]}</td>
          <td>{thumb}</td>
          {status_cell}
        </tr>""")

    return f"""<!DOCTYPE html>
<html><head><meta charset="utf-8">
<title>Тест pl_barcode</title>
<style>
body{{font-family:sans-serif;margin:20px;background:#f5f5f5}}
h2{{margin-bottom:4px}}
.stat{{font-size:13px;color:#555;margin-bottom:14px}}
table{{border-collapse:collapse;background:#fff;width:100%;
       box-shadow:0 1px 4px rgba(0,0,0,.1)}}
th{{background:#333;color:#fff;padding:8px 10px;text-align:left;font-size:13px}}
td{{padding:6px 10px;border-bottom:1px solid #eee;vertical-align:middle;font-size:13px}}
.mono{{font-family:monospace}}
.small{{font-size:11px}}
.gray{{color:#777}}
.ok{{color:green;font-weight:bold}}
.bad{{color:#c00;font-weight:bold}}
.err{{color:#a00;font-style:italic}}
button{{padding:5px 12px;border:none;border-radius:4px;cursor:pointer;
        background:#3a9;color:#fff;font-size:12px;font-weight:bold}}
button.ok{{background:#3a9}}
button.bad{{background:#c33}}
button:disabled{{opacity:.6;cursor:default}}
#log{{position:fixed;bottom:10px;right:14px;background:#222;color:#0f0;
      font-family:monospace;font-size:12px;padding:8px 14px;border-radius:6px;
      max-width:400px;display:none}}
</style>
</head><body>
<h2>Тест записи штрихкодов в 1С</h2>
<div class="stat">
  Заказов: {len(rows)} &nbsp;|&nbsp;
  <span class="ok">Штрихкод найден: {found_count}</span> &nbsp;|&nbsp;
  <span class="bad">Ошибки/не найден: {err_count}</span>
</div>
<table>
  <tr>
    <th>Заказ</th><th>Файл</th><th>Скан</th>
    <th>Вырезка&nbsp;штрихкода</th><th>ШК</th><th>Действие</th>
  </tr>
  {"".join(table_rows)}
</table>
<div id="log"></div>
<script>
function showLog(msg, ok) {{
  var el = document.getElementById('log');
  el.style.display = 'block';
  el.style.color = ok ? '#0f0' : '#f66';
  el.textContent = msg;
  setTimeout(function() {{ el.style.display='none'; }}, 4000);
}}
function doWrite(btn, oid, shk, num) {{
  btn.disabled = true;
  btn.textContent = '...';
  var xhr = new XMLHttpRequest();
  xhr.open('POST', '/write', true);
  xhr.setRequestHeader('Content-Type', 'application/json');
  xhr.onload = function() {{
    try {{
      var d = JSON.parse(xhr.responseText);
      if (d.ok) {{
        btn.textContent = 'Записано OK';
        btn.style.background = '#3a9';
        showLog('ШК ' + shk + ' записан в смену ' + num, true);
      }} else {{
        btn.textContent = 'Ошибка: ' + (d.error || '?');
        btn.style.background = '#c33';
        btn.disabled = false;
        showLog('Ошибка: ' + (d.error || xhr.responseText), false);
      }}
    }} catch(e) {{
      btn.textContent = 'Ошибка ответа';
      btn.style.background = '#c33';
      btn.disabled = false;
      showLog('Ответ: ' + xhr.responseText, false);
    }}
  }};
  xhr.onerror = function() {{
    btn.textContent = 'Нет связи';
    btn.style.background = '#c33';
    btn.disabled = false;
  }};
  xhr.send(JSON.stringify({{order_id: oid, shk: shk, order_num: num}}));
}}
</script>
</body></html>"""


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(generate_html().encode("utf-8"))

    def do_POST(self):
        if self.path != "/write":
            self.send_response(404); self.end_headers(); return
        length = int(self.headers.get("Content-Length", 0))
        body   = json.loads(self.rfile.read(length))
        oid    = body["order_id"]
        shk    = body["shk"]
        num    = body["order_num"]
        try:
            api_resp = call_pl_barcode(oid, shk)
            ok = api_resp.get("status") == "Ок"
            RESULTS[oid] = {"ok": ok, "resp": api_resp}
            result = {"ok": ok, "resp": api_resp}
            print(f"  ЗАПИСАН: заказ {num}, ШК {shk} → {api_resp}")
        except Exception as exc:
            RESULTS[oid] = {"ok": False, "error": str(exc)}
            result = {"ok": False, "error": str(exc)}
            print(f"  ОШИБКА: заказ {num} → {exc}")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(result, ensure_ascii=False).encode("utf-8"))

    def log_message(self, *args):
        pass


server = HTTPServer(("127.0.0.1", PORT), Handler)
threading.Timer(0.8, lambda: webbrowser.open(f"http://127.0.0.1:{PORT}")).start()
print(f"Открываю http://127.0.0.1:{PORT}")
print("Ctrl+C для выхода")
try:
    server.serve_forever()
except KeyboardInterrupt:
    print("\nСтоп.")
