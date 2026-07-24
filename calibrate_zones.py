"""
Визуальный редактор зон полей ЭСМ-2.
Открывает браузер с перетаскиваемыми/растягиваемыми прямоугольниками поверх скана.
После настройки — кнопка «Экспорт» выводит готовый Python-код для замены SCAN_FIELDS.

python calibrate_zones.py "путь\\к\\скану.pdf"
"""
import sys, os, base64, io, json, webbrowser
sys.path.insert(0, r"C:\Users\molod")

if len(sys.argv) < 2:
    print("Укажи PDF: python calibrate_zones.py scan.pdf")
    sys.exit(1)

pdf_path = sys.argv[1]
if not os.path.exists(pdf_path):
    print("Файл не найден:", pdf_path)
    sys.exit(1)

import pypdfium2 as pdfium
import waybill_app as w

doc = pdfium.PdfDocument(pdf_path)
page = doc[0]
pw, ph = page.get_width(), page.get_height()
SCALE = 2
portrait = w._scan_is_portrait(pw, ph)

bitmap = page.render(scale=SCALE)
img = bitmap.to_pil().convert("RGB")
buf = io.BytesIO()
img.save(buf, format="JPEG", quality=90)
img_b64 = base64.b64encode(buf.getvalue()).decode()

zones = {}
for field, coords in w.SCAN_FIELDS.items():
    x0, y0, x1, y1 = coords[:4]
    xins = coords[4] if len(coords) > 4 else None
    yins = coords[5] if len(coords) > 5 else None
    sx0, sy0, sx1, sy1 = w._tmpl_region_to_scan(x0, y0, x1, y1, pw, ph)
    zones[field] = {
        "px0": round(sx0 * SCALE),
        "py0": round(sy0 * SCALE),
        "px1": round(sx1 * SCALE),
        "py1": round(sy1 * SCALE),
        "narrow": field in w._NARROW_FIELDS,
        "xins_off": (xins - x0) if xins is not None else None,
        "yins_off": (yins - y1) if yins is not None else None,
    }

zones_js = json.dumps(zones, ensure_ascii=False)

html = f"""<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<title>Калибровка — {os.path.basename(pdf_path)}</title>
<style>
* {{ box-sizing: border-box; margin: 0; padding: 0; }}
body {{ background: #1a1a1a; font-family: monospace; user-select: none; }}
#toolbar {{
  background: #2a2a2a; padding: 8px 14px;
  display: flex; gap: 10px; align-items: center;
  position: sticky; top: 0; z-index: 1000;
  border-bottom: 1px solid #444;
}}
#toolbar button {{
  padding: 6px 16px; cursor: pointer; border-radius: 4px;
  border: none; font-size: 13px; font-family: monospace;
}}
#btnExport {{ background: #3a9; color: #fff; font-weight: bold; }}
#btnReset  {{ background: #666; color: #fff; }}
#hint {{ color: #888; font-size: 12px; }}
#canvas-wrap {{ position: relative; display: inline-block; margin: 14px; }}
#scan-img {{ display: block; }}
.zone {{
  position: absolute;
  border: 3px solid;
  cursor: move;
}}
.zone.narrow {{ border-color: #4af; }}
.zone.wide   {{ border-color: #f90; }}
.zone.added  {{ border-color: #4c4; }}
.zlabel {{
  position: absolute; top: 1px; left: 3px;
  font-size: 10px; font-weight: bold; color: #fff;
  text-shadow: 1px 1px 2px #000, -1px -1px 2px #000;
  pointer-events: none; white-space: nowrap;
}}
.rh {{
  position: absolute; bottom: -2px; right: -2px;
  width: 12px; height: 12px;
  background: rgba(255,255,255,0.7);
  cursor: se-resize;
}}
#export-box {{
  display: none;
  background: #111; color: #4f4;
  padding: 14px 18px; margin: 0 14px 14px;
  border-radius: 6px; white-space: pre;
  overflow-x: auto; font-size: 12px;
  border: 1px solid #3a9; line-height: 1.7;
}}
</style>
</head>
<body>

<div id="toolbar">
  <button id="btnExport">Экспорт координат</button>
  <button id="btnReset">Сбросить</button>
  <button id="btnAdd" style="background:#c70;color:#fff">+ Добавить поле</button>
  <button id="btnDel" style="background:#c33;color:#fff">✕ Удалить выбранное</button>
  <span id="hint">
    <span style="color:#4af">&#9632;</span> узкое &nbsp;
    <span style="color:#f90">&#9632;</span> широкое &nbsp;
    <span style="color:#0c0">&#9632;</span> новое &nbsp;|&nbsp;
    тяни за середину — двигай &nbsp;|&nbsp; тяни за белый уголок — растягивай
  </span>
</div>

<div id="canvas-wrap">
  <img id="scan-img" src="data:image/jpeg;base64,{img_b64}">
</div>

<pre id="export-box"></pre>

<script>
const SCALE  = {SCALE};
const TMPL_W = {w._TMPL_W};
const TMPL_H = {w._TMPL_H};
const PW = {pw};
const PH = {ph};
const PORTRAIT = {'true' if portrait else 'false'};

const ORIG  = {zones_js};
// working copy
const zones = JSON.parse(JSON.stringify(ORIG));

const wrap = document.getElementById('canvas-wrap');

// активное перетаскивание
let active = null; // {{ field, mode, sx, sy, sz }}

let selected = null; // выбранное поле (для удаления)

/* ---- создание одного элемента ---- */
function makeEl(field, z) {{
    const el = document.createElement('div');
    el.className = 'zone ' + (z.narrow ? 'narrow' : z.added ? 'added' : 'wide');
    el.id = 'z_' + field;
    el.style.left   = z.px0 + 'px';
    el.style.top    = z.py0 + 'px';
    el.style.width  = (z.px1 - z.px0) + 'px';
    el.style.height = (z.py1 - z.py0) + 'px';

    const lbl = document.createElement('div');
    lbl.className = 'zlabel';
    lbl.textContent = field;
    el.appendChild(lbl);

    const rh = document.createElement('div');
    rh.className = 'rh';
    el.appendChild(rh);

    el.addEventListener('mousedown', e => {{
        selected = field;
        document.querySelectorAll('.zone').forEach(z => z.style.outline = '');
        el.style.outline = '2px solid #fff';
        const mode = e.target.classList.contains('rh') ? 'resize' : 'move';
        active = {{
            field, mode,
            sx: e.clientX, sy: e.clientY,
            sz: {{...zones[field]}}
        }};
        e.preventDefault();
        e.stopPropagation();
    }});

    wrap.appendChild(el);
}}

/* ---- создание элементов ---- */
for (const [field, z] of Object.entries(zones)) {{
    makeEl(field, z);
}}

/* ---- глобальные обработчики ---- */
document.addEventListener('mousemove', e => {{
    if (!active) return;
    const {{ field, mode, sx, sy, sz }} = active;
    const dx = e.clientX - sx;
    const dy = e.clientY - sy;
    const z  = zones[field];
    if (mode === 'move') {{
        z.px0 = sz.px0 + dx;  z.py0 = sz.py0 + dy;
        z.px1 = sz.px1 + dx;  z.py1 = sz.py1 + dy;
    }} else {{
        z.px1 = Math.max(sz.px0 + 10, sz.px1 + dx);
        z.py1 = Math.max(sz.py0 + 6,  sz.py1 + dy);
    }}
    const el = document.getElementById('z_' + field);
    el.style.left   = z.px0 + 'px';
    el.style.top    = z.py0 + 'px';
    el.style.width  = (z.px1 - z.px0) + 'px';
    el.style.height = (z.py1 - z.py0) + 'px';
}});

document.addEventListener('mouseup', () => {{ active = null; }});

/* ---- конвертация пикселей → координаты шаблона ---- */
function pixToTmpl(px0, py0, px1, py1) {{
    const xp0 = px0/SCALE, yp0 = py0/SCALE;
    const xp1 = px1/SCALE, yp1 = py1/SCALE;
    if (!PORTRAIT) {{
        return [
            Math.round(xp0 * TMPL_W / PW), Math.round(yp0 * TMPL_H / PH),
            Math.round(xp1 * TMPL_W / PW), Math.round(yp1 * TMPL_H / PH),
        ];
    }}
    return [
        Math.round(yp0 * TMPL_W / PH),
        Math.round(TMPL_H - xp1 * TMPL_H / PW),
        Math.round(yp1 * TMPL_W / PH),
        Math.round(TMPL_H - xp0 * TMPL_H / PW),
    ];
}}

/* ---- экспорт ---- */
document.getElementById('btnExport').onclick = () => {{
    const p = n => String(n).padStart(3);
    const lines = ['SCAN_FIELDS: dict[str, tuple] = {{'];
    for (const [field, z] of Object.entries(zones)) {{
        const [x0, y0, x1, y1] = pixToTmpl(z.px0, z.py0, z.px1, z.py1);
        let xi = ' None', yi = ' None';
        if (z.xins_off !== null) xi = String(x0 + Math.round(z.xins_off)).padStart(5);
        if (z.yins_off !== null) yi = String(y1 + Math.round(z.yins_off)).padStart(5);
        lines.push(`    "${{field.padEnd(12)}}": (${{p(x0)}}, ${{p(y0)}}, ${{p(x1)}}, ${{p(y1)}}, ${{xi}}, ${{yi}}),`);
    }}
    lines.push('}}');
    const box = document.getElementById('export-box');
    box.textContent = lines.join('\\n');
    box.style.display = 'block';
    box.scrollIntoView({{behavior: 'smooth'}});
}};

document.getElementById('btnAdd').onclick = () => {{
    const name = prompt('Имя нового поля (например work_object_2):');
    if (!name || !name.trim()) return;
    const field = name.trim().replace(/\s+/g, '_');
    if (zones[field]) {{ alert('Поле уже существует: ' + field); return; }}
    const img = document.getElementById('scan-img');
    const cx = Math.round(img.clientWidth  / 2);
    const cy = Math.round(img.clientHeight / 2);
    zones[field] = {{ px0: cx-60, py0: cy-20, px1: cx+60, py1: cy+20,
                      narrow: false, added: true, xins_off: null, yins_off: null }};
    makeEl(field, zones[field]);
}};

document.getElementById('btnDel').onclick = () => {{
    if (!selected) {{ alert('Сначала кликни на поле'); return; }}
    if (!confirm('Удалить поле: ' + selected + '?')) return;
    delete zones[selected];
    const el = document.getElementById('z_' + selected);
    if (el) el.remove();
    selected = null;
}};

document.getElementById('btnReset').onclick = () => {{
    for (const [field, z] of Object.entries(zones)) {{
        const o = ORIG[field];
        Object.assign(z, o);
        const el = document.getElementById('z_' + field);
        el.style.left   = o.px0 + 'px';
        el.style.top    = o.py0 + 'px';
        el.style.width  = (o.px1 - o.px0) + 'px';
        el.style.height = (o.py1 - o.py0) + 'px';
    }}
    document.getElementById('export-box').style.display = 'none';
}};
</script>
</body>
</html>"""

out = pdf_path.rsplit('.', 1)[0] + '_calibrate.html'
with open(out, 'w', encoding='utf-8') as f:
    f.write(html)
print('Открываю:', out)
webbrowser.open(out)
